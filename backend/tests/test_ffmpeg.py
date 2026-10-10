from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from backend.app.adapters import ffmpeg


def test_video_orientation_uses_height_greater_than_width(monkeypatch):
    def fake_run(cmd, capture_output=False, text=False, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="720,1280\n", stderr="")

    monkeypatch.setattr(ffmpeg.subprocess, "run", fake_run)

    assert ffmpeg.get_video_orientation(Path("video.mp4")) == "portrait"


def test_video_orientation_defaults_to_landscape_when_probe_fails(monkeypatch):
    def fake_run(cmd, capture_output=False, text=False, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="ffprobe failed")

    monkeypatch.setattr(ffmpeg.subprocess, "run", fake_run)

    assert ffmpeg.get_video_orientation(Path("video.mp4")) == "landscape"


def test_subtitle_styles_match_backend_orientation_rules():
    portrait = ffmpeg.subtitle_style_for_orientation("portrait", "Noto Sans CJK SC", "zh")
    landscape = ffmpeg.subtitle_style_for_orientation("landscape", "Noto Sans CJK SC", "zh")

    assert "FontSize=11.04" in portrait
    assert "MarginV=70" in portrait
    assert "FontSize=22.08" in landscape
    assert "MarginV=5" in landscape


def test_subtitle_styles_use_smaller_size_for_english():
    portrait_en = ffmpeg.subtitle_style_for_orientation("portrait", "Arial", "en")
    landscape_en = ffmpeg.subtitle_style_for_orientation("landscape", "Arial", "en")

    assert "FontSize=8.28" in portrait_en
    assert "FontSize=16.56" in landscape_en


def test_subtitle_filter_picks_chinese_font_for_zh_srt(monkeypatch, tmp_path):
    monkeypatch.setattr(ffmpeg, "get_video_orientation", lambda _: "landscape")
    sub_zh = tmp_path / "subtitles.zh.srt"
    sub_zh.write_text("", encoding="utf-8")
    assert "FontName=Noto Sans CJK SC" in ffmpeg.subtitle_filter(tmp_path / "v.mp4", sub_zh, tmp_path)
    sub_en = tmp_path / "subtitles.en.srt"
    sub_en.write_text("", encoding="utf-8")
    assert "FontName=Arial" in ffmpeg.subtitle_filter(tmp_path / "v.mp4", sub_en, tmp_path)


def test_merge_video_burns_portrait_subtitles(monkeypatch, tmp_path):
    session = tmp_path / "session"
    metadata_dir = session / "metadata"
    metadata_dir.mkdir(parents=True)
    timings = metadata_dir / "timings.json"
    timings.write_text(
        json.dumps(
            {
                "translation": [
                    {
                        "start_time": 0,
                        "end_time": 1200,
                        "actual_start_time": 0,
                        "actual_end_time": 1200,
                        "zh": "你好",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    commands: list[list[str]] = []
    cwd_values: list[Path | None] = []

    def fake_run(cmd, capture_output=False, text=False, check=False, **kwargs):
        commands.append(cmd)
        cwd_values.append(kwargs.get("cwd"))
        if cmd[0] == "ffprobe":
            return subprocess.CompletedProcess(cmd, 0, stdout="720,1280\n", stderr="")
        Path(cmd[-1]).write_bytes(b"media")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(ffmpeg.subprocess, "run", fake_run)

    final_video = ffmpeg.merge_video(
        tmp_path / "video.mp4",
        tmp_path / "dubbing.wav",
        timings,
        session,
    )

    assert final_video == session / "media" / "video_final.mp4"
    assert len(commands) == 2
    final_command = commands[-1]
    filter_arg = final_command[final_command.index("-vf") + 1]
    assert filter_arg.startswith("subtitles=filename='metadata/subtitles.zh.srt'")
    assert "FontSize=11.04" in filter_arg
    assert "MarginV=70" in filter_arg
    assert "-c:s" not in final_command
    assert cwd_values[-1] == session.resolve()


def test_merge_video_uses_absolute_media_paths_when_cwd_is_session(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    session = Path("workfolder") / "uploader" / "title__videoid"
    metadata_dir = session / "metadata"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    timings = metadata_dir / "timings.json"
    timings.write_text(
        json.dumps(
            {
                "translation": [
                    {
                        "start_time": 0,
                        "end_time": 1200,
                        "actual_start_time": 0,
                        "actual_end_time": 1200,
                        "zh": "你好",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    commands: list[list[str]] = []
    cwd_values: list[Path | None] = []

    def fake_run(cmd, capture_output=False, text=False, check=False, **kwargs):
        commands.append(cmd)
        cwd_values.append(kwargs.get("cwd"))
        if cmd[0] == "ffprobe":
            return subprocess.CompletedProcess(cmd, 0, stdout="720,1280\n", stderr="")
        Path(cmd[-1]).write_bytes(b"media")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(ffmpeg.subprocess, "run", fake_run)

    ffmpeg.merge_video(
        session / "media" / "video_source.mp4",
        session / "tmp" / "audio_dubbing.wav",
        timings,
        session,
    )

    final_command = commands[-1]
    assert Path(final_command[final_command.index("-i") + 1]).is_absolute()
    assert Path(final_command[final_command.index("-i", final_command.index("-i") + 1) + 1]).is_absolute()
    assert Path(final_command[-1]).is_absolute()
    assert cwd_values[-1] == session.resolve()


def test_merge_video_subtitles_transcodes_original_audio_to_aac(monkeypatch, tmp_path):
    session = tmp_path / "session"
    metadata_dir = session / "metadata"
    metadata_dir.mkdir(parents=True)
    translation = metadata_dir / "translation.zh.json"
    translation.write_text(
        json.dumps(
            {
                "translation": [
                    {"start_time": 0, "end_time": 1000, "zh": "你好"}
                ]
            }
        ),
        encoding="utf-8",
    )
    commands: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        if cmd[0] == "ffprobe":
            return subprocess.CompletedProcess(cmd, 0, stdout="1920,1080\n", stderr="")
        Path(cmd[-1]).write_bytes(b"media")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(ffmpeg.subprocess, "run", fake_run)

    ffmpeg.merge_video(
        tmp_path / "video.mp4",
        None,
        translation,
        session,
        output_mode="subtitles",
    )

    assert len(commands) == 2
    final_command = commands[-1]
    assert final_command.count("-i") == 1
    assert "-vf" in final_command
    assert final_command[final_command.index("-map", final_command.index("-map") + 1) + 1] == "0:a?"
    assert final_command[final_command.index("-c:a") + 1] == "aac"
    assert "-shortest" not in final_command


def test_merge_video_dubbing_omits_hard_subtitles(monkeypatch, tmp_path):
    session = tmp_path / "session"
    metadata_dir = session / "metadata"
    metadata_dir.mkdir(parents=True)
    timings = metadata_dir / "timings.json"
    timings.write_text('{"translation": []}', encoding="utf-8")
    commands: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        Path(cmd[-1]).write_bytes(b"media")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(ffmpeg.subprocess, "run", fake_run)

    ffmpeg.merge_video(
        tmp_path / "video.mp4",
        tmp_path / "dubbing.wav",
        timings,
        session,
        output_mode="dubbing",
    )

    # One pass: the dub is the only audio, padded so the video is not cut at the last line.
    assert len(commands) == 1
    final_command = commands[-1]
    assert "-vf" not in final_command
    assert not (metadata_dir / "subtitles.zh.srt").exists()
    assert final_command.count("-i") == 2
    assert final_command[final_command.index("-map", final_command.index("-map") + 1) + 1] == "1:a:0"
    assert final_command[final_command.index("-af") + 1] == "apad"
    assert final_command[final_command.index("-c:a") + 1] == "aac"
    assert "-shortest" in final_command
    assert not (session / "tmp" / "audio_mixed.m4a").exists()


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="needs ffmpeg")
def test_merge_video_keeps_the_whole_video_when_the_dub_ends_early(monkeypatch, tmp_path):
    monkeypatch.delenv("DUBBING_VIDEO_ENCODER", raising=False)
    session = tmp_path / "session"
    (session / "metadata").mkdir(parents=True)
    timings = session / "metadata" / "timings.json"
    timings.write_text('{"translation": []}', encoding="utf-8")
    video = tmp_path / "video.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=black:s=64x64:r=10:d=3",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                    "-c:v", "mpeg4", "-c:a", "aac", "-shortest", str(video)], check=True)
    dub = tmp_path / "audio_dubbing.wav"
    sf.write(dub, np.full(16000, 0.1, dtype=np.float32), 16000)

    final = ffmpeg.merge_video(video, dub, timings, session, output_mode="dubbing")

    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,duration",
                            "-of", "json", str(final)], capture_output=True, text=True, check=True)
    durations = {s["codec_type"]: float(s["duration"]) for s in json.loads(probe.stdout)["streams"]}
    assert durations["video"] == pytest.approx(3, abs=0.2)
    assert durations["audio"] == pytest.approx(3, abs=0.2)


def test_merge_video_replaces_corrupt_final_with_fresh_ffmpeg_output(monkeypatch, tmp_path):
    session = tmp_path / "session"
    metadata_dir = session / "metadata"
    media_dir = session / "media"
    metadata_dir.mkdir(parents=True)
    media_dir.mkdir()
    translation = metadata_dir / "translation.zh.json"
    translation.write_text('{"translation": []}', encoding="utf-8")
    final_video = media_dir / "video_final.mp4"
    final_video.write_bytes(b"corrupt")
    commands: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        if cmd[0] == "ffprobe":
            return subprocess.CompletedProcess(cmd, 0, stdout="1920,1080\n", stderr="")
        Path(cmd[-1]).write_bytes(b"fresh mp4")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(ffmpeg.subprocess, "run", fake_run)

    result = ffmpeg.merge_video(
        tmp_path / "video.mp4",
        None,
        translation,
        session,
        output_mode="subtitles",
    )

    ffmpeg_output = Path(commands[-1][-1])
    assert result == final_video
    assert final_video.read_bytes() == b"fresh mp4"
    assert ffmpeg_output != final_video
    assert ffmpeg_output.parent == media_dir.resolve()
    assert ffmpeg_output.suffix == ".mp4"
    assert list(media_dir.glob(".video_final.*.mp4")) == []


def test_merge_video_failure_cleans_temporary_output_and_preserves_visible_failure(monkeypatch, tmp_path):
    session = tmp_path / "session"
    metadata_dir = session / "metadata"
    media_dir = session / "media"
    metadata_dir.mkdir(parents=True)
    media_dir.mkdir()
    translation = metadata_dir / "translation.zh.json"
    translation.write_text('{"translation": []}', encoding="utf-8")
    final_video = media_dir / "video_final.mp4"
    final_video.write_bytes(b"corrupt")

    def fake_run(cmd, **kwargs):
        if cmd[0] == "ffprobe":
            return subprocess.CompletedProcess(cmd, 0, stdout="1920,1080\n", stderr="")
        Path(cmd[-1]).write_bytes(b"partial")
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(ffmpeg.subprocess, "run", fake_run)

    with pytest.raises(subprocess.CalledProcessError):
        ffmpeg.merge_video(
            tmp_path / "video.mp4",
            None,
            translation,
            session,
            output_mode="subtitles",
        )

    assert final_video.read_bytes() == b"corrupt"
    assert list(media_dir.glob(".video_final.*.mp4")) == []


def test_merge_video_requires_dubbing_audio_for_dubbing_modes(tmp_path):
    with pytest.raises(ValueError, match="Dubbing audio is required"):
        ffmpeg.merge_video(tmp_path / "video.mp4", None, tmp_path / "timings.json", tmp_path / "session")


def test_merge_video_rejects_unknown_output_mode(tmp_path):
    with pytest.raises(ValueError, match="output_mode must be one of"):
        ffmpeg.merge_video(
            tmp_path / "video.mp4",
            None,
            tmp_path / "timings.json",
            tmp_path / "session",
            output_mode="captions",
        )


def test_split_subtitle_text_breaks_on_punctuation_and_keeps_protected():
    out = ffmpeg.split_subtitle_text("我们今天讨论一下宇宙的边界，那是一个神秘话题；不过别担心，我会详细解释。")
    assert len(out) >= 3
    assert all(len(s) >= 2 for s in out)
    protected = ffmpeg.split_subtitle_text("他说《三体，黑暗森林》是经典，必读。")
    assert any("《三体，黑暗森林》" in s for s in protected)


def test_write_srt_keeps_caption_for_original_audio_mode(tmp_path):
    session = tmp_path / "session"
    metadata_dir = session / "metadata"
    metadata_dir.mkdir(parents=True)
    timings = metadata_dir / "timings.json"
    timings.write_text(
        json.dumps(
            {
                "translation": [
                    {
                        "start_time": 0,
                        "end_time": 800,
                        "dst": "（笑声）",
                        "dst_lang": "zh",
                        "audio_mode": "original",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    srt = ffmpeg.write_srt(timings, session)

    assert "（笑声）" in srt.read_text(encoding="utf-8")


def test_write_srt_splits_long_sentence_into_multiple_entries(tmp_path):
    session = tmp_path / "session"
    metadata_dir = session / "metadata"
    metadata_dir.mkdir(parents=True)
    timings = metadata_dir / "timings.json"
    timings.write_text(
        json.dumps(
            {
                "translation": [
                    {
                        "start_time": 0,
                        "end_time": 6000,
                        "actual_start_time": 0,
                        "actual_end_time": 6000,
                        "zh": "我们今天讨论宇宙的边界，那是一个神秘话题；不过别担心，我会详细解释",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    srt = ffmpeg.write_srt(timings, session)
    content = srt.read_text(encoding="utf-8")
    blocks = [b for b in content.strip().split("\n\n") if b.strip()]
    assert len(blocks) >= 3
    assert all("-->" in b for b in blocks)


def test_probe_video_size_uses_configured_ffprobe(monkeypatch):
    commands: list[list[str]] = []

    def fake_run(cmd, capture_output=False, text=False, **kwargs):
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="1920,1080\n", stderr="")

    monkeypatch.setenv("FFPROBE_PATH", "/opt/bin/ffprobe")
    monkeypatch.setattr(ffmpeg.subprocess, "run", fake_run)

    assert ffmpeg.probe_video_size(Path("video.mp4")) == (1920, 1080)
    assert commands[0][0] == "/opt/bin/ffprobe"


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
    session = tmp_path / "session"
    (session / "metadata").mkdir(parents=True)
    timings = session / "metadata/timings.json"
    timings.write_text('{"translation": []}')
    commands = []
    monkeypatch.setattr(ffmpeg, "video_encoding_args", lambda: ["-c:v", "h264_nvenc", "-preset", "p4"])
    def run(cmd, **kwargs):
        commands.append(list(cmd))
        Path(cmd[-1]).write_bytes(b"partial" if "h264_nvenc" in cmd else b"complete")
        if "h264_nvenc" in cmd:
            raise subprocess.CalledProcessError(1, cmd)
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(ffmpeg.subprocess, "run", run)
    result = ffmpeg.merge_video(tmp_path/"v.mp4", tmp_path/"d.wav", timings, session, output_mode="dubbing")
    assert len(commands) == 2
    assert result.read_bytes() == b"complete"
    assert "h264_nvenc" in commands[0]
    assert "libx264" in commands[1]
    assert commands[1][commands[1].index("-af") + 1] == "apad"
    assert commands[1][commands[1].index("-c:a") + 1] == "aac"
