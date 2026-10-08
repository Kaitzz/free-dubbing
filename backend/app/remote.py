"""Authenticated pull worker; each lease executes and commits one pipeline stage."""
from __future__ import annotations
import hashlib
import os
from pathlib import Path
import secrets
import threading
import time
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, Body
from fastapi.responses import FileResponse
from . import config, database, runtime_security
from .stages import STAGE_NAMES
from .remote_archive import pack, unpack, rebase_local_info, MAX_ARCHIVE_BYTES

router = APIRouter()
lock = threading.RLock()
LEASE_SECONDS = 180

def enabled():
    return os.getenv('YOUDUB_EXECUTION_BACKEND', 'local') == 'colab'

def init():
    with database.connect() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS remote_leases (
            task_id TEXT PRIMARY KEY, token TEXT NOT NULL, expires REAL NOT NULL,
            stage TEXT NOT NULL, snapshot TEXT NOT NULL, state TEXT NOT NULL)""")

def storage():
    return runtime_security.ensure_private_directory(config.DATA_DIR/'remote')

_failed_auth = []


def authenticate(token):
    # Bound online guessing of the user-selected short password.
    with lock:
        now = time.monotonic()
        _failed_auth[:] = [t for t in _failed_auth if now-t < 60]
        if len(_failed_auth) >= 10:
            return False
        expected = database.get_setting('remote.token_hash')
        accepted = enabled() and bool(expected) and secrets.compare_digest(
            hashlib.sha256(token.encode()).hexdigest(), expected)
        if not accepted:
            _failed_auth.append(now)
        return accepted


def expire():
    init()
    with database.connect() as c:
        rows = c.execute("SELECT * FROM remote_leases WHERE state='active' AND expires<?", (time.time(),)).fetchall()
        for row in rows:
            # Live progress is provisional; restore the last committed checkpoint.
            import json
            snapshot = json.loads(row['snapshot'])
            for s in snapshot['stages']:
                c.execute('UPDATE task_stages SET status=?, progress=? WHERE task_id=? AND name=?',
                          (s['status'], s['progress'], row['task_id'], s['name']))
            c.execute("UPDATE tasks SET status='failed',error_message=?,completed_at=? WHERE id=?",
                      ('Colab disconnected; resume from the last received checkpoint.', database.now_iso(), row['task_id']))
            c.execute("UPDATE task_stages SET status='failed',error_message=? WHERE task_id=? AND name=?",
                      ('Colab lease expired', row['task_id'], row['stage']))
            c.execute("UPDATE remote_leases SET state='expired' WHERE task_id=?", (row['task_id'],))

def lease(token):
    with database.connect() as c:
        row = c.execute("SELECT * FROM remote_leases WHERE token=? AND state='active'", (token,)).fetchone()
    if not row or row['expires'] < time.time():
        raise HTTPException(409, 'Lease expired or already completed')
    return dict(row)

@router.get('/api/remote/status')
def status():
    with lock:
        expire()
    last = float(database.get_setting('remote.last_seen', '0'))
    import json
    connection=config.DATA_DIR/'gui/connection.json'
    tunnel=json.loads(connection.read_text(encoding='utf-8')).get('tunnel','') if connection.exists() else ''
    return {'tunnel_url':tunnel, 'enabled': enabled(), 'connected': time.time()-last < 40,
            'paired': bool(database.get_setting('remote.token_hash'))}

@router.post('/api/remote/token')
def new_token(payload: dict | None = Body(default=None)):
    if not enabled():
        raise HTTPException(409, 'Start the server in Colab mode first')
    with lock:
        expire()
        with database.connect() as c:
            if c.execute("SELECT 1 FROM remote_leases WHERE state='active'").fetchone():
                raise HTTPException(409, 'Wait for the active stage before changing the worker key')
        token = payload.get('password') if isinstance(payload, dict) and 'password' in payload else secrets.token_urlsafe(32)
        if isinstance(payload, dict) and 'password' in payload:
            if not isinstance(token, str) or len(token) != 8 or not token.isascii() or not token.isdigit():
                raise HTTPException(422, 'Connection password must be exactly 8 digits')
        database.set_setting('remote.token_hash', hashlib.sha256(token.encode()).hexdigest())
    return {'token':token}

@router.post('/api/colab-worker/claim')
def claim():
    import json
    with lock:
        expire()
        database.set_setting('remote.last_seen', str(time.time()))
        with database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if c.execute("SELECT 1 FROM remote_leases WHERE state='active'").fetchone():
                return {'job':None}
            row = c.execute("SELECT id FROM tasks WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if not row:
                return {'job':None}
            task_id = row['id']
            c.execute("UPDATE tasks SET status='running',started_at=COALESCE(started_at,?) WHERE id=?",
                      (database.now_iso(), task_id))
        task = database.get_task(task_id)
        stage = next((s['name'] for s in task['stages'] if s['status'] not in {'succeeded','skipped'}), 'merge_video')
        cookie_settings = {}
        if stage == 'download':
            from .youtube import is_youtube_url
            if is_youtube_url(task['url']):
                from scripts.colab_credentials import youtube_cookie_text
                cookie_path = config.YOUTUBE_COOKIE_PATH
                metadata = runtime_security.private_file_stat(cookie_path)
                if metadata and metadata.st_size:
                    if metadata.st_size > 1024*1024:
                        database.update_task(task_id,status='failed',error_message='YouTube Cookie exceeds 1 MiB')
                        raise HTTPException(422,'YouTube Cookie exceeds 1 MiB')
                    try:
                        cookie_settings['youtube_cookies'] = youtube_cookie_text(cookie_path.read_text(encoding='utf-8'))
                    except ValueError:
                        database.update_task(task_id,status='failed',error_message='Update the YouTube Cookie in GUI settings (Netscape format)')
                        raise HTTPException(422,'Update the YouTube Cookie in GUI settings (Netscape format)') from None
        token = uuid4().hex
        try:
            folder = runtime_security.ensure_private_directory(storage()/token)
            session = Path(task['session_path']) if task.get('session_path') else None
            if session and not session.resolve().is_relative_to(config.WORKFOLDER.resolve()):
                raise ValueError('Session outside workfolder')
            pack(folder/'input.zip', {'session':session,
                 'uploads':config.WORKFOLDER/'_uploads'/task_id})
            with database.connect() as c:
                c.execute('INSERT OR REPLACE INTO remote_leases VALUES (?,?,?,?,?,?)',
                          (task_id, token, time.time()+LEASE_SECONDS, stage, json.dumps(task), 'active'))
        except Exception:
            database.update_task(task_id, status='failed', error_message='Could not prepare Colab input archive')
            raise
        # Only download jobs receive the latest GUI cookie, never translation keys.
        settings=database.get_openai_settings()
        return {'job':{'lease':token, 'task':task, 'settings':{k:v for k,v in settings.items() if k!='api_key'},
                       **cookie_settings}}


@router.get('/api/colab-worker/{token}/input')
def download_input(token: str):
    with lock:
        lease(token)
        return FileResponse(storage()/token/'input.zip', media_type='application/zip')

@router.post('/api/colab-worker/{token}/heartbeat')
async def heartbeat(token: str, request: Request):
    body = await request.body()
    if len(body)>32768:
        raise HTTPException(413, 'Progress payload too large')
    import json
    data = json.loads(body or b'{}')
    with lock:
        row = lease(token)
        database.set_setting('remote.last_seen',str(time.time()))
        with database.connect() as c:
            c.execute('UPDATE remote_leases SET expires=? WHERE token=?',(time.time()+LEASE_SECONDS, token))
        stage = data.get('stage')
        if stage in STAGE_NAMES:
            progress = data.get('progress')
            if progress is not None:
                progress = max(0,min(100,int(progress)))
            database.update_task(row['task_id'],current_stage=stage)
            database.update_stage(row['task_id'],stage,status='running',progress=progress,
                                  last_message=str(data.get('message',''))[:1000])
        message = str(data.get('log',''))[:16000]
        if message:
            with runtime_security.open_private_append_text(database.log_path(row['task_id'])) as f:
                f.write(f'[{database.now_iso()}] [Colab] {message}\n')
    return {'ok':True}

@router.put('/api/colab-worker/{token}/output')
async def upload_output(token: str, request: Request, offset: int=0):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body)>8*1024*1024:
            raise HTTPException(413,'Chunk too large')
    with lock:
        lease(token)
        path=storage()/token/'output.zip'
        size=path.stat().st_size if path.exists() else 0
        if offset<0 or offset!=size:
            raise HTTPException(409,f'Upload offset mismatch: {size}')
        if size+len(body)>MAX_ARCHIVE_BYTES:
            raise HTTPException(413,'Archive too large')
        with path.open('ab') as f:f.write(body)
        with database.connect() as c:
            c.execute('UPDATE remote_leases SET expires=? WHERE token=?',(time.time()+LEASE_SECONDS,token))
    return {'offset':size+len(body)}

@router.post('/api/colab-worker/{token}/finish')
async def finish(token: str, request: Request):
    import json
    raw=await request.body()
    if len(raw)>65536:raise HTTPException(413,'Completion payload too large')
    data=json.loads(raw)
    remote=data.get('task',{})
    if remote.get('status') not in {'paused','succeeded','failed'}:
        raise HTTPException(422,'Invalid task status')
    stages=remote.get('stages',[])
    if (len(stages)!=len(STAGE_NAMES) or {s.get('name') for s in stages}!=set(STAGE_NAMES)
        or any(s.get('status') not in {'pending','running','succeeded','skipped','failed'} for s in stages)):
        raise HTTPException(422,'Invalid stage snapshot')
    with lock:
        row=lease(token)
        destination=runtime_security.ensure_private_directory(config.WORKFOLDER/'_remote'/row['task_id']/token)
        try:
            unpack(storage()/token/'output.zip',destination)
            session=destination/'session'
            rebase_local_info(session,config.WORKFOLDER/'_uploads'/row['task_id'])
            final=session/'media/video_final.mp4'
            if remote['status']=='succeeded' and not final.is_file():
                raise ValueError('Missing final video')
        except (ValueError, OSError) as exc:
            raise HTTPException(422,str(exc)) from exc
        original=database.get_task(row['task_id'])
        next_status=remote['status']
        if next_status=='paused' and original['execution_mode']=='auto':next_status='queued'
        stage_fields=('status','progress','started_at','completed_at','last_message','error_message')
        with database.connect() as c:
            for s in stages:
                values=[s.get(k) for k in stage_fields]
                c.execute('UPDATE task_stages SET '+','.join(k+'=?' for k in stage_fields)+' WHERE task_id=? AND name=?',
                          [*values,row['task_id'],s['name']])
            c.execute('UPDATE tasks SET status=?,current_stage=?,session_path=?,title=?,final_video_path=?,error_message=?,completed_at=? WHERE id=?',
                      (next_status,remote.get('current_stage'),str(session) if session.exists() else original.get('session_path'),
                       remote.get('title') or original.get('title'),str(final) if remote['status']=='succeeded' else None,
                       remote.get('error_message'),database.now_iso() if next_status in {'succeeded','failed'} else None,row['task_id']))
            c.execute("UPDATE remote_leases SET state='finished' WHERE token=?",(token,))
    return {'status':next_status}


@router.get('/api/remote/files/{kind}')
def worker_files(kind: str):
    private_launcher=config.DATA_DIR/'gui/Dubbing_Launcher.ipynb'
    paths={'notebook':private_launcher if private_launcher.is_file() else config.REPO_ROOT/'notebooks/Dubbing_Launcher.ipynb',
           'bundle':config.REPO_ROOT/'youdub-colab-worker.zip'}
    path=paths.get(kind)
    if path is None or not path.is_file():raise HTTPException(404,'File not ready')
    return FileResponse(path,filename=path.name)
