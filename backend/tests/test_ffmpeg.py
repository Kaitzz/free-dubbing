from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from backend.app.adapters import ffmpeg

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def fake_media(monkeypatch, codecs: dict[str, str], fail_with: Exception | None = None) -> list[list[str]]:
    """Record ffmpeg commands; ffprobe reports the given codecs."""
    commands: list[list[str]] = []

    def run(cmd, **kwargs):
        if "-show_entries" in cmd:
            streams = [{"codec_type": kind, "codec_name": name} for kind, name in codecs.items()]
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"streams": streams}), stderr="")
        commands.append(list(cmd))
        Path(cmd[-1]).write_bytes(b"fresh mp4")
        if fail_with is not None:
            raise fail_with
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(ffmpeg.subprocess, "run", run)
    return commands


def option(command: list[str], name: str, occurrence: int = 0) -> str:
    indexes = [index for index, part in enumerate(command) if part == name]
    return command[indexes[occurrence] + 1]


def test_original_audio_output_copies_h264_and_aac_untouched(monkeypatch, tmp_path):
    commands = fake_media(monkeypatch, {"video": "h264", "audio": "aac"})

    final = ffmpeg.merge_video(tmp_path / "video.mp4", tmp_path / "session")

    assert final == tmp_path / "session" / "media" / "video_final.mp4"
    assert final.read_bytes() == b"fresh mp4"
    [command] = commands
    assert command.count("-i") == 1
    assert (option(command, "-map"), option(command, "-map", 1)) == ("0:v:0", "0:a?")
    assert (option(command, "-c:v"), option(command, "-c:a")) == ("copy", "copy")
    assert "-vf" not in command and "-shortest" not in command


def test_other_codecs_are_reencoded_once(monkeypatch, tmp_path):
    monkeypatch.setattr(ffmpeg, "video_encoding_args", lambda: ["-c:v", "libx264", "-preset", "fast", "-crf", "23"])
    commands = fake_media(monkeypatch, {"video": "vp9", "audio": "opus"})

    ffmpeg.merge_video(tmp_path / "video.mp4", tmp_path / "session")

    [command] = commands
    assert (option(command, "-c:v"), option(command, "-c:a")) == ("libx264", "aac")


def test_dub_replaces_the_audio_and_is_padded_to_the_video(monkeypatch, tmp_path):
    commands = fake_media(monkeypatch, {"video": "h264", "audio": "aac"})

    ffmpeg.merge_video(tmp_path / "video.mp4", tmp_path / "session", tmp_path / "audio_dubbing.wav")

    [command] = commands
    assert command.count("-i") == 2
    assert option(command, "-map", 1) == "1:a:0"
    assert option(command, "-af") == "apad"
    assert (option(command, "-c:v"), option(command, "-c:a")) == ("copy", "aac")
    assert "-shortest" in command
    assert not (tmp_path / "session" / "tmp" / "audio_mixed.m4a").exists()


def test_merge_video_uses_absolute_media_paths(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    commands = fake_media(monkeypatch, {"video": "h264"})
    session = Path("workfolder") / "uploader" / "title__videoid"

    ffmpeg.merge_video(session / "media" / "video_source.mp4", session, session / "tmp" / "audio_dubbing.wav")

    [command] = commands
    assert Path(option(command, "-i")).is_absolute()
    assert Path(option(command, "-i", 1)).is_absolute()
    assert Path(command[-1]).is_absolute()


def test_merge_video_replaces_corrupt_final_with_fresh_ffmpeg_output(monkeypatch, tmp_path):
    media_dir = tmp_path / "session" / "media"
    media_dir.mkdir(parents=True)
    final_video = media_dir / "video_final.mp4"
    final_video.write_bytes(b"corrupt")
    commands = fake_media(monkeypatch, {"video": "h264"})

    assert ffmpeg.merge_video(tmp_path / "video.mp4", tmp_path / "session") == final_video

    ffmpeg_output = Path(commands[-1][-1])
    assert final_video.read_bytes() == b"fresh mp4"
    assert ffmpeg_output != final_video
    assert ffmpeg_output.parent == media_dir.resolve()
    assert list(media_dir.glob(".video_final.*.mp4")) == []


def test_merge_video_failure_cleans_temporary_output_and_keeps_the_old_final(monkeypatch, tmp_path):
    media_dir = tmp_path / "session" / "media"
    media_dir.mkdir(parents=True)
    final_video = media_dir / "video_final.mp4"
    final_video.write_bytes(b"corrupt")
    fake_media(monkeypatch, {"video": "h264"}, fail_with=subprocess.CalledProcessError(1, "ffmpeg"))

    with pytest.raises(subprocess.CalledProcessError):
        ffmpeg.merge_video(tmp_path / "video.mp4", tmp_path / "session")

    assert final_video.read_bytes() == b"corrupt"
    assert list(media_dir.glob(".video_final.*.mp4")) == []


def test_media_codecs_reads_the_first_stream_of_each_type(monkeypatch):
    streams = [{"codec_type": "video", "codec_name": "h264"}, {"codec_type": "audio", "codec_name": "aac"},
               {"codec_type": "audio", "codec_name": "opus"}]
    commands: list[list[str]] = []

    def run(cmd, **kwargs):
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"streams": streams}), stderr="")

    monkeypatch.setenv("FFPROBE_PATH", "/opt/bin/ffprobe")
    monkeypatch.setattr(ffmpeg.subprocess, "run", run)
    assert ffmpeg.media_codecs(Path("video.mp4")) == {"video": "h264", "audio": "aac"}
    assert commands[0][0] == "/opt/bin/ffprobe"

    monkeypatch.setattr(ffmpeg.subprocess, "run", lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="bad"))
    assert ffmpeg.media_codecs(Path("video.mp4")) == {}


@pytest.mark.parametrize("result", [0, 1, "timeout"])
def test_auto_encoder_probes_hardware_and_falls_back(monkeypatch, result):
    monkeypatch.setenv("DUBBING_VIDEO_ENCODER", "auto")
    def run(cmd, **kwargs):
        assert kwargs["timeout"] == 15
        if result == "timeout":
            raise subprocess.TimeoutExpired(cmd, 15)
        return subprocess.CompletedProcess(cmd, result)
    monkeypatch.setattr(ffmpeg.subprocess, "run", run)
    args = ffmpeg.video_encoding_args()
    assert args[1] == ("h264_nvenc" if result == 0 else "libx264")
    if result != 0:
        assert "veryfast" in args


def test_nvenc_failure_retries_cpu_and_keeps_the_padded_dub(monkeypatch, tmp_path):
    commands = []
    monkeypatch.setattr(ffmpeg, "video_encoding_args", lambda: ["-c:v", "h264_nvenc", "-preset", "p4"])
    def run(cmd, **kwargs):
        if "-show_entries" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout='{"streams": [{"codec_type": "video", "codec_name": "av1"}]}')
        commands.append(list(cmd))
        Path(cmd[-1]).write_bytes(b"partial" if "h264_nvenc" in cmd else b"complete")
        if "h264_nvenc" in cmd:
            raise subprocess.CalledProcessError(1, cmd)
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(ffmpeg.subprocess, "run", run)
    result = ffmpeg.merge_video(tmp_path/"v.mp4", tmp_path/"session", tmp_path/"d.wav")
    assert len(commands) == 2
    assert result.read_bytes() == b"complete"
    assert "h264_nvenc" in commands[0]
    assert "libx264" in commands[1]
    assert option(commands[1], "-af") == "apad"
    assert option(commands[1], "-c:a") == "aac"


def _make_video(path: Path, seconds: int, *codec: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", f"color=c=black:s=64x64:r=10:d={seconds}",
                    "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                    *codec, "-shortest", str(path)], check=True)


def _streams(path: Path) -> dict[str, tuple[str, float]]:
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,duration",
                            "-of", "json", str(path)], capture_output=True, text=True, check=True)
    return {s["codec_type"]: (s["codec_name"], float(s["duration"])) for s in json.loads(probe.stdout)["streams"]}


def _video_packets_md5(path: Path) -> str:
    return subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v:0", "-c", "copy", "-f", "md5", "-"],
                          capture_output=True, text=True, check=True).stdout.strip()


@pytest.mark.skipif(not HAS_FFMPEG, reason="needs ffmpeg")
def test_real_h264_video_is_copied_and_keeps_its_length_when_the_dub_ends_early(monkeypatch, tmp_path):
    monkeypatch.delenv("DUBBING_VIDEO_ENCODER", raising=False)
    video = tmp_path / "video.mp4"
    _make_video(video, 3, "-c:v", "libx264", "-c:a", "aac")
    dub = tmp_path / "audio_dubbing.wav"
    sf.write(dub, np.full(16000, 0.1, dtype=np.float32), 16000)

    final = ffmpeg.merge_video(video, tmp_path / "session", dub)

    streams = _streams(final)
    assert streams["video"][0] == "h264" and streams["video"][1] == pytest.approx(3, abs=0.2)
    assert streams["audio"][0] == "aac" and streams["audio"][1] == pytest.approx(3, abs=0.2)
    assert _video_packets_md5(final) == _video_packets_md5(video)


@pytest.mark.skipif(not HAS_FFMPEG, reason="needs ffmpeg")
def test_real_non_h264_video_is_reencoded_to_h264(monkeypatch, tmp_path):
    monkeypatch.delenv("DUBBING_VIDEO_ENCODER", raising=False)
    video = tmp_path / "video.mp4"
    _make_video(video, 2, "-c:v", "mpeg4", "-c:a", "aac")

    final = ffmpeg.merge_video(video, tmp_path / "session")

    streams = _streams(final)
    assert streams["video"][0] == "h264" and streams["video"][1] == pytest.approx(2, abs=0.2)
    assert streams["audio"][0] == "aac"
