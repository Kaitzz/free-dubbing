import io
import json
from pathlib import Path
import pytest
from backend.app.adapters.online_assets import caption_candidates, parse_captions, download_assets


def track(ext="json3", url="https://example.test/cc"):
    return {"ext": ext, "url": url}


def test_manual_original_language_precedes_automatic_and_excludes_translation():
    info={"language":"en", "subtitles":{"fr":[track()], "en":[track()]},
          "automatic_captions":{"en":[track(url="https://example.test/?tlang=en")], "en-orig":[track()]}}
    result=caption_candidates(info,"en")
    assert [(kind,lang) for kind,lang,_ in result]==[("manual","en"),("automatic","en-orig")]
    assert caption_candidates({**info,"language":"ja"},"en")==[]


def test_rolling_captions_dedupe_using_original_context_and_preserve_later_repetition():
    events=[{"tStartMs":start,"dDurationMs":duration,"segs":[{"utf8":text}]} for start,duration,text in
            [(0,2000,"hello"),(500,2000,"hello world"),(1000,2000,"hello world again"),
             (1500,1500,"hello world again"),(4000,1000,"hello world again")]]
    rows=parse_captions(json.dumps({"events":events}),"json3",True)
    assert [r[2] for r in rows]==["hello","world","again","hello world again"]
    assert all(a[1]<=b[0] for a,b in zip(rows,rows[1:]))


def test_manual_vtt_strips_markup_and_keeps_timestamps():
    rows=parse_captions("WEBVTT\n\n00:01.000 --> 00:02.500 align:start\n<c>Hello</c> &amp; welcome\n\n", "vtt")
    assert rows==[[1000,2500,"Hello & welcome"]]
    with pytest.raises(ValueError):parse_captions('{"events":[]}',"json3")


def test_optional_assets_failure_falls_back_and_cover_is_saved(tmp_path):
    (tmp_path/"metadata").mkdir();(tmp_path/"media").mkdir()
    class Ydl:
        def urlopen(self,url):
            if url.endswith("cover"):return io.BytesIO(b"\xff\xd8\xffimage")
            if url.endswith("manual"):raise RuntimeError("unavailable")
            return io.BytesIO(json.dumps({"events":[{"tStartMs":100,"dDurationMs":900,"segs":[{"utf8":"hello"}]}]}).encode())
    info={"thumbnail":"https://example.test/cover","subtitles":{"en":[track(url="https://example.test/manual")]},
          "automatic_captions":{"en-orig":[track()]}}
    download_assets(Ydl(),info,tmp_path,"en")
    assert (tmp_path/"media/thumbnail.jpg").is_file()
    payload=json.loads((tmp_path/"metadata/source_subtitles.json").read_text())
    assert payload['subtitle_source']['kind']=='automatic'
    assert "00:00:00,100 --> 00:00:01,000" in (tmp_path/"metadata/source_subtitles.srt").read_text()
    # Transport does not lose the new artifacts.
    from backend.app.remote_archive import pack,unpack
    pack(tmp_path/"out.zip",{"session":tmp_path/"metadata"})
    unpack(tmp_path/"out.zip",tmp_path/"received")
    assert (tmp_path/"received/session/source_subtitles.srt").is_file()


def test_unavailable_assets_do_not_fail_video_task(tmp_path):
    (tmp_path/"metadata").mkdir();(tmp_path/"media").mkdir()
    class Ydl:
        def urlopen(self,url):raise RuntimeError("offline")
    download_assets(Ydl(),{"subtitles":{"en":[track()]}},tmp_path,"en")
    assert not (tmp_path/"metadata/source_subtitles.json").exists()


def test_cc_pipeline_skips_asr_but_calls_translation(monkeypatch,tmp_path):
    from backend.app.pipeline import PipelineRunner
    from backend.app import database
    from backend.app.adapters import openai_translate
    (tmp_path/"metadata").mkdir()
    payload={"subtitle_source":{"kind":"manual","language":"en"},"result":{"utterances":[
        {"text":"hello","start_time":0,"end_time":1000,"words":[],"additions":{"speaker":"1"}}]}}
    (tmp_path/"metadata/source_subtitles.json").write_text(json.dumps(payload))
    runner=PipelineRunner("test");runner.artifacts.session=tmp_path
    monkeypatch.setattr(runner,"stage_message",lambda *a:None)
    task={"url":"https://www.youtube.com/watch?v=abcdefghijk"}
    runner._asr(task);runner._asr_fix(task)
    calls=[]
    def translate(path,session,settings,source):
        calls.append(json.loads(path.read_text()))
        result=session/"metadata/translation.zh.json"
        result.write_text('{"translation":[]}')
        return result
    monkeypatch.setattr(openai_translate,"translate_asr",translate)
    monkeypatch.setattr(database,"get_openai_settings",lambda:{"model":"test","base_url":"https://example.test"})
    runner._translate(task)
    assert calls==[payload]
