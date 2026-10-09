"""Optional source captions and cover, fetched with the authenticated yt-dlp session."""
from __future__ import annotations
import html
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse, parse_qs


def caption_candidates(info, language):
    # Do not mistake a translated track for the original audio language.
    original = str(info.get("language") or language).split("-")[0]
    if original != language.split("-")[0]:
        return []
    candidates = []
    for kind, key in (("manual", "subtitles"), ("automatic", "automatic_captions")):
        tracks = info.get(key) or {}
        for lang in sorted(tracks, key=lambda x: (x != language, "orig" not in x, x)):
            if lang.split("-")[0] != original:
                continue
            for track in sorted(tracks[lang], key=lambda t: t.get("ext") != "json3"):
                url = track.get("url", "")
                if track.get("ext") in {"json3", "vtt", "srt"} and not parse_qs(urlparse(url).query).get("tlang"):
                    candidates.append((kind, lang, track))
    return candidates


def _text(value):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", "", value))).strip()


def _clock(value):
    fields = value.replace(",", ".").split(":")
    total = 0.0
    for field in fields:
        total = total * 60 + float(field)
    return round(total * 1000)


def parse_captions(content, ext, automatic=False):
    rows = []
    if ext == "json3":
        for event in json.loads(content).get("events", []):
            text = _text("".join(segment.get("utf8", "") for segment in event.get("segs", [])))
            if not text:
                continue  # Window styling and newline-only append events.
            if "tStartMs" not in event or "dDurationMs" not in event:
                raise ValueError("Text event lacks reliable timing; try another format")
            start = round(float(event["tStartMs"]))
            end = start + round(float(event["dDurationMs"]))
            if not end > start >= 0:
                raise ValueError("Invalid caption timing")
            rows.append([start, end, text])
    else:
        pattern = r"((?:\d+:)?\d{2}:\d{2}[.,]\d{3})\s*-->\s*((?:\d+:)?\d{2}:\d{2}[.,]\d{3})[^\n]*\n(.*?)(?=\n\s*\n|\Z)"
        matches = list(re.finditer(pattern, content.replace("\r", ""), re.S))
        if len(matches) != content.count("-->"):
            raise ValueError("Some subtitle timing blocks could not be parsed")
        for match in matches:
            start, end = _clock(match[1]), _clock(match[2])
            text = _text(match[3])
            if text:
                if not end > start >= 0:
                    raise ValueError("Invalid caption timing")
                rows.append([start, end, text])
    cleaned = []
    previous_raw = None
    for start, end, text in sorted(rows, key=lambda r: (r[0], r[1])):
        raw = (start, end, text)
        if automatic and ext != "json3" and previous_raw and start < previous_raw[1]:
            old, new = previous_raw[2].split(), text.split()
            for size in range(min(len(old), len(new)), 0, -1):
                if old[-size:] == new[:size]:
                    text = " ".join(new[size:])
                    break
        previous_raw = raw
        if not text:
            if cleaned:
                cleaned[-1][1] = max(cleaned[-1][1], end)
            continue
        if cleaned and start < cleaned[-1][1]:
            previous = cleaned[-1]
            if start <= previous[0]:
                previous[2] += " " + text
                previous[1] = max(previous[1], end)
                continue
            previous[1] = start
        cleaned.append([start, end, text])
    if not cleaned:
        raise ValueError("No usable timed captions")
    return cleaned


def _fetch(ydl, url, limit):
    if urlparse(url).scheme != "https":
        raise ValueError("Asset URL must use HTTPS")
    with ydl.urlopen(url) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Asset exceeds size limit")
    return data


def _thumbnail_candidates(info):
    thumbs = list(info.get("thumbnails") or [])
    if info.get("thumbnail"):
        thumbs.append({"url": info["thumbnail"]})
    def rank(thumb):
        url = thumb.get("url", "")
        name = Path(urlparse(url).path).stem
        # These are cover variants; numbered thumbnails are sampled video frames.
        tiers = {"maxresdefault": 6, "hq720": 5, "sddefault": 4,
                 "hqdefault": 3, "mqdefault": 2, "default": 1}
        area = (thumb.get("width") or 0) * (thumb.get("height") or 0)
        return (tiers.get(name, 0), area, (thumb["preference"] if thumb.get("preference") is not None else -100),
                url == info.get("thumbnail"))
    seen = set()
    for thumb in sorted(thumbs, key=rank, reverse=True):
        url = thumb.get("url")
        if url and url not in seen:
            seen.add(url)
            yield thumb


def _image_size(data):
    from ..config import ffprobe_binary
    result = subprocess.run([ffprobe_binary(), "-v", "error", "-i", "pipe:0",
                             "-select_streams", "v:0", "-show_entries", "stream=width,height",
                             "-of", "json"], input=data, capture_output=True, timeout=10)
    if result.returncode:
        raise ValueError("Cannot decode thumbnail")
    stream = json.loads(result.stdout)["streams"][0]
    width, height = int(stream["width"]), int(stream["height"])
    if width <= 0 or height <= 0:
        raise ValueError("Invalid thumbnail dimensions")
    return width, height



def convert_cover(data: bytes, image_format: str = "jpg") -> bytes:
    """Encode the actual image format, without scaling the source image."""
    if image_format not in {"jpg", "png"}:
        raise ValueError("Cover format must be jpg or png")
    if image_format == "jpg" and data.startswith(b"\xff\xd8\xff"):
        return data
    from ..config import ffmpeg_binary
    codec = ["-c:v", "mjpeg", "-q:v", "2", "-pix_fmt", "yuvj444p"] if image_format == "jpg" else ["-c:v", "png"]
    result = subprocess.run([ffmpeg_binary(), "-hide_banner", "-loglevel", "error",
        "-i", "pipe:0", "-frames:v", "1", *codec, "-f", "image2pipe", "pipe:1"],
        input=data, capture_output=True, timeout=30)
    signature = b"\xff\xd8\xff" if image_format == "jpg" else b"\x89PNG\r\n\x1a\n"
    if result.returncode or not result.stdout.startswith(signature):
        raise ValueError("Cover image conversion failed")
    return result.stdout

def download_cover(ydl, info, session):
    media = session / "media"
    best = None
    # Existing low-resolution covers can be upgraded without re-downloading video.
    for path in media.glob("thumbnail.*"):
        if path.suffix not in {".jpg", ".png", ".webp"}:
            continue
        try:
            size = _image_size(path.read_bytes())
            if min(size) >= 720:
                if path.suffix != ".jpg":
                    (media / "thumbnail.jpg").write_bytes(convert_cover(path.read_bytes()))
                return
        except Exception:
            pass
    for thumb in list(_thumbnail_candidates(info))[:8]:
        try:
            data = _fetch(ydl, thumb["url"], 10 * 1024 * 1024)
            ext = ("jpg" if data.startswith(b"\xff\xd8\xff") else
                   "png" if data.startswith(b"\x89PNG\r\n\x1a\n") else
                   "webp" if data[:4] == b"RIFF" and data[8:12] == b"WEBP" else None)
            if not ext:
                continue
            width, height = _image_size(data)
            if best is None or width*height > best[0]:
                best = (width*height, data, ext, width, height)
            if min(width, height) >= 720:
                break
        except Exception:
            continue
    if best:
        _, data, ext, width, height = best
        target = media / "thumbnail.jpg"
        # Do not replace a usable larger image with a smaller fallback.
        for previous in media.glob("thumbnail.*"):
            if previous.suffix in {".jpg", ".png", ".webp"}:
                try:
                    w, h = _image_size(previous.read_bytes())
                    if w*h > width*height:
                        return
                except Exception:
                    pass
        data = convert_cover(data)
        target.write_bytes(data)
        for previous in media.glob("thumbnail.*"):
            if previous != target and previous.suffix in {".jpg", ".png", ".webp"}:
                previous.unlink()
        print(f"[download] Cover saved: {width}x{height}", flush=True)


def download_assets(ydl, info, session: Path, language):
    media, metadata = session / "media", session / "metadata"
    download_cover(ydl, info, session)
    if (metadata / "source_subtitles.json").exists():
        return
    # Try alternate encodings when a track cannot be parsed without losing text.
    attempted = set()
    for kind, lang, track in caption_candidates(info, language):
        if (kind, lang, track["ext"]) in attempted:
            continue
        attempted.add((kind, lang, track["ext"]))
        try:
            data = _fetch(ydl, track["url"], 8 * 1024 * 1024)
            raw_dir = metadata / "raw_subtitles"
            raw_dir.mkdir(exist_ok=True)
            safe_language = re.sub(r"[^A-Za-z0-9_-]", "_", lang)
            (raw_dir / f"{kind}.{safe_language}.{track['ext']}").write_bytes(data)
            rows = parse_captions(data.decode("utf-8-sig"), track["ext"], kind == "automatic")
            payload = {"subtitle_source": {"kind": kind, "language": lang}, "result": {
                "text": " ".join(row[2] for row in rows),
                "utterances": [{"start_time": a, "end_time": b, "text": text,
                                "additions": {"speaker": "1"}, "words": []} for a, b, text in rows]}}
            from .ffmpeg import _srt_time
            srt = "\n\n".join(f"{i}\n{_srt_time(a)} --> {_srt_time(b)}\n{text}" for i, (a,b,text) in enumerate(rows, 1)) + "\n"
            (metadata / "source_subtitles.srt").write_text(srt, encoding="utf-8")
            (metadata / "source_subtitles.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            print(f"[download] Source CC: {kind}, {lang}, {len(rows)} cues", flush=True)
            return
        except Exception as exc:
            print(f"[download] CC unavailable ({kind}, {lang}, {type(exc).__name__}); trying fallback", flush=True)
    print("[download] No usable source CC; will use SenseVoice", flush=True)
