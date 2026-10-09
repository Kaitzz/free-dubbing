"""Optional source captions and cover, fetched with the authenticated yt-dlp session."""
from __future__ import annotations
import html
import json
import re
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


def download_assets(ydl, info, session: Path, language):
    media, metadata = session / "media", session / "metadata"
    if not any(media.glob("thumbnail.*")):
        thumbs = info.get("thumbnails") or ([{"url": info["thumbnail"]}] if info.get("thumbnail") else [])
        for thumb in list(reversed(thumbs))[:3]:
            try:
                data = _fetch(ydl, thumb["url"], 10 * 1024 * 1024)
                ext = ("jpg" if data.startswith(b"\xff\xd8\xff") else
                       "png" if data.startswith(b"\x89PNG\r\n\x1a\n") else
                       "webp" if data[:4] == b"RIFF" and data[8:12] == b"WEBP" else None)
                if not ext:
                    continue
                (media / f"thumbnail.{ext}").write_bytes(data)
                print("[download] Cover saved", flush=True)
                break
            except Exception:
                continue
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
