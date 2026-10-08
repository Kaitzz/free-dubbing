"""Start local GUI + restricted worker gateway + temporary Cloudflare Tunnel."""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

from dotenv import dotenv_values
from pwdlib import PasswordHash

ROOT=Path(__file__).resolve().parents[1]
RUN=ROOT/'data/gui'
RUN.mkdir(parents=True,exist_ok=True)
children=[]

def install_tunnel():
    exe=ROOT/'.tools/cloudflared.exe'
    if exe.exists():return exe
    request=urllib.request.Request('https://api.github.com/repos/cloudflare/cloudflared/releases/latest',
                                  headers={'User-Agent':'YouDub-local-setup'})
    release=json.load(urllib.request.urlopen(request,timeout=60))
    asset=next(a for a in release['assets'] if a['name']=='cloudflared-windows-amd64.exe')
    digest=asset.get('digest','')
    if not digest.startswith('sha256:'):raise RuntimeError('Release has no SHA256 digest; install cloudflared manually')
    content=urllib.request.urlopen(asset['browser_download_url'],timeout=120).read()
    if hashlib.sha256(content).hexdigest()!=digest.split(':')[1]:raise RuntimeError('Tunnel binary checksum mismatch')
    exe.parent.mkdir(exist_ok=True);exe.write_bytes(content)
    return exe

def launch(command,name,env,cwd=ROOT):
    log=(RUN/(name+'.log')).open('wb')
    p=subprocess.Popen(command,cwd=cwd,env=env,stdout=log,stderr=subprocess.STDOUT,
                       creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    log.close();children.append(p);return p

def wait_http(url,process):
    for _ in range(90):
        if process.poll() is not None:raise RuntimeError('Service exited; inspect data/gui logs')
        try:
            urllib.request.urlopen(url,timeout=1).close();return
        except Exception:time.sleep(1)
    raise RuntimeError('Service startup timed out')

def main():
    for port in (3000,8000,8011):
        with socket.socket() as s:s.bind(('127.0.0.1',port))
    env=os.environ.copy()
    env.update({k:v for k,v in dotenv_values(ROOT/'.env').items() if v is not None})
    env.update(YOUDUB_EXECUTION_BACKEND='colab',DEVICE='cpu',YOUDUB_AUTH_COOKIE_SECURE='false',
               YOUDUB_AUTH_COOKIE_SAMESITE='lax',NEXT_SERVER_API_BASE_URL='http://127.0.0.1:8000')
    if not env.get('YOUDUB_AUTH_PASSWORD_HASH'):
        password_path=RUN/'login-password.txt'
        if not password_path.exists():password_path.write_text(secrets.token_urlsafe(18),encoding='utf-8')
        env['YOUDUB_AUTH_PASSWORD_HASH']=PasswordHash.recommended().hash(password_path.read_text(encoding='utf-8').strip())
    cloudflared=install_tunnel()
    try:
        api=launch([sys.executable,'-m','uvicorn','backend.app.main:app','--host','127.0.0.1','--port','8000'],'backend',env)
        wait_http('http://127.0.0.1:8000/api/health',api)
        gateway=launch([sys.executable,'-m','uvicorn','scripts.worker_gateway:app','--host','127.0.0.1','--port','8011'],'gateway',env)
        node=shutil.which('node')
        if not node:raise RuntimeError('Node.js is required')
        front=launch([node,'node_modules/next/dist/bin/next','start','--hostname','127.0.0.1','--port','3000'],'frontend',env,ROOT/'apps/web')
        wait_http('http://127.0.0.1:3000/login',front)
        tunnel=launch([str(cloudflared),'tunnel','--url','http://127.0.0.1:8011','--no-autoupdate'],'tunnel',env)
        for _ in range(90):
            text=(RUN/'tunnel.log').read_text(encoding='utf-8',errors='replace')
            match=re.search(r'https://[a-z0-9-]+\.trycloudflare\.com',text)
            if match:break
            if tunnel.poll() is not None:raise RuntimeError('Tunnel exited; inspect tunnel.log')
            time.sleep(1)
        else:raise RuntimeError('Tunnel URL unavailable')
        info={'gui':'http://127.0.0.1:3000','tunnel':match.group(),'password_file':str(RUN/'login-password.txt')}
        (RUN/'connection.json').write_text(json.dumps(info,indent=2),encoding='utf-8')
        print(json.dumps(info),flush=True)
        while all(p.poll() is None for p in children):time.sleep(2)
    finally:
        for p in reversed(children):
            if p.poll() is None:p.terminate()
        for p in children:
            try:p.wait(timeout=10)
            except subprocess.TimeoutExpired:p.kill()

if __name__=='__main__':main()
