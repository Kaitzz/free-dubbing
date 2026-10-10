from pathlib import Path

import pytest

from backend.app.adapters import ytdlp
from backend.app.sources import SourceConfig


def _make_source(*, use_proxy: bool, cookie_dir: Path) -> SourceConfig:
    cookie_path = cookie_dir / "missing-cookie.txt"

    class _Source(SourceConfig):
        @property
        def cookie_path(self):
            return cookie_path

    return _Source(
        name="test",
        matches=lambda url: True,
        use_proxy=use_proxy,
        cookie_filename="missing-cookie.txt",
        asr_language="en",
        target_language="zh",
    )


def test_ytdlp_proxy_port_takes_priority(monkeypatch, tmp_path):
    monkeypatch.setenv("HTTP_PROXY", "http://env-proxy:8080")
    source = _make_source(use_proxy=True, cookie_dir=tmp_path)

    options = ytdlp._ydl_base(source, "7890")
    assert options["noprogress"] is True
    assert options["no_warnings"] is False

    assert options["proxy"] == "http://127.0.0.1:7890"


def test_ytdlp_proxy_falls_back_to_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("HTTP_PROXY", "http://env-proxy:8080")
    source = _make_source(use_proxy=True, cookie_dir=tmp_path)

    options = ytdlp._ydl_base(source, "")

    assert options["proxy"] == "http://env-proxy:8080"


def test_ytdlp_disables_proxy_when_source_opts_out(monkeypatch, tmp_path):
    monkeypatch.setenv("HTTP_PROXY", "http://env-proxy:8080")
    source = _make_source(use_proxy=False, cookie_dir=tmp_path)

    options = ytdlp._ydl_base(source, "7890")

    assert options["proxy"] == ""


def test_ytdlp_enables_node_js_runtime(tmp_path):
    source = _make_source(use_proxy=True, cookie_dir=tmp_path)

    options = ytdlp._ydl_base(source, "")

    assert options["js_runtimes"] == {"node": {}}


def test_ytdlp_format_candidates_prefer_h264_then_fall_back_to_any_codec():
    first = ytdlp.FORMAT_CANDIDATES[0].split("/")
    assert first[0] == "bestvideo[height<=1080][vcodec^=avc1]+bestaudio[ext=m4a]"
    assert first[-2:] == ["bestvideo[height<=1080]+bestaudio", "best"]
    assert "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best" not in ytdlp.FORMAT_CANDIDATES


def _youtube_source() -> SourceConfig:
    return SourceConfig(
        name="youtube",
        matches=lambda url: True,
        use_proxy=False,
        cookie_filename=None,
        asr_language="en",
        target_language="zh",
    )


def test_download_video_passes_only_the_canonical_url_to_both_ytdlp_sinks(
    monkeypatch, tmp_path
):
    extracted_urls: list[str] = []
    downloaded_urls: list[str] = []

    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def extract_info(self, url, *, download, process):
            extracted_urls.append(url)
            assert download is False
            assert process is False
            return {
                "id": "abcdefghijk",
                "uploader": "tester",
                "title": "canonical",
                "webpage_url": url,
            }

        def sanitize_info(self, info):
            return info

        def download(self, urls):
            downloaded_urls.extend(urls)
            Path(self.options["outtmpl"]).write_bytes(b"video")

    monkeypatch.setattr(ytdlp.yt_dlp, "YoutubeDL", FakeYoutubeDL)

    session, _ = ytdlp.download_video(
        "HTTPS://WWW.YOUTUBE.COM:443/watch?v=abcdefghijk",
        tmp_path,
        _youtube_source(),
    )

    expected = "https://www.youtube.com/watch?v=abcdefghijk"
    assert extracted_urls == [expected]
    assert downloaded_urls == [expected]
    assert (session / "media" / "video_source.mp4").read_bytes() == b"video"


def test_output_mode_task_ids_use_separate_remote_sessions(tmp_path):
    info = {
        "id": "abcdefghijk",
        "uploader": "tester",
        "title": "same video",
    }

    subtitles = ytdlp._session_path(tmp_path, info, "abcdefghijk-subtitles")
    dubbing = ytdlp._session_path(tmp_path, info, "abcdefghijk-dubbing")
    both = ytdlp._session_path(tmp_path, info, "abcdefghijk")

    assert len({subtitles, dubbing, both}) == 3
    assert subtitles.name.endswith("__abcdefghijk-subtitles")
    assert dubbing.name.endswith("__abcdefghijk-dubbing")
    assert both.name.endswith("__abcdefghijk")


def test_download_video_rejects_deceptive_url_before_cookie_or_ytdlp(
    monkeypatch, tmp_path
):
    calls: list[str] = []
    monkeypatch.setattr(ytdlp, "_ensure_cookie", lambda source: calls.append("cookie"))
    monkeypatch.setattr(
        ytdlp.yt_dlp,
        "YoutubeDL",
        lambda options: calls.append("ytdlp"),
    )

    with pytest.raises(ValueError):
        ytdlp.download_video(
            "https://youtube.com.evil.example/watch?v=abcdefghijk",
            tmp_path,
            _youtube_source(),
        )

    assert calls == []


def test_metadata_with_only_storyboards_fails_before_download(monkeypatch,tmp_path):
    class FakeYoutubeDL:
        def __init__(self,options): assert options['no_warnings'] is False
        def __enter__(self): return self
        def __exit__(self,*a): return False
        def extract_info(self,url,*,download,process):
            assert process is False
            return {'id':'abcdefghijk','formats':[{'vcodec':'none','acodec':'none','ext':'mhtml'}]}
        def download(self,*a): raise AssertionError('Must not download storyboards')
    monkeypatch.setattr(ytdlp.yt_dlp,'YoutubeDL',FakeYoutubeDL)
    with pytest.raises(RuntimeError,match='no usable video/audio'):
        ytdlp.download_video('https://www.youtube.com/watch?v=abcdefghijk',tmp_path,_youtube_source())
