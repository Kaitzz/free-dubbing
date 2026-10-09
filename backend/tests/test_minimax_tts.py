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
