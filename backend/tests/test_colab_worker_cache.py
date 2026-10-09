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


def test_pending_logs_retain_adjacent_events_and_retry_failed_send():
    import httpx
    import pytest
    logs=colab_worker.PendingLogs()
    logs.add("Task started")
    logs.add("[tts] Started")
    calls=[]
    def handler(request):
        import json
        calls.append(json.loads(request.content))
        return httpx.Response(503 if len(calls)==1 else 200)
    with httpx.Client(base_url="https://example.test",transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            logs.send(client,"/worker",{})
        assert len(logs.lines)==2
        logs.send(client,"/worker",{})
    assert calls[0]['log']==calls[1]['log']=="Task started\n[tts] Started"
    assert not logs.lines


def test_transcript_export_prefers_fixed_and_retains_source(tmp_path):
    import json
    metadata=tmp_path/'metadata';metadata.mkdir()
    for name,text in [('asr.json','raw'),('asr_fixed.json','fixed')]:
        (metadata/name).write_text(json.dumps({'result':{'utterances':[{'start_time':1000,'end_time':2500,'text':text}]}}))
    colab_worker.export_transcript(tmp_path)
    assert (metadata/'transcript.txt').read_text()=='fixed\n'
    assert '00:00:01,000 --> 00:00:02,500' in (metadata/'transcript.srt').read_text()
    assert 'raw' in (metadata/'asr.json').read_text()
