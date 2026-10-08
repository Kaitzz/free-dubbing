from __future__ import annotations

import json

import pytest

from backend.app.adapters import asr_sentence_fixer


def _utt(text: str, start: int, end: int) -> dict:
    return {"text": text, "start_time": start, "end_time": end}


def _write_asr(tmp_path, utterances: list, duration: int = 10000, text: str = "") -> tuple:
    session = tmp_path / "session"
    (session / "metadata").mkdir(parents=True)
    asr_file = session / "metadata" / "asr.json"
    payload = {
        "audio_info": {"duration": duration},
        "result": {"text": text, "utterances": utterances},
    }
    asr_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return asr_file, session


def test_fix_asr_sentences_passes_through_utterances(tmp_path):
    utts = [_utt("Hello world.", 100, 1200), _utt("How are you?", 1500, 2800)]
    asr_file, session = _write_asr(tmp_path, utts)

    fixed = asr_sentence_fixer.fix_asr_sentences(asr_file, session, start_pad=50, end_pad=100)
    out = json.loads(fixed.read_text(encoding="utf-8"))["result"]["utterances"]

    assert [u["text"] for u in out] == ["Hello world.", "How are you?"]


def test_fix_asr_sentences_drops_empty_text(tmp_path):
    utts = [_utt("Hello.", 0, 500), _utt("   ", 600, 800), _utt("World.", 900, 1500)]
    asr_file, session = _write_asr(tmp_path, utts)

    fixed = asr_sentence_fixer.fix_asr_sentences(asr_file, session)
    out = json.loads(fixed.read_text(encoding="utf-8"))["result"]["utterances"]

    assert [u["text"] for u in out] == ["Hello.", "World."]


def test_fix_asr_sentences_applies_padding_within_gap(tmp_path):
    utts = [_utt("a", 1000, 2000), _utt("b", 3000, 4000)]
    asr_file, session = _write_asr(tmp_path, utts, duration=5000)

    fixed = asr_sentence_fixer.fix_asr_sentences(asr_file, session, start_pad=100, end_pad=300)
    out = json.loads(fixed.read_text(encoding="utf-8"))["result"]["utterances"]

    assert out[0]["start_time"] == 900
    assert out[0]["end_time"] == 2300
    assert out[1]["start_time"] == 2900
    assert out[1]["end_time"] == 4300


def test_fix_asr_sentences_clamps_to_duration(tmp_path):
    utts = [_utt("only", 100, 4900)]
    asr_file, session = _write_asr(tmp_path, utts, duration=5000)

    fixed = asr_sentence_fixer.fix_asr_sentences(asr_file, session, start_pad=200, end_pad=500)
    out = json.loads(fixed.read_text(encoding="utf-8"))["result"]["utterances"]

    assert out[0]["start_time"] == 0  # 100 - 200 -> clamp 0
    assert out[0]["end_time"] == 5000


def test_fix_asr_sentences_raises_when_empty(tmp_path):
    asr_file, session = _write_asr(tmp_path, [_utt("  ", 0, 100)])

    with pytest.raises(RuntimeError):
        asr_sentence_fixer.fix_asr_sentences(asr_file, session)


def test_fix_asr_sentences_reuses_cache(tmp_path):
    utts = [_utt("hi", 0, 500)]
    asr_file, session = _write_asr(tmp_path, utts)

    first = asr_sentence_fixer.fix_asr_sentences(asr_file, session)
    first.write_text('{"already": true}', encoding="utf-8")
    second = asr_sentence_fixer.fix_asr_sentences(asr_file, session)

    assert json.loads(second.read_text(encoding="utf-8")) == {"already": True}
