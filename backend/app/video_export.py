"""Named, attributed downloads; never export yt-dlp transport credentials."""
from __future__ import annotations
import hashlib
import json
import re
import subprocess
import uuid
from pathlib import Path
from .config import DATA_DIR, ffmpeg_binary, ffprobe_binary
from .sources import detect_source


def _clean(value, limit=100):
    text=re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", str(value or ""))
    text=re.sub(r"\s+", " ", text).strip(" .")[:limit].rstrip(" .")
    if text.split(".")[0].upper() in {"CON","PRN","AUX","NUL",*[f"COM{i}" for i in range(1,10)],*[f"LPT{i}" for i in range(1,10)]}:
        text="_"+text
    return text


def export_info(task):
    info={}
    if task.get("session_path"):
        path=Path(task["session_path"])/"metadata/ytdlp_info.json"
        if path.is_file():
            info=json.loads(path.read_text(encoding="utf-8"))
    source_name = ""
    try:
        source=detect_source(task["url"])
        src,dst=source.asr_language,source.target_language
        source_name=source.name
    except ValueError:
        src,dst="", ""
    mode=task.get("output_mode") or "both"
    title=info.get("title") or task.get("title") or "Untitled"
    author=info.get("uploader") or info.get("channel") or ""
    video_id=info.get("id") or task["id"]
    # Use public canonical URLs only, never signed media URLs or local paths.
    if source_name == "youtube":
        url="https://www.youtube.com/watch?v="+str(video_id)
    elif str(task.get("url", "")).startswith("https://www.bilibili.com/video/"):
        url=task["url"].split("?")[0]
    else:
        url=""
    # Subtitles ship as separate files, so the name only says what is heard.
    audio=src if mode=="subtitles" else dst
    label="原音" if mode=="subtitles" else "配音"
    language={"zh":"中文","en":"英文","ja":"日文"}.get(audio,audio)
    parts=[_clean(title,90)]
    if author: parts.append(_clean(author,35))
    name=" — ".join(parts)+f" [{_clean(video_id,30)}] [{language}{label}].mp4"
    return {"schema_version":1,"filename":name,"title":title,"original_creator":author,
        "source_url":url,"source_video_id":video_id,"original_upload_date":info.get("upload_date"),
        "description":info.get("description") or "","source_language":src,"target_language":dst,
        "audio_language":audio,"output_mode":mode,
        "processing_note":"Translated/processed with dubbing; creator attribution refers to the original video."}


def prepare_export(task):
    original=Path(task["final_video_path"])
    info=export_info(task)
    # Compatibility for legacy tasks without a session directory.
    if not task.get("session_path"):
        return original,info,None
    folder=DATA_DIR / "video-exports" / hashlib.sha256(str(task["id"]).encode()).hexdigest()[:20]
    folder.mkdir(parents=True,exist_ok=True)
    stamp=original.stat()
    key=hashlib.sha256((json.dumps(info,ensure_ascii=False,sort_keys=True)+str(original.resolve())+str(stamp.st_mtime_ns)+str(stamp.st_size)).encode()).hexdigest()[:20]
    video=folder/f"{key}.mp4"
    manifest=folder/f"{key}.json"
    if video.exists() and manifest.exists():
        return video,info,manifest
    temporary=folder/f".{uuid.uuid4().hex}.mp4"
    tags={"title":info["title"],"artist":info["original_creator"],
          "description":info["description"],"date":info["original_upload_date"] or "",
          "comment":f"Source: {info['source_url']}\nOriginal creator: {info['original_creator']}\nLanguages: {info['source_language']} -> {info['target_language']}\nMode: {info['output_mode']}\n{info['processing_note']}"}
    cmd=[ffmpeg_binary(),"-hide_banner","-loglevel","error","-y","-i",str(original),
         "-map","0","-map_metadata","-1","-c","copy","-movflags","+faststart"]
    for tag,value in tags.items():
        if value:cmd.extend(["-metadata",f"{tag}={value}"])
    lang={"zh":"zho","en":"eng","ja":"jpn"}.get(info["audio_language"],"und")
    cmd.extend(["-metadata:s:a:0",f"language={lang}",str(temporary)])
    try:
        subprocess.run(cmd,check=True,capture_output=True,timeout=180)
        probe=subprocess.run([ffprobe_binary(),"-v","error","-show_entries",
            "stream=index,codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels:format=duration,size,bit_rate",
            "-of","json",str(temporary)],check=True,capture_output=True,text=True,timeout=30)
        info["media"]=json.loads(probe.stdout)
        temporary.replace(video)
        temp_json=folder/f".{uuid.uuid4().hex}.json"
        temp_json.write_text(json.dumps(info,ensure_ascii=False,indent=2),encoding="utf-8")
        temp_json.replace(manifest)
    finally:
        temporary.unlink(missing_ok=True)
    return video,info,manifest
