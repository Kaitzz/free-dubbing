from contextlib import nullcontext
import json
from types import SimpleNamespace

import pytest

from backend.app.adapters import openai_translate as t
from backend.app.sources import detect_source

SOURCE = detect_source("https://www.youtube.com/watch?v=abcdefghijk")


def run(monkeypatch, fake, texts):
    monkeypatch.setattr(t, "_client", lambda *a, **kw: nullcontext(object()))
    monkeypatch.setattr(t, "_call_json", fake)
    return t.translate_batch(texts, SOURCE, {}, t.PreprocessResponse(),
                             base_url="https://example.invalid/v1", api_key="test", model="MiniMax-M3",
                             concurrency=1)


def rows(items):
    return {"translations": [{"id": x["id"], "dst": "译:" + x["text"], "audio_mode": "tts"}
                              for x in items]}


def test_100_segments_bounded_batches_reordered_output(monkeypatch):
    calls = []
    def fake(client, model, system, user):
        items = json.loads(user)["items"]
        calls.append(items)
        assert "exactly ONE" not in system and "user 每次只会给一句" not in system
        return rows(list(reversed(items)))
    texts = [f"s{i}" for i in range(100)]
    result = run(monkeypatch, fake, texts)
    assert [len(items) for items in calls] == [50, 50]
    assert [x.dst for x in result] == ["译:" + x for x in texts]


def test_missing_duplicate_and_invalid_retry_only_unresolved(monkeypatch):
    calls = []
    def fake(client, model, system, user):
        items = json.loads(user)["items"]
        calls.append([x["id"] for x in items])
        if len(calls) == 1:
            response = rows(items[:2])
            response["translations"].append(response["translations"][1].copy())
            response["translations"].append({"id": 3, "dst": "", "audio_mode": "tts"})
            return response
        return rows(items)
    result = run(monkeypatch, fake, ["a", "b", "c", "d"])
    assert calls == [[1, 2, 3, 4], [2, 3, 4]]
    assert [x.dst for x in result] == ["译:a", "译:b", "译:c", "译:d"]


def test_truncated_batch_splits_and_recovers(monkeypatch):
    sizes = []
    def fake(client, model, system, user):
        items = json.loads(user)["items"]
        sizes.append(len(items))
        if len(items) > 1:
            raise ValueError("truncated")
        return rows(items)
    assert len(run(monkeypatch, fake, ["a", "b"])) == 2
    assert sizes == [2, 2, 1, 1]


@pytest.mark.parametrize("bad_id", [True, "1", 999])
def test_invalid_ids_fail_closed(monkeypatch, bad_id):
    def fake(*args):
        return {"translations": [{"id": bad_id, "dst": "hello", "audio_mode": "tts"}]}
    with pytest.raises(RuntimeError, match="ID 1"):
        run(monkeypatch, fake, ["a"])


def test_limits_create_smaller_batches(monkeypatch):
    calls = []
    def fake(client, model, system, user):
        items = json.loads(user)["items"]
        calls.append(items)
        return rows(items)
    run(monkeypatch, fake, ["x"] * 101)
    assert [len(x) for x in calls] == [50, 50, 1]
    calls.clear()
    run(monkeypatch, fake, ["x" * 7000] * 2)
    assert [len(x) for x in calls] == [1, 1]


def test_filler_original_mode_survives_batch(monkeypatch):
    def fake(*args):
        return {"translations": [{"id": 1, "dst": "", "audio_mode": "original"}]}
    assert run(monkeypatch, fake, ["um"])[0].model_dump() == {"dst": "", "audio_mode": "original"}


def test_minimax_request_options_and_reasoning_cleanup():
    captured = {}
    def create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
            content='<think>{"not": "the answer"}</think>```json\n{"ok": true}\n```'))])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    assert t._call_json(client, "MiniMax-M3", "sys", "text") == {"ok": True}
    assert captured["extra_body"] == {"thinking": {"type": "disabled"}}
    assert captured["max_tokens"] == 16384


def test_finish_length_is_rejected_even_with_valid_json():
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw:
        SimpleNamespace(choices=[SimpleNamespace(finish_reason="length", message=SimpleNamespace(content='{"translations": []}'))]))))
    with pytest.raises(ValueError, match="truncated"):
        t._call_json(client, "MiniMax-M3", "sys", "text")


def test_transport_failure_does_not_fan_out(monkeypatch):
    calls = []
    def fake(*args):
        calls.append(1)
        raise ConnectionError("offline")
    with pytest.raises(ConnectionError):
        run(monkeypatch, fake, ["a", "b", "c"])
    assert len(calls) == 1


def test_oversized_single_segment_fails_before_api(monkeypatch):
    def fake(*args):
        raise AssertionError("No request should be sent")
    with pytest.raises(ValueError, match="text budget"):
        run(monkeypatch, fake, ["x" * 12001])


def test_sparse_retry_keeps_neighbors_and_single_item_recovers(monkeypatch):
    calls=[]
    def fake(client,model,system,user):
        request=json.loads(user);calls.append(request)
        if 'item' in request:
            assert request['item']['id']==60
            assert [item['id'] for item in request['context']]==[58,59,61,62]
            assert 'translations array' in system
            return {'dst':'向特定的人指出这件事可能会','audio_mode':'tts'}
        items=request['items']
        if len(items)>1:return rows([item for item in items if item['id']!=60])
        assert [item['id'] for item in request['context']]==[58,59,61,62]
        return {'translations':[{'id':1,'dst':'wrong ID','audio_mode':'tts'}]}
    output=run(monkeypatch,fake,[f's{i}' for i in range(1,74)])
    assert len(calls)==4
    assert len(output)==73
    assert output[59].dst=='向特定的人指出这件事可能会'
    assert output[58].dst=='译:s59' and output[60].dst=='译:s61'


def test_invalid_fields_have_actionable_diagnostics(monkeypatch,capsys):
    def fake(*args):return {'translations':[{'id':1,'dst':'','audio_mode':'tts'}]}
    with pytest.raises(RuntimeError,match='ID 1'):
        run(monkeypatch,fake,['fragment'])
    assert 'Unresolved IDs: 1' in capsys.readouterr().out


@pytest.mark.parametrize("text", ["anyone feeling attacked.", "want to remind you guys that this is", "[Music] hello", "（笑声）你好"])
def test_speech_original_misclassification_retries(monkeypatch, text):
    calls = []
    def fake(client, model, system, user):
        items = json.loads(user)["items"]
        calls.append(items)
        if len(calls) == 1:
            return {"translations": [{"id": 1, "dst": "（音乐）", "audio_mode": "original"}]}
        return rows(items)
    result = run(monkeypatch, fake, [text])
    assert len(calls) == 2
    assert result[0].audio_mode == "tts"


@pytest.mark.parametrize("text", ["[Music]", "(laughter)", "（音乐）", "um", "♪"])
def test_explicit_nonverbal_original_allowed(text):
    assert t._allows_original(text)


def test_single_item_fallback_cannot_restore_spoken_original(monkeypatch):
    def fake(client, model, system, user):
        request = json.loads(user)
        entry = {"dst": "", "audio_mode": "original"}
        return entry if "item" in request else {"translations": [{"id": 1, **entry}]}
    with pytest.raises(RuntimeError, match="requires audio_mode tts"):
        run(monkeypatch, fake, ["anyone feeling attacked."])


@pytest.mark.parametrize("text", ["(upbeat music)", "(clock ticking)", "[soft piano music]", "(audience laughing)"])
def test_descriptive_manual_sound_cues_allowed(monkeypatch,text):
    def fake(*args):
        return {"translations":[{"id":1,"dst":"（声音）","audio_mode":"original"}]}
    assert run(monkeypatch,fake,[text])[0].audio_mode=="original"


@pytest.mark.parametrize("text", ["I like upbeat music.", "(I like upbeat music)", "(clock ticking) I know what you mean", "(Music) hello"])
def test_descriptive_cues_do_not_admit_dialogue(text):
    assert not t._allows_original(text)


def test_missing_mode_is_inferred_without_retries(monkeypatch):
    calls=[]
    def fake(client,model,system,user):
        items=json.loads(user)["items"];calls.append(items)
        return {"translations":[{"id":x["id"],"dst":"中文译文"} for x in items]}
    result=run(monkeypatch,fake,["But for whatever reason in that moment,", "(upbeat music)"])
    assert [x.audio_mode for x in result]==["tts","original"]
    assert len(calls)==1


def test_missing_mode_does_not_accept_empty_spoken_translation():
    with pytest.raises(ValueError):
        t._parse_source_translation({"dst":""},"anyone feeling attacked.")


def test_single_retry_accepts_missing_mode_for_valid_translation(monkeypatch):
    def fake(client,model,system,user):
        request=json.loads(user)
        return {"dst":"正常译文"} if "item" in request else {"translations":[]}
    assert run(monkeypatch,fake,["Hello there."])[0].audio_mode=="tts"
