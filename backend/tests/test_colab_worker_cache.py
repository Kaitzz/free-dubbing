import os
import subprocess
import sys
from pathlib import Path

from scripts import colab_worker


def test_shared_cache_passes_child_runtime_initialization(tmp_path, monkeypatch):
    shared = tmp_path/'shared-models'
    shared.mkdir()
    marker = shared/'existing-model'
    marker.write_bytes(b'cached')
    monkeypatch.setenv('MODEL_CACHE_DIR', str(shared))
    selected = colab_worker.model_cache_path()
    assert Path(selected) == shared.resolve()
    env = os.environ.copy()
    env.update(MODEL_CACHE_DIR=selected, YOUDUB_DATA_DIR=str(tmp_path/'data'),
               WORKFOLDER=str(tmp_path/'work'))
    subprocess.run([sys.executable, '-c',
        'from backend.app import config; config.ensure_runtime_dirs()'], env=env, check=True)
    assert marker.read_bytes() == b'cached'


def test_default_cache_path(tmp_path, monkeypatch):
    monkeypatch.delenv('MODEL_CACHE_DIR', raising=False)
    monkeypatch.setattr(colab_worker, 'ROOT', tmp_path)
    assert colab_worker.model_cache_path() == str((tmp_path/'model-cache').resolve())
