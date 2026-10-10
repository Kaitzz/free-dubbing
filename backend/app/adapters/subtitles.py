"""SRT files for the translation and the original text, cut on one shared timeline."""
from __future__ import annotations

import itertools
import json
import re
from pathlib import Path

from ..audio_mode import target_text

SUBTITLE_PUNCTUATION = {"，", ",", "；", ";", "：", ":", "。", "?", "？", "!", "！", "、"}
SUBTITLE_PROTECTED_PAIRS = {"《": "》", "（": "）", "【": "】", "「": "」", "『": "』"}
SUBTITLE_CLOSING_QUOTES = {'"', "'", "」", "』", "》", "）", "】", "”", "’", "]"}
SUBTITLE_MIN_FRAGMENT_LEN = 5
SUBTITLE_MIN_DURATION_MS = 200
SUBTITLE_TAIL_BUFFER_MS = 100
SUBTITLE_DURATION_FLOOR_MS = 600

# Kana, Han and full-width forms: scripts written without spaces between words.
_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿＀-￯]")
_BREAK_AFTER = tuple(",.;:!?，。；：！？、…")


def _srt_time(ms: int) -> str:
    hours = ms // 3_600_000
    ms -= hours * 3_600_000
    minutes = ms // 60_000
    ms -= minutes * 60_000
    seconds = ms // 1000
    millis = ms - seconds * 1000
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _join(left: str, right: str) -> str:
    """Concatenate two pieces of text, keeping a space between Latin words."""
    if left and right and left[-1].isascii() and left[-1].isalnum() and right[0].isascii() and right[0].isalnum():
        return f"{left} {right}"
    return f"{left}{right}"


def _split_protected(text: str) -> list[str]:
    segments: list[str] = []
    buf: list[str] = []
    inside = None
    for ch in text:
        if inside is None and ch in SUBTITLE_PROTECTED_PAIRS:
            inside = SUBTITLE_PROTECTED_PAIRS[ch]
            buf.append(ch)
            continue
        if inside is not None and ch == inside:
            inside = None
            buf.append(ch)
            continue
        if inside is None and ch in SUBTITLE_PUNCTUATION:
            chunk = "".join(buf).strip()
            if chunk:
                segments.append(chunk)
            buf.clear()
            continue
        buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        segments.append(tail)
    return segments


def _attach_closing_quotes(segments: list[str]) -> list[str]:
    fixed: list[str] = []
    for seg in segments:
        if seg and seg[0] in SUBTITLE_CLOSING_QUOTES and fixed:
            fixed[-1] = f"{fixed[-1]}{seg}".strip()
            continue
        fixed.append(seg.strip())
    return fixed


def _merge_short_fragments(segments: list[str]) -> list[str]:
    merged: list[str] = []
    i = 0
    while i < len(segments):
        cur = segments[i]
        if len(cur.strip()) < SUBTITLE_MIN_FRAGMENT_LEN and i + 1 < len(segments):
            segments[i + 1] = _join(cur, segments[i + 1]).strip()
            i += 1
            continue
        merged.append(cur)
        i += 1
    return merged


def _strip_trailing_punct(segments: list[str]) -> list[str]:
    cleaned: list[str] = []
    for item in segments:
        text = item.strip()
        if not text:
            continue
        if text.endswith(("，", ",", "。")):
            text = text[:-1]
        cleaned.append(re.sub(r"\s+", " ", text).strip())
    return cleaned


def split_subtitle_text(text: str) -> list[str]:
    original = (text or "").strip()
    if not original:
        return []
    segments = _split_protected(original)
    if not segments:
        return [original]
    segments = _attach_closing_quotes(segments)
    segments = _merge_short_fragments(segments)
    cleaned = _strip_trailing_punct(segments)
    return cleaned or [original]


def _allocate_durations(fragments: list[str], total_duration: int) -> list[int]:
    if len(fragments) == 1:
        return [total_duration]
    weights = [max(1, len(f.replace(" ", ""))) for f in fragments]
    total_weight = sum(weights)
    durations: list[int] = []
    allocated = 0
    for i, weight in enumerate(weights[:-1]):
        share = round(total_duration * weight / total_weight)
        if total_duration >= SUBTITLE_DURATION_FLOOR_MS:
            ceiling = total_duration - allocated - SUBTITLE_TAIL_BUFFER_MS
            share = max(SUBTITLE_MIN_DURATION_MS, min(share, ceiling))
        else:
            share = max(int(SUBTITLE_MIN_DURATION_MS / 2), share)
        durations.append(share)
        allocated += share
    durations.append(max(SUBTITLE_TAIL_BUFFER_MS, total_duration - allocated))
    return durations


def _segment_times(item: dict) -> tuple[int, int]:
    start = int(item.get("actual_start_time", item["start_time"]))
    end = int(item.get("actual_end_time", item["end_time"]))
    return start, end


def _dst_lang(translation: list[dict]) -> str:
    for item in translation:
        lang = item.get("dst_lang")
        if lang:
            return lang
    return "zh"


def _tokens(text: str) -> tuple[list[str], bool]:
    """Words, or characters for CJK text (Latin words stay whole); True means CJK."""
    text = " ".join(text.split())
    letters = text.replace(" ", "")
    if not letters:
        return [], False
    if len(_CJK.findall(letters)) * 2 < len(letters):
        return text.split(" "), False
    return re.findall(r"[A-Za-z0-9]+(?:['’.-][A-Za-z0-9]+)*|\S", text), True


def _join_tokens(tokens: list[str], cjk: bool) -> str:
    if not cjk:
        return " ".join(tokens)
    text = ""
    for token in tokens:
        text = _join(text, token)
    return text


def _merge_to_count(fragments: list[str], count: int) -> list[str]:
    """Join the shortest neighbouring fragments until only count remain."""
    fragments = list(fragments)
    while len(fragments) > count:
        index = min(range(len(fragments) - 1), key=lambda i: len(fragments[i]) + len(fragments[i + 1]))
        fragments[index:index + 2] = [_join(fragments[index], fragments[index + 1])]
    return fragments


def _distribute(tokens: list[str], weights: list[int], cjk: bool) -> list[str]:
    """Cut tokens into len(weights) non-empty pieces sized like weights, preferring cuts after punctuation."""
    count = len(weights)
    ends = list(itertools.accumulate(len(token) for token in tokens))
    total, total_weight = ends[-1], sum(weights)
    slack = 0.3 * total / count
    bounds, done = [0], 0
    for index in range(1, count):
        done += weights[index - 1]
        target = total * done / total_weight
        cuts = range(bounds[-1] + 1, len(tokens) - (count - index) + 1)
        bounds.append(min(cuts, key=lambda cut: abs(ends[cut - 1] - target)
                          - (slack if tokens[cut - 1].endswith(_BREAK_AFTER) else 0)))
    bounds.append(len(tokens))
    # A cue does not end on a comma or full stop, matching split_subtitle_text.
    pieces = [_join_tokens(tokens[start:end], cjk) for start, end in zip(bounds, bounds[1:])]
    return [piece[:-1] if piece.endswith(("，", ",", "。")) and len(piece) > 1 else piece for piece in pieces]


def _shared_cues(fragments: list[str], source: str, start: int, end: int) -> list[tuple[int, int, str, str]]:
    """Cues for one segment. The translation decides where cues break; the original text is
    spread over the same cues by length, so its words need not line up exactly."""
    tokens, cjk = _tokens(source)
    if tokens and len(tokens) < len(fragments):
        fragments = _merge_to_count(fragments, len(tokens))
    durations = _allocate_durations(fragments, end - start)
    pieces = _distribute(tokens, durations, cjk) if tokens else [""] * len(fragments)
    cues = []
    cursor = start
    for fragment, duration, piece in zip(fragments, durations, pieces):
        cues.append((cursor, cursor + duration, fragment, piece))
        cursor += duration
    return cues


def _srt(entries: list[tuple[int, int, str]]) -> str:
    return "".join(f"{index}\n{_srt_time(start)} --> {_srt_time(end)}\n{text}\n\n"
                   for index, (start, end, text) in enumerate(entries, 1))


def write_subtitles(timeline_file: Path, session: Path, source_language: str | None = None) -> list[Path]:
    """Write metadata/subtitles.<lang>.srt for the translation and, when every segment has its
    original text, for the original language too. Both files share every cue's start and end.

    UTF-8 with a BOM and CRLF line ends is the SRT form that common video editors all read."""
    translation = json.loads(timeline_file.read_text(encoding="utf-8"))["translation"]
    target_language = _dst_lang(translation)
    source_language = next((item["src_lang"] for item in translation if item.get("src_lang")), source_language)
    cues: list[tuple[int, int, str, str]] = []
    has_source = True
    for item in translation:
        start, end = _segment_times(item)
        text = target_text(item)
        fragments = split_subtitle_text(text if isinstance(text, str) else "")
        if end <= start or not fragments:
            continue
        source = item.get("src") if isinstance(item.get("src"), str) else ""
        has_source = has_source and bool(source.strip())
        cues.extend(_shared_cues(fragments, source, start, end))

    files = {target_language: [(start, end, text) for start, end, text, _ in cues]}
    if cues and has_source and source_language and source_language != target_language:
        files[source_language] = [(start, end, piece) for start, end, _, piece in cues]
    metadata = session / "metadata"
    metadata.mkdir(parents=True, exist_ok=True)
    for stale in metadata.glob("subtitles.*.srt"):
        stale.unlink()
    written = []
    for language, entries in files.items():
        path = metadata / f"subtitles.{language}.srt"
        path.write_text(_srt(entries), encoding="utf-8-sig", newline="\r\n")
        written.append(path)
    return written
