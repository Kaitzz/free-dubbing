"""Authenticated pull worker; each lease executes one pipeline stage and commits only its changes."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import threading
import time
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, Body
from fastapi.responses import FileResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool
from . import config, database, runtime_security
from .stages import STAGE_NAMES
from .remote_archive import listing, safe_name, stream_files, unpack, MAX_ARCHIVE_BYTES

router = APIRouter()
lock = threading.RLock()
LEASE_SECONDS = 180
# Bump on incompatible worker protocol changes; mismatches are rejected, never downgraded.
TRANSFER_VERSION = 3
MAX_CHUNK = 64 * 1024 * 1024
MAX_JSON = 4 * 1024 * 1024
_TIMESTAMP = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})')
_log_written: dict[str, int] = {}
_schema_ready: set[str] = set()

def enabled():
    return os.getenv('YOUDUB_EXECUTION_BACKEND', 'local') == 'colab'

def _ensure_schema():
    key = str(Path(database.DB_PATH).absolute())
    if key in _schema_ready:
        return
    with database.connect() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS remote_leases (
            task_id TEXT PRIMARY KEY, token TEXT NOT NULL, expires REAL NOT NULL,
            stage TEXT NOT NULL, snapshot TEXT NOT NULL, state TEXT NOT NULL)""")
        columns = {row['name'] for row in c.execute('PRAGMA table_info(remote_leases)')}
        for column in ('claim_id', 'result'):
            if column not in columns:
                c.execute(f'ALTER TABLE remote_leases ADD COLUMN {column} TEXT')
        # The lease that committed each succeeded stage names its checkpoint on the worker's Drive.
        c.execute("""CREATE TABLE IF NOT EXISTS remote_stage_versions (
            task_id TEXT NOT NULL, stage TEXT NOT NULL, lease TEXT NOT NULL, PRIMARY KEY (task_id, stage))""")
    _schema_ready.add(key)

def forget(task_id):
    _ensure_schema()
    with database.connect() as c:
        c.execute('DELETE FROM remote_stage_versions WHERE task_id=?', (task_id,))

def revoke(task_id):
    """Cancel the task's lease; the worker gets 409 on its next heartbeat and stops the stage."""
    with lock:
        _ensure_schema()
        with database.connect() as c:
            row = c.execute("SELECT * FROM remote_leases WHERE task_id=? AND state IN ('active','committing')",
                            (task_id,)).fetchone()
            if row is None:
                return
            if row['state'] == 'committing':
                raise HTTPException(409, 'A stage result is being saved; try again in a moment.')
            c.execute("UPDATE remote_leases SET state='cancelled' WHERE token=?", (row['token'],))
    _discard_lease_files(task_id, row['token'])

def init():
    _ensure_schema()
    with database.connect() as c:
        # A commit interrupted by a coordinator restart never completed.
        c.execute("UPDATE remote_leases SET state='expired' WHERE state='committing'")
        active = {row['token'] for row in c.execute("SELECT token FROM remote_leases WHERE state='active'")}
    # Transfer scratch from earlier runs; committed data lives in task sessions.
    root = config.DATA_DIR/'remote'
    for folder in list(root.iterdir()) if root.is_dir() else ():
        if folder.name not in active:
            shutil.rmtree(folder, ignore_errors=True)
    remote_root = config.WORKFOLDER/'_remote'
    for staging in list(remote_root.glob('*/.incoming-*')) if remote_root.is_dir() else ():
        if staging.name.removeprefix('.incoming-') not in active:
            shutil.rmtree(staging, ignore_errors=True)

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


def _staging(task_id, token):
    return config.WORKFOLDER/'_remote'/task_id/f'.incoming-{token}'

def _discard_lease_files(task_id, token):
    shutil.rmtree(config.DATA_DIR/'remote'/token, ignore_errors=True)
    shutil.rmtree(_staging(task_id, token), ignore_errors=True)
    _log_written.pop(token, None)

def _log(task_id, message):
    with runtime_security.open_private_append_text(database.log_path(task_id)) as f:
        f.write(f'[{database.now_iso()}] {message}\n')

def _fail_lease(c, row, task_error, stage_error, state):
    # Live progress is provisional; restore the last committed checkpoint.
    snapshot = json.loads(row['snapshot'])
    for s in snapshot['stages']:
        c.execute('UPDATE task_stages SET status=?, progress=? WHERE task_id=? AND name=?',
                  (s['status'], s['progress'], row['task_id'], s['name']))
    c.execute("UPDATE tasks SET status='failed',error_message=?,completed_at=? WHERE id=?",
              (task_error, database.now_iso(), row['task_id']))
    c.execute("UPDATE task_stages SET status='failed',error_message=? WHERE task_id=? AND name=?",
              (stage_error, row['task_id'], row['stage']))
    c.execute('UPDATE remote_leases SET state=? WHERE token=?', (state, row['token']))

def expire():
    _ensure_schema()
    with database.connect() as c:
        rows = c.execute("SELECT * FROM remote_leases WHERE state='active' AND expires<?", (time.time(),)).fetchall()
        for row in rows:
            _fail_lease(c, row, 'Colab disconnected; resume from the last received checkpoint.',
                        'Colab lease expired', 'expired')
    for row in rows:
        _discard_lease_files(row['task_id'], row['token'])

def lease(token):
    with database.connect() as c:
        row = c.execute("SELECT * FROM remote_leases WHERE token=? AND state='active'", (token,)).fetchone()
    if not row or row['expires'] < time.time():
        raise HTTPException(409, 'Lease expired or already completed')
    return dict(row)

def checkpoint_files(task):
    """Committed coordinator files the worker mirrors, keyed by transport name."""
    roots = {'uploads': config.WORKFOLDER/'_uploads'/task['id']}
    if task.get('session_path'):
        session = Path(task['session_path'])
        if not session.resolve().is_relative_to(config.WORKFOLDER.resolve()):
            raise ValueError('Session outside workfolder')
        roots['session'] = session
    # Only the download stage reads the original upload, which can be several GiB.
    downloaded = any(s['name'] == 'download' and s['status'] in {'succeeded', 'skipped'} for s in task['stages'])
    return listing(roots, exclude=('uploads/video/',) if downloaded else ())

def _signatures(files):
    return {name: [size, mtime_ns] for name, (_, size, mtime_ns) in files.items()}

async def _json_body(request, limit):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise HTTPException(413, 'Request too large')
    try:
        data = json.loads(body or b'{}')
    except ValueError:
        raise HTTPException(422, 'Invalid JSON') from None
    if not isinstance(data, dict):
        raise HTTPException(422, 'Invalid JSON')
    return data

@router.get('/api/remote/status')
def status():
    with lock:
        expire()
    last = float(database.get_setting('remote.last_seen', '0'))
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
            if c.execute("SELECT 1 FROM remote_leases WHERE state IN ('active','committing')").fetchone():
                raise HTTPException(409, 'Wait for the active stage before changing the worker key')
        token = payload.get('password') if isinstance(payload, dict) and 'password' in payload else secrets.token_urlsafe(32)
        if isinstance(payload, dict) and 'password' in payload:
            if not isinstance(token, str) or len(token) != 8 or not token.isascii() or not token.isdigit():
                raise HTTPException(422, 'Connection password must be exactly 8 digits')
        database.set_setting('remote.token_hash', hashlib.sha256(token.encode()).hexdigest())
    return {'token':token}

@router.get('/api/colab-worker/hello')
def hello():
    database.set_setting('remote.last_seen', str(time.time()))
    return {'transfer_version': TRANSFER_VERSION}

def _youtube_cookies(task):
    from .youtube import is_youtube_url
    if not is_youtube_url(task['url']):
        return ''
    from scripts.colab_credentials import youtube_cookie_text
    metadata = runtime_security.private_file_stat(config.YOUTUBE_COOKIE_PATH)
    if not metadata or not metadata.st_size:
        return ''
    if metadata.st_size > 1024*1024:
        database.update_task(task['id'],status='failed',error_message='YouTube Cookie exceeds 1 MiB')
        raise HTTPException(422,'YouTube Cookie exceeds 1 MiB')
    try:
        return youtube_cookie_text(config.YOUTUBE_COOKIE_PATH.read_text(encoding='utf-8'))
    except ValueError:
        database.update_task(task['id'],status='failed',error_message='Update the YouTube Cookie in GUI settings (Netscape format)')
        raise HTTPException(422,'Update the YouTube Cookie in GUI settings (Netscape format)') from None

def _job(task, token, stage):
    settings = database.get_openai_settings()
    with database.connect() as c:
        versions = {row['stage']: row['lease'] for row in
                    c.execute('SELECT stage, lease FROM remote_stage_versions WHERE task_id=?', (task['id'],))}
    job = {'lease': token, 'task': task, 'transfer_version': TRANSFER_VERSION,
           'settings': {k: v for k, v in settings.items() if k != 'api_key'},
           'files': _signatures(checkpoint_files(task)), 'stage_leases': versions}
    # Only download jobs receive the latest GUI cookie, never translation keys.
    if stage == 'download' and (cookies := _youtube_cookies(task)):
        job['youtube_cookies'] = cookies
    return job

@router.post('/api/colab-worker/claim')
def claim(payload: dict | None = Body(default=None)):
    if not isinstance(payload, dict) or payload.get('transfer_version') != TRANSFER_VERSION:
        raise HTTPException(426, f'Colab worker code is outdated (transfer protocol {TRANSFER_VERSION} required); '
                                 're-run the launcher notebook')
    claim_id = str(payload.get('claim_id') or '')[:64] or None
    with lock:
        expire()
        database.set_setting('remote.last_seen', str(time.time()))
        with database.connect() as c:
            if claim_id:
                row = c.execute("SELECT * FROM remote_leases WHERE state='active' AND claim_id=?", (claim_id,)).fetchone()
                if row:
                    # The response to this claim was lost; hand out the same lease again.
                    return {'job': _job(json.loads(row['snapshot']), row['token'], row['stage'])}
            c.execute('BEGIN IMMEDIATE')
            if c.execute("SELECT 1 FROM remote_leases WHERE state IN ('active','committing')").fetchone():
                return {'job':None}
            row = c.execute("SELECT id FROM tasks WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if not row:
                return {'job':None}
            task_id = row['id']
            c.execute("UPDATE tasks SET status='running',started_at=COALESCE(started_at,?) WHERE id=?",
                      (database.now_iso(), task_id))
        task = database.get_task(task_id)
        stage = next((s['name'] for s in task['stages'] if s['status'] not in {'succeeded','skipped'}), 'merge_video')
        token = uuid4().hex
        try:
            job = _job(task, token, stage)
        except (ValueError, OSError, RuntimeError) as exc:
            database.update_task(task_id, status='failed', error_message=f'Could not prepare Colab input: {exc}')
            return {'job':None}
        with database.connect() as c:
            c.execute('INSERT OR REPLACE INTO remote_leases (task_id, token, expires, stage, snapshot, state, claim_id, result) '
                      "VALUES (?,?,?,?,?,'active',?,NULL)",
                      (task_id, token, time.time()+LEASE_SECONDS, stage, json.dumps(task), claim_id))
        return {'job':job}


@router.post('/api/colab-worker/{token}/files')
async def download_files(token: str, request: Request):
    names = (await _json_body(request, MAX_JSON)).get('paths')
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise HTTPException(422, 'Invalid file request')
    def select():
        with lock:
            row = lease(token)
        files = checkpoint_files(json.loads(row['snapshot']))
        missing = [name for name in names if name not in files]
        if missing:
            raise HTTPException(409, f'Checkpoint changed since claim: {missing[0]}')
        return [(name, files[name][0]) for name in names]
    return StreamingResponse(stream_files(await run_in_threadpool(select)), media_type='application/zip')


def _log_line(entry):
    stamp = entry.get('t')
    stamp = stamp if isinstance(stamp, str) and _TIMESTAMP.fullmatch(stamp) else database.now_iso()
    text = str(entry.get('m', ''))[:16000]
    return ''.join(f'[{stamp}] [Colab] {line}\n' for line in text.splitlines() or [''])

def _record_heartbeat(token, data):
    entries = data.get('log') or []
    if isinstance(entries, str):
        entries = [{'m': entries}]
    start = data.get('log_start')
    if (not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries)
            or (start is not None and (type(start) is not int or start < 0))):
        raise HTTPException(422, 'Invalid heartbeat')
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
        # A batch is re-sent when its response is lost; skip lines already written.
        written = _log_written.get(token, 0)
        skip = written - start if start is not None and start < written else 0
        lines = ''.join(_log_line(entry) for entry in entries[skip:])
        if lines:
            with runtime_security.open_private_append_text(database.log_path(row['task_id'])) as f:
                f.write(lines)
        if start is not None:
            _log_written[token] = max(written, start + len(entries))

@router.post('/api/colab-worker/{token}/heartbeat')
async def heartbeat(token: str, request: Request):
    data = await _json_body(request, 1024*1024)
    await run_in_threadpool(_record_heartbeat, token, data)
    return {'ok':True}


def _tail_matches(path, offset, body):
    with path.open('rb') as f:
        f.seek(offset)
        return f.read(len(body)) == body

def _append_output(token, offset, body):
    with lock:
        lease(token)
        path = runtime_security.ensure_private_directory(storage()/token)/'output.zip'
        size = path.stat().st_size if path.exists() else 0
        if 0 < offset < size and offset + len(body) == size and _tail_matches(path, offset, body):
            return {'offset': size}  # Retried chunk that was already written.
        if offset != 0 and offset != size:
            raise HTTPException(409, f'Upload offset mismatch: {size}')
        if offset + len(body) > MAX_ARCHIVE_BYTES:
            raise HTTPException(413, 'Archive too large')
        # Offset 0 restarts the upload from the beginning.
        with path.open('wb' if offset == 0 else 'ab') as f:
            f.write(body)
        with database.connect() as c:
            c.execute('UPDATE remote_leases SET expires=? WHERE token=?', (time.time()+LEASE_SECONDS, token))
    return {'offset': offset + len(body)}

@router.put('/api/colab-worker/{token}/output')
async def upload_output(token: str, request: Request, offset: int=0):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_CHUNK:
            raise HTTPException(413,'Chunk too large')
    return await run_in_threadpool(_append_output, token, offset, bytes(body))


def _apply_checkpoint(token, base, files, deleted):
    """Move uploaded files into the task's single session directory."""
    task_id = base['id']
    session = Path(base['session_path']) if base.get('session_path') else config.WORKFOLDER/'_remote'/task_id/'session'
    if not session.resolve().is_relative_to(config.WORKFOLDER.resolve()):
        raise ValueError('Session outside workfolder')
    incoming = {}
    if files:
        archive = config.DATA_DIR/'remote'/token/'output.zip'
        if not archive.is_file():
            raise ValueError('Uploaded checkpoint is missing')
        staging = _staging(task_id, token)
        unpack(archive, staging)
        incoming = {path.relative_to(staging).as_posix(): path for path in staging.rglob('*') if path.is_file()}
        if set(incoming) != set(files) or any(incoming[name].stat().st_size != size for name, size in files.items()):
            raise ValueError('Uploaded checkpoint does not match its file list')
    root = runtime_security.ensure_private_directory(session).resolve()
    def target(name):
        path = session/name.split('/', 1)[1]
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError('Unsafe checkpoint path')
        return path
    for name in deleted:
        path = target(name)
        if path.is_file():
            path.unlink()
    for name, source in incoming.items():
        path = target(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, path)
    return session

def _finish(token, remote, files, deleted, ran, report):
    with lock:
        with database.connect() as c:
            row = c.execute('SELECT * FROM remote_leases WHERE token=?', (token,)).fetchone()
            if row is not None and row['state'] == 'finished' and row['result']:
                return json.loads(row['result'])  # Retried after a lost response.
            if row is not None and row['state'] == 'committing':
                raise HTTPException(409, 'Checkpoint commit in progress')
            if row is None or row['state'] != 'active' or row['expires'] < time.time():
                raise HTTPException(409, 'Lease expired or already completed')
            c.execute("UPDATE remote_leases SET state='committing' WHERE token=?", (token,))
    row = dict(row)
    task_id = row['task_id']
    started = time.monotonic()
    try:
        session = _apply_checkpoint(token, json.loads(row['snapshot']), files, deleted)
        final = session/'media/video_final.mp4'
        if remote['status'] == 'succeeded' and not final.is_file():
            raise ValueError('Missing final video')
        with lock:
            original = database.get_task(task_id)
            next_status = remote['status']
            if next_status == 'paused' and original['execution_mode'] == 'auto':
                next_status = 'queued'
            stage_fields = ('status','progress','started_at','completed_at','last_message','error_message')
            with database.connect() as c:
                for s in remote['stages']:
                    c.execute('UPDATE task_stages SET '+','.join(k+'=?' for k in stage_fields)+' WHERE task_id=? AND name=?',
                              [*(s.get(k) for k in stage_fields), task_id, s['name']])
                    # Includes stages the worker re-ran because their outputs were lost.
                    if s['name'] in ran and s['status'] == 'succeeded':
                        c.execute('INSERT OR REPLACE INTO remote_stage_versions VALUES (?,?,?)', (task_id, s['name'], token))
                c.execute('UPDATE tasks SET status=?,current_stage=?,session_path=?,title=?,final_video_path=?,error_message=?,completed_at=? WHERE id=?',
                          (next_status, remote.get('current_stage'), str(session), remote.get('title') or original.get('title'),
                           str(final) if remote['status'] == 'succeeded' else None, remote.get('error_message'),
                           database.now_iso() if next_status in {'succeeded','failed'} else None, task_id))
            result = {'status': next_status, 'files': _signatures(checkpoint_files(database.get_task(task_id)))}
            with database.connect() as c:
                c.execute("UPDATE remote_leases SET state='finished', result=? WHERE token=?", (json.dumps(result), token))
    except Exception as exc:
        # Never leave a lease in 'committing': expiry would not recover it.
        detail = f'Checkpoint commit failed: {str(exc).strip() or type(exc).__name__}'
        with lock, database.connect() as c:
            _fail_lease(c, row, detail, detail, 'failed')
        _discard_lease_files(task_id, token)
        _log(task_id, f'[transfer] {detail}')
        raise HTTPException(422, detail) from exc
    _discard_lease_files(task_id, token)
    size = sum(files.values()) / 2**20
    _log(task_id, f"[transfer] {report + '; ' if report else ''}committed {len(files)} files ({size:.1f} MiB), "
                  f'{len(deleted)} deleted in {time.monotonic()-started:.1f}s')
    return result

@router.post('/api/colab-worker/{token}/finish')
async def finish(token: str, request: Request):
    data = await _json_body(request, MAX_JSON)
    remote = data.get('task')
    if not isinstance(remote, dict) or remote.get('status') not in {'paused','succeeded','failed'}:
        raise HTTPException(422,'Invalid task status')
    stages = remote.get('stages')
    if (not isinstance(stages, list) or len(stages)!=len(STAGE_NAMES) or not all(isinstance(s, dict) for s in stages)
            or {s.get('name') for s in stages}!=set(STAGE_NAMES)
            or any(s.get('status') not in {'pending','running','succeeded','skipped','failed'} for s in stages)):
        raise HTTPException(422,'Invalid stage snapshot')
    files, deleted, ran = data.get('files', {}), data.get('deleted', []), data.get('ran', [])
    try:
        if (not isinstance(files, dict) or not isinstance(deleted, list)
                or not isinstance(ran, list) or not set(ran) <= set(STAGE_NAMES)
                or any(type(size) is not int or size < 0 for size in files.values())
                or not all(safe_name(name).startswith('session/') for name in [*files, *deleted])):
            raise ValueError
    except (ValueError, TypeError):
        raise HTTPException(422, 'Invalid checkpoint file list') from None
    report = ' '.join(str(data.get('report', '')).split())[:2000]
    return await run_in_threadpool(_finish, token, remote, files, deleted, ran, report)


@router.get('/api/remote/files/{kind}')
def worker_files(kind: str):
    private_launcher=config.DATA_DIR/'gui/Dubbing_Launcher.ipynb'
    paths={'notebook':private_launcher if private_launcher.is_file() else config.REPO_ROOT/'notebooks/Dubbing_Launcher.ipynb',
           'bundle':config.REPO_ROOT/'youdub-colab-worker.zip'}
    path=paths.get(kind)
    if path is None or not path.is_file():raise HTTPException(404,'File not ready')
    return FileResponse(path,filename=path.name)
