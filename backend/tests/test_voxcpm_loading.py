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
