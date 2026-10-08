"""Run just STT (no translation API, video downloader, or voice cloning)."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def srt_time(ms: int) -> str:
    seconds, millis = divmod(ms, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path, help="Audio/video, or original asr.json with --resegment")
    parser.add_argument("--resegment", action="store_true", help="Regroup existing word timestamps without model inference")
    parser.add_argument("--language", choices=["en", "zh", "ja", "ko", "yue"], default=None)
    parser.add_argument("--device", default="auto")
    parser.add_argument("--output", type=Path, required=True, help="A new output directory")
    args = parser.parse_args()
    if not args.audio.is_file():
        parser.error("Input audio/video does not exist")
    if args.output.exists():
        parser.error("Use a new output directory to avoid reusing stale artifacts")
    os.environ["FUNASR_DEVICE"] = args.device
    from backend.app.adapters.sensevoice_asr import recognize_speech, release_model
    from backend.app.adapters.asr_sentence_fixer import fix_asr_sentences
    try:
        if args.resegment:
            from backend.app.adapters.sensevoice_asr import _utterances, _number
            data = json.loads(args.audio.read_text(encoding="utf-8"))
            language = args.language or data.get("language", "en")
            if language not in {"en", "zh", "ja", "ko", "yue"}:
                parser.error("Specify --language for this transcript")
            words = []
            previous_end = 0
            duration = _number(data["audio_info"]["duration"])
            for cue in data["result"]["utterances"]:
                if not cue.get("words"):
                    parser.error("Original asr.json with word timestamps is required; asr_fixed.json and SRT are not supported")
                for word in cue["words"]:
                    start, end = _number(word["start_time"]), _number(word["end_time"])
                    if (not isinstance(word.get("text"), str) or not word["text"].strip()
                            or start < previous_end or end <= start or end > duration):
                        parser.error("Invalid word alignment in input JSON")
                    previous_end = end
                    words.append({**word, "start_time": round(start), "end_time": round(end)})
            if not words:
                parser.error("No aligned words in input JSON")
            utterances = _utterances(words, language)
            data["language"] = language
            data["result"] = {**data["result"], "text": " ".join(u["text"] for u in utterances), "utterances": utterances}
            path = args.output / "metadata" / "asr.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            language = args.language or "en"
            path = recognize_speech(args.audio, args.output, language)
        fixed = fix_asr_sentences(path, args.output, language=language)
        data = json.loads(fixed.read_text(encoding="utf-8"))
        cues = [f"{i}\n{srt_time(u['start_time'])} --> {srt_time(u['end_time'])}\n{u['text']}\n"
                for i, u in enumerate(data["result"]["utterances"], 1)]
        (args.output / "subtitles.srt").write_text("\n".join(cues), encoding="utf-8")
        print(f"STT complete: {len(cues)} cues in {args.output}")
    finally:
        release_model()


if __name__ == "__main__":
    main()
