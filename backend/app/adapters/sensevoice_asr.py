"""Local FunASR STT: explicit VAD windows and CTC alignment on the original timeline."""
from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path

import numpy as np
from pydub import AudioSegment

from ..devices import resolve_device, validate_device_available

_MODELS = None
SAMPLE_RATE = 16000
MAX_WINDOW_MS = 30000
MAX_CUE_MS = 4500
MAX_CUE_CHARS = 65
SENTENCE_PAUSE_MS = 1000
_TAGS = re.compile(r"<\|[^|]*\|>")


def release_model() -> bool:
    global _MODELS
    loaded = _MODELS is not None
    _MODELS = None
    return loaded


def _load_models():
    global _MODELS
    if _MODELS is None:
        from funasr import AutoModel
        resolution = resolve_device("funasr")
        validate_device_available(resolution.selected, resolution.setting_name)
        selected = "cuda:0" if resolution.selected == "cuda" else resolution.selected
        asr = AutoModel(
            model=os.getenv("FUNASR_MODEL", "FunAudioLLM/SenseVoiceSmall"),
            hub=os.getenv("FUNASR_HUB", "hf"), device=selected,
            disable_update=True, trust_remote_code=False,
        )
        vad = AutoModel(
            model=os.getenv("FUNASR_VAD_MODEL", "fsmn-vad"),
            hub=os.getenv("FUNASR_VAD_HUB", "ms"), device="cpu",
            max_single_segment_time=MAX_WINDOW_MS,
            disable_update=True, trust_remote_code=False,
        )
        _MODELS = (asr, vad)
    return _MODELS


def _number(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise RuntimeError("Invalid non-finite STT timestamp")
    return float(value)


def _windows(regions, duration: int):
    previous_end = 0
    for region in regions:
        if not isinstance(region, (list, tuple)) or len(region) != 2:
            raise RuntimeError("Invalid VAD interval")
        start, end = (round(_number(v)) for v in region)
        if start < previous_end or start < 0 or end <= start or end > duration + 50:
            raise RuntimeError("Invalid or overlapping VAD intervals")
        end = min(end, duration)
        previous_end = end
        # Defensive cap even if an upstream VAD version ignores its segment limit.
        while start < end:
            stop = min(start + MAX_WINDOW_MS, end)
            yield start, stop
            start = stop


def _aligned_words(result: dict, offset: int, clip_ms: int) -> list[dict]:
    words, stamps = result.get("words"), result.get("timestamp")
    text = _TAGS.sub("", result.get("text", "")).strip()
    if not text and not words:
        return []
    if not isinstance(words, list) or not isinstance(stamps, list) or not words or len(words) != len(stamps):
        raise RuntimeError("SenseVoice did not return paired words/timestamp alignment; check FunASR version")
    aligned = []
    previous_end = 0
    for word, stamp in zip(words, stamps):
        if not isinstance(word, str) or not isinstance(stamp, (list, tuple)) or len(stamp) != 2:
            raise RuntimeError("Invalid SenseVoice alignment entry")
        start, end = (round(_number(v)) for v in stamp)
        if start < previous_end or start < 0 or end <= start or end > clip_ms + 60:
            raise RuntimeError("SenseVoice alignment lies outside its audio window or is not monotonic")
        end = min(end, clip_ms)
        if end <= start:
            raise RuntimeError("SenseVoice alignment has an empty interval")
        previous_end = end
        clean = _TAGS.sub("", word).strip()
        if clean:
            aligned.append({"text": clean, "start_time": offset + start, "end_time": offset + end})
    return aligned


def _join(words: list[dict], language: str) -> str:
    separator = " " if language == "en" else ""
    text = separator.join(w["text"] for w in words)
    if language == "en":
        text = re.sub(r"\s+([,.;:!?])", r"\1", text)
        text = re.sub(r"(?<=\w)\s*['’]\s*(?=\w)", "'", text)
    return text.strip()


def _balanced_cues(words: list[dict], language: str) -> list[dict]:
    """Choose boundaries across a speech run, including the cost of its tail.

    Inputs remain indivisible and retain their acoustic timestamps. A single
    oversized input is allowed rather than manufacturing an internal alignment.
    """
    count = len(words)
    costs = [float("inf")] * (count + 1)
    next_cut = [0] * count
    costs[count] = 0.0
    dangling = {"a", "an", "the", "of", "to", "in", "on", "with", "for",
                "and", "or", "our", "your", "their", "my", "its"}
    for start in range(count - 1, -1, -1):
        for end in range(start + 1, count + 1):
            part = words[start:end]
            text = _join(part, language)
            duration = part[-1]["end_time"] - part[0]["start_time"]
            if end > start + 1 and (duration > MAX_CUE_MS or len(text) > MAX_CUE_CHARS):
                break
            # A modest per-cue cost avoids excessive splits; the duration term
            # encourages readable short phrases rather than filling every cue.
            cost = 4.0 + ((duration - 3300) / 1800) ** 2
            if duration < 1200:
                cost += 8 * (1 - duration / 1200)
            if language == "en" and len(text.split()) < 3:
                cost += 8
            if end < count:
                gap = words[end]["start_time"] - part[-1]["end_time"]
                cost -= min(max(gap, 0) / 150, 3.0)
                if re.search(r"[,;:，；：]$", text):
                    cost -= 2
                if language == "en":
                    if text.split()[-1].lower() in dangling:
                        cost += 5
                    # Do not strand the apostrophe/suffix in tokenized we'll.
                    if part[-1]["text"].endswith(("'", "’")) or words[end]["text"].startswith(("'", "’")):
                        cost += 20
            total = cost + costs[end]
            if total < costs[start]:
                costs[start], next_cut[start] = total, end
    cues = []
    start = 0
    while start < count:
        end = next_cut[start]
        part = words[start:end]
        cues.append({"text": _join(part, language),
                     "start_time": part[0]["start_time"], "end_time": part[-1]["end_time"],
                     "words": list(part)})
        start = end
    return cues


def _utterances(words: list[dict], language: str) -> list[dict]:
    utterances, current = [], []
    def flush():
        if current:
            utterances.extend(_balanced_cues(current, language))
            current.clear()
    for word in words:
        if current and word["start_time"] - current[-1]["end_time"] >= SENTENCE_PAUSE_MS:
            flush()
        current.append(word)
        if re.search(r"[。！？!?]|(?<!\d)\.$", word["text"]):
            flush()
    flush()
    return utterances


def recognize_speech(vocals_file: Path, session: Path, language: str) -> Path:
    if language not in {"en", "zh", "ja", "ko", "yue", "auto"}:
        raise ValueError("Unsupported SenseVoice language")
    metadata = session / "metadata"
    metadata.mkdir(parents=True, exist_ok=True)
    output = metadata / "asr.json"
    # Pipeline stage state handles reuse; direct calls deliberately recompute.
    audio = AudioSegment.from_file(vocals_file).set_channels(1).set_frame_rate(SAMPLE_RATE).set_sample_width(2)
    samples = np.asarray(audio.get_array_of_samples(), dtype=np.float32) / 32768.0
    duration = len(audio)
    if not len(samples):
        raise RuntimeError("STT input audio is empty")
    asr, vad = _load_models()
    detected = vad.generate(input=samples, fs=SAMPLE_RATE, cache={}, is_final=True)
    if not detected or not isinstance(detected[0].get("value"), list):
        raise RuntimeError("VAD did not return speech intervals")
    aligned_words = []
    for start, end in _windows(detected[0]["value"], duration):
        clip = samples[start * 16:end * 16]
        results = asr.generate(input=clip, fs=SAMPLE_RATE, cache={}, language=language,
                               use_itn=False, output_timestamp=True, batch_size=1)
        if not results or len(results) != 1:
            raise RuntimeError("SenseVoice returned an unexpected number of results")
        words = _aligned_words(results[0], start, end - start)
        aligned_words.extend(words)
    # VAD windows bound inference, not subtitle sentences. Keep the original
    # absolute alignment and segment once across the entire recording.
    utterances = _utterances(aligned_words, language)
    if not utterances:
        raise RuntimeError("SenseVoice found no transcribable speech")
    payload = {"audio_info": {"duration": duration},
               "asr_backend": "sensevoice", "language": language,
               "model": os.getenv("FUNASR_MODEL", "FunAudioLLM/SenseVoiceSmall"),
               "result": {"text": " ".join(u["text"] for u in utterances), "utterances": utterances}}
    pending = output.with_suffix(".json.tmp")
    try:
        pending.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        pending.replace(output)
    finally:
        pending.unlink(missing_ok=True)
    return output
