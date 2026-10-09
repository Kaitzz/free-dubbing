import json
from unittest.mock import MagicMock
import numpy as np
import pytest
import soundfile as sf
from backend.app.adapters import voxcpm as v


def tone(amplitude=0.1, seconds=2, rate=16000):
    return amplitude * np.sin(np.arange(int(rate*seconds))*2*np.pi*220/rate).astype(np.float32)


def test_loudness_matching_is_bounded_silence_aware_and_peak_limited():
    quiet=tone(0.03); loud=tone(0.4)
    q=v.match_loudness(quiet,16000); l=v.match_loudness(loud,16000)
    assert len(q)==len(quiet)
    assert np.sqrt(np.mean(q*q))>np.sqrt(np.mean(quiet*quiet))
    assert np.sqrt(np.mean(l*l))<np.sqrt(np.mean(loud*loud))
    assert np.max(np.abs(q))<=np.max(np.abs(quiet))*10**(4/20)+1e-7
    assert np.max(np.abs(v.match_loudness(tone(1),16000)))<=10**(-1/20)+1e-7
    assert not v.match_loudness(np.zeros(16000),16000).any()
    with pytest.raises(ValueError):v.match_loudness(np.array([np.nan]),16000)
    padded=np.concatenate([np.zeros(16000),quiet,np.zeros(16000)])
    assert np.allclose(v.match_loudness(padded,16000)[16000:-16000],q,atol=1e-5)


def test_fixed_reference_favors_longer_steady_speech_and_bounds_length(tmp_path):
    vocals=tmp_path/'vocals';vocals.mkdir()
    sf.write(vocals/'0001.wav',tone(0.1,0.5),16000)
    sf.write(vocals/'0002.wav',tone(0.1,8),16000)
    sf.write(vocals/'0003.wav',np.zeros(16000*6),16000)
    refs=v._fixed_references(vocals,[{'dst':'a'},{'dst':'b'},{'dst':'c'}],tmp_path)
    audio,rate=sf.read(refs['1'])
    assert len(audio)/rate==6
    with pytest.raises(ValueError,match='No usable'):
        v._fixed_references(vocals,[{'dst':'a','speaker':'A'},{'dst':'b','speaker':'A'},{'dst':'c','speaker':'B'}],tmp_path)


def test_fixed_caches_once_per_speaker_and_uses_eight_steps(monkeypatch,tmp_path):
    for key in ('VOXCPM_REFERENCE_MODE','VOXCPM_INFERENCE_TIMESTEPS','VOXCPM_MATCH_LOUDNESS'):
        monkeypatch.delenv(key,raising=False)
    vocals=tmp_path/'vocals';vocals.mkdir()
    for i in range(1,5):sf.write(vocals/f'{i:04d}.wav',tone(),16000)
    items=[{'dst':'one','speaker':'A'},{'dst':'two','speaker':'B'},
           {'dst':'three','speaker':'A'}, {'dst':'sound','audio_mode':'original','start_time':0,'end_time':1000}]
    translation=tmp_path/'translation.json';translation.write_text(json.dumps({'translation':items}))
    original=tmp_path/'original.wav';sf.write(original,tone(0.2),16000)
    model=MagicMock();model.tts_model.sample_rate=16000
    model.tts_model.build_prompt_cache.side_effect=[{'voice':'A'},{'voice':'B'}]
    tensor=MagicMock();tensor.squeeze.return_value.cpu.return_value.numpy.return_value=tone(0.03)
    model.tts_model.generate_with_prompt_cache.return_value=(tensor,None,None)
    loader=MagicMock(return_value=model);monkeypatch.setattr(v,'_load_model',loader)
    output=v.generate_tts(translation,vocals,tmp_path,original_vocals_file=original)
    assert model.tts_model.build_prompt_cache.call_count==2
    calls=model.tts_model.generate_with_prompt_cache.call_args_list
    assert [c.kwargs['prompt_cache']['voice'] for c in calls]==['A','B','A']
    assert all(c.kwargs['inference_timesteps']==8 for c in calls)
    model.generate.assert_not_called()
    copied,_=sf.read(output/'0004.wav');source,_=sf.read(original)
    assert np.array_equal(copied,source[:16000])
    # Completed cached work does not reload the model or rerun normalization.
    before=(output/'0001.wav').read_bytes()
    v.generate_tts(translation,vocals,tmp_path,original_vocals_file=original)
    assert loader.call_count==1
    assert (output/'0001.wav').read_bytes()==before
