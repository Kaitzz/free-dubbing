"""Interactive Colab pull worker. No listening server or tunnel runs on Colab."""
from __future__ import annotations
import argparse
from collections import deque
from datetime import datetime, timezone
import json
import os
import re
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from urllib.parse import urlparse
from uuid import uuid4

import httpx
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.app.remote_archive import pack_files, rebase_local_info, safe_name, unpack
from backend.app.youtube import is_youtube_url
from scripts.colab_credentials import write_youtube_cookie

ROOT=Path(__file__).resolve().parents[1]
TRANSFER_VERSION = 2
CHUNK = 64 * 1024 * 1024  # Below Cloudflare's 100 MB request limit.
KEEP_WORKSPACES = 3

def model_cache_path():
    # Resolve the intentionally shared cache before the runtime checks its root.
    return str(Path(os.environ.get('MODEL_CACHE_DIR', str(ROOT/'model-cache'))).expanduser().resolve())


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def snapshot(job_dir, original, exitcode=None):
    path=job_dir/'data/youdub.sqlite'
    if not path.exists():
        return {**original,'status':'failed','error_message':f'Worker process exited {exitcode} before initializing'}
    with sqlite3.connect(path) as c:
        c.row_factory=sqlite3.Row
        row=c.execute('SELECT * FROM tasks WHERE id=?',(original['id'],)).fetchone()
        if row is None:return {**original,'status':'failed','error_message':'Worker task initialization failed'}
        task=dict(row)
        task['stages']=[dict(s) for s in c.execute('SELECT * FROM task_stages WHERE task_id=?',(original['id'],))]
    if exitcode is not None and task['status'] in {'queued','running'}:
        task['status']='failed'
        task['error_message']=f'Worker process exited {exitcode}; last checkpoint preserved'
        for stage in task['stages']:
            if stage['name']==task.get('current_stage'):stage['status']='failed'
    return task

def console_line(line):
    """Keep phase messages and warnings; detailed output stays in the local log."""
    value = line.strip()
    if (not value or ('%' in value and '|' in value)
            or re.match(r'^\[download\]\s+\d+(?:\.\d+)?%', value)):
        return False
    return (value.startswith(('Task ', 'Device plan:', '['))
            or any(word in value.lower() for word in ('warning', 'error', 'failed', 'traceback')))


def _write_if_changed(path, text):
    # An unchanged rewrite would still look like stage output to the transfer.
    if not path.is_file() or path.read_text(encoding='utf-8') != text:
        path.write_text(text, encoding='utf-8')


def export_transcript(session):
    """Human-readable copies alongside the canonical ASR/CC JSON artifacts."""
    metadata = session / "metadata"
    source = next((metadata/name for name in ("asr_fixed.json", "asr.json", "source_subtitles.json")
                   if (metadata/name).is_file()), None)
    if source is None:
        return
    try:
        from backend.app.adapters.ffmpeg import _srt_time
        rows = json.loads(source.read_text(encoding="utf-8"))["result"]["utterances"]
        text = "\n".join(row["text"] for row in rows) + "\n"
        srt = "\n\n".join(f"{index}\n{_srt_time(int(row['start_time']))} --> {_srt_time(int(row['end_time']))}\n{row['text']}"
                             for index, row in enumerate(rows, 1)) + "\n"
        _write_if_changed(metadata / "transcript.txt", text)
        _write_if_changed(metadata / "transcript.srt", srt)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print(f"[files] Transcript export unavailable ({type(exc).__name__}); canonical JSON retained", flush=True)


class PendingLogs:
    """Timestamped lines; the running index lets the coordinator drop re-sent batches."""
    BATCH = 100

    def __init__(self):
        self.lines = deque()
        self.sent = 0
        self.lock = threading.Lock()
        self.send_lock = threading.Lock()

    def add(self, line):
        with self.lock:
            self.lines.append({'t': now_iso(), 'm': line.strip()[:1800]})

    def send(self, client, prefix, data):
        # Serialize sends through acknowledgement; producers can still append.
        with self.send_lock:
            with self.lock:
                batch = list(self.lines)[:self.BATCH]
                start = self.sent
            response = client.post(prefix+'/heartbeat',
                                   json={**data, 'log_start': start, 'log': batch}, timeout=20)
            response.raise_for_status()
            with self.lock:
                for _ in batch:
                    self.lines.popleft()
                self.sent += len(batch)
            return response


class Workspace:
    """Per-task files kept across leases on Colab disk.

    state.json maps each file to the committed coordinator version it mirrors
    (size, mtime) and to its own stat when synced, so stale or locally modified
    files are detected without reading their contents.
    """
    def __init__(self, root, task_id):
        self.root = root
        self.work = root/'workfolder'
        self.session = self.work/'session'
        self.uploads = self.work/'_uploads'/task_id
        self.state_file = root/'state.json'
        try:
            self.files = json.loads(self.state_file.read_text(encoding='utf-8'))['files']
        except (OSError, ValueError, KeyError):
            self.files = {}

    def path(self, name):
        prefix, relative = safe_name(name).split('/', 1)
        return (self.session if prefix == 'session' else self.uploads)/relative

    def scan(self, prefixes=('session', 'uploads')):
        found = {}
        for prefix in prefixes:
            base = self.session if prefix == 'session' else self.uploads
            for path in base.rglob('*') if base.is_dir() else ():
                if path.is_symlink():
                    raise ValueError('Cannot transport symlinks')
                if path.is_file():
                    metadata = path.stat()
                    found[f'{prefix}/{path.relative_to(base).as_posix()}'] = [
                        metadata.st_size, metadata.st_mtime_ns, metadata.st_ino]
        return found

    def record(self, name, remote):
        metadata = self.path(name).stat()
        self.files[name] = {'remote': list(remote), 'local': [metadata.st_size, metadata.st_mtime_ns, metadata.st_ino]}

    def save(self):
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.state_file.with_suffix('.tmp')
        temporary.write_text(json.dumps({'files': self.files}), encoding='utf-8')
        os.replace(temporary, self.state_file)


def call(client, method, url, attempts=6, **kwargs):
    """Retry network errors and 5xx/429 responses; other 4xx responses raise at once."""
    delay = 2
    for attempt in range(attempts):
        try:
            response = client.request(method, url, **kwargs)
            if response.status_code < 500 and response.status_code != 429:
                response.raise_for_status()
                return response
            failure = f'HTTP {response.status_code}'
        except httpx.TransportError as exc:
            failure = type(exc).__name__
        if attempt + 1 == attempts:
            raise RuntimeError(f'{method} {url} failed after {attempts} attempts ({failure})')
        print(f'[transfer] {method} {url.rsplit("/", 1)[-1]} failed ({failure}); retry in {delay}s', flush=True)
        time.sleep(delay)
        delay = min(delay * 2, 30)


class _ServerError(Exception):
    pass


def download(client, prefix, names, archive):
    for attempt in range(4):
        try:
            with client.stream('POST', prefix+'/files', json={'paths': names}, timeout=httpx.Timeout(120, connect=30)) as response:
                if response.status_code >= 500:
                    raise _ServerError(f'HTTP {response.status_code}')
                response.raise_for_status()
                with archive.open('wb') as f:
                    for chunk in response.iter_bytes(1024*1024):
                        f.write(chunk)
            return
        except (httpx.TransportError, _ServerError) as exc:
            if attempt == 3:
                raise RuntimeError(f'Input download failed ({exc})') from exc
            print(f'[transfer] Input download interrupted ({type(exc).__name__}); retrying', flush=True)
            time.sleep(5 * (attempt + 1))


def sync_input(client, prefix, workspace, remote, lease_dir):
    """Mirror the coordinator's committed files; download only missing or stale ones."""
    local = workspace.scan()
    keep = {name for name, signature in remote.items()
            if (record := workspace.files.get(name)) and record['remote'] == signature
            and local.get(name) == record['local']}
    # Everything else here is uncommitted output, out of date, or no longer needed.
    for name in local:
        if name not in keep:
            workspace.path(name).unlink()
    workspace.files = {name: workspace.files[name] for name in keep}
    for entry in list(workspace.work.iterdir()) if workspace.work.is_dir() else ():
        if entry not in (workspace.session, workspace.uploads.parent):
            shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
    needed = sorted(name for name in remote if name not in keep)
    if needed:
        archive, staging = lease_dir/'input.zip', lease_dir/'incoming'
        download(client, prefix, needed, archive)
        unpack(archive, staging)
        for name in needed:
            source = staging/name
            if not source.is_file() or source.stat().st_size != remote[name][0]:
                raise ValueError(f'Downloaded checkpoint is incomplete: {name}')
            target = workspace.path(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
        if 'session/metadata/local_info.json' in needed:
            rebase_local_info(workspace.session, workspace.uploads)
        for name in needed:
            workspace.record(name, remote[name])
        archive.unlink()
        shutil.rmtree(staging)
    workspace.save()
    return len(keep), needed


def adopt_session(workspace, task):
    """The download stage names its folder after the video; leases always run in work/session."""
    produced = Path(task['session_path']) if task.get('session_path') else None
    if not produced or not produced.is_dir() or produced.resolve() == workspace.session.resolve():
        return
    if not produced.resolve().is_relative_to(workspace.work.resolve()):
        raise ValueError('Invalid worker session')
    if workspace.session.exists():
        if any(path.is_file() for path in workspace.session.rglob('*')):
            raise ValueError('Worker session already holds files')
        shutil.rmtree(workspace.session)
    produced.rename(workspace.session)


def upload(client, prefix, archive):
    size = archive.stat().st_size
    offset, restarted = 0, False
    with archive.open('rb') as f:
        while offset < size:
            f.seek(offset)
            block = f.read(CHUNK)
            try:
                response = call(client, 'PUT', prefix+'/output', params={'offset': offset}, content=block, timeout=600)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 409 or restarted:
                    raise
                offset, restarted = 0, True  # The coordinator's partial copy diverged; start over.
                continue
            offset = response.json()['offset']


def commit(client, prefix, payload):
    """Finish is idempotent on the coordinator, so retry until it answers or the lease is gone."""
    deadline = time.monotonic() + 300
    delay = 3
    while True:
        try:
            response = client.post(prefix+'/finish', json=payload, timeout=900)
            busy = response.status_code == 409 and 'in progress' in response.text
            if response.status_code < 500 and not busy:
                response.raise_for_status()
                return response.json()
            failure = 'commit in progress' if busy else f'HTTP {response.status_code}'
        except httpx.TransportError as exc:
            failure = type(exc).__name__
        if time.monotonic() > deadline:
            raise RuntimeError(f'Checkpoint commit did not complete ({failure})')
        print(f'[transfer] Commit pending ({failure}); retry in {delay}s', flush=True)
        time.sleep(delay)
        delay = min(delay * 2, 30)


def run_job(client, job):
    started=time.monotonic()
    token=job['lease']; original=job['task']
    if job.get('transfer_version') != TRANSFER_VERSION:
        raise RuntimeError('Coordinator returned an incompatible job; restart the local GUI services')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', original['id']):
        raise ValueError('Unexpected task id')
    stage_name = next((s['name'] for s in original['stages'] if s['status'] not in {'succeeded','skipped'}), 'merge_video')
    prefix=f'/api/colab-worker/{token}'
    workspace = Workspace(ROOT/'remote-runs'/'tasks'/original['id'], original['id'])
    lease_dir = workspace.root/'leases'/token
    lease_dir.mkdir(parents=True,exist_ok=False)
    stopped=threading.Event(); lost=threading.Event()
    pending=PendingLogs()
    def say(message):
        print(message, flush=True)
        pending.add(message)
    def heartbeat():
        last_success=time.monotonic()
        last_progress=None
        while not stopped.wait(10):
            try:
                task=snapshot(lease_dir,original)
                stage=next((s for s in task.get('stages',[]) if s['status']=='running'),{})
                data={'stage':stage.get('name'),'progress':stage.get('progress'),
                      'message':stage.get('last_message') or 'Colab processing'}
                progress=(data['stage'],data['progress'],data['message'])
                if data['stage'] and progress!=last_progress:
                    pending.add(f"[{data['stage']}] {data['progress']}% — {data['message']}")
                    last_progress=progress
                pending.send(client,prefix,data)
                last_success=time.monotonic()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in {401,409}:lost.set();return
                if time.monotonic()-last_success>150:lost.set();return
            except (httpx.HTTPError,sqlite3.Error):
                if time.monotonic()-last_success>150:
                    lost.set();return # Lease expiry prevents a late worker from committing results.
    thread=threading.Thread(target=heartbeat,daemon=True);thread.start()
    process=None
    cookie_file=None
    try:
        reused, needed = sync_input(client, prefix, workspace, job['files'], lease_dir)
        input_mb = sum(job['files'][name][0] for name in needed) / 2**20
        input_done=time.monotonic()
        say(f'[transfer] Input: {len(needed)} files ({input_mb:.1f} MiB) downloaded, {reused} reused, {input_done-started:.1f}s')
        (lease_dir/'task.json').write_text(json.dumps(original),encoding='utf-8')
        settings=job['settings']
        env=os.environ.copy()
        env.update(YOUDUB_DATA_DIR=str(lease_dir/'data'),WORKFOLDER=str(workspace.work),
            YOUDUB_EXECUTION_BACKEND='local',MODEL_CACHE_DIR=model_cache_path(),
            DEVICE='cuda',FUNASR_DEVICE='cuda:0',DEMUCS_DEVICE='cuda',DEMUCS_CHUNK_SECONDS=os.getenv('DEMUCS_CHUNK_SECONDS','180'),
            DUBBING_VIDEO_ENCODER=os.getenv('DUBBING_VIDEO_ENCODER','auto'),
            DUBBING_TTS_PROVIDER=os.getenv('DUBBING_TTS_PROVIDER','voxcpm'),
            MINIMAX_TTS_MODEL=os.getenv('MINIMAX_TTS_MODEL','speech-2.8-turbo'),
            MINIMAX_TTS_VOICE=os.getenv('MINIMAX_TTS_VOICE','Chinese_casual_instructor_nv1'),
            VOXCPM_REFERENCE_MODE='fixed',
            VOXCPM_LOW_MEMORY_INIT='true',VOXCPM_OPTIMIZE='false',VOXCPM_LOAD_DENOISER='false',
            OPENAI_API_KEY=os.environ['OPENAI_API_KEY'],OPENAI_BASE_URL=settings['base_url'],
            OPENAI_MODEL=settings['model'],OPENAI_TRANSLATE_CONCURRENCY=settings['translate_concurrency'] or '2')
        env.pop('YOUDUB_WORKER_TOKEN',None)
        cookie_value=job.get('youtube_cookies') or env.pop('YOUTUBE_COOKIES','')
        env.pop('YOUTUBE_COOKIES',None)
        if is_youtube_url(original['url']):
            cookie_file=write_youtube_cookie(cookie_value,lease_dir/'data')
        del cookie_value
        if lost.is_set():raise RuntimeError('Lease lost during input transfer')
        before = workspace.scan(('session',))
        print(f"Task {original['id']}: running {stage_name}",flush=True)
        process=subprocess.Popen([sys.executable,'-u',str(ROOT/'scripts/remote_job.py'),str(lease_dir)],
            env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
        # Read output on a background thread so loss of lease can stop inference.
        tail=deque(maxlen=60)
        def relay():
            with (lease_dir/'worker.log').open('w',encoding='utf-8') as log:
                for line in process.stdout:
                    log.write(line)
                    tail.append(line)
                    if not console_line(line):continue
                    print(line,end='',flush=True)
                    pending.add(line)
        reader=threading.Thread(target=relay,daemon=True);reader.start()
        while process.poll() is None:
            if lost.wait(1):
                process.terminate();break
        code=process.wait();reader.join(timeout=7)
        stage_done=time.monotonic()
        if code:
            print(''.join(tail),flush=True)
        say(f"[worker] Stage process: {stage_done-input_done:.1f}s, exit code {code}; full log: {lease_dir/'worker.log'}")
        if lost.is_set():raise RuntimeError('Coordinator rejected lease; stopped this worker')
        task=snapshot(lease_dir,original,code)
        adopt_session(workspace, task)
        export_transcript(workspace.session)
        if (workspace.session/'metadata').is_dir():
            print(f"[files] Subtitles/transcripts saved in: {workspace.session/'metadata'}",flush=True)
        after = workspace.scan(('session',))
        changed = sorted(name for name, signature in after.items() if before.get(name) != signature)
        deleted = sorted(name for name in before if name not in after)
        output_mb = sum(after[name][0] for name in changed) / 2**20
        archive = lease_dir/'output.zip'
        if changed:
            pack_files(archive, [(name, workspace.path(name)) for name in changed])
            upload(client, prefix, archive)
        upload_done = time.monotonic()
        rate = f', {output_mb/(upload_done-stage_done):.1f} MiB/s' if changed and upload_done > stage_done else ''
        say(f'[transfer] Output: {len(changed)} files ({output_mb:.1f} MiB), {len(deleted)} deleted, '
            f'packed and uploaded in {upload_done-stage_done:.1f}s{rate}')
        try:
            while pending.lines:
                pending.send(client,prefix,{})
        except httpx.HTTPError:
            pass
        stopped.set();thread.join(timeout=25)
        report = (f'stage {stage_name}: input {len(needed)} files ({input_mb:.1f} MiB) {input_done-started:.1f}s; '
                  f'process {stage_done-input_done:.1f}s; output {len(changed)} files ({output_mb:.1f} MiB) '
                  f'{upload_done-stage_done:.1f}s{rate}')
        result = commit(client, prefix, {'task': task, 'files': {name: after[name][0] for name in changed},
                                         'deleted': deleted, 'report': report})
        committed = result.get('files', {})
        for name in deleted:
            workspace.files.pop(name, None)
        for name in changed:
            if name in committed:
                workspace.record(name, committed[name])
            else:
                workspace.files.pop(name, None)
        workspace.save()
        archive.unlink(missing_ok=True)
        print('Checkpoint returned:',result['status'],flush=True)
        print(f"[transfer] Commit {time.monotonic()-upload_done:.1f}s; stage round trip {time.monotonic()-started:.1f}s",flush=True)
    finally:
        if process and process.poll() is None:
            process.terminate()
            try:process.wait(timeout=20)
            except subprocess.TimeoutExpired:process.kill();process.wait()
        if cookie_file is not None:cookie_file.unlink(missing_ok=True)
        stopped.set();thread.join(timeout=35)


def prune_workspaces(current=None):
    tasks = ROOT/'remote-runs'/'tasks'
    if not tasks.is_dir():
        return
    def last_used(folder):
        state = folder/'state.json'
        return state.stat().st_mtime if state.exists() else 0
    folders = sorted((p for p in tasks.iterdir() if p.is_dir()), key=last_used, reverse=True)
    for folder in folders[KEEP_WORKSPACES:]:
        if folder != current:
            shutil.rmtree(folder, ignore_errors=True)


def coordinator_version(client):
    response = client.get('/api/colab-worker/hello', timeout=30)
    if response.status_code == 404:
        return None  # Local services predate the hello endpoint.
    response.raise_for_status()
    return response.json().get('transfer_version')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',required=True)
    parser.add_argument('--minutes',type=int,default=480)
    args=parser.parse_args()
    parsed=urlparse(args.url)
    if parsed.scheme!='https' or parsed.username or parsed.password or parsed.query or parsed.fragment:
        parser.error('Use the HTTPS tunnel origin, without credentials or query parameters')
    token=os.environ.get('YOUDUB_WORKER_TOKEN','')
    if not token:parser.error('Missing YOUDUB_WORKER_TOKEN')
    if not os.environ.get('OPENAI_API_KEY'):parser.error('Set the MiniMax key in Colab first')
    # Per-lease folders from the previous transfer format are never reused.
    runs=ROOT/'remote-runs'
    for folder in list(runs.iterdir()) if runs.is_dir() else ():
        if re.fullmatch('[a-f0-9]{32}', folder.name):shutil.rmtree(folder,ignore_errors=True)
    with httpx.Client(base_url=args.url.rstrip('/'),headers={'Authorization':'Bearer '+token},
                      timeout=httpx.Timeout(120, connect=30),follow_redirects=False) as client:
        until=time.monotonic()+args.minutes*60
        print('Colab worker connected; create a task in the local GUI. Stop this cell to disconnect.',flush=True)
        ready=False; claim_id=None; warned=0.0
        while time.monotonic()<until:
            try:
                if not ready:
                    version=coordinator_version(client)
                    if version!=TRANSFER_VERSION:
                        if time.monotonic()-warned>300:
                            print(f'[worker] Local GUI services speak transfer protocol {version or "1 or older"}; '
                                  f'this worker needs {TRANSFER_VERSION}. Restart scripts/start_colab_gui.ps1 '
                                  'locally; retrying every 30 s.',flush=True)
                            warned=time.monotonic()
                        time.sleep(30);continue
                    ready=True
                # Reusing the id lets the coordinator return a lease whose response was lost.
                claim_id=claim_id or uuid4().hex
                response=client.post('/api/colab-worker/claim',json={'transfer_version':TRANSFER_VERSION,'claim_id':claim_id})
                if response.status_code in {404,426}:
                    ready=False;continue
                response.raise_for_status()
                job=response.json()['job']
            except httpx.HTTPError as exc:
                print(f'[worker] Coordinator request failed ({type(exc).__name__}: {exc}); retrying in 15 s',flush=True)
                time.sleep(15);continue
            if not job:
                time.sleep(5);continue
            claim_id=None
            try:
                run_job(client,job)
            except Exception:
                # The coordinator expires uncommitted leases; keep serving later claims.
                print('[worker] Lease did not complete:\n'+traceback.format_exc(),flush=True)
                time.sleep(5)
            finally:
                prune_workspaces(ROOT/'remote-runs'/'tasks'/job['task']['id'])

if __name__=='__main__':main()
