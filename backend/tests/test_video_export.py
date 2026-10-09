import json
from pathlib import Path
from backend.app.video_export import export_info, prepare_export, _clean


def task(tmp_path):
    (tmp_path/"metadata").mkdir()
    (tmp_path/"metadata/ytdlp_info.json").write_text(json.dumps({
        "title":"测试：Title / example?", "uploader":"Creator", "id":"abcdefghijk",
        "upload_date":"20261008", "description":"Original description",
        "url":"https://secret.invalid/signed?token=private", "http_headers":{"Cookie":"private"}
    }),encoding="utf-8")
    return {"id":"abcdefghijk","url":"https://www.youtube.com/watch?v=abcdefghijk",
            "session_path":str(tmp_path),"output_mode":"both"}


def test_named_export_is_safe_and_omits_transport_secrets(tmp_path):
    info=export_info(task(tmp_path))
    assert "Creator" in info["filename"] and "abcdefghijk" in info["filename"]
    assert "中文配音+字幕" in info["filename"]
    assert "/" not in info["filename"] and "?" not in info["filename"]
    assert "private" not in json.dumps(info)
    assert info["audio_language"]=="zh"
    assert _clean("CON")=="_CON"


def test_subtitle_only_keeps_original_audio_language(tmp_path):
    t=task(tmp_path);t["output_mode"]="subtitles"
    assert export_info(t)["audio_language"]=="en"


def test_export_caches_and_invalidates_on_source_change(tmp_path,monkeypatch):
    from backend.app import video_export as m
    from types import SimpleNamespace
    monkeypatch.setattr(m,"DATA_DIR",tmp_path / "private-data")
    t=task(tmp_path)
    source=tmp_path/"video_final.mp4";source.write_bytes(b"video")
    t["final_video_path"]=str(source)
    commands=[]
    def run(cmd,**kwargs):
        commands.append(cmd)
        if "-show_entries" in cmd:
            return SimpleNamespace(stdout=json.dumps({"streams":[{"width":1920,"height":1080}]}))
        assert cmd[cmd.index("-c")+1]=="copy"
        Path(cmd[-1]).write_bytes(b"export")
        return SimpleNamespace()
    monkeypatch.setattr(m.subprocess,"run",run)
    video,info,manifest=prepare_export(t)
    assert "metadata" not in info["filename"]
    assert json.loads(manifest.read_text(encoding="utf-8"))["media"]["streams"][0]["height"]==1080
    assert prepare_export(t)[0]==video
    assert len(commands)==2
    source.write_bytes(b"new video content")
    assert prepare_export(t)[0]!=video
    assert len(commands)==4
