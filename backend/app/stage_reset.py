from __future__ import annotations

import shutil
from pathlib import Path

from .sources import SourceConfig
from .stages import STAGE_NAMES


# audio_bgm.wav and audio_mixed.m4a only exist in sessions from before vocal separation was
# removed; listing them keeps a redo from leaving them behind.
STAGE_OWN_ARTIFACTS: dict[str, tuple[str, ...]] = {
    "download": ("media", "metadata", "segments", "tmp"),
    "separate": ("media/audio_vocals.wav", "media/audio_bgm.wav"),
    "asr": ("metadata/asr.json",),
    "asr_fix": ("metadata/asr_fixed.json",),
    "translate": ("metadata/translation_preprocess.json",),
    "split_audio": ("segments/vocals",),
    "tts": ("segments/tts", "tmp/tts_references"),
    "merge_audio": ("tmp/audio_dubbing.wav", "metadata/timings.json", "segments/stretched"),
    "merge_video": ("tmp/audio_mixed.m4a", "media/video_final.mp4"),
}


def _translation_globs(session: Path, target_language: str) -> list[Path]:
    metadata = session / "metadata"
    paths = list(metadata.glob(f"translation.{target_language}.json"))
    paths.extend(metadata.glob("subtitles.*.srt"))
    return paths


def collect_artifact_paths(session: Path, from_stage: str, source: SourceConfig) -> list[Path]:
    if from_stage not in STAGE_NAMES:
        raise ValueError(f"Unknown stage: {from_stage}")

    start = STAGE_NAMES.index(from_stage)
    paths: list[Path] = []
    for stage in STAGE_NAMES[start:]:
        for relative in STAGE_OWN_ARTIFACTS[stage]:
            paths.append(session / relative)
        if stage == "translate":
            paths.extend(_translation_globs(session, source.target_language))
    return paths


def owner_stage(relative: str, target_language: str) -> str:
    """Stage whose redo removes this session file; the most specific path wins."""
    name = relative.rsplit("/", 1)[-1]
    if relative.startswith("metadata/") and (
        name == f"translation.{target_language}.json" or (name.startswith("subtitles.") and name.endswith(".srt"))
    ):
        return "translate"
    best, length = "download", -1
    for stage, paths in STAGE_OWN_ARTIFACTS.items():
        for path in paths:
            if (relative == path or relative.startswith(path + "/")) and len(path) > length:
                best, length = stage, len(path)
    return best


def remove_stage_artifacts(session: Path, from_stage: str, source: SourceConfig) -> None:
    for path in collect_artifact_paths(session, from_stage, source):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink()
