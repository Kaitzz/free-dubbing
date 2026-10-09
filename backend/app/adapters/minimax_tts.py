"""MiniMax speech API adapter with per-clip resumable, configuration-aware caching."""
from __future__ import annotations
import hashlib
import io
import json
import os
import time
import uuid
import threading
import math
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path
from urllib.parse import urlparse
import requests
import soundfile as sf
import numpy as np
from .. import database
from ..audio_mode import is_original_audio, target_text

DEFAULT_VOICE = "Chinese_casual_instructor_nv1"
DEFAULT_MODEL = "speech-2.8-turbo"
AVAILABLE_VOICES = (DEFAULT_VOICE, "Chinese (Mandarin)_Sincere_Adult")


class RequestGate:
    """All requests in one TTS stage share pacing and provider cooldown."""
    def __init__(self, rpm, clock=None, sleep=None):
        if not math.isfinite(rpm) or rpm<=0:
            raise ValueError("MINIMAX_TTS_RPM must be positive and finite")
        self.interval=60/rpm
        self.clock=clock or time.monotonic
        self.sleep=sleep or time.sleep
        self.next_at=0.0
        self.lock=threading.Lock()

    def acquire(self, stop):
        while not stop.is_set():
            with self.lock:
                now=self.clock()
                delay=self.next_at-now
                if delay<=0:
                    self.next_at=now+self.interval
                    return
            self.sleep(min(delay,1))
        raise RuntimeError("TTS requests stopped after another segment failed")

    def cooldown(self, seconds):
        with self.lock:
            self.next_at=max(self.next_at,self.clock()+seconds)


def _retry_delay(response, attempt, limited):
    delay=min(60*(attempt+1),120) if limited else min(2**attempt,30)
    try:
        value=float(getattr(response,"headers",{}).get("Retry-After",0))
        if math.isfinite(value):delay=max(delay,min(value,300))
    except (ValueError,TypeError):pass
    return delay


def generate_tts(translation_file, vocals_dir, session, progress_callback=None, *, original_vocals_file=None):
    items=json.loads(Path(translation_file).read_text(encoding="utf-8"))["translation"]
    output=Path(session)/"segments/tts"; output.mkdir(parents=True,exist_ok=True)
    if not items:return output
    settings=database.get_openai_settings()
    key=os.getenv("MINIMAX_API_KEY") or settings["api_key"]
    base=os.getenv("MINIMAX_TTS_BASE_URL", "https://api.minimaxi.com/v1").rstrip("/")
    if urlparse(base).scheme!="https" or urlparse(base).hostname not in {"api.minimaxi.com","api.minimax.cn","api.minimax.io"}:
        raise ValueError("MiniMax TTS requires an official HTTPS endpoint")
    if not key:raise ValueError("MiniMax TTS requires MINIMAX_API_KEY or the configured MiniMax API key")
    if not os.getenv("MINIMAX_API_KEY") and urlparse(settings["base_url"]).hostname not in {"api.minimaxi.com","api.minimax.cn","api.minimax.io"}:
        raise ValueError("Configured translation key is not a MiniMax key; set MINIMAX_API_KEY")
    voice=os.getenv("MINIMAX_TTS_VOICE",DEFAULT_VOICE)
    model=os.getenv("MINIMAX_TTS_MODEL",DEFAULT_MODEL)
    print(f"[tts] MiniMax model={model}; voice={voice}; API generation (no local TTS model)",flush=True)
    latest=max(int(x.get("end_time",0)) for x in items)
    gate=RequestGate(float(os.getenv("MINIMAX_TTS_RPM","20")))
    stop=threading.Event()
    def generate(index,item):
        path=output/f"{index:04d}.wav"
        sidecar=output/f"{index:04d}.minimax.json"
        if is_original_audio(item):
            sidecar.unlink(missing_ok=True)
            if original_vocals_file is None:raise ValueError("Original vocals required for original-audio item")
            from .voxcpm import _write_original_target_audio
            _write_original_target_audio(path,item,original_vocals_file,
                allow_tail_clamp=index==len(items) and int(item.get("end_time",0))==latest)
            return "original"
        text=target_text(item)
        if not isinstance(text,str) or not text.strip():raise ValueError(f"TTS segment {index} has no translated text")
        voice_setting={"voice_id":voice,"speed":1,"vol":1,"pitch":0}
        emotion=item.get("tts_emotion")
        if emotion in {"happy","sad","angry","fearful","disgusted","surprised","calm"}:
            voice_setting["emotion"]=emotion
        payload={"model":model,"text":text,"stream":False,"voice_setting":voice_setting,
                 "language_boost":{"zh":"Chinese","en":"English","ja":"Japanese"}.get(item.get("dst_lang"),"auto"),
                 "audio_setting":{"format":"wav","sample_rate":32000,"channel":1},"output_format":"hex"}
        signature=hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        if path.is_file() and sidecar.is_file():
            try:
                if json.loads(sidecar.read_text())["signature"]==signature and sf.info(path).frames>0:return "cached"
            except (ValueError,KeyError,RuntimeError):pass
        for attempt in range(6):
            try:
                gate.acquire(stop)
                response=requests.post(base+"/t2a_v2",headers={"Authorization":"Bearer "+key},
                                       json=payload,timeout=(15,120),allow_redirects=False)
                if response.status_code==429 or response.status_code>=500:
                    if attempt<5:
                        delay=_retry_delay(response,attempt,response.status_code==429)
                        gate.cooldown(delay)
                        print(f"[tts] HTTP {response.status_code}; shared cooldown {delay:g}s; segment {index} retry {attempt+1}/5",flush=True)
                        continue
                if response.status_code!=200:raise RuntimeError(f"MiniMax TTS HTTP {response.status_code} at segment {index}")
                result=response.json();status=result.get("base_resp",{}).get("status_code")
                if status in {1002,1039,1000,1001} and attempt<5:
                    delay=_retry_delay(response,attempt,status in {1002,1039})
                    gate.cooldown(delay)
                    print(f"[tts] MiniMax status {status}; shared cooldown {delay:g}s; segment {index} retry {attempt+1}/5",flush=True)
                    continue
                if status!=0:raise RuntimeError(f"MiniMax TTS status {status} at segment {index}; completed clips preserved")
                samples,rate=sf.read(io.BytesIO(bytes.fromhex(result["data"]["audio"])),dtype="float32")
                if not len(samples) or not np.isfinite(samples).all():raise ValueError("Empty or invalid MiniMax audio")
                temporary=output/f".{uuid.uuid4().hex}.wav"
                try:
                    sf.write(temporary,samples,rate);temporary.replace(path)
                finally:temporary.unlink(missing_ok=True)
                sidecar.write_text(json.dumps({"signature":signature,"model":model,"voice_id":voice}),encoding="utf-8")
                return "generated"
            except (requests.Timeout,requests.ConnectionError):
                if attempt==5:raise RuntimeError(f"MiniMax TTS network failure at segment {index}") from None
                gate.cooldown(min(2**attempt,30))
        raise RuntimeError(f"MiniMax TTS retries exhausted at segment {index}")
    workers=max(1,min(4,int(os.getenv("MINIMAX_TTS_CONCURRENCY","2"))))
    executor=ThreadPoolExecutor(max_workers=workers)
    remaining=iter(enumerate(items,1))
    pending={}
    completed=0
    def submit_next():
        entry=next(remaining,None)
        if entry is not None:
            index,item=entry
            pending[executor.submit(generate,index,item)]=index
    try:
        for _ in range(workers):submit_next()
        while pending:
            done,_=wait(pending,return_when=FIRST_COMPLETED)
            # Resolve failures before adding more work, so a fatal error cannot
            # leave the rest of a 200-clip job continuing to consume API quota.
            results=[(future,future.result()) for future in done]
            for future,status in results:
                index=pending.pop(future)
                completed+=1
                message=f"MiniMax {completed}/{len(items)} clips; segment {index} {status}"
                if progress_callback:progress_callback(round(completed/len(items)*100),message)
                else:print("[tts] "+message,flush=True)
            for _ in results:submit_next()
    finally:
        stop.set()
        executor.shutdown(wait=True,cancel_futures=True)
    return output
