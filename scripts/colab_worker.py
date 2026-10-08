"""Interactive Colab pull worker. No listening server or tunnel runs on Colab."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse

import httpx
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.app.remote_archive import unpack, pack

ROOT=Path(__file__).resolve().parents[1]

def model_cache_path():
    # Resolve the intentionally shared cache before the runtime checks its root.
    return str(Path(os.environ.get('MODEL_CACHE_DIR', str(ROOT/'model-cache'))).expanduser().resolve())


def snapshot(job_dir, original, exitcode=None):
    path=job_dir/'data/youdub.sqlite'
    if not path.exists():
        return {**original,'status':'failed','error_message':f'Worker process exited {exitcode} before initializing',
                'session_path':str(job_dir/'workfolder/session')}
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

def run_job(client, job):
    token=job['lease']; original=job['task']
    prefix=f'/api/colab-worker/{token}'
    folder=ROOT/'remote-runs'/token
    folder.mkdir(parents=True,exist_ok=False)
    stopped=threading.Event(); lost=threading.Event()
    def heartbeat():
        last_success=time.monotonic()
        while not stopped.wait(10):
            try:
                task=snapshot(folder,original)
                stage=next((s for s in task['stages'] if s['status']=='running'),{})
                data={'stage':stage.get('name'),'progress':stage.get('progress'),
                      'message':stage.get('last_message') or 'Colab processing'}
                response=client.post(prefix+'/heartbeat',json=data,timeout=20)
                if response.status_code in {401,409}:lost.set();return
                response.raise_for_status()
                last_success=time.monotonic()
            except (httpx.HTTPError,sqlite3.Error):
                if time.monotonic()-last_success>150:
                    lost.set();return # Lease expiry prevents a late worker from committing results.
    thread=threading.Thread(target=heartbeat,daemon=True);thread.start()
    process=None
    try:
        with client.stream('GET',prefix+'/input') as response:
            response.raise_for_status()
            with (folder/'input.zip').open('wb') as f:
                for chunk in response.iter_bytes():f.write(chunk)
        unpack(folder/'input.zip',folder/'received')
        work=folder/'workfolder';work.mkdir()
        received=folder/'received'
        if (received/'session').exists():shutil.move(str(received/'session'),str(work/'session'))
        uploads=work/'_uploads'/original['id'];uploads.parent.mkdir(parents=True)
        if (received/'uploads').exists():shutil.move(str(received/'uploads'),str(uploads))
        (folder/'task.json').write_text(json.dumps(original),encoding='utf-8')
        settings=job['settings']
        env=os.environ.copy()
        env.update(YOUDUB_DATA_DIR=str(folder/'data'),WORKFOLDER=str(work),
            YOUDUB_EXECUTION_BACKEND='local',MODEL_CACHE_DIR=model_cache_path(),
            DEVICE='cuda',FUNASR_DEVICE='cuda:0',DEMUCS_DEVICE='cuda',DEMUCS_CHUNK_SECONDS='60',
            VOXCPM_LOW_MEMORY_INIT='true',VOXCPM_OPTIMIZE='false',VOXCPM_LOAD_DENOISER='false',
            OPENAI_API_KEY=os.environ['OPENAI_API_KEY'],OPENAI_BASE_URL=settings['base_url'],
            OPENAI_MODEL=settings['model'],OPENAI_TRANSLATE_CONCURRENCY=settings['translate_concurrency'] or '2')
        env.pop('YOUDUB_WORKER_TOKEN',None)
        if lost.is_set():raise RuntimeError('Lease lost during input transfer')
        print(f"Task {original['id']}: running next stage",flush=True)
        process=subprocess.Popen([sys.executable,'-u',str(ROOT/'scripts/remote_job.py'),str(folder)],
            env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
        # Read output on a background thread so loss of lease can stop inference.
        def relay():
            for line in process.stdout:
                print(line,end='',flush=True)
                if line.startswith(('Task ', '[')) and '|' not in line:
                    try:client.post(prefix+'/heartbeat',json={'log':line[:4000]})
                    except httpx.HTTPError:pass
        reader=threading.Thread(target=relay,daemon=True);reader.start()
        while process.poll() is None:
            if lost.wait(1):
                process.terminate();break
        code=process.wait();reader.join(timeout=5)
        if lost.is_set():raise RuntimeError('Coordinator rejected lease; stopped this worker')
        task=snapshot(folder,original,code)
        session=Path(task['session_path']) if task.get('session_path') else work/'session'
        if not session.resolve().is_relative_to(work.resolve()):raise ValueError('Invalid worker session')
        output=folder/'output.zip'
        pack(output,{'session':session})
        with output.open('rb') as f:
            offset=0
            while block:=f.read(8*1024*1024):
                response=client.put(prefix+'/output',params={'offset':offset},content=block)
                response.raise_for_status();offset=response.json()['offset']
        response=client.post(prefix+'/finish',json={'task':task});response.raise_for_status()
        print('Checkpoint returned:',response.json()['status'],flush=True)
    finally:
        if process and process.poll() is None:
            process.terminate()
            try:process.wait(timeout=20)
            except subprocess.TimeoutExpired:process.kill();process.wait()
        stopped.set();thread.join(timeout=35)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',required=True)
    parser.add_argument('--minutes',type=int,default=120)
    args=parser.parse_args()
    parsed=urlparse(args.url)
    if parsed.scheme!='https' or parsed.username or parsed.password or parsed.query or parsed.fragment:
        parser.error('Use the HTTPS tunnel origin, without credentials or query parameters')
    token=os.environ.get('YOUDUB_WORKER_TOKEN','')
    if not token:parser.error('Missing YOUDUB_WORKER_TOKEN')
    if not os.environ.get('OPENAI_API_KEY'):parser.error('Set the MiniMax key in Colab first')
    with httpx.Client(base_url=args.url.rstrip('/'),headers={'Authorization':'Bearer '+token},
                      timeout=120,follow_redirects=False) as client:
        until=time.monotonic()+args.minutes*60
        print('Colab worker connected; create a task in the local GUI. Stop this cell to disconnect.',flush=True)
        while time.monotonic()<until:
            response=client.post('/api/colab-worker/claim');response.raise_for_status()
            job=response.json()['job']
            if job:run_job(client,job)
            else:time.sleep(5)

if __name__=='__main__':main()
