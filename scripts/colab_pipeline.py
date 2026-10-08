"""Run the existing full pipeline from Colab without starting public servers."""
from __future__ import annotations
import argparse
import json
import os
import shutil
import sys
import uuid
from pathlib import Path
from urllib.parse import urlencode
from zipfile import ZipFile, ZIP_DEFLATED

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def create_local_task(video, direction, output_mode, state):
    from backend.app import database
    from backend.app.config import WORKFOLDER
    from backend.app.adapters.local_video import uploaded_video_dir
    if state.exists():
        raise ValueError("State file already exists; resume this task or choose a new state file")
    if not video.is_file():
        raise ValueError("Input video does not exist")
    task_id = uuid.uuid4().hex
    destination = uploaded_video_dir(WORKFOLDER, task_id)
    destination.mkdir(parents=True, exist_ok=False)
    shutil.copy2(video, destination / ('input' + video.suffix.lower()))
    url = f"local://upload/{task_id}?" + urlencode({"direction": direction, "filename": video.name})
    database.create_task(url, task_id=task_id, output_mode=output_mode)
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"task_id": task_id}), encoding="utf-8")
    return task_id


def resume_task(state, dead_pid=None):
    from backend.app import database
    task_id = json.loads(state.read_text(encoding="utf-8"))["task_id"]
    task = database.get_task(task_id)
    if not task:
        raise ValueError("Task database is missing; resume requires the same Colab runtime and files")
    if task["status"] == "running":
        if dead_pid is None or dead_pid <= 0:
            raise ValueError("Task is still marked running; do not start a second process")
        try:
            os.kill(dead_pid, 0)
        except ProcessLookupError:
            database.reset_failed_for_resume(task_id)
            task = database.get_task(task_id)
        else:
            raise ValueError("Previous process still exists; refusing recovery")
    if task["status"] == "failed":
        database.reset_failed_for_resume(task_id)
    elif task["status"] not in {"queued", "paused", "succeeded"}:
        raise ValueError("Task is not resumable")
    return task_id


def export_result(task, destination):
    if task["status"] != "succeeded":
        raise ValueError("Cannot export a failed or incomplete task")
    session = Path(task["session_path"]).resolve()
    video = Path(task["final_video_path"]).resolve()
    if not video.is_relative_to(session) or not video.is_file():
        raise ValueError("Final video is missing or outside the task session")
    paths = [video]
    metadata = session / 'metadata'
    for pattern in ('asr.json', 'asr_fixed.json', 'translation.*.json', 'timings.json', 'subtitles.*.srt'):
        paths.extend(metadata.glob(pattern))
    destination.parent.mkdir(parents=True, exist_ok=True)
    pending = destination.with_suffix('.zip.tmp')
    try:
        with ZipFile(pending, 'w', compression=ZIP_DEFLATED) as z:
            for p in sorted(set(paths)):
                if not p.is_file() or p.is_symlink() or not p.resolve().is_relative_to(session):
                    raise ValueError("Unexpected export path")
                z.write(p, p.relative_to(session).as_posix())
        pending.replace(destination)
    finally:
        pending.unlink(missing_ok=True)
    return video


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--video', type=Path)
    source.add_argument('--resume', action='store_true')
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--result', type=Path, required=True)
    parser.add_argument('--direction', choices=['en-zh', 'ja-zh', 'zh-en'], default='en-zh')
    parser.add_argument('--output-mode', choices=['both', 'subtitles', 'dubbing'], default='both')
    parser.add_argument('--recover-dead-pid', type=int, help='Previously waited-on pipeline PID; must no longer exist')
    args = parser.parse_args()
    from backend.app import database, gpu_memory
    from backend.app.config import ensure_runtime_dirs
    from backend.app.pipeline import PipelineRunner
    ensure_runtime_dirs()
    database.init_db()
    if not database.get_openai_settings()['api_key']:
        parser.error('Set OPENAI_API_KEY with Colab Secrets or hidden input first')
    task_id = resume_task(args.state, args.recover_dead_pid) if args.resume else create_local_task(
        args.video, args.direction, args.output_mode, args.state)

    class ConsoleRunner(PipelineRunner):
        def log(self, message):
            super().log(message)
            print(f"[{database.now_iso()}] {message}", flush=True)

    task = database.get_task(task_id)
    if task['status'] != 'succeeded':
        try:
            ConsoleRunner(task_id).run()
        finally:
            gpu_memory.release_task_memory()
    task = database.get_task(task_id)
    if task['status'] != 'succeeded':
        print(f"Stopped at {task['current_stage']}; status={task['status']}. Fix the error and rerun this cell.", flush=True)
        return 1
    final_video = export_result(task, args.result)
    print(f"Complete: {final_video}\nDownload archive: {args.result}", flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
