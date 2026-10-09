import io
import json
import time
from pathlib import Path
from zipfile import ZipFile
import pytest
from fastapi.testclient import TestClient
from backend.app import config,database,remote,main,auth
from backend.tests.test_settings_and_api import configure_tmp_runtime,authenticated_client
from backend.app.remote_archive import unpack,pack

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

def test_worker_auth_cannot_access_browser_settings(setup):
    browser,worker,_=setup
    assert TestClient(main.app).post('/api/colab-worker/claim').status_code==401
    assert worker.get('/api/settings/openai').status_code==401
    assert worker.post('/api/colab-worker/claim').json()=={'job':None}
    assert browser.get('/api/remote/status').json()['connected']

def test_claim_checkpoint_and_manual_continue(setup):
    browser,worker,root=setup
    database.save_openai_settings('https://example.com/v1','secret-must-stay-local','MiniMax-M3')
    task_id=database.create_task('local://upload/test?direction=en-zh',execution_mode='manual')
    uploads=config.WORKFOLDER/'_uploads'/task_id/'video';uploads.mkdir(parents=True)
    (uploads/'input.mp4').write_bytes(b'video')
    job=worker.post('/api/colab-worker/claim').json()['job']
    assert 'api_key' not in job['settings']
    assert 'secret-must-stay-local' not in json.dumps(job)
    assert worker.post('/api/colab-worker/claim').json()['job'] is None
    assert browser.post('/api/remote/token').status_code==409
    lease=job['lease'];prefix=f'/api/colab-worker/{lease}'
    response=worker.get(prefix+'/input')
    with ZipFile(io.BytesIO(response.content)) as z:
        assert z.namelist()==['uploads/video/input.mp4']
    session=root/'remote-session';(session/'media').mkdir(parents=True)
    (session/'media/video_source.mp4').write_bytes(b'video')
    archive=root/'output.zip';pack(archive,{'session':session})
    assert worker.put(prefix+'/output?offset=1',content=b'x').status_code==409
    assert worker.put(prefix+'/output?offset=0',content=archive.read_bytes()).status_code==200
    task=job['task'];task['status']='paused';task['current_stage']='download'
    for s in task['stages']:
        if s['name']=='download':s['status']='succeeded'
    assert worker.post(prefix+'/finish',json={'task':task}).json()['status']=='paused'
    saved=database.get_task(task_id)
    assert (Path(saved['session_path'])/'media/video_source.mp4').read_bytes()==b'video'
    assert worker.post(prefix+'/heartbeat',json={}).status_code==409
    assert browser.post(f'/api/tasks/{task_id}/continue').status_code==200
    next_job=worker.post('/api/colab-worker/claim').json()['job']
    assert next_job['task']['stages'][0]['status']=='succeeded'

def test_expired_lease_preserves_checkpoint_and_rejects_late_worker(setup):
    _,worker,_=setup
    task_id=database.create_task('https://www.youtube.com/watch?v=abcdefghijk')
    database.update_stage(task_id,'download',status='succeeded',progress=100)
    job=worker.post('/api/colab-worker/claim').json()['job']
    prefix=f"/api/colab-worker/{job['lease']}"
    assert worker.post(prefix+'/heartbeat',json={'stage':'separate','progress':50}).status_code==200
    with database.connect() as c:c.execute('UPDATE remote_leases SET expires=?',(time.time()-1,))
    remote.expire()
    task=database.get_task(task_id)
    assert task['status']=='failed'
    assert task['stages'][0]['status']=='succeeded'
    assert worker.post(prefix+'/heartbeat',json={}).status_code==409

@pytest.mark.parametrize('name',['../escape','session/../../escape','session/evil:stream','cookies/youtube.txt','session/CON.txt','session/../escape'])
def test_archive_rejects_unsafe_and_cookie_paths(tmp_path,name):
    archive=tmp_path/'in.zip'
    with ZipFile(archive,'w') as z:z.writestr(name,'secret')
    with pytest.raises(ValueError):unpack(archive,tmp_path/'out')

def test_full_final_video_return_and_gui_download(setup):
    browser,worker,root=setup
    task_id=database.create_task('https://www.youtube.com/watch?v=abcdefghijk')
    job=worker.post('/api/colab-worker/claim').json()['job'];token=job['lease']
    session=root/'session';(session/'media').mkdir(parents=True)
    (session/'media/video_final.mp4').write_bytes(b'final-video')
    archive=root/'final.zip';pack(archive,{'session':session})
    worker.put(f'/api/colab-worker/{token}/output',content=archive.read_bytes())
    task=job['task'];task.update(status='succeeded',current_stage='done')
    for s in task['stages']:s['status']='succeeded'
    assert worker.post(f'/api/colab-worker/{token}/finish',json={'task':task}).status_code==200
    response=browser.get(f'/api/tasks/{task_id}/artifact/final-video')
    assert response.status_code==200 and response.content==b'final-video'


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
    job=worker.post('/api/colab-worker/claim').json()['job']
    assert ('youtube_cookies' in job) is not download_done
    if not download_done:
        assert 'gui-cookie-value' in job['youtube_cookies']
        assert 'not-for-colab' not in job['youtube_cookies']
    assert 'gui-cookie-value' not in json.dumps(database.get_task(task_id))
    data=worker.get('/api/colab-worker/'+job['lease']+'/input').content
    with ZipFile(io.BytesIO(data)) as z:
        assert all(b'gui-cookie-value' not in z.read(n) for n in z.namelist())


def test_incremental_round_trip_and_new_worker_recovery(setup):
    from backend.app.remote_delta import manifest, pack_delta, restore, MANIFEST
    _, worker, root = setup
    task_id = database.create_task('https://www.youtube.com/watch?v=abcdefghijk')
    session = config.WORKFOLDER/'base-session'; session.mkdir(parents=True)
    (session/'video.mp4').write_bytes(b'large-video' * 10000)
    (session/'removed.txt').write_text('old')
    database.update_task(task_id, session_path=str(session))
    job = worker.post('/api/colab-worker/claim').json()['job']
    assert job['transfer_version'] == 1
    prefix = f"/api/colab-worker/{job['lease']}"
    base = manifest({'session':session})
    response = worker.post(prefix+'/input', json=base)
    assert response.status_code == 200
    archive = root/'input.zip'; archive.write_bytes(response.content)
    with ZipFile(archive) as z: assert z.namelist() == [MANIFEST]
    restore(archive, root/'colab', {'session':session})
    remote_session = root/'colab/session'
    (remote_session/'removed.txt').unlink()
    (remote_session/'translated.json').write_text('{"translation":"hello"}')
    archive = root/'output-delta.zip'
    pack_delta(archive, {'session':remote_session}, base)
    worker.put(prefix+'/output', content=archive.read_bytes()).raise_for_status()
    task = job['task']; task['status'] = 'paused'
    task['stages'][0]['status'] = 'succeeded'
    worker.post(prefix+'/finish', json={'task':task}).raise_for_status()
    saved = Path(database.get_task(task_id)['session_path'])
    assert (saved/'video.mp4').read_bytes() == (session/'video.mp4').read_bytes()
    assert not (saved/'removed.txt').exists()
    assert (session/'removed.txt').exists()
    next_job = worker.post('/api/colab-worker/claim').json()['job']
    response = worker.post(f"/api/colab-worker/{next_job['lease']}/input", json={})
    assert response.status_code == 200
    archive.write_bytes(response.content)
    restore(archive, root/'fresh-worker', {})
    assert manifest({'session':root/'fresh-worker/session'}) == manifest({'session':saved})
