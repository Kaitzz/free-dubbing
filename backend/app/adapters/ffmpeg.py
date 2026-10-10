from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path

from ..config import ffmpeg_binary, ffprobe_binary


def video_encoding_args() -> list[str]:
    # Probe a real encode: being listed by ffmpeg does not mean NVENC is usable.
    automatic = os.getenv("DUBBING_VIDEO_ENCODER", "cpu") == "auto"
    if automatic:
        try:
            probe = subprocess.run(
                [ffmpeg_binary(), "-hide_banner", "-loglevel", "error",
                 "-f", "lavfi", "-i", "color=c=black:s=128x128:r=1",
                 "-frames:v", "1", "-c:v", "h264_nvenc", "-preset", "p4",
                 "-rc", "vbr", "-cq", "23", "-b:v", "0", "-f", "null", "-"],
                capture_output=True, timeout=15,
            )
            if probe.returncode == 0:
                print("[merge_video] Encoder: NVIDIA NVENC", flush=True)
                return ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr",
                        "-cq", "23", "-b:v", "0"]
        except (OSError, subprocess.TimeoutExpired):
            pass
    preset = "veryfast" if automatic else "fast"
    print(f"[merge_video] Encoder: CPU libx264 ({preset})", flush=True)
    return ["-c:v", "libx264", "-preset", preset, "-crf", "23"]


def media_codecs(video_file: Path) -> dict[str, str]:
    """The first video and audio codec names, e.g. {"video": "h264", "audio": "aac"}."""
    result = subprocess.run(
        [ffprobe_binary(), "-v", "error", "-show_entries", "stream=codec_type,codec_name",
         "-of", "json", str(video_file)],
        capture_output=True, text=True,
    )
    codecs: dict[str, str] = {}
    if result.returncode == 0:
        for stream in json.loads(result.stdout or "{}").get("streams", []):
            codecs.setdefault(stream.get("codec_type", ""), stream.get("codec_name", ""))
    return codecs


def merge_video(video_file: Path, session: Path, dubbing_file: Path | None = None) -> Path:
    """Write media/video_final.mp4. Subtitles ship as separate files and are never burned in,
    so H.264 video, which browsers and editors all read, is copied without re-encoding.
    Other codecs are re-encoded once. A dub replaces the original audio."""
    media_dir = session / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    final_video = media_dir / "video_final.mp4"
    video_input = video_file.resolve()
    codecs = media_codecs(video_input)

    temporary_video = media_dir / f".video_final.{uuid.uuid4().hex}.mp4"
    try:
        command = [ffmpeg_binary(), "-hide_banner", "-loglevel", "warning", "-nostats", "-y", "-i", str(video_input)]
        if dubbing_file is not None:
            command.extend(["-i", str(dubbing_file.resolve())])
        command.extend(["-map", "0:v:0"])
        if dubbing_file is not None:
            # The dub is the only audio. It ends with the last line, so pad it with
            # silence and let -shortest end the output with the video.
            command.extend(["-map", "1:a:0", "-af", "apad"])
        else:
            command.extend(["-map", "0:a?"])
        if codecs.get("video") == "h264":
            print("[merge_video] Video: H.264 stream copied, no re-encode", flush=True)
            command.extend(["-c:v", "copy"])
        else:
            command.extend(video_encoding_args())
        keep_audio = dubbing_file is None and codecs.get("audio") == "aac"
        command.extend(["-c:a", "copy" if keep_audio else "aac"])
        command.extend(["-movflags", "+faststart"])
        if dubbing_file is not None:
            command.append("-shortest")
        command.append(str(temporary_video.resolve()))
        try:
            subprocess.run(command, check=True)
        except subprocess.CalledProcessError:
            if "h264_nvenc" not in command:
                raise
            # A driver may pass the small probe but fail on the actual resolution.
            print("[merge_video] NVENC failed; retrying with CPU libx264", flush=True)
            start, end = command.index("-c:v"), command.index("-c:a")
            command[start:end] = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]
            subprocess.run(command, check=True)
        temporary_video.replace(final_video)
        return final_video
    except Exception:
        temporary_video.unlink(missing_ok=True)
        raise
