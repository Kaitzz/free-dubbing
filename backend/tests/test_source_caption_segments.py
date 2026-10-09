import json
from backend.app.adapters.online_assets import parse_captions
from backend.app.adapters.source_caption_segments import timed_fragments, regroup


def fixture(tmp_path, events):
    content=json.dumps({"events":events})
    path=tmp_path / "automatic.en.json3"
    path.write_text(content,encoding="utf-8")
    rows=parse_captions(content,"json3",True)
    payload={"subtitle_source":{"kind":"automatic","language":"en"},
             "result":{"utterances":[{"start_time":a,"end_time":b,"text":t,"words":[]}
                                       for a,b,t in rows]}}
    return payload,path


def test_onsets_not_invented_ends_and_repeated_words_preserved(tmp_path):
    payload,path=fixture(tmp_path,[{"tStartMs":1000,"dDurationMs":5000,"segs":[
        {"utf8":"I"},{"utf8":" I", "tOffsetMs":350},
        {"utf8":" really", "tOffsetMs":550},{"utf8":" agree.","tOffsetMs":1000}]}])
    result=regroup(payload,path,2600)
    words=[w for u in result["result"]["utterances"] for w in u["words"]]
    assert [w["start_time"] for w in words]==[1000,1350,1550,2000]
    assert all(w["end_time"] is None for w in words)
    assert result["result"]["text"]=="I I really agree."
    assert result["result"]["utterances"][-1]["end_time"]==2600
    assert payload["result"]["utterances"][0]["words"]==[]


def test_missing_offsets_are_unknown_and_fall_back(tmp_path):
    payload,path=fixture(tmp_path,[{"tStartMs":1000,"dDurationMs":1000,
        "segs":[{"utf8":"Hello"},{"utf8":" world"}]}])
    words=timed_fragments(path.read_text())
    assert words[1]["start_time"] is None
    assert regroup(payload,path) is payload


def test_wide_onset_gap_does_not_imply_word_end(tmp_path):
    payload,path=fixture(tmp_path,[{"tStartMs":0,"dDurationMs":7000,
        "segs":[{"utf8":"Hello."},{"utf8":" Welcome.","tOffsetMs":4000}]}])
    result=regroup(payload,path)
    rows=result["result"]["utterances"]
    assert len(rows)==2
    assert rows[0]["end_time"]==4000
    assert rows[0]["end_time_kind"]=="display_upper_bound"
    assert rows[0]["words"][0]["end_time"] is None


def test_incompatible_raw_track_does_not_replace_text(tmp_path):
    payload,path=fixture(tmp_path,[{"tStartMs":0,"dDurationMs":1000,"segs":[{"utf8":"Hello"}]}])
    payload["result"]["utterances"][0]["text"]="different caption"
    assert regroup(payload,path) is payload


def test_regroup_crosses_screen_cue_boundaries(tmp_path):
    payload,path=fixture(tmp_path,[
        {"tStartMs":1000,"dDurationMs":1200,"segs":[{"utf8":"We"},{"utf8":" want", "tOffsetMs":200}]},
        {"tStartMs":1600,"dDurationMs":1800,"segs":[{"utf8":"to"},{"utf8":" explain.","tOffsetMs":400}]},
    ])
    result=regroup(payload,path)
    assert len(result["result"]["utterances"])==1
    assert result["result"]["text"]=="We want to explain."
