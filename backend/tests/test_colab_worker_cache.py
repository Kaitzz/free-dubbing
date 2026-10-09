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


STAGE_NAMES = ('download', 'separate', 'asr', 'asr_fix', 'translate', 'split_audio', 'tts', 'merge_audio', 'merge_video')


def _job(succeeded, leases=None, files=None):
    stages = [{'name': name, 'status': 'succeeded' if name in succeeded else 'pending', 'progress': None,
               'started_at': None, 'completed_at': None, 'last_message': None, 'error_message': None}
              for name in STAGE_NAMES]
    return {'lease': 'c'*32, 'transfer_version': colab_worker.TRANSFER_VERSION, 'stage_leases': leases or {},
            'files': files or {}, 'settings': {'base_url': 'https://example.com/v1', 'model': 'm', 'translate_concurrency': '2'},
            'task': {'id': 'task-1', 'url': 'https://www.youtube.com/watch?v=abcdefghijk', 'stages': stages}}


def _write(workspace, name, data):
    path = workspace.path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_prepare_restores_a_lost_workspace_from_drive_and_drops_reset_stages(tmp_path):
    import json
    import httpx
    drive_dir = tmp_path/'drive'
    # An earlier runtime committed download, separate and asr and checkpointed each to Drive.
    old = colab_worker.Workspace(tmp_path/'old', 'task-1')
    store = colab_worker.DriveStore(drive_dir, 'task-1')
    store.save('download', 'L1', [('session/media/video_source.mp4', _write(old, 'session/media/video_source.mp4', b'video'))])
    store.save('separate', 'L2', [(name, _write(old, name, data)) for name, data in
                                  (('session/media/audio_vocals.wav', b'vocals'), ('session/media/audio_bgm.wav', b'bgm'))])
    store.save('asr', 'L3', [('session/metadata/asr.json', _write(old, 'session/metadata/asr.json', b'{}'))])
    # The new runtime starts empty, and asr was redone in the GUI since then.
    workspace = colab_worker.Workspace(tmp_path/'new', 'task-1')
    job = _job({'download', 'separate'}, {'download': 'L1', 'separate': 'L2', 'asr': 'L3'})
    lease = tmp_path/'lease'; lease.mkdir()
    def unexpected(request):
        raise AssertionError('nothing should come from the local GUI')
    with httpx.Client(base_url='https://test.invalid', transport=httpx.MockTransport(unexpected)) as client:
        stats, missing = colab_worker.prepare(client, '/w', workspace, colab_worker.DriveStore(drive_dir, 'task-1'), job, lease)
    assert (stats['drive'], stats['fetched'], missing) == (2, 0, [])
    assert workspace.path('session/media/audio_vocals.wav').read_bytes() == b'vocals'
    assert sorted(workspace.stages) == ['download', 'separate']
    assert sorted(json.loads((drive_dir/'tasks/task-1/stages.json').read_text())) == ['download', 'separate']
    assert not list((drive_dir/'tasks/task-1').glob('asr.*'))


def test_prepare_takes_text_results_from_the_gui_and_reruns_lost_audio(tmp_path):
    import json
    import httpx
    workspace = colab_worker.Workspace(tmp_path/'ws', 'task-1')
    # Output of a lease that never committed must not leak into the next stage.
    _write(workspace, 'session/segments/tts/0001.wav', b'partial')
    remote = {'session/metadata/asr.json': [2, 1], 'session/metadata/ytdlp_info.json': [2, 1],
              'session/metadata/translation.zh.json': [2, 1]}
    job = _job({'download', 'separate', 'asr'}, {'download': 'L1', 'separate': 'L2', 'asr': 'L3'}, remote)
    requested = []
    def handler(request):
        names = json.loads(request.content)['paths']
        requested.extend(names)
        return _zip_response({name: '{}' for name in names})
    lease = tmp_path/'lease'; lease.mkdir()
    with httpx.Client(base_url='https://test.invalid', transport=httpx.MockTransport(handler)) as client:
        stats, missing = colab_worker.prepare(client, '/w', workspace, None, job, lease)
    # translate is pending, so the GUI's copy of its output is stale and not fetched.
    assert sorted(requested) == ['session/metadata/asr.json', 'session/metadata/ytdlp_info.json']
    assert missing == ['download', 'separate']
    assert not workspace.path('session/segments/tts/0001.wav').exists()
    assert workspace.stages['asr'] == {'lease': 'L3', 'files': {'session/metadata/asr.json': 2}}


def test_run_job_checkpoints_the_stage_to_drive_and_uploads_only_gui_files(tmp_path, monkeypatch):
    import io
    import json
    import httpx
    import pytest
    from zipfile import ZipFile
    monkeypatch.setattr(colab_worker, 'ROOT', tmp_path)
    monkeypatch.setenv('OPENAI_API_KEY', 'key')
    drive_dir = tmp_path/'drive'; drive_dir.mkdir()
    monkeypatch.setenv('DUBBING_DRIVE_DIR', str(drive_dir))
    workspace = colab_worker.Workspace(tmp_path/'remote-runs/tasks/task-1', 'task-1')
    _write(workspace, 'session/media/video_source.mp4', b'video')
    workspace.own('download', 'L1', {'session/media/video_source.mp4': 5}); workspace.save()
    job = _job({'download'}, {'download': 'L1'})
    class Process:
        stdout = io.StringIO('[separate] Completed\n')
        def poll(self): return 0
        def wait(self): return 0
    def start(command, env, **kwargs):
        session = Path(env['WORKFOLDER'])/'session'
        (session/'media/audio_vocals.wav').write_bytes(b'vocals')
        (session/'metadata').mkdir(exist_ok=True)
        (session/'metadata/separation.json').write_text('{}')
        return Process()
    monkeypatch.setattr(colab_worker.subprocess, 'Popen', start)
    def snapshot(folder, task, exitcode=None):
        stages = [{**s, 'status': 'succeeded'} if s['name'] == 'separate' else s for s in task['stages']]
        return {**task, 'stages': stages, 'status': 'paused'}
    monkeypatch.setattr(colab_worker, 'snapshot', snapshot)
    uploads, finished = [], {}
    def handler(request):
        if request.url.path.endswith('/files'):
            pytest.fail('nothing should be downloaded')
        if request.url.path.endswith('/output'):
            uploads.append(request.content)
            return httpx.Response(200, json={'offset': len(request.content)})
        if request.url.path.endswith('/finish'):
            finished.update(json.loads(request.content))
            return httpx.Response(200, json={'status': 'paused', 'files': {}})
        return httpx.Response(200, json={'ok': True})
    with httpx.Client(base_url='https://test.invalid', transport=httpx.MockTransport(handler)) as client:
        colab_worker.run_job(client, job)
    with ZipFile(io.BytesIO(b''.join(uploads))) as z:
        assert z.namelist() == ['session/metadata/separation.json']
    assert finished['files'] == {'session/metadata/separation.json': 2} and finished['ran'] == ['separate']
    assert 'Drive checkpoint 2 files' in finished['report']
    index = json.loads((drive_dir/'tasks/task-1/stages.json').read_text())
    assert index['separate']['lease'] == job['lease']
    assert sorted(index['separate']['files']) == ['session/media/audio_vocals.wav', 'session/metadata/separation.json']
    state = json.loads((tmp_path/'remote-runs/tasks/task-1/state.json').read_text())['stages']
    assert state['separate']['lease'] == job['lease'] and state['download']['lease'] == 'L1'


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


def test_download_rerun_merges_into_a_session_restored_from_drive(tmp_path):
    workspace = colab_worker.Workspace(tmp_path/'ws', 'task-1')
    _write(workspace, 'session/segments/tts/0001.wav', b'tts')
    produced = workspace.work/'Uploader'/'Title__task-1'
    (produced/'media').mkdir(parents=True); (produced/'media/video_source.mp4').write_bytes(b'v')
    colab_worker.adopt_session(workspace, {'session_path': str(produced)})
    assert workspace.path('session/media/video_source.mp4').read_bytes() == b'v'
    assert workspace.path('session/segments/tts/0001.wav').read_bytes() == b'tts'
    assert not (workspace.work/'Uploader').exists()
