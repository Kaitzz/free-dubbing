"""The source video's own audio, used as the voice track for ASR and TTS references."""
from __future__ import annotations

import subprocess
from pathlib import Path

from ..config import ffmpeg_binary

# The format Demucs used to write, so ASR, segment splitting and TTS references see no change.
SAMPLE_RATE = 44100
CHANNELS = 2


def extract_audio(video_file: Path, session: Path) -> Path:
    """Decode the first audio stream to media/audio_vocals.wav without separating vocals."""
    media = session / "media"
    media.mkdir(parents=True, exist_ok=True)
    output = media / "audio_vocals.wav"
    pending = media / ".audio_vocals.pending.wav"
    output.unlink(missing_ok=True)
    pending.unlink(missing_ok=True)
    try:
        result = subprocess.run(
            [ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
             "-i", str(video_file), "-map", "0:a:0", "-vn",
             "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le",
             "-rf64", "auto", "-f", "wav", str(pending)],
            capture_output=True, text=True, errors="replace",
        )
        if result.returncode:
            detail = " ".join(result.stderr.strip().splitlines()[-3:]) or f"exit code {result.returncode}"
            raise RuntimeError(f"FFmpeg could not extract audio from {video_file.name}: {detail}")
        if not pending.is_file() or pending.stat().st_size == 0:
            raise RuntimeError(f"FFmpeg produced no audio for {video_file.name}")
        pending.replace(output)
    finally:
        pending.unlink(missing_ok=True)
    return output
