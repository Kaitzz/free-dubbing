"""Generate a small, cached MiniMax HD/Turbo audition using local API settings."""
from pathlib import Path
import json
import sys
import time
import shutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import requests
import numpy as np
import soundfile as sf
from backend.app import database
from backend.app.adapters.audio import merge_tts_audio

# Reviewed translation of gMFlbVQ3AYk, 4.799-27.439 seconds.
SEGMENTS = [
    (0, 6641, "好，开始之前我想说明一下：这段视频不会点名，也不是要攻击谁。"),
    (6641, 11281, "我并不讨厌这么做的人，很多方面我其实还挺佩服他们。"),
    (11281, 18320, "所以，如果你认出了我说的是谁，请不要去攻击他们。我不赞成这么做。"),
    (18320, 22640, "这些事根本不值得让任何人觉得自己受到了攻击。"),
]


def main():
    settings = database.get_openai_settings()
    from urllib.parse import urlparse
    if urlparse(settings["base_url"]).hostname not in {"api.minimaxi.com", "api.minimax.cn"}:
        raise RuntimeError("Configure a mainland MiniMax API key before this audition")
    if not settings["api_key"]:
        raise RuntimeError("No MiniMax API key configured")
    endpoint = settings["base_url"].rstrip("/") + "/t2a_v2"
    folder = ROOT / "data" / "tts-preview" / "minimax-comparison"
    folder.mkdir(parents=True, exist_ok=True)
    voice = "male-qn-qingse"
    report = {"voice_id": voice, "source_video": "gMFlbVQ3AYk", "source_start_ms": 4799,
              "target_duration_seconds": 22.640, "segments": SEGMENTS, "models": {}}
    for model in ("speech-2.8-hd", "speech-2.8-turbo"):
        session = folder / model
        clips = session / "segments" / "tts"
        clips.mkdir(parents=True, exist_ok=True)
        metadata = session / "metadata"
        metadata.mkdir(exist_ok=True)
        rows, timings = [], []
        for index, (start, end, text) in enumerate(SEGMENTS, 1):
            path = clips / f"{index:04d}.wav"
            elapsed = None
            if not path.exists():
                tick = time.monotonic()
                response = requests.post(endpoint, headers={"Authorization": "Bearer " + settings["api_key"]},
                    json={"model": model, "text": text, "stream": False,
                          "language_boost": "Chinese", "output_format": "hex",
                          "voice_setting": {"voice_id": voice, "speed": 1, "vol": 1, "pitch": 0},
                          "audio_setting": {"format": "wav", "sample_rate": 32000, "channel": 1}}, timeout=120)
                if response.status_code != 200:
                    raise RuntimeError(f"MiniMax HTTP {response.status_code}; no response body logged")
                payload = response.json()
                status = payload.get("base_resp", {})
                if status.get("status_code") != 0:
                    raise RuntimeError(f"MiniMax status {status.get('status_code')}: {str(status.get('status_msg', ''))[:160]}")
                data = bytes.fromhex(payload["data"]["audio"])
                import io
                samples, rate = sf.read(io.BytesIO(data), dtype="float32")
                sf.write(path, samples, rate)
                elapsed = round(time.monotonic() - tick, 2)
            info = sf.info(path)
            timings.append({"segment": index, "request_seconds": elapsed, "audio_seconds": info.duration})
            rows.append({"dst": text, "start_time": start, "end_time": end, "audio_mode": "tts"})
            request_label = f"{elapsed}s" if elapsed is not None else "cached"
            print(f"{model} {index}/4: audio={info.duration:.2f}s, request={request_label}", flush=True)
        translation = metadata / "translation.zh.json"
        translation.write_text(json.dumps({"translation": rows}, ensure_ascii=False), encoding="utf-8")
        samples = [sf.read(clips / f"{i:04d}.wav", dtype="float32")[0] for i in range(1, 5)]
        sf.write(folder / f"{model}-raw.wav", np.concatenate(samples), 32000)
        aligned, _ = merge_tts_audio(translation, clips, session)
        shutil.copyfile(aligned, folder / f"{model}-aligned.wav")
        report["models"][model] = timings
        (folder / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Audition saved to", folder, flush=True)


if __name__ == "__main__":
    main()
