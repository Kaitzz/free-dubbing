import sys
from types import SimpleNamespace
import pytest
from backend.app.adapters import voxcpm as adapter


@pytest.mark.parametrize('fail', [False, True])
def test_low_memory_init_restores_default_dtype(monkeypatch, tmp_path, fail):
    dtype = ['float32']
    torch = SimpleNamespace(float16='float16', get_default_dtype=lambda:dtype[0],
        set_default_dtype=lambda value:dtype.__setitem__(0,value))
    def load(path, **kwargs):
        assert dtype[0] == 'float16'
        assert kwargs['optimize'] is False
        assert kwargs['load_denoiser'] is False
        if fail:raise RuntimeError('load failed')
        return 'loaded'
    monkeypatch.setitem(sys.modules, 'torch', torch)
    monkeypatch.setitem(sys.modules, 'voxcpm', SimpleNamespace(VoxCPM=SimpleNamespace(from_pretrained=load)))
    monkeypatch.setattr(adapter, '_MODEL', None)
    monkeypatch.setattr(adapter, '_model_path', lambda: tmp_path)
    monkeypatch.setenv('VOXCPM_LOW_MEMORY_INIT','true')
    monkeypatch.setenv('VOXCPM_OPTIMIZE','false')
    monkeypatch.setenv('VOXCPM_LOAD_DENOISER','false')
    if fail:
        with pytest.raises(RuntimeError):adapter._load_model()
        assert adapter._MODEL is None
    else:
        assert adapter._load_model() == 'loaded'
    assert dtype[0] == 'float32'


def test_model_download_uses_hugging_face_and_separate_cache(monkeypatch, tmp_path, capsys):
    calls = []
    def download(**kwargs):
        calls.append(kwargs)
        return kwargs['local_dir']
    monkeypatch.delenv('VOXCPM_MODEL_DIR', raising=False)
    monkeypatch.delenv('VOXCPM_MODEL', raising=False)
    monkeypatch.setenv('HF_TOKEN', 'test-private-token')
    monkeypatch.setattr(adapter, 'MODEL_CACHE_DIR', tmp_path)
    monkeypatch.setitem(sys.modules, 'huggingface_hub', SimpleNamespace(snapshot_download=download))
    assert adapter._model_path() == tmp_path/'huggingface/OpenBMB__VoxCPM2'
    assert calls == [dict(repo_id='OpenBMB/VoxCPM2',
        local_dir=str(tmp_path/'huggingface/OpenBMB__VoxCPM2'),
        endpoint='https://huggingface.co', token='test-private-token')]
    assert 'test-private-token' not in capsys.readouterr().out


def test_explicit_local_model_does_not_download(monkeypatch, tmp_path):
    monkeypatch.setenv('VOXCPM_MODEL_DIR', str(tmp_path))
    assert adapter._model_path() == tmp_path
