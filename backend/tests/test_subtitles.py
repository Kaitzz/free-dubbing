from __future__ import annotations

import json
from pathlib import Path

from backend.app.adapters import subtitles


def write_timeline(session: Path, items: list[dict]) -> Path:
    metadata = session / "metadata"
    metadata.mkdir(parents=True, exist_ok=True)
    path = metadata / "timings.json"
    path.write_text(json.dumps({"translation": items}, ensure_ascii=False), encoding="utf-8")
    return path


def read_cues(path: Path) -> list[tuple[str, str]]:
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    assert "\n" not in text.replace("\r\n", "")
    blocks = [block.split("\r\n") for block in text.strip().split("\r\n\r\n")]
    assert [lines[0] for lines in blocks] == [str(index) for index in range(1, len(blocks) + 1)]
    return [(lines[1], " ".join(lines[2:])) for lines in blocks]


def test_translation_and_original_share_every_cue_time(tmp_path):
    source = ("We are going to talk about the edge of the universe, which is a mysterious topic; "
              "but don't worry, I will explain it in detail.")
    timeline = write_timeline(tmp_path, [{
        "src": source, "dst": "我们今天讨论宇宙的边界，那是一个神秘话题；不过别担心，我会详细解释",
        "src_lang": "en", "dst_lang": "zh", "start_time": 0, "end_time": 6000,
        "actual_start_time": 500, "actual_end_time": 6500,
    }])

    written = subtitles.write_subtitles(timeline, tmp_path)

    assert [path.name for path in written] == ["subtitles.zh.srt", "subtitles.en.srt"]
    chinese, english = (read_cues(path) for path in written)
    assert [time for time, _ in chinese] == [time for time, _ in english]
    assert chinese[0][0].startswith("00:00:00,500 --> ") and chinese[-1][0].endswith(" --> 00:00:06,500")
    assert [text for _, text in chinese] == ["我们今天讨论宇宙的边界", "那是一个神秘话题", "不过别担心", "我会详细解释"]
    assert [text for _, text in english] == [
        "We are going to talk about the edge of the universe",
        "which is a mysterious topic;",
        "but don't worry",
        "I will explain it in detail.",
    ]


def test_without_original_text_only_the_translation_is_written(tmp_path):
    (tmp_path / "metadata").mkdir()
    stale = tmp_path / "metadata" / "subtitles.en.srt"
    stale.write_text("old", encoding="utf-8")
    timeline = write_timeline(tmp_path, [{"src": "", "dst": "你好", "src_lang": "en", "dst_lang": "zh",
                                          "start_time": 0, "end_time": 1000}])

    written = subtitles.write_subtitles(timeline, tmp_path)

    assert [path.name for path in written] == ["subtitles.zh.srt"]
    assert not stale.exists()
    assert read_cues(written[0]) == [("00:00:00,000 --> 00:00:01,000", "你好")]


def test_chinese_original_is_spread_over_english_cues(tmp_path):
    timeline = write_timeline(tmp_path, [{
        "src": "我们今天讨论宇宙的边界，那是一个神秘话题", "src_lang": "zh", "dst_lang": "en",
        "dst": "Today we discuss the edge of the universe, which is a mysterious topic",
        "start_time": 0, "end_time": 4000,
    }])

    english, chinese = (read_cues(path) for path in subtitles.write_subtitles(timeline, tmp_path))

    assert [time for time, _ in english] == [time for time, _ in chinese]
    assert [text for _, text in english] == ["Today we discuss the edge of the universe", "which is a mysterious topic"]
    assert [text for _, text in chinese] == ["我们今天讨论宇宙的边界", "那是一个神秘话题"]


def test_short_original_merges_translation_cues_so_both_files_match(tmp_path):
    timeline = write_timeline(tmp_path, [{
        "src": "Yes indeed", "dst": "第一部分很长很长，第二部分也很长，第三部分同样很长",
        "src_lang": "en", "dst_lang": "zh", "start_time": 0, "end_time": 3000,
    }])

    chinese, english = (read_cues(path) for path in subtitles.write_subtitles(timeline, tmp_path))

    assert len(chinese) == len(english) == 2
    assert [time for time, _ in chinese] == [time for time, _ in english]
    assert [text for _, text in english] == ["Yes", "indeed"]


def test_default_source_language_applies_when_items_omit_it(tmp_path):
    timeline = write_timeline(tmp_path, [{"src": "Hello there", "dst": "你好", "start_time": 0, "end_time": 900}])

    written = subtitles.write_subtitles(timeline, tmp_path, "en")

    assert [path.name for path in written] == ["subtitles.zh.srt", "subtitles.en.srt"]


def test_original_audio_caption_is_kept(tmp_path):
    timeline = write_timeline(tmp_path, [{"start_time": 0, "end_time": 800, "dst": "（笑声）",
                                          "dst_lang": "zh", "audio_mode": "original"}])

    [chinese] = subtitles.write_subtitles(timeline, tmp_path)

    assert read_cues(chinese) == [("00:00:00,000 --> 00:00:00,800", "（笑声）")]


def test_long_sentence_splits_into_multiple_entries(tmp_path):
    timeline = write_timeline(tmp_path, [{
        "start_time": 0, "end_time": 6000, "actual_start_time": 0, "actual_end_time": 6000,
        "zh": "我们今天讨论宇宙的边界，那是一个神秘话题；不过别担心，我会详细解释",
    }])

    [chinese] = subtitles.write_subtitles(timeline, tmp_path)

    assert len(read_cues(chinese)) >= 3


def test_split_subtitle_text_breaks_on_punctuation_and_keeps_protected():
    out = subtitles.split_subtitle_text("我们今天讨论一下宇宙的边界，那是一个神秘话题；不过别担心，我会详细解释。")
    assert len(out) >= 3
    assert all(len(s) >= 2 for s in out)
    protected = subtitles.split_subtitle_text("他说《三体，黑暗森林》是经典，必读。")
    assert any("《三体，黑暗森林》" in s for s in protected)


def test_merging_short_english_fragments_keeps_the_space():
    assert subtitles.split_subtitle_text("Yes, I know that") == ["Yes I know that"]
