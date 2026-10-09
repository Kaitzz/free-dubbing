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


def test_console_keeps_errors_and_stages_without_progress_spam():
    assert colab_worker.console_line("[tts] Started")
    assert colab_worker.console_line("RuntimeError: GPU allocation failed")
    assert colab_worker.console_line("WARNING: missing format")
    assert not colab_worker.console_line(" 50%|##### | 100/200 [00:12<00:12]")
    assert not colab_worker.console_line("ordinary model debug detail")


def test_console_hides_ytdlp_progress_but_keeps_download_diagnostics():
    for line in (
        "[download]   0.0% of 18.04MiB at Unknown B/s ETA Unknown",
        "[download] 100% of 18.04MiB in 00:00:00 at 38.33MiB/s",
        "[download]  55.1% of 18.04MiB at 60.87MiB/s ETA 00:00",
    ):
        assert not colab_worker.console_line(line)
    for line in (
        "[download] Started", "[download] Completed",
        "[download] Available formats: video=21, audio=9",
        "[download] ERROR: unable to download video",
        "[download] Got error: HTTP Error 403. Retrying fragment 1",
    ):
        assert colab_worker.console_line(line)
