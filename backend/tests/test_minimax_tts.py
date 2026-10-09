import io
import json
from types import SimpleNamespace
import numpy as np
import pytest
import soundfile as sf
from backend.app.adapters import minimax_tts as m


@pytest.fixture
def setup(tmp_path,monkeypatch):
    monkeypatch.delenv("MINIMAX_API_KEY",raising=False)
    monkeypatch.setattr(m.database,"get_openai_settings",lambda:{"api_key":"test","base_url":"https://api.minimaxi.com/v1"})
    translation=tmp_path/"translation.json"
    translation.write_text(json.dumps({"translation":[{"dst":"你好","dst_lang":"zh","start_time":0,"end_time":1000}]}))
    calls=[]
    buf=io.BytesIO();sf.write(buf,np.zeros(3200),32000,format="WAV")
    def post(*args,**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(status_code=200,json=lambda:{"base_resp":{"status_code":0},"data":{"audio":buf.getvalue().hex()}})
    monkeypatch.setattr(m.requests,"post",post)
    return tmp_path,translation,calls


def test_default_voice_and_resume_cache(setup,monkeypatch):
    root,translation,calls=setup
    folder=m.generate_tts(translation,root,root)
    assert calls[0]["json"]["voice_setting"]["voice_id"]=="Chinese_casual_instructor_nv1"
    assert calls[0]["json"]["model"]=="speech-2.8-turbo"
    assert sf.info(folder/"0001.wav").frames==3200
    m.generate_tts(translation,root,root)
    assert len(calls)==1
    monkeypatch.setenv("MINIMAX_TTS_VOICE","Chinese (Mandarin)_Sincere_Adult")
    m.generate_tts(translation,root,root)
    assert len(calls)==2
    translation.write_text(json.dumps({"translation":[{"dst":"再见","start_time":0,"end_time":1000}]}))
    m.generate_tts(translation,root,root)
    assert len(calls)==3


def test_api_error_is_not_cached(setup,monkeypatch):
    root,translation,calls=setup
    monkeypatch.setattr(m.requests,"post",lambda *a,**k:SimpleNamespace(status_code=200,json=lambda:{"base_resp":{"status_code":1008}}))
    with pytest.raises(RuntimeError,match="1008"):
        m.generate_tts(translation,root,root)
    assert not (root/"segments/tts/0001.wav").exists()


def test_unknown_emotion_uses_auto(setup):
    root,translation,calls=setup
    translation.write_text(json.dumps({"translation":[{"dst":"你好","tts_emotion":"invalid"}]}))
    m.generate_tts(translation,root,root)
    assert "emotion" not in calls[0]["json"]["voice_setting"]


def test_legacy_unmarked_wav_is_not_reused(setup):
    root,translation,calls=setup
    folder=root/"segments/tts";folder.mkdir(parents=True)
    sf.write(folder/"0001.wav",np.zeros(1600),16000)
    m.generate_tts(translation,root,root)
    assert len(calls)==1


@pytest.mark.parametrize("code",[1002,1039,1001])
def test_business_transient_error_retries_and_caches(setup,monkeypatch,code):
    root,translation,calls=setup
    clock=[0.0]
    gate=m.RequestGate(20,clock=lambda:clock[0],sleep=lambda seconds:clock.__setitem__(0,clock[0]+seconds))
    monkeypatch.setattr(m,"RequestGate",lambda rpm:gate)
    original=m.requests.post
    attempts=[]
    def post(*a,**k):
        attempts.append(clock[0])
        if len(attempts)==1:
            return SimpleNamespace(status_code=200,json=lambda:{"base_resp":{"status_code":code}})
        return original(*a,**k)
    monkeypatch.setattr(m.requests,"post",post)
    out=m.generate_tts(translation,root,root)
    assert len(attempts)==2
    assert attempts[1]>= (60 if code in {1002,1039} else 3)
    assert (out/"0001.minimax.json").is_file()


def test_shared_gate_honors_cooldown_for_next_request():
    clock=[0.0]
    gate=m.RequestGate(20,clock=lambda:clock[0],sleep=lambda seconds:clock.__setitem__(0,clock[0]+seconds))
    stop=m.threading.Event()
    gate.acquire(stop)
    gate.acquire(stop)
    assert clock[0]==3
    gate.cooldown(60)
    gate.acquire(stop)
    assert clock[0]==63
    gate.acquire(stop)
    assert clock[0]==66


def test_permanent_error_stops_queued_segments_and_preserves_success(setup,monkeypatch):
    root,translation,calls=setup
    monkeypatch.setenv("MINIMAX_TTS_CONCURRENCY","1")
    clock=[0.0]
    gate=m.RequestGate(20,clock=lambda:clock[0],sleep=lambda seconds:clock.__setitem__(0,clock[0]+seconds))
    monkeypatch.setattr(m,"RequestGate",lambda rpm:gate)
    translation.write_text(json.dumps({"translation":[{"dst":str(i)} for i in range(10)]}))
    original=m.requests.post
    attempted=[]
    def post(*a,**k):
        attempted.append(k["json"]["text"])
        if len(attempted)==2:return SimpleNamespace(status_code=200,json=lambda:{"base_resp":{"status_code":1004}})
        return original(*a,**k)
    monkeypatch.setattr(m.requests,"post",post)
    with pytest.raises(RuntimeError,match="1004"):
        m.generate_tts(translation,root,root)
    assert attempted==["0","1"]
    assert (root/"segments/tts/0001.minimax.json").is_file()
    assert not (root/"segments/tts/0002.wav").exists()
    monkeypatch.setattr(m.requests,"post",original)
    m.generate_tts(translation,root,root)
    assert len(calls)==10 # One initial success plus nine remaining; first clip cached.


def test_http_retry_after_delay():
    response=SimpleNamespace(headers={"Retry-After":"90"})
    assert m._retry_delay(response,0,True)==90



def test_persistent_rate_limit_has_bounded_retries(setup,monkeypatch):
    root,translation,calls=setup
    clock=[0.0]
    gate=m.RequestGate(20,clock=lambda:clock[0],sleep=lambda seconds:clock.__setitem__(0,clock[0]+seconds))
    monkeypatch.setattr(m,"RequestGate",lambda rpm:gate)
    attempted=[]
    def post(*a,**k):
        attempted.append(clock[0])
        return SimpleNamespace(status_code=200,json=lambda:{"base_resp":{"status_code":1002}})
    monkeypatch.setattr(m.requests,"post",post)
    with pytest.raises(RuntimeError,match="1002"):
        m.generate_tts(translation,root,root)
    assert len(attempted)==6
    assert not (root/"segments/tts/0001.wav").exists()
