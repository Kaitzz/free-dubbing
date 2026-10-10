from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import soundfile as sf

from backend.app.adapters import source_audio


def test_extract_audio_decodes_first_stream_to_stereo_pcm(monkeypatch, tmp_path):
    session = tmp_path / "session"
    (session / "media").mkdir(parents=True)
    (session / "media" / "audio_vocals.wav").write_bytes(b"stale")
    commands = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        Path(cmd[-1]).write_bytes(b"RIFF")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(source_audio.subprocess, "run", fake_run)

    output = source_audio.extract_audio(tmp_path / "video_source.mp4", session)

    assert output == session / "media" / "audio_vocals.wav"
    assert output.read_bytes() == b"RIFF"
    command = commands[0]
    assert command[command.index("-map") + 1] == "0:a:0"
    assert command[command.index("-ac") + 1] == "2"
    assert command[command.index("-ar") + 1] == "44100"
    assert command[command.index("-c:a") + 1] == "pcm_s16le"
    assert command[command.index("-rf64") + 1] == "auto"
    assert list((session / "media").glob(".audio_vocals.*")) == []


def test_extract_audio_failure_reports_ffmpeg_error_and_leaves_no_output(monkeypatch, tmp_path):
    session = tmp_path / "session"
    (session / "media").mkdir(parents=True)
    (session / "media" / "audio_vocals.wav").write_bytes(b"stale")

    def fake_run(cmd, **kwargs):
        Path(cmd[-1]).write_bytes(b"partial")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="noise\nStream map '0:a:0' matches no streams.\n")

    monkeypatch.setattr(source_audio.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="matches no streams"):
        source_audio.extract_audio(tmp_path / "silent.mp4", session)

    assert list((session / "media").iterdir()) == []


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_extract_audio_with_real_ffmpeg_duplicates_mono_to_stereo(tmp_path):
    video = tmp_path / "video.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=black:s=64x64:r=10:d=1.5",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=22050:duration=1.5",
                    "-ac", "1", "-c:v", "mpeg4", "-c:a", "aac", "-shortest", str(video)], check=True)

    output = source_audio.extract_audio(video, tmp_path / "session")

    info = sf.info(output)
    assert (info.samplerate, info.channels) == (44100, 2)
    assert info.duration == pytest.approx(1.5, abs=0.1)
