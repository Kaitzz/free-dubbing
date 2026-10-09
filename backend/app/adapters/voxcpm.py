from __future__ import annotations

import io
import json
import os
import re
import shutil
import time
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

import numpy as np
import soundfile as sf
from pydub import AudioSegment

from .. import runtime_security
from ..audio_mode import is_original_audio, target_text
from ..config import MODEL_CACHE_DIR

_MODEL = None

_PROMPT_CACHE_GENERATION_DEFAULTS = {
    "min_len": 2,
    "max_len": 4096,
    "retry_badcase": True,
    "retry_badcase_max_times": 3,
    "retry_badcase_ratio_threshold": 6.0,
}


def release_model() -> bool:
    global _MODEL
    was_loaded = _MODEL is not None
    _MODEL = None
    return was_loaded


def _model_path() -> Path:
    configured_dir = os.getenv("VOXCPM_MODEL_DIR")
    if configured_dir:
        return Path(configured_dir).expanduser()

    model_id = os.getenv("VOXCPM_MODEL", "OpenBMB/VoxCPM2")
    local_dir = MODEL_CACHE_DIR / model_id.replace("/", "__")
    from modelscope import snapshot_download

    downloaded = snapshot_download(model_id, local_dir=str(local_dir))
    return Path(downloaded)


@contextmanager
def _timed_phase(label):
    started = time.monotonic()
    finished = threading.Event()
    print(f"[tts] {label}: started", flush=True)
    def report():
        while not finished.wait(30):
            print(f"[tts] {label}: still running ({time.monotonic()-started:.0f}s)", flush=True)
    thread = threading.Thread(target=report, daemon=True)
    thread.start()
    try:
        yield
    except BaseException:
        print(f"[tts] {label}: failed after {time.monotonic()-started:.1f}s", flush=True)
        raise
    else:
        print(f"[tts] {label}: completed in {time.monotonic()-started:.1f}s", flush=True)
    finally:
        finished.set()
        thread.join(timeout=1)


def _load_model():
    global _MODEL
    if _MODEL is None:
        with _timed_phase("Import VoxCPM dependencies"):
            from voxcpm import VoxCPM
            import torch

        with _timed_phase("Resolve/download model files"):
            path = _model_path()
        optimize = os.getenv("VOXCPM_OPTIMIZE", "true").lower() == "true"
        low_memory = os.getenv("VOXCPM_LOW_MEMORY_INIT", "false").lower() == "true"
        started = time.monotonic()
        print(f"[tts] VoxCPM initialization: low_memory={low_memory}, optimize={optimize}", flush=True)
        previous_dtype = torch.get_default_dtype()
        try:
            if low_memory:
                # Upstream constructs parameters on CPU before casting and
                # loading weights. Avoid the transient FP32 parameter copy.
                # Upstream still chooses the final LM dtype and restores VAE
                # to FP32 before loading checkpoint weights.
                torch.set_default_dtype(torch.float16)
            with _timed_phase("Initialize model and load weights"):
                _MODEL = VoxCPM.from_pretrained(
                    str(path),
                    load_denoiser=os.getenv("VOXCPM_LOAD_DENOISER", "false").lower() == "true",
                    optimize=optimize,
                )
        finally:
            if low_memory:
                torch.set_default_dtype(previous_dtype)
        print(f"[tts] VoxCPM initialized in {time.monotonic() - started:.1f}s", flush=True)
    return _MODEL


def _first_reference(files: list[Path], min_ms: int) -> Path | None:
    for path in files:
        if len(AudioSegment.from_file(path)) >= min_ms:
            return path
    if files:
        return files[0]
    return None


def _speaker(item: dict) -> str:
    speaker = item.get("speaker")
    if speaker is None:
        return "1"
    speaker = str(speaker).strip()
    return speaker or "1"


def _fallback_references(vocals_dir: Path, items: list[dict], min_ms: int) -> tuple[dict[str, Path], Path]:
    files = [
        vocals_dir / f"{index:04d}.wav"
        for index, item in enumerate(items, start=1)
        if not is_original_audio(item)
        and (vocals_dir / f"{index:04d}.wav").exists()
    ]
    if not files:
        raise FileNotFoundError("No vocal segments were generated for VoxCPM references.")

    global_fallback = _first_reference(files, min_ms) or files[0]
    speaker_files: dict[str, list[Path]] = {}
    for index, item in enumerate(items, start=1):
        if is_original_audio(item):
            continue
        reference = vocals_dir / f"{index:04d}.wav"
        if reference.exists():
            speaker_files.setdefault(_speaker(item), []).append(reference)

    fallbacks: dict[str, Path] = {}
    for speaker, refs in speaker_files.items():
        fallback = _first_reference(refs, min_ms)
        if fallback is not None:
            fallbacks[speaker] = fallback

    return fallbacks, global_fallback


def match_loudness(wav, sample_rate):
    """Gentle active-frame RMS matching, not LUFS normalization or compression."""
    samples = np.asarray(wav, dtype=np.float32)
    if not np.isfinite(samples).all():
        raise ValueError("TTS returned non-finite audio")
    if not samples.size:
        raise ValueError("TTS returned empty audio")
    mono = samples if samples.ndim == 1 else samples.mean(axis=-1)
    frame = max(1, int(sample_rate * 0.02))
    energies = np.array([np.mean(part.astype(np.float64)**2)
                         for part in np.array_split(mono, max(1, len(mono)//frame))])
    active = energies[energies >= max(1e-5, float(energies.max()) * 0.001)]
    if not len(active):
        return samples  # Never amplify silence or near-silence.
    rms = float(np.sqrt(active.mean()))
    gain_db = np.clip(20 * np.log10(0.1 / rms), -4.0, 4.0)
    gain = min(10 ** (gain_db / 20), 10 ** (-1 / 20) / max(float(np.max(np.abs(samples))), 1e-9))
    return samples * gain


def _fixed_references(vocals_dir, items, session):
    """Select one bounded, reasonably steady reference per existing speaker label."""
    best = {}
    speakers = {_speaker(item) for item in items if not is_original_audio(item)}
    for index, item in enumerate(items, 1):
        if is_original_audio(item):
            continue
        path = vocals_dir / f"{index:04d}.wav"
        if not path.exists():
            continue
        with sf.SoundFile(path) as audio:
            rate = audio.samplerate
            samples = audio.read(min(audio.frames, rate * 6), dtype="float32", always_2d=True).mean(axis=1)
        if not len(samples) or not np.isfinite(samples).all():
            continue
        frame = max(1, rate // 20)
        rms = np.array([np.sqrt(np.mean(part.astype(np.float64)**2))
                        for part in np.array_split(samples, max(1, len(samples)//frame))])
        active = rms > max(0.003, float(rms.max()) * 0.03)
        if not active.any():
            continue
        # Favor usable duration and speech coverage, penalize clipping and level variation.
        active_rms = rms[active]
        score = (min(len(samples)/rate, 4) + float(active.mean())
                 - float(np.std(20*np.log10(active_rms))) / 10
                 - 20*float(np.mean(np.abs(samples) >= 0.99)))
        speaker = _speaker(item)
        if speaker not in best or score > best[speaker][0]:
            best[speaker] = (score, path, samples, rate)
    missing = speakers - best.keys()
    if missing:
        raise ValueError("No usable vocal reference for a speaker; check separated audio")
    folder = session / "tmp" / "tts_references"
    folder.mkdir(parents=True, exist_ok=True)
    references = {}
    for index, (speaker, (_, source, samples, rate)) in enumerate(sorted(best.items()), 1):
        path = folder / f"{index:04d}.wav"
        sf.write(path, match_loudness(samples, rate), rate)
        references[speaker] = path
        print(f"[tts] Fixed reference {index}: {source.name}, {len(samples)/rate:.1f}s", flush=True)
    return references


def _tts_text(item: dict) -> str:
    text = target_text(item)
    if not isinstance(text, str) or not text.strip():
        raise ValueError("target text must be a non-empty string")
    text = text.replace("\n", " ")
    return re.sub(r"\s+", " ", text)


def _write_original_target_audio(
    output_file: Path,
    item: dict,
    original_vocals_file: Path,
    *,
    allow_tail_clamp: bool = False,
) -> None:
    start = max(0, int(item.get("start_time", 0)))
    end = int(item.get("end_time", start))
    if end <= start:
        raise ValueError(f"Original audio does not cover target segment {start}-{end} ms")

    with sf.SoundFile(original_vocals_file) as source:
        start_frame = max(0, int(start * source.samplerate / 1000))
        requested_end_frame = int(end * source.samplerate / 1000)
        end_frame = requested_end_frame
        available_duration_ms = source.frames / source.samplerate * 1000
        range_error = (
            f"Original audio does not cover target segment {start}-{end} ms: "
            f"requested frames {start_frame}-{requested_end_frame}, available audio is "
            f"{source.frames} frames ({available_duration_ms:.3f} ms)"
        )
        if start_frame >= source.frames:
            raise ValueError(range_error)
        overflow_scaled = end * source.samplerate - source.frames * 1000
        if overflow_scaled > 0:
            if overflow_scaled >= source.samplerate:
                # Source captions may linger just beyond EOF. Only the final
                # segment may use this bounded tolerance; missing audio elsewhere
                # still indicates a bad timeline or truncated source.
                if not allow_tail_clamp or overflow_scaled > 2000 * source.samplerate:
                    raise ValueError(range_error)
                print(f"[tts] Final original-audio segment trimmed to audio EOF: "
                      f"{end} -> {available_duration_ms:.3f} ms", flush=True)
            end_frame = source.frames
        if end_frame <= start_frame:
            raise ValueError(range_error)
        source.seek(start_frame)
        frames = source.read(
            end_frame - start_frame,
            dtype="float32",
            always_2d=True,
        )
        if len(frames) <= 0:
            raise ValueError(range_error)

        encoded = io.BytesIO()
        sf.write(
            encoded,
            frames,
            source.samplerate,
            format="WAV",
            subtype="PCM_16",
        )
        encoded.seek(0)

    runtime_security.remove_private_file(output_file, missing_ok=True)
    with runtime_security.open_private_binary_exclusive(output_file) as handle:
        shutil.copyfileobj(encoded, handle)
        handle.flush()


def generate_tts(
    translation_file: Path,
    vocals_dir: Path,
    session: Path,
    progress_callback: Callable[[int, str], None] | None = None,
    *,
    original_vocals_file: Path | None = None,
) -> Path:
    output_dir = session / "segments" / "tts"
    output_dir.mkdir(parents=True, exist_ok=True)
    data = json.loads(translation_file.read_text(encoding="utf-8"))
    items = data["translation"]
    total = len(items)
    if total == 0:
        if progress_callback:
            progress_callback(100, "No TTS clips to generate")
        return output_dir

    # A resumed MiniMax run may have populated these same WAV paths. Never
    # mistake those clips for VoxCPM output after switching provider.
    removed = 0
    for index in range(1, total + 1):
        marker = output_dir / f"{index:04d}.minimax.json"
        if marker.is_file():
            runtime_security.remove_private_file(output_dir / f"{index:04d}.wav", missing_ok=True)
            runtime_security.remove_private_file(marker, missing_ok=True)
            removed += 1
    if removed:
        print(f"[tts] Discarded {removed} MiniMax clips; regenerating with VoxCPM", flush=True)

    has_original_audio = any(is_original_audio(item) for item in items)
    if has_original_audio and original_vocals_file is None:
        raise ValueError("original_vocals_file is required for original audio items")

    # Prepare/validate original clips before loading the expensive TTS model.
    latest_end = max(int(item.get("end_time", 0)) for item in items)
    for index, item in enumerate(items, 1):
        if is_original_audio(item):
            assert original_vocals_file is not None
            _write_original_target_audio(
                output_dir / f"{index:04d}.wav", item, original_vocals_file,
                allow_tail_clamp=(index == total and int(item.get("end_time", 0)) == latest_end),
            )

    if all(is_original_audio(item) or (output_dir / f"{index:04d}.wav").is_file()
           for index, item in enumerate(items, 1)):
        for index, item in enumerate(items, start=1):
            output_file = output_dir / f"{index:04d}.wav"
            if progress_callback:
                progress = round(index / total * 100)
                progress_callback(progress, f"Prepared {index}/{total} TTS clips")
        return output_dir

    reference_mode = os.getenv("VOXCPM_REFERENCE_MODE", "fixed").strip().lower()
    if reference_mode not in {"fixed", "segment"}:
        raise ValueError("VOXCPM_REFERENCE_MODE must be fixed or segment")
    inference_timesteps = int(os.getenv("VOXCPM_INFERENCE_TIMESTEPS", "8"))
    if inference_timesteps < 1:
        raise ValueError("VOXCPM_INFERENCE_TIMESTEPS must be positive")
    normalize_audio = os.getenv("VOXCPM_MATCH_LOUDNESS", "true").lower() == "true"
    model = _load_model()
    print(f"[tts] Model ready; preparing {total} clips", flush=True)
    min_reference_ms = int(os.getenv("VOXCPM_MIN_REFERENCE_MS", "1200"))
    if reference_mode == "fixed":
        fallback_references = _fixed_references(vocals_dir, items, session)
        global_fallback = None
    else:
        fallback_references, global_fallback = _fallback_references(vocals_dir, items, min_reference_ms)
    cfg_value = float(os.getenv("VOXCPM_CFG_VALUE", "2.0"))
    print(f"[tts] Reference mode={reference_mode}; steps={inference_timesteps}; loudness matching={normalize_audio}", flush=True)

    fallback_caches = {}

    for index, item in enumerate(items, start=1):
        output_file = output_dir / f"{index:04d}.wav"
        if is_original_audio(item):
            if progress_callback:
                progress = round(index / total * 100)
                progress_callback(progress, f"Prepared {index}/{total} TTS clips")
            continue
        if not output_file.exists():
            reference = vocals_dir / f"{index:04d}.wav"
            text = _tts_text(item)
            if reference_mode == "fixed" or not reference.exists() or len(AudioSegment.from_file(reference)) < min_reference_ms:
                speaker = _speaker(item)
                if speaker not in fallback_caches:
                    fallback = fallback_references.get(speaker, global_fallback)
                    fallback_caches[speaker] = model.tts_model.build_prompt_cache(
                        reference_wav_path=str(fallback)
                    )
                result = model.tts_model.generate_with_prompt_cache(
                    target_text=text,
                    prompt_cache=fallback_caches[speaker],
                    cfg_value=cfg_value,
                    inference_timesteps=inference_timesteps,
                    **_PROMPT_CACHE_GENERATION_DEFAULTS,
                )
                wav_tensor, _, _ = result
                wav = wav_tensor.squeeze(0).cpu().numpy()
            else:
                wav = model.generate(
                    text=text,
                    reference_wav_path=str(reference),
                    cfg_value=cfg_value,
                    inference_timesteps=inference_timesteps,
                )
            if normalize_audio:
                wav = match_loudness(wav, model.tts_model.sample_rate)
            sf.write(output_file, wav, model.tts_model.sample_rate)
        if progress_callback:
            progress = round(index / total * 100)
            progress_callback(progress, f"Prepared {index}/{total} TTS clips")

    return output_dir
