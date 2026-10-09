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
    from backend.app.adapters.ffmpeg import _srt_time
    vtt="WEBVTT\n\n"+"\n\n".join(f"{_srt_time(e['tStartMs'])} --> {_srt_time(e['tStartMs']+e['dDurationMs'])}\n{e['segs'][0]['utf8']}" for e in events)
    rows=parse_captions(vtt,"vtt",True)
    assert [r[2] for r in rows]==["hello","world","again","hello world again"]
    assert all(a[1]<=b[0] for a,b in zip(rows,rows[1:]))


def test_manual_vtt_strips_markup_and_keeps_timestamps():
    rows=parse_captions("WEBVTT\n\n00:01.000 --> 00:02.500 align:start\n<c>Hello</c> &amp; welcome\n\n", "vtt")
    assert rows==[[1000,2500,"Hello & welcome"]]
    with pytest.raises(ValueError):parse_captions('{"events":[]}',"json3")


def test_optional_assets_failure_falls_back_and_cover_is_saved(tmp_path, monkeypatch):
    from backend.app.adapters import online_assets
    monkeypatch.setattr(online_assets, '_image_size', lambda data:(1920,1080))
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


def test_json3_preserves_real_repetition_and_rejects_partial_transcripts():
    events=[{"tStartMs":0,"dDurationMs":2000,"segs":[{"utf8":"no no"}]},
            {"tStartMs":1000,"dDurationMs":2000,"aAppend":1,"segs":[{"utf8":"no thanks"}]}]
    assert [r[2] for r in parse_captions(json.dumps({"events":events}),"json3",True)]==["no no","no thanks"]
    events.append({"tStartMs":2500,"aAppend":1,"segs":[{"utf8":"missing duration"}]})
    with pytest.raises(ValueError,match="reliable timing"):
        parse_captions(json.dumps({"events":events}),"json3",True)
    events[-1]['segs']=[{'utf8':'\n'}]
    assert len(parse_captions(json.dumps({"events":events}),"json3",True))==2


def test_bad_json3_tries_vtt_for_same_manual_track(tmp_path):
    (tmp_path/'metadata').mkdir();(tmp_path/'media').mkdir()
    class Ydl:
        def urlopen(self,url):
            if url.endswith('json3'):
                return io.BytesIO(b'{"events":[{"tStartMs":0,"segs":[{"utf8":"hello"}]}]}')
            return io.BytesIO(b'WEBVTT\n\n00:00.000 --> 00:01.000\nhello\n')
    download_assets(Ydl(),{'subtitles':{'en':[track(url='https://example.test/json3'),track('vtt','https://example.test/vtt')]}},tmp_path,'en')
    assert json.loads((tmp_path/'metadata/source_subtitles.json').read_text())['subtitle_source']['kind']=='manual'
    assert (tmp_path/'metadata/raw_subtitles/manual.en.json3').is_file()
    assert (tmp_path/'metadata/raw_subtitles/manual.en.vtt').is_file()


def test_captions_real_translation_artifact_and_audio_slice_contract(monkeypatch,tmp_path):
    from backend.app.adapters import openai_translate as tr, audio
    from backend.app.adapters.ffmpeg import write_srt
    from backend.app.sources import detect_source
    from pydub import AudioSegment
    (tmp_path/'metadata').mkdir();(tmp_path/'media').mkdir()
    class Ydl:
        def urlopen(self,url):return io.BytesIO(b'WEBVTT\n\n00:00.500 --> 00:01.500\nhello\n')
    download_assets(Ydl(),{'subtitles':{'en':[track('vtt')]}},tmp_path,'en')
    monkeypatch.setattr(tr,'preprocess',lambda *a,**k:tr.PreprocessResponse())
    monkeypatch.setattr(tr,'translate_batch',lambda texts,*a,**k:[tr.TranslationItem(dst='你好',audio_mode='tts') for _ in texts])
    output=tr.translate_asr(tmp_path/'metadata/source_subtitles.json',tmp_path,{},detect_source('https://www.youtube.com/watch?v=abcdefghijk'))
    item=json.loads(output.read_text(encoding='utf-8'))['translation'][0]
    assert (item['src'],item['start_time'],item['end_time'],item['speaker'])==('hello',500,1500,'1')
    vocals=tmp_path/'media/vocals.wav'
    AudioSegment.silent(duration=2500).export(vocals,format='wav')
    clips=audio.split_audio_by_translation(vocals,output,tmp_path)
    assert len(AudioSegment.from_wav(clips/'0001.wav'))==1240
    assert '00:00:00,500 --> 00:00:01,500' in write_srt(output,tmp_path).read_text(encoding='utf-8')


def test_partial_vtt_is_rejected_instead_of_losing_a_cue():
    with pytest.raises(ValueError):
        parse_captions('WEBVTT\n\n00:00.000 --> 00:01.000\nhello\n\nBAD --> 00:02.000\nworld\n','vtt')


def test_cover_prefers_hd_from_unsorted_raw_metadata_and_upgrades_old(tmp_path,monkeypatch):
    from backend.app.adapters import online_assets as a
    media=tmp_path/'media';media.mkdir()
    low=b'\xff\xd8\xfflow'; high=b'\xff\xd8\xffhigh'
    (media/'thumbnail.jpg').write_bytes(low)
    monkeypatch.setattr(a,'_image_size',lambda data:(120,90) if data==low else (1920,1080))
    requested=[]
    class Ydl:
        def urlopen(self,url):
            requested.append(url)
            return io.BytesIO(high if 'maxresdefault' in url else low)
    info={'thumbnail':'https://example.test/maxresdefault.jpg','thumbnails':[
        {'url':'https://example.test/maxresdefault.jpg','width':1920,'height':1080},
        {'url':'https://example.test/3.jpg'}]}
    a.download_cover(Ydl(),info,tmp_path)
    assert requested==['https://example.test/maxresdefault.jpg']
    assert (media/'thumbnail.jpg').read_bytes()==high


def test_cover_rejects_low_res_placeholder_as_hd_and_tries_next(tmp_path,monkeypatch):
    from backend.app.adapters import online_assets as a
    (tmp_path/'media').mkdir()
    low=b'\xff\xd8\xfflow';high=b'\xff\xd8\xffhigh'
    monkeypatch.setattr(a,'_image_size',lambda data:(120,90) if data==low else (1280,720))
    class Ydl:
        def urlopen(self,url):return io.BytesIO(low if 'maxres' in url else high)
    a.download_cover(Ydl(),{'thumbnails':[{'url':'https://example.test/maxresdefault.jpg'},
                                        {'url':'https://example.test/hq720.jpg'}]},tmp_path)
    assert (tmp_path/'media/thumbnail.jpg').read_bytes()==high
