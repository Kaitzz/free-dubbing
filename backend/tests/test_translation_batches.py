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


def test_100_segments_one_request_reordered_output(monkeypatch):
    calls = []
    def fake(client, model, system, user):
        items = json.loads(user)["items"]
        calls.append(items)
        assert "exactly ONE" not in system and "user 每次只会给一句" not in system
        return rows(list(reversed(items)))
    texts = [f"s{i}" for i in range(100)]
    result = run(monkeypatch, fake, texts)
    assert len(calls) == 1
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
    assert [len(x) for x in calls] == [100, 1]
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
