import json
import sys
from types import SimpleNamespace

import numpy as np
import pytest
from pydub import AudioSegment

from backend.app.adapters import sensevoice_asr as s


def test_windows_cap_long_regions_and_keep_absolute_offsets():
    assert list(s._windows([[500, 65500]], 70000)) == [(500, 30500), (30500, 60500), (60500, 65500)]


@pytest.mark.parametrize("regions", [[[20, 10]], [[-1, 20]], [[0, 100], [50, 200]], [[0, float("nan")]], [[0, 9000]]])
def test_invalid_vad_intervals_rejected(regions):
    with pytest.raises(RuntimeError):
        list(s._windows(regions, 5000))


def test_aligned_words_keep_absolute_ms_and_clean_tags():
    result = {"text": "hello", "words": ["<|en|>Hello", "world."], "timestamp": [[50, 200], [200, 450]]}
    words = s._aligned_words(result, 30000, 1000)
    cues = s._utterances(words, "en")
    assert cues[0]["text"] == "Hello world."
    assert (cues[0]["start_time"], cues[0]["end_time"]) == (30050, 30450)


@pytest.mark.parametrize("result", [
    {"text": "hello"},
    {"text": "hello", "words": ["hello"], "timestamp": []},
    {"text": "hello", "words": ["hello"], "timestamp": [[0, 2000]]},
    {"text": "hello", "words": ["a", "b"], "timestamp": [[100, 200], [50, 250]]},
])
def test_bad_alignment_never_becomes_fake_timestamps(result):
    with pytest.raises(RuntimeError):
        s._aligned_words(result, 0, 1000)


def test_segments_split_on_pause_and_length():
    words = [{"text": "word", "start_time": i * 1000, "end_time": i * 1000 + 900} for i in range(20)]
    cues = s._utterances(words, "en")
    assert len(cues) == 5
    assert sum(len(c["words"]) for c in cues) == 20
    assert max(c["end_time"] - c["start_time"] for c in cues) <= s.MAX_CUE_MS
    words[1]["start_time"] = 2500
    words[1]["end_time"] = 2900
    assert s._utterances(words[:2], "en")[0]["text"] == "word"


def test_japanese_does_not_insert_spaces():
    words = [{"text": t, "start_time": i*100, "end_time": (i+1)*100} for i,t in enumerate(["今日", "は", "晴れ", "。"])]
    assert s._utterances(words, "ja")[0]["text"] == "今日は晴れ。"


def test_model_loading_is_local_inference_without_remote_code(monkeypatch):
    calls = []
    monkeypatch.setattr(s, "_MODELS", None)
    monkeypatch.setitem(sys.modules, "funasr", SimpleNamespace(AutoModel=lambda **kw: calls.append(kw) or object()))
    monkeypatch.setattr(s, "resolve_device", lambda _: SimpleNamespace(selected="cuda", setting_name="DEVICE"))
    monkeypatch.setattr(s, "validate_device_available", lambda *a: None)
    for k in ("FUNASR_MODEL", "FUNASR_HUB", "FUNASR_VAD_MODEL", "FUNASR_VAD_HUB"):
        monkeypatch.delenv(k, raising=False)
    s._load_models()
    s._load_models()
    assert len(calls) == 2
    assert calls[0]["model"] == "FunAudioLLM/SenseVoiceSmall"
    assert calls[0]["device"] == "cuda:0" and calls[1]["device"] == "cpu"
    assert all(x["trust_remote_code"] is False for x in calls)
    assert s.release_model() is True
    assert s.release_model() is False


def test_recognition_normalizes_audio_and_offsets_each_vad_window(tmp_path, monkeypatch):
    audio = AudioSegment.silent(duration=4000, frame_rate=44100).set_channels(2)
    monkeypatch.setattr(s.AudioSegment, "from_file", lambda _: audio)
    calls = []
    def generate(**kw):
        calls.append(kw)
        assert kw["input"].ndim == 1 and kw["input"].dtype == np.float32
        return [{"text": "今日は晴れ。", "words": ["今日", "は晴れ。"], "timestamp": [[100, 300], [300, 700]]}]
    asr = SimpleNamespace(generate=generate)
    vad = SimpleNamespace(generate=lambda **kw: [{"value": [[500, 1500], [2500, 3500]]}])
    monkeypatch.setattr(s, "_load_models", lambda: (asr, vad))
    output = s.recognize_speech(tmp_path/'audio.wav', tmp_path, 'ja')
    result = json.loads(output.read_text(encoding='utf-8'))
    assert result["asr_backend"] == "sensevoice"
    cues = result["result"]["utterances"]
    assert [c["start_time"] for c in cues] == [600, 2600]
    assert [c["end_time"] for c in cues] == [1200, 3200]
    assert all(c["language"] == 'ja' and c["output_timestamp"] and not c["use_itn"] for c in calls)
    assert not output.with_suffix('.json.tmp').exists()


def test_empty_speech_leaves_no_partial_artifact(tmp_path, monkeypatch):
    monkeypatch.setattr(s.AudioSegment, "from_file", lambda _: AudioSegment.silent(duration=1000))
    monkeypatch.setattr(s, "_load_models", lambda: (None, SimpleNamespace(generate=lambda **kw: [{"value": []}])))
    with pytest.raises(RuntimeError, match="no transcribable"):
        s.recognize_speech(tmp_path/'audio.wav', tmp_path, 'en')
    assert not (tmp_path/'metadata/asr.json').exists()


def test_vad_timeline_can_exceed_one_hour():
    assert list(s._windows([[3_700_000, 3_702_000]], 3_800_000)) == [(3_700_000, 3_702_000)]


def test_funasr_device_override_is_used(monkeypatch):
    from backend.app import devices
    monkeypatch.setenv("FUNASR_DEVICE", "cpu")
    monkeypatch.setattr(devices, "default_device", lambda: "cuda")
    resolved = devices.resolve_device("funasr")
    assert resolved.selected == "cpu" and resolved.setting_name == "FUNASR_DEVICE"
    assert devices.MANAGED_COMPONENTS == ("demucs", "funasr")


def test_cli_exports_srt_from_aligned_json(tmp_path, monkeypatch):
    from scripts import run_stt
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"test")
    output = tmp_path / "result"
    def fake_recognize(path, session, language):
        assert path == audio and language == "ja"
        metadata = session / "metadata"
        metadata.mkdir(parents=True)
        artifact = metadata / "asr.json"
        artifact.write_text(json.dumps({"audio_info": {"duration": 4000}, "result": {
            "text": "こんにちは。", "utterances": [{"text": "こんにちは。", "start_time": 1000, "end_time": 2000}]}}), encoding="utf-8")
        return artifact
    monkeypatch.setattr(s, "recognize_speech", fake_recognize)
    monkeypatch.setattr(sys, "argv", ["run_stt.py", str(audio), "--language", "ja", "--device", "cpu", "--output", str(output)])
    monkeypatch.setenv("FUNASR_DEVICE", "cpu")
    run_stt.main()
    text = (output / "subtitles.srt").read_text(encoding="utf-8")
    assert "こんにちは。" in text
    assert "00:00:00,900 --> 00:00:02,300" in text
    assert (output / "metadata/asr_fixed.json").exists()


def test_vad_boundaries_do_not_force_subtitle_boundaries(tmp_path, monkeypatch):
    monkeypatch.setattr(s.AudioSegment, "from_file", lambda _: AudioSegment.silent(duration=3000))
    responses = iter([
        [{"text": "machine learning", "words": ["machine", "learning"], "timestamp": [[0, 400], [400, 900]]}],
        [{"text": "models", "words": ["models"], "timestamp": [[0, 500]]}],
    ])
    monkeypatch.setattr(s, "_load_models", lambda: (
        SimpleNamespace(generate=lambda **kw: next(responses)),
        SimpleNamespace(generate=lambda **kw: [{"value": [[0, 1000], [1400, 2400]]}]),
    ))
    result = json.loads(s.recognize_speech(tmp_path / 'a.wav', tmp_path, 'en').read_text())
    cues = result["result"]["utterances"]
    assert len(cues) == 1
    assert cues[0]["text"] == "machine learning models"
    assert (cues[0]["start_time"], cues[0]["end_time"]) == (0, 1900)
    assert cues[0]["words"][-1]["start_time"] == 1400


def test_contraction_spacing():
    assert s._join([{"text": t} for t in ["we", "'", "ll", "see"]], "en") == "we'll see"


def test_resegment_cli_reuses_word_alignment_without_models(tmp_path, monkeypatch):
    from scripts import run_stt
    def forbidden(*a, **kw):
        raise AssertionError("Must not run inference")
    monkeypatch.setattr(s, "recognize_speech", forbidden)
    monkeypatch.setattr(s, "_load_models", forbidden)
    words = [{"text": "learning", "start_time": 1000, "end_time": 2000},
             {"text": "models", "start_time": 2300, "end_time": 3000}]
    original = tmp_path / 'asr.json'
    original.write_text(json.dumps({"audio_info": {"duration": 4000}, "language": "en",
        "result": {"utterances": [{**w, "words": [w]} for w in words]}}))
    before = original.read_bytes()
    output = tmp_path / 'regrouped'
    monkeypatch.setattr(sys, "argv", ["run_stt.py", str(original), "--resegment", "--output", str(output)])
    run_stt.main()
    assert original.read_bytes() == before
    cues = json.loads((output / 'metadata/asr.json').read_text())["result"]["utterances"]
    assert len(cues) == 1 and cues[0]["words"] == words
    assert 'learning models' in (output / 'subtitles.srt').read_text()


def test_resegment_rejects_missing_word_alignment_before_writing(tmp_path, monkeypatch):
    from scripts import run_stt
    original = tmp_path / 'asr_fixed.json'
    original.write_text(json.dumps({"audio_info": {"duration": 4000},
        "result": {"utterances": [{"text": "hello", "start_time": 0, "end_time": 1000}]}}))
    output = tmp_path / 'regrouped'
    monkeypatch.setattr(sys, "argv", ["run_stt.py", str(original), "--resegment", "--output", str(output)])
    with pytest.raises(SystemExit):
        run_stt.main()
    assert not output.exists()


def test_user_fragmented_srt_sample_keeps_text_and_timing():
    from pathlib import Path
    # Input entries are SRT fragments, not inferred word timestamps. This
    # regression checks regrouping; the integration test checks real offsets.
    fixture = Path(__file__).parent / "fixtures/sensevoice_fragmented_cues.json"
    fragments = json.loads(fixture.read_text(encoding="utf-8"))
    cues = s._utterances(fragments, "en")
    assert 15 <= len(cues) <= 18
    assert [w for c in cues for w in c["words"]] == fragments
    assert all(c["text"] not in {"models", "feature", "of the"} for c in cues)
    assert all(c["start_time"] == c["words"][0]["start_time"] for c in cues)
    assert all(c["end_time"] == c["words"][-1]["end_time"] for c in cues)
    # A supplied SRT fragment is indivisible here. Real ASR uses words, so
    # it can split the long smoothing fragment without inventing timestamps.
    assert all(c["end_time"] - c["start_time"] <= max(s.MAX_CUE_MS,
        max(w["end_time"] - w["start_time"] for w in c["words"])) for c in cues)
    assert any(c["text"].startswith("we'll see") for c in cues)


def test_sentence_punctuation_and_character_limit_still_split():
    words = [{"text": "hello.", "start_time": 0, "end_time": 200},
             {"text": "world", "start_time": 200, "end_time": 400}]
    assert len(s._utterances(words, "en")) == 2
    words = [{"text": "longword", "start_time": i*100, "end_time": (i+1)*100} for i in range(50)]
    cues = s._utterances(words, "en")
    assert all(len(c["text"]) <= s.MAX_CUE_CHARS for c in cues)
    assert [w for c in cues for w in c["words"]] == words


@pytest.mark.parametrize("text", [
    "to our estimates in order to improve the accuracy of our classifier",
    "then we can divide by the total number of documents in our collection",
    "prior of each class by counting for each document in our labeled document collection",
])
def test_word_level_balancing_avoids_orphan_tail(text):
    # Synthetic alignment for regression only, not timestamps inferred from SRT.
    tokens = text.split()
    words = [{"text": t, "start_time": i*350, "end_time": i*350+300} for i,t in enumerate(tokens)]
    cues = s._utterances(words, "en")
    assert len(cues) >= 2
    assert all(len(c["text"].split()) >= 3 for c in cues)
    assert all(c["end_time"] - c["start_time"] <= s.MAX_CUE_MS for c in cues)
    assert [w for c in cues for w in c["words"]] == words
    assert not any(c["text"].split()[-1] in {"our", "the", "of"} for c in cues[:-1])


def test_prefers_nearby_pause_over_hard_length_cut():
    tokens = "like other supervised learning models naive bayes has several parameters".split()
    words = [{"text": t, "start_time": i*400+(500 if i>=5 else 0),
              "end_time": i*400+350+(500 if i>=5 else 0)} for i,t in enumerate(tokens)]
    cues = s._utterances(words, "en")
    assert [c["text"] for c in cues] == ["like other supervised learning models", "naive bayes has several parameters"]
    assert [w for c in cues for w in c["words"]] == words


def test_genuine_isolated_word_is_not_merged_across_silence():
    words = [{"text": "yes", "start_time": 0, "end_time": 500},
             {"text": "next", "start_time": 3000, "end_time": 3400}]
    assert len(s._utterances(words, "en")) == 2
