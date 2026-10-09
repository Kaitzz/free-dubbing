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
    assert calls[0]['log']==calls[1]['log']
    assert [entry['m'] for entry in calls[1]['log']]==["Task started","[tts] Started"]
    assert all(entry['t'].endswith('+00:00') for entry in calls[1]['log'])
    assert calls[0]['log_start']==calls[1]['log_start']==0
    assert not logs.lines and logs.sent==2


def test_transcript_export_prefers_fixed_and_retains_source(tmp_path):
    import json
    metadata=tmp_path/'metadata';metadata.mkdir()
    for name,text in [('asr.json','raw'),('asr_fixed.json','fixed')]:
        (metadata/name).write_text(json.dumps({'result':{'utterances':[{'start_time':1000,'end_time':2500,'text':text}]}}))
    colab_worker.export_transcript(tmp_path)
    assert (metadata/'transcript.txt').read_text()=='fixed\n'
    assert '00:00:01,000 --> 00:00:02,500' in (metadata/'transcript.srt').read_text()
    assert 'raw' in (metadata/'asr.json').read_text()


def test_concurrent_log_flush_serializes_batches():
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from types import SimpleNamespace
    logs = colab_worker.PendingLogs()
    logs.add('first')
    entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
    received = []
    def post(url, *, json, timeout):
        received.append((json['log_start'], [entry['m'] for entry in json['log']]))
        if len(received) == 1:
            entered.set()
            assert release.wait(3)
        return SimpleNamespace(raise_for_status=lambda: None)
    client = SimpleNamespace(post=post)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(logs.send, client, '/job', {})
        assert entered.wait(3)
        logs.add('added during request')
        def second_send():
            second_started.set()
            return logs.send(client, '/job', {})
        second = pool.submit(second_send)
        assert second_started.wait(3)
        release.set()
        first.result(timeout=3); second.result(timeout=3)
    assert received == [(0, ['first']), (1, ['added during request'])]
    assert not logs.lines


def test_failed_log_send_keeps_batch_for_retry():
    from types import SimpleNamespace
    import pytest
    logs = colab_worker.PendingLogs(); logs.add('retain me')
    def fail(): raise RuntimeError('request failed')
    client = SimpleNamespace(post=lambda *a, **kw: SimpleNamespace(raise_for_status=fail))
    with pytest.raises(RuntimeError): logs.send(client, '/job', {})
    assert [entry['m'] for entry in logs.lines] == ['retain me'] and logs.sent == 0
    received = []
    def post(*a, **kw):
        received.append([entry['m'] for entry in kw['json']['log']])
        return SimpleNamespace(raise_for_status=lambda: None)
    logs.send(SimpleNamespace(post=post), '/job', {})
    assert received == [['retain me']]
    assert not logs.lines


def _zip_response(files):
    import io
    import httpx
    from zipfile import ZipFile
    buffer = io.BytesIO()
    with ZipFile(buffer, 'w') as z:
        for name, data in files.items():
            z.writestr(name, data)
    return httpx.Response(200, content=buffer.getvalue())


def test_sync_reuses_mirrored_files_and_replaces_stale_ones(tmp_path):
    import json
    import httpx
    import pytest
    workspace = colab_worker.Workspace(tmp_path/'ws', 'task-1')
    keep = workspace.path('session/media/video_source.mp4')
    keep.parent.mkdir(parents=True); keep.write_bytes(b'video')
    edited = workspace.path('session/metadata/asr.json')
    edited.parent.mkdir(parents=True); edited.write_text('old')
    workspace.record('session/media/video_source.mp4', [5, 111])
    workspace.record('session/metadata/asr.json', [3, 222])
    edited.write_text('edited after mirroring')
    uncommitted = workspace.path('session/segments/tts/0001.wav')
    uncommitted.parent.mkdir(parents=True); uncommitted.write_bytes(b'partial')
    stray = workspace.work/'Uploader'/'Title__task-1'
    stray.mkdir(parents=True); (stray/'video_source.mp4').write_bytes(b'x')
    remote = {'session/media/video_source.mp4': [5, 111], 'session/metadata/asr.json': [3, 333]}
    requested = []
    def handler(request):
        requested.append(json.loads(request.content)['paths'])
        return _zip_response({'session/metadata/asr.json': 'new'})
    lease = tmp_path/'lease'; lease.mkdir()
    with httpx.Client(base_url='https://test.invalid', transport=httpx.MockTransport(handler)) as client:
        assert colab_worker.sync_input(client, '/w', workspace, remote, lease) == (1, ['session/metadata/asr.json'])
    assert requested == [['session/metadata/asr.json']]
    assert edited.read_text() == 'new' and keep.read_bytes() == b'video'
    assert not uncommitted.exists() and not (workspace.work/'Uploader').exists()
    # Mirror state survives a worker restart: nothing is downloaded again.
    def unexpected(request):
        pytest.fail('nothing should be downloaded')
    with httpx.Client(base_url='https://test.invalid', transport=httpx.MockTransport(unexpected)) as client:
        assert colab_worker.sync_input(client, '/w', colab_worker.Workspace(tmp_path/'ws', 'task-1'), remote, lease) == (2, [])


def test_run_job_uploads_only_files_the_stage_changed(tmp_path, monkeypatch):
    import io
    import json
    import httpx
    import pytest
    from zipfile import ZipFile
    monkeypatch.setattr(colab_worker, 'ROOT', tmp_path)
    monkeypatch.setenv('OPENAI_API_KEY', 'key')
    workspace = colab_worker.Workspace(tmp_path/'remote-runs/tasks/task-1', 'task-1')
    video = workspace.path('session/media/video_source.mp4')
    video.parent.mkdir(parents=True); video.write_bytes(b'video')
    workspace.record('session/media/video_source.mp4', [5, 1]); workspace.save()
    names = ('download', 'separate', 'asr', 'asr_fix', 'translate', 'split_audio', 'tts', 'merge_audio', 'merge_video')
    task = {'id': 'task-1', 'url': 'local://upload/task-1?direction=en-zh',
            'stages': [{'name': name, 'status': 'succeeded' if name == 'download' else 'pending'} for name in names]}
    job = {'lease': 'b'*32, 'transfer_version': colab_worker.TRANSFER_VERSION, 'task': task,
           'settings': {'base_url': 'https://example.com/v1', 'model': 'm', 'translate_concurrency': '2'},
           'files': {'session/media/video_source.mp4': [5, 1]}}
    class Process:
        stdout = io.StringIO('[separate] Completed\n')
        def poll(self): return 0
        def wait(self): return 0
    def start(command, env, **kwargs):
        (Path(env['WORKFOLDER'])/'session/media/audio_vocals.wav').write_bytes(b'vocals')
        return Process()
    monkeypatch.setattr(colab_worker.subprocess, 'Popen', start)
    monkeypatch.setattr(colab_worker, 'snapshot', lambda folder, original, exitcode=None: {**original, 'status': 'paused'})
    uploads, finished = [], {}
    def handler(request):
        if request.url.path.endswith('/files'):
            pytest.fail('nothing should be downloaded')
        if request.url.path.endswith('/output'):
            uploads.append(request.content)
            return httpx.Response(200, json={'offset': len(request.content)})
        if request.url.path.endswith('/finish'):
            finished.update(json.loads(request.content))
            return httpx.Response(200, json={'status': 'paused', 'files': {
                'session/media/video_source.mp4': [5, 1], 'session/media/audio_vocals.wav': [6, 2]}})
        return httpx.Response(200, json={'ok': True})
    with httpx.Client(base_url='https://test.invalid', transport=httpx.MockTransport(handler)) as client:
        colab_worker.run_job(client, job)
    with ZipFile(io.BytesIO(b''.join(uploads))) as z:
        assert z.namelist() == ['session/media/audio_vocals.wav']
    assert finished['files'] == {'session/media/audio_vocals.wav': 6} and finished['deleted'] == []
    assert 'stage separate' in finished['report']
    state = json.loads((tmp_path/'remote-runs/tasks/task-1/state.json').read_text())['files']
    assert state['session/media/audio_vocals.wav']['remote'] == [6, 2]


def test_commit_retries_while_coordinator_is_busy(monkeypatch):
    import httpx
    import pytest
    monkeypatch.setattr(colab_worker.time, 'sleep', lambda seconds: None)
    replies = [httpx.Response(502), httpx.Response(409, json={'detail': 'Checkpoint commit in progress'}),
               httpx.Response(200, json={'status': 'paused', 'files': {}})]
    with httpx.Client(base_url='https://test.invalid', transport=httpx.MockTransport(lambda r: replies.pop(0))) as client:
        assert colab_worker.commit(client, '/w', {}) == {'status': 'paused', 'files': {}}
    expired = httpx.MockTransport(lambda r: httpx.Response(409, json={'detail': 'Lease expired or already completed'}))
    with httpx.Client(base_url='https://test.invalid', transport=expired) as client:
        with pytest.raises(httpx.HTTPStatusError):
            colab_worker.commit(client, '/w', {})


def test_download_session_folder_is_adopted_as_work_session(tmp_path):
    workspace = colab_worker.Workspace(tmp_path/'ws', 'task-1')
    produced = workspace.work/'Uploader'/'Title__task-1'
    (produced/'media').mkdir(parents=True); (produced/'media/video_source.mp4').write_bytes(b'v')
    (workspace.session/'media').mkdir(parents=True)  # Empty folders left after a redo of download.
    colab_worker.adopt_session(workspace, {'session_path': str(produced)})
    assert (workspace.session/'media/video_source.mp4').read_bytes() == b'v'
    assert not produced.exists()
