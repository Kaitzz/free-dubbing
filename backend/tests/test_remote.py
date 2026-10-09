import io
import json
import time
from pathlib import Path
from zipfile import ZipFile
import pytest
from fastapi.testclient import TestClient
from backend.app import config,database,remote,main,auth
from backend.tests.test_settings_and_api import configure_tmp_runtime,authenticated_client
from backend.app.remote_archive import unpack

CLAIM={'transfer_version':remote.TRANSFER_VERSION}

@pytest.fixture
def setup(monkeypatch,tmp_path):
    configure_tmp_runtime(monkeypatch,tmp_path)
    monkeypatch.setattr(config,'DATA_DIR',tmp_path/'data')
    monkeypatch.setenv('YOUDUB_EXECUTION_BACKEND','colab')
    remote.init()
    browser=authenticated_client()
    token=browser.post('/api/remote/token').json()['token']
    worker=TestClient(main.app)
    worker.headers['Authorization']='Bearer '+token
    return browser,worker,tmp_path

def claim(worker,**extra):
    return worker.post('/api/colab-worker/claim',json={**CLAIM,**extra}).json()['job']

def upload(worker,job,root,files):
    archive=root/f"{job['lease']}.zip"
    with ZipFile(archive,'w') as z:
        for name,data in files.items():z.writestr(name,data)
    response=worker.put(f"/api/colab-worker/{job['lease']}/output",params={'offset':0},content=archive.read_bytes())
    assert response.status_code==200

def finish_payload(job,status,files=None,deleted=(),succeeded=(),ran=()):
    task=json.loads(json.dumps(job['task']));task['status']=status
    for s in task['stages']:
        if s['name'] in succeeded:s['status']='succeeded'
    return {'task':task,'files':{name:len(data) for name,data in (files or {}).items()},'deleted':list(deleted),
            'ran':list(ran)}

def commit(worker,job,root,status,files=None,deleted=(),succeeded=()):
    if files:upload(worker,job,root,files)
    return worker.post(f"/api/colab-worker/{job['lease']}/finish",json=finish_payload(job,status,files,deleted,succeeded))

def test_worker_auth_cannot_access_browser_settings(setup):
    browser,worker,_=setup
    assert TestClient(main.app).post('/api/colab-worker/claim',json=CLAIM).status_code==401
    assert worker.get('/api/settings/openai').status_code==401
    assert worker.post('/api/colab-worker/claim',json=CLAIM).json()=={'job':None}
    assert browser.get('/api/remote/status').json()['connected']

def test_outdated_worker_is_rejected_before_a_task_starts(setup):
    _,worker,_=setup
    task_id=database.create_task('https://www.youtube.com/watch?v=abcdefghijk')
    assert worker.get('/api/colab-worker/hello').json()=={'transfer_version':remote.TRANSFER_VERSION}
    response=worker.post('/api/colab-worker/claim')
    assert response.status_code==426 and 're-run the launcher' in response.json()['detail']
    assert database.get_task(task_id)['status']=='queued'

def test_claim_checkpoint_and_manual_continue(setup):
    browser,worker,root=setup
    database.save_openai_settings('https://example.com/v1','secret-must-stay-local','MiniMax-M3')
    task_id=database.create_task('local://upload/test?direction=en-zh',execution_mode='manual')
    uploads=config.WORKFOLDER/'_uploads'/task_id/'video';uploads.mkdir(parents=True)
    (uploads/'input.mp4').write_bytes(b'video')
    job=claim(worker)
    assert 'api_key' not in job['settings']
    assert 'secret-must-stay-local' not in json.dumps(job)
    assert list(job['files'])==['uploads/video/input.mp4']
    assert claim(worker) is None
    assert browser.post('/api/remote/token').status_code==409
    prefix=f"/api/colab-worker/{job['lease']}"
    response=worker.post(prefix+'/files',json={'paths':['uploads/video/input.mp4']})
    with ZipFile(io.BytesIO(response.content)) as z:
        assert z.read('uploads/video/input.mp4')==b'video'
    assert worker.post(prefix+'/files',json={'paths':['session/missing.txt']}).status_code==409
    response=commit(worker,job,root,'paused',{'session/media/video_source.mp4':b'video'},succeeded={'download'})
    assert response.json()['status']=='paused'
    session=Path(database.get_task(task_id)['session_path'])
    assert session==config.WORKFOLDER/'_remote'/task_id/'session'
    assert (session/'media/video_source.mp4').read_bytes()==b'video'
    # Only the download stage reads the original upload.
    assert list(response.json()['files'])==['session/media/video_source.mp4']
    assert worker.post(prefix+'/heartbeat',json={}).status_code==409
    assert browser.post(f'/api/tasks/{task_id}/continue').status_code==200
    next_job=claim(worker)
    assert next_job['task']['stages'][0]['status']=='succeeded'
    assert next_job['files']==response.json()['files']

def test_later_stage_commits_only_its_changes_into_the_same_session(setup):
    _,worker,root=setup
    task_id=database.create_task('https://www.youtube.com/watch?v=abcdefghijk')
    job=claim(worker)
    first={'session/media/video_source.mp4':b'v'*1000,'session/metadata/old.json':b'{}'}
    assert commit(worker,job,root,'paused',first,succeeded={'download'}).json()['status']=='queued'
    session=Path(database.get_task(task_id)['session_path'])
    video=session/'media/video_source.mp4';before=video.stat()
    job=claim(worker)
    assert job['files']['session/media/video_source.mp4']==[before.st_size,before.st_mtime_ns]
    response=commit(worker,job,root,'paused',{'session/metadata/asr.json':b'{"result":1}'},
                    deleted=['session/metadata/old.json'],succeeded={'download','separate'})
    assert response.status_code==200
    assert Path(database.get_task(task_id)['session_path'])==session
    assert video.stat().st_mtime_ns==before.st_mtime_ns
    assert (session/'metadata/asr.json').read_bytes()==b'{"result":1}'
    assert not (session/'metadata/old.json').exists()
    assert [p.name for p in (config.WORKFOLDER/'_remote'/task_id).iterdir()]==['session']
    assert not any((config.DATA_DIR/'remote').iterdir())
    assert 'committed 1 files' in database.log_path(task_id).read_text()

def test_finish_retry_returns_stored_result_without_reapplying(setup):
    _,worker,root=setup
    task_id=database.create_task('https://www.youtube.com/watch?v=abcdefghijk',execution_mode='manual')
    job=claim(worker)
    files={'session/media/video_source.mp4':b'video'}
    first=commit(worker,job,root,'paused',files,succeeded={'download'})
    # The worker lost the first response and sends the same request again.
    again=worker.post(f"/api/colab-worker/{job['lease']}/finish",json=finish_payload(job,'paused',files,succeeded={'download'}))
    assert first.status_code==again.status_code==200
    assert again.json()==first.json()
    assert database.get_task(task_id)['status']=='paused'

def test_output_upload_replays_retried_chunks_and_restarts(setup):
    _,worker,_=setup
    database.create_task('https://www.youtube.com/watch?v=abcdefghijk')
    job=claim(worker)
    def put(offset,data):
        return worker.put(f"/api/colab-worker/{job['lease']}/output",params={'offset':offset},content=data)
    assert put(0,b'aaaa').json()=={'offset':4}
    assert put(4,b'bbbb').json()=={'offset':8}
    assert put(4,b'bbbb').json()=={'offset':8}
    assert put(4,b'cccc').status_code==409
    assert put(12,b'dddd').status_code==409
    assert put(0,b'xy').json()=={'offset':2}
    assert (config.DATA_DIR/'remote'/job['lease']/'output.zip').read_bytes()==b'xy'

def test_commit_failure_fails_task_with_reason_and_cleans_up(setup):
    _,worker,root=setup
    task_id=database.create_task('https://www.youtube.com/watch?v=abcdefghijk')
    job=claim(worker);prefix=f"/api/colab-worker/{job['lease']}"
    upload(worker,job,root,{'session/a.txt':b'a'})
    payload=finish_payload(job,'paused',{'session/a.txt':b'a','session/b.txt':b'b'},succeeded={'download'})
    response=worker.post(prefix+'/finish',json=payload)
    assert response.status_code==422 and 'Checkpoint commit failed' in response.json()['detail']
    task=database.get_task(task_id)
    assert task['status']=='failed' and 'does not match' in task['error_message']
    assert task['stages'][0]['status']=='failed'
    assert worker.post(prefix+'/heartbeat',json={}).status_code==409
    assert not (config.DATA_DIR/'remote'/job['lease']).exists()
    assert 'Checkpoint commit failed' in database.log_path(task_id).read_text()

def test_stage_versions_follow_the_lease_that_ran_the_stage(setup):
    _,worker,_=setup
    database.create_task('https://www.youtube.com/watch?v=abcdefghijk')
    first=claim(worker)
    assert first['stage_leases']=={}
    payload=finish_payload(first,'paused',succeeded={'download'},ran=['download'])
    assert worker.post(f"/api/colab-worker/{first['lease']}/finish",json=payload).status_code==200
    second=claim(worker)
    assert second['stage_leases']=={'download':first['lease']}
    # A worker that lost download's output re-ran it: the new lease names the checkpoint.
    payload=finish_payload(second,'paused',succeeded={'download','separate'},ran=['download'])
    assert worker.post(f"/api/colab-worker/{second['lease']}/finish",json=payload).status_code==200
    assert claim(worker)['stage_leases']=={'download':second['lease']}

def test_finish_rejects_unknown_stage_in_ran(setup):
    _,worker,_=setup
    database.create_task('https://www.youtube.com/watch?v=abcdefghijk')
    job=claim(worker)
    payload=finish_payload(job,'paused',succeeded={'download'},ran=['bogus'])
    assert worker.post(f"/api/colab-worker/{job['lease']}/finish",json=payload).status_code==422

def test_finish_rejects_paths_outside_the_session(setup):
    _,worker,root=setup
    database.create_task('https://www.youtube.com/watch?v=abcdefghijk')
    job=claim(worker)
    for name in ('uploads/video/x.mp4','session/../escape','../escape'):
        payload=finish_payload(job,'paused',{name:b'x'})
        assert worker.post(f"/api/colab-worker/{job['lease']}/finish",json=payload).status_code==422

def test_retried_claim_returns_the_same_lease(setup):
    _,worker,_=setup
    database.create_task('https://www.youtube.com/watch?v=abcdefghijk')
    first=claim(worker,claim_id='attempt-1')
    assert claim(worker,claim_id='attempt-1')['lease']==first['lease']
    assert claim(worker,claim_id='attempt-2') is None

def test_heartbeat_keeps_worker_timestamps_and_drops_resent_batches(setup):
    _,worker,_=setup
    task_id=database.create_task('https://www.youtube.com/watch?v=abcdefghijk')
    job=claim(worker);prefix=f"/api/colab-worker/{job['lease']}"
    entries=[{'t':'2026-10-09T09:00:00+00:00','m':'Task started'},
             {'t':'2026-10-09T09:00:05+00:00','m':'[asr] Started\nsecond line'}]
    for _ in range(2):  # The first response was lost, so the batch is sent again.
        assert worker.post(prefix+'/heartbeat',json={'log_start':0,'log':entries}).status_code==200
    worker.post(prefix+'/heartbeat',json={'log_start':1,'log':[entries[1],{'t':'bogus','m':'next'}]})
    lines=database.log_path(task_id).read_text().splitlines()
    assert lines[:3]==['[2026-10-09T09:00:00+00:00] [Colab] Task started',
                       '[2026-10-09T09:00:05+00:00] [Colab] [asr] Started',
                       '[2026-10-09T09:00:05+00:00] [Colab] second line']
    assert len(lines)==4 and lines[3].endswith('[Colab] next') and not lines[3].startswith('[bogus')

def test_expired_lease_preserves_checkpoint_and_rejects_late_worker(setup):
    _,worker,_=setup
    task_id=database.create_task('https://www.youtube.com/watch?v=abcdefghijk')
    database.update_stage(task_id,'download',status='succeeded',progress=100)
    job=claim(worker)
    prefix=f"/api/colab-worker/{job['lease']}"
    assert worker.post(prefix+'/heartbeat',json={'stage':'separate','progress':50}).status_code==200
    worker.put(prefix+'/output',params={'offset':0},content=b'partial')
    with database.connect() as c:c.execute('UPDATE remote_leases SET expires=?',(time.time()-1,))
    remote.expire()
    task=database.get_task(task_id)
    assert task['status']=='failed'
    assert task['stages'][0]['status']=='succeeded'
    assert worker.post(prefix+'/heartbeat',json={}).status_code==409
    assert not (config.DATA_DIR/'remote'/job['lease']).exists()

def test_startup_removes_stale_transfer_files_but_keeps_active_lease(setup):
    _,worker,_=setup
    database.create_task('https://www.youtube.com/watch?v=abcdefghijk')
    job=claim(worker)
    worker.put(f"/api/colab-worker/{job['lease']}/output",params={'offset':0},content=b'partial')
    stale=config.DATA_DIR/'remote'/('f'*32);stale.mkdir(parents=True);(stale/'input.zip').write_bytes(b'old')
    remote.init()
    assert not stale.exists()
    assert (config.DATA_DIR/'remote'/job['lease']/'output.zip').read_bytes()==b'partial'

def test_restart_keeps_queued_colab_tasks_waiting(setup):
    queued=database.create_task('https://www.youtube.com/watch?v=abcdefghijk')
    running=database.create_task('https://www.youtube.com/watch?v=bcdefghijkl')
    database.update_task(running,status='running',current_stage='separate')
    database.fail_stale_active_tasks(('running',))
    assert database.get_task(queued)['status']=='queued'
    assert database.get_task(running)['status']=='failed'

@pytest.mark.parametrize('name',['../escape','session/../../escape','session/evil:stream','cookies/youtube.txt','session/CON.txt','session/../escape'])
def test_archive_rejects_unsafe_and_cookie_paths(tmp_path,name):
    archive=tmp_path/'in.zip'
    with ZipFile(archive,'w') as z:z.writestr(name,'secret')
    with pytest.raises(ValueError):unpack(archive,tmp_path/'out')

def test_full_final_video_return_and_gui_download(setup):
    browser,worker,root=setup
    task_id=database.create_task('https://www.youtube.com/watch?v=abcdefghijk')
    job=claim(worker)
    files={'session/media/video_final.mp4':b'final-video'}
    upload(worker,job,root,files)
    payload=finish_payload(job,'succeeded',files,succeeded={s['name'] for s in job['task']['stages']})
    payload['task']['current_stage']='done'
    assert worker.post(f"/api/colab-worker/{job['lease']}/finish",json=payload).status_code==200
    response=browser.get(f'/api/tasks/{task_id}/artifact/final-video')
    assert response.status_code==200 and response.content==b'final-video'

def test_deleting_task_removes_its_remote_checkpoints(setup):
    browser,worker,root=setup
    task_id=database.create_task('https://www.youtube.com/watch?v=abcdefghijk',execution_mode='manual')
    job=claim(worker)
    upload(worker,job,root,{'session/metadata/ytdlp_info.json':b'{}'})
    payload=finish_payload(job,'paused',{'session/metadata/ytdlp_info.json':b'{}'},succeeded={'download'},ran=['download'])
    assert worker.post(f"/api/colab-worker/{job['lease']}/finish",json=payload).status_code==200
    legacy=config.WORKFOLDER/'_remote'/task_id/('e'*32)/'session';legacy.mkdir(parents=True)
    assert browser.delete(f'/api/tasks/{task_id}').status_code==204
    assert not (config.WORKFOLDER/'_remote'/task_id).exists()
    with database.connect() as c:
        assert not c.execute('SELECT 1 FROM remote_stage_versions WHERE task_id=?',(task_id,)).fetchone()


def test_custom_password_persists_and_rejects_invalid_values(setup):
    browser, _, _ = setup
    remote._failed_auth.clear()
    for invalid in ('1234567', '123456789', 'abcdefgh', 12345678):
        assert browser.post('/api/remote/token', json={'password':invalid}).status_code == 422
    assert browser.post('/api/remote/token', json={'password':'01234567'}).status_code == 200
    remote.init()
    assert remote.authenticate('01234567')
    assert not remote.authenticate('76543210')
    remote._failed_auth.clear()


def test_short_password_guessing_is_limited(setup):
    browser, _, _ = setup
    remote._failed_auth.clear()
    browser.post('/api/remote/token', json={'password':'01234567'})
    try:
        for _ in range(10): assert not remote.authenticate('incorrect')
        assert not remote.authenticate('01234567')
        remote._failed_auth[:] = [time.monotonic()-61]
        assert remote.authenticate('01234567')
    finally:
        remote._failed_auth.clear()


@pytest.mark.parametrize('download_done', [False, True])
def test_gui_cookie_only_sent_for_youtube_download(setup,monkeypatch,download_done):
    browser,worker,root=setup
    cookie=root/'youtube-cookie.txt'
    cookie.write_text('# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t2147483647\tSID\tgui-cookie-value\n.example.com\tTRUE\t/\tTRUE\t2147483647\tOTHER\tnot-for-colab\n')
    monkeypatch.setattr(config,'YOUTUBE_COOKIE_PATH',cookie)
    task_id=database.create_task('https://www.youtube.com/watch?v=abcdefghijk')
    if download_done: database.update_stage(task_id,'download',status='succeeded')
    job=claim(worker)
    assert ('youtube_cookies' in job) is not download_done
    if not download_done:
        assert 'gui-cookie-value' in job['youtube_cookies']
        assert 'not-for-colab' not in job['youtube_cookies']
    assert 'gui-cookie-value' not in json.dumps(database.get_task(task_id))
    data=worker.post('/api/colab-worker/'+job['lease']+'/files',json={'paths':list(job['files'])}).content
    with ZipFile(io.BytesIO(data)) as z:
        assert all(b'gui-cookie-value' not in z.read(n) for n in z.namelist())


def test_gateway_exposes_only_worker_routes():
    from scripts import worker_gateway
    gateway=TestClient(worker_gateway.app)
    assert gateway.post('/api/colab-worker/'+'a'*32+'/input',headers={'Authorization':'Bearer x'}).status_code==404
    assert gateway.get('/api/colab-worker/hello').status_code==401
    assert gateway.get('/api/settings/openai').status_code==404
