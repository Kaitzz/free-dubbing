"""Resegment automatic JSON3 captions without inventing word end times."""
from __future__ import annotations
import copy
import json
import math
import re
from pathlib import Path
from .online_assets import _text


def timed_fragments(content: str) -> list[dict]:
    fragments = []
    for event in json.loads(content).get("events", []):
        start = event.get("tStartMs")
        duration = event.get("dDurationMs")
        if not isinstance(start, (int, float)) or not isinstance(duration, (int, float)):
            continue
        for index, part in enumerate(event.get("segs", [])):
            text = _text(part.get("utf8", ""))
            if not text:
                continue
            offset = part.get("tOffsetMs", 0 if index == 0 else None)
            onset = round(start + offset) if isinstance(offset, (int, float)) else None
            fragments.append({"text": text, "start_time": onset, "end_time": None,
                              "display_start_time": round(start),
                              "display_end_time": round(start + duration)})
    return fragments


def regroup(payload: dict, raw_path: Path, audio_duration_ms: int | None = None) -> dict:
    fragments = timed_fragments(raw_path.read_text(encoding="utf-8-sig"))
    original = payload["result"]["utterances"]
    # A fallback encoding or unexpected JSON3 representation must not lose text.
    expected = " ".join(u["text"] for u in original).split()
    if not fragments or " ".join(w["text"] for w in fragments).split() != expected:
        return payload
    if any(w["start_time"] is None for w in fragments):
        return payload
    if any(a["start_time"] >= b["start_time"] for a, b in zip(fragments, fragments[1:])):
        return payload
    n = len(fragments)
    costs, previous = [math.inf] * (n+1), [None] * (n+1)
    costs[0] = 0
    bad_tail = {"a", "an", "the", "of", "to", "with", "from", "for", "and", "or", "my", "your", "our", "their", "going", "behalf"}
    for end in range(1, n+1):
        for start in range(end-1, max(-1, end-36), -1):
            group = fragments[start:end]
            text = " ".join(w["text"] for w in group)
            span = (group[-1]["start_time"]-group[0]["start_time"])/1000
            if len(group)>1 and (span>7 or len(text)>135):
                break
            # Widely separated onsets are a grouping boundary, NOT a VAD silence claim.
            if any(b["start_time"]-a["start_time"]>1500 for a,b in zip(group,group[1:])):
                continue
            score = 1 + ((len(text)-70)/55)**2
            if len(text)<20: score += 3
            if re.search(r"[.!?]$", text): score -= 1.5
            elif re.search(r"[,;:]$", text): score -= 2.0
            if re.sub(r"[^a-z]", "", group[-1]["text"].lower()) in bad_tail: score += 3
            if end<n and fragments[end]["text"].lower().strip(".,!?") in {"of", "to"}:
                score += 2
            # Prefer ending at sentence punctuation rather than swallowing it.
            score += sum(2 for w in group[:-1] if re.search(r"[.!?]$", w["text"]))
            if costs[start]+score < costs[end]:
                costs[end],previous[end] = costs[start]+score,start
    if previous[n] is None:
        return payload
    ranges=[]
    end=n
    while end:
        start=previous[end]
        ranges.append((start,end));end=start
    utterances=[]
    for start,end in reversed(ranges):
        group=fragments[start:end]
        # This is a display/scheduling bound, explicitly not a speech or word end.
        upper=group[-1]["display_end_time"]
        if end<n: upper=min(upper,fragments[end]["start_time"])
        if audio_duration_ms is not None: upper=min(upper,audio_duration_ms)
        if upper<=group[-1]["start_time"]:
            return payload
        utterances.append({"start_time":group[0]["start_time"],"end_time":upper,
            "end_time_kind":"display_upper_bound", "text":" ".join(w["text"] for w in group),
            "words":copy.deepcopy(group),"additions":{"speaker":"1"}})
    result=copy.deepcopy(payload)
    result["result"]["utterances"]=utterances
    result["result"]["text"]=" ".join(u["text"] for u in utterances)
    result["subtitle_source"]["segmentation"]="word_onsets_v1"
    result["subtitle_source"]["word_end_times_known"]=False
    return result
