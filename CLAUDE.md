# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

YouDub WebUI is a video localization pipeline: download, vocal separation, ASR, LLM translation, TTS dubbing, then mixing and subtitle burn-in. It has a FastAPI backend and a Next.js GUI. This repo (`origin` = `github.com/Kaitzz/free-dubbing`) is a fork of `liuzhao1225/YouDub-webui` that adds a **Colab remote-execution mode**. In that mode the local Windows machine (no GPU) runs the GUI plus a coordinator, and a Google Colab GPU notebook pulls work one stage at a time over a Cloudflare tunnel. This is how the project is used day to day. The upstream mode, where everything runs on one GPU machine, still exists and shares the same pipeline code.

The docs are in Chinese and partly out of date. When docs and code disagree, trust the code:
- `COLAB_GUI.md`: the current GUI + Colab workflow (most relevant).
- `README.md`: upstream setup.
- `COLAB_PIPELINE.md` and `COLAB_STT.md`: the standalone notebooks.
- `LOCAL_DESIGN.md`: STT and translation design notes. It still says translation batches hold 100 items; the code uses `BATCH_MAX_ITEMS = 50`.

Code, comments and commit messages are in English. Commit subjects are short imperative sentences, e.g. "Serialize heartbeat log flushes to prevent worker crashes".

## Commands

The local machine runs Windows with no GPU and no full ML environment. torch, funasr, demucs and voxcpm are not installed, and the README's `.venv` does not exist. Real inference only runs on Colab. The local virtualenvs are:
- `.venv-test`: lightweight test deps from `backend/requirements-test.txt`. Use it for pytest.
- `.venv-gui`: the coordinator runtime from `requirements-gui.txt`, created by `scripts/start_colab_gui.ps1`.

The commands below work in both PowerShell and Git Bash:

```bash
# Backend tests (about 25 s; POSIX-permission tests are skipped on Windows)
.venv-test/Scripts/python.exe -m pytest backend/tests -q
.venv-test/Scripts/python.exe -m pytest backend/tests/test_remote.py::test_claim_checkpoint_and_manual_continue -q

# Frontend (apps/web: Next.js 16, React 19, vitest + jsdom)
npm --prefix apps/web test                                    # vitest run
npm --prefix apps/web test -- src/lib/use-serial-polling.test.tsx
npm --prefix apps/web run lint
npm --prefix apps/web run build
cd apps/web && npx tsc --noEmit
```

CI (`.github/workflows/ci.yml`, Ubuntu, Python 3.12) installs only `backend/requirements-test.txt`. It **fails if torch, funasr, demucs, voxcpm, librosa, audiostretchy, spacy, openunmix, whisper or modelscope can be imported**. Keep heavy ML imports inside functions so they load lazily, never at module top level in `backend/app`, and mock them in tests.

`apps/web/src/lib/upload-contract.json` (allowed video extensions) is shared with `ALLOWED_VIDEO_SUFFIXES` in `backend/app/main.py`, and a backend test asserts they match.

### Running the GUI + Colab coordinator (the daily setup)

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start_colab_gui.ps1
```

The script installs `.venv-gui`, runs `next build`, then starts `scripts/serve_colab_gui.py`, which launches:
- the backend on `127.0.0.1:8000`, with `YOUDUB_EXECUTION_BACKEND=colab`, `YOUDUB_LOCAL_GUI=true` and `DEVICE=cpu` forced;
- the worker gateway on `:8011`;
- `next start` on `:3000`;
- `cloudflared`. The tunnel is the fixed `https://dubbing.corneliazhang.me` when `data/gui/cloudflare-token.txt` exists, otherwise a temporary trycloudflare URL.

Service logs go to `data/gui/{backend,gateway,frontend,tunnel}.log`. The script serves a production build, so frontend changes need a restart.

The upstream single-machine mode is `uvicorn backend.app.main:app --reload --port 8000` plus `npm --prefix apps/web run dev`. This fork's frontend has no login form, so the backend needs `YOUDUB_LOCAL_GUI=true` for the GUI to work. Without it, the backend expects password login, requires `YOUDUB_AUTH_PASSWORD_HASH` (an Argon2 hash), and won't start without that hash.

## How code reaches Colab

The private launcher notebook is `data/gui/Dubbing_Launcher.ipynb`. It is gitignored because it holds the 8-digit worker password; the public template is in `notebooks/`. Each run goes through these steps:
1. The launcher resolves `main` of `Kaitzz/free-dubbing` to a commit SHA.
2. It executes the code cells of `notebooks/YouDub_GUI_Colab.ipynb` from that SHA.
3. That working notebook checks out the same SHA to `/content/free-dubbing/<sha>/`.
4. It builds a per-commit `.venv-colab` by installing `requirements-colab.txt` against Colab's preinstalled torch, and fetches the pinned Demucs commit.
5. It runs `scripts/colab_preflight.py`.
6. It mounts the Colab account's Google Drive. The user authorizes a popup once per runtime; if the mount fails, the worker runs without checkpoints.
7. It starts `scripts/colab_worker.py` with `DUBBING_DRIVE_DIR` and `DUBBING_WORKSPACE` set.

Colab Secrets (`MINIMAX_API_KEY`, plus the optional `YOUTUBE_COOKIES` and `HF_TOKEN`) belong to the Google account running Colab. Switching accounts means setting them again and opening the private launcher from that account.

What follows from this:
- **Worker-side changes need a push.** Changes to `backend/app/**`, `scripts/colab_worker.py`, `scripts/remote_job.py`, `notebooks/YouDub_GUI_Colab.ipynb` or `requirements-colab.txt` reach Colab only after pushing to `origin/main` and re-running the launcher. Every new SHA gets a fresh checkout and a fresh pip install, which takes minutes. A running worker is never hot-updated.
- **Coordinator-side changes need a restart.** Changes to `main.py`, `remote.py` and the rest of the local side take effect after restarting `start_colab_gui.ps1`. uvicorn runs without `--reload`, so a stale process keeps serving old code; to confirm which code is live, compare the uvicorn process start time with the commit time. The two sides can run different commits, and `remote.TRANSFER_VERSION` / `colab_worker.TRANSFER_VERSION` must match. A mismatch is rejected rather than downgraded: an old worker's claim gets 426, and a new worker that sees an old coordinator (`/hello` returns 404) keeps retrying every 30 s and tells the user to restart the local services. Bump the version on any incompatible protocol change, and change both sides together.
- **The Colab worker ignores the local `.env`.** Its environment is hardcoded in `colab_worker.run_job`: `DEVICE=cuda`, `DEMUCS_CHUNK_SECONDS=180`, `DUBBING_VIDEO_ENCODER=auto`, VoxCPM low-memory init with no compile, and so on. The GUI's translation base URL, model and concurrency are passed along. The API key comes from the Colab Secret `MINIMAX_API_KEY` and is never sent from the local machine.
- **Colab installs can drift.** Requirements are mostly unpinned, and yt-dlp is always upgraded, so a fresh runtime can pick up new library versions. If Colab's torch version is missing from the notebook's `codec_versions` map (for TorchCodec), the install fails immediately.
- **The Demucs pin lives in two places:** the `submodule/demucs` pointer and the notebook. Bump both together. The submodule is not initialized locally; tests don't need it.
- **Notebooks are tested.** `backend/tests/test_dubbing_launcher.py` requires every notebook code cell to compile and to be saved without outputs.

## Architecture

### Pipeline (shared by both modes)

- **Stages.** `backend/app/stages.py` defines nine ordered stages: `download, separate, asr, asr_fix, translate, split_audio, tts, merge_audio, merge_video`. Stage names are also hardcoded in `database.get_task` (ORDER BY), `stage_reset.STAGE_OWN_ARTIFACTS`, `pipeline._STAGE_ARTIFACTS` and the frontend `lib/i18n.tsx`.
- **The runner.** `PipelineRunner.run()` in `backend/app/pipeline.py` walks the stages:
  - It never re-runs a `succeeded` stage. Instead it "restores" the stage by requiring its artifact files at fixed paths in the session directory; a missing file fails the task.
  - It skips stages based on `output_mode`. `subtitles` skips `split_audio`, `tts` and `merge_audio`, and also skips `separate` when an SRT was uploaded.
  - `execution_mode=manual` pauses after every stage.
  - Stage handlers import their adapters lazily.
  - On an exception, the current stage and the task are marked failed. `/resume` resets failed and running stages to pending.
- **Shortcuts that skip models.** An uploaded translated SRT (local uploads only) replaces both ASR and translation (`adapters/local_subtitles.py`). YouTube captions fetched during download (`adapters/online_assets.py`, saved to `metadata/source_subtitles.json`) replace SenseVoice. Automatic captions are regrouped in `asr_fix` (`adapters/source_caption_segments.py`).
- **Sources and task ids.** `sources.detect_source(url)` maps a task URL to a source and language pair: YouTube en→zh, Bilibili zh→en, and `local://upload/<task_id>?direction=en-zh|ja-zh|zh-en`. URL tasks use the video id as the task id, with `-<output_mode>` appended unless the mode is `both`. Resubmitting a URL therefore returns the existing task.
- **Adapters** live in `backend/app/adapters/`:
  - `ytdlp` and `local_video`: input.
  - `demucs`: the vendored `submodule/demucs` source, model `htdemucs_ft`, processed in chunks with crossfades.
  - `sensevoice_asr`: FunASR VAD plus CTC word timestamps. `whisper_asr` is legacy and unused.
  - `asr_sentence_fixer`: sentence segmentation.
  - `openai_translate`: any OpenAI-compatible chat API, MiniMax-M3 by default. It runs a preprocess pass, then sends ID-keyed JSON batches that are validated, retried, and split in half on failure. Each item carries an `audio_mode` that decides between dubbing and keeping the original audio (see `audio_mode.py`).
  - `voxcpm` or `minimax_tts`, selected by `DUBBING_TTS_PROVIDER`.
  - `audio`: splits segments, time-stretches them and assembles the dubbing track.
  - `ffmpeg`: audio mix, SRT, subtitle burn-in, and an NVENC probe with a libx264 fallback.
- **Model lifetime.** Models are module-level singletons. `gpu_memory.release_stage_memory` releases them after GPU stages.

### Session directory

The session directory is the contract between stages, resume, redo and the Colab transfer.

URL tasks use `<WORKFOLDER>/<uploader>/<title>__<id>/`. Local uploads use `<WORKFOLDER>/local/<title>__<id>/`, and the original upload files stay in `<WORKFOLDER>/_uploads/<task_id>/{video,subtitle}/`. Inside a session:
- `media/`: `video_source.mp4`, `audio_vocals.wav`, `audio_bgm.wav`, `thumbnail.*`, `video_final.mp4`
- `metadata/`: `ytdlp_info.json` or `local_info.json`, `raw_subtitles/`, `source_subtitles.{json,srt}`, `asr.json`, `asr_fixed.json`, `translation_preprocess.json`, `translation.<lang>.json`, `timings.json`, `subtitles.<lang>.srt`
- `segments/`: `vocals/NNNN.wav`, `tts/NNNN.wav`, `stretched/`
- `tmp/`: `audio_dubbing.wav`, `audio_mixed.m4a`, `tts_references/`

If you add or rename a stage artifact, update two places:
- `pipeline._STAGE_ARTIFACTS`, which drives resume and the Colab worker's "re-run what's missing" check;
- `stage_reset.STAGE_OWN_ARTIFACTS`, which drives per-stage redo and tells the worker which stage owns a file fetched from the GUI.

If the local GUI starts reading a new session file, add it to `remote_archive.gui_file`; otherwise it never leaves Colab. Many adapters skip their work when the output file already exists, so stale files get reused without any warning.

### Persistence, config and auth

- **Database.** SQLite at `data/youdub.sqlite` holds `tasks`, `task_stages`, `settings` and `auth_*`, plus `remote_leases`, which `remote.init()` creates. `database.connect()` opens a new connection on every call: no pool, default rollback journal.
- **Settings precedence.** Settings saved in the DB (`openai.*`, `ytdlp.*`, `remote.token_hash`) override env defaults; env only provides the initial values.
- **Import-time config.** `config.py` has side effects on import: it loads `.env`, sets the umask and registers the Windows FFmpeg DLL directory. It exposes paths as module constants. Tests redirect state by monkeypatching `database.DB_PATH`, `config.WORKFOLDER`, `config.LOG_DIR` and similar constants. Reuse `configure_tmp_runtime()` and `authenticated_client()` from `backend/tests/test_settings_and_api.py`.
- **File security.** `runtime_security.py` enforces owner-only permissions and rejects symlinks. Write runtime files through its helpers (`atomic_write_private_text`, `open_private_append_text`, `ensure_private_directory`). Startup refuses to proceed if permissions are unsafe.
- **Auth.** `auth.AuthMiddleware` has three paths:
  - `/api/colab-worker/*` needs `Bearer <worker password>`. The GUI sets this as a fixed 8-digit password and stores its SHA-256 in settings. Authentication locks out for a minute after 10 failures.
  - With `YOUDUB_LOCAL_GUI=true`, password login is skipped, but requests must come from a loopback peer and host and carry a per-process CSRF token. The frontend assumes this mode.
  - Otherwise it uses Argon2 password login, a session cookie and a CSRF header. This path is still implemented and tested, but no UI uses it.

### Execution modes

Backend startup marks interrupted tasks failed ("Backend restarted before the task completed."). In local mode that covers queued and running tasks. In colab mode it covers only running tasks: queued tasks keep waiting for a worker. A running task's lease survives the restart, so a worker that is still alive can still commit its stage.

**local** (`YOUDUB_EXECUTION_BACKEND` unset): `worker.py` runs one daemon thread with a FIFO queue inside the uvicorn process and calls `pipeline.run_task`.

**colab** (`YOUDUB_EXECUTION_BACKEND=colab`): `worker.enqueue` and `worker.start` do nothing, and tasks stay `queued` in SQLite until a worker claims them. One lease covers one stage. `backend/app/remote.py` talks to `scripts/colab_worker.py` through `scripts/worker_gateway.py`, which forwards only `hello`, `claim` and `<lease>/{files,heartbeat,output,finish}`. The current protocol is `TRANSFER_VERSION = 3`.

Where each copy of a task's data lives:
- **Colab local disk** holds the full working copy. The workspace root is `$DUBBING_WORKSPACE/tasks/<task_id>/` (the notebook sets `/content/dubbing-workspace`; it defaults to `remote-runs/`). It contains `workfolder/{session,_uploads/<id>}`, `state.json`, and `leases/<lease>/{data,task.json,worker.log}`, and at most 3 task workspaces are kept. `state.json` records, per committed stage, the lease that produced it and the session files it owns. A file belongs to the last stage that wrote it.
- **Google Drive** holds the durable copy: one uncompressed zip per committed stage at `$DUBBING_DRIVE_DIR/tasks/<task_id>/<stage>.<lease>.zip`, indexed by `stages.json`. The notebook mounts this Colab account's Drive at `MyDrive/free-dubbing`. Checkpoints are kept for the 20 most recent tasks (`DUBBING_DRIVE_KEEP_TASKS`). Without Drive the worker still runs, but a lost runtime re-runs the audio stages.
- **The local coordinator** holds only what the GUI shows (`remote_archive.gui_file`): `media/video_final.mp4`, `media/thumbnail.*` and `metadata/**`. New tasks use one session at `workfolder/_remote/<task_id>/session`; legacy tasks keep their older, full `session_path`.
- **`remote_stage_versions`** (on the coordinator) maps each succeeded stage to the lease that committed it, so stale checkpoints are never reused.

Nothing in the transfer hashes file contents. Each lease goes like this:
1. **Claim.** `POST /api/colab-worker/claim` with `{transfer_version, claim_id}` returns:
   - a lease token (180 s TTL) and a task snapshot;
   - the translation settings without the key;
   - `files`: the coordinator's files as `{name: [size, mtime_ns]}`. That means uploads, GUI files, or the full session of a legacy task. `uploads/video/*` is omitted once download has succeeded.
   - `stage_leases`.

   The YouTube cookie is included only for the download stage. Only one lease can be active or committing at a time. Re-sending the same `claim_id` returns the same lease.
2. **Prepare** (`colab_worker.prepare`).
   - A stage is valid when it has succeeded and its lease matches `stage_leases` (`legacy` if it has no version). The worker drops outputs of invalid stages, untracked files (output of leases that never committed), and stray folders.
   - Valid stages missing locally are restored from their Drive archive.
   - Then any coordinator file the worker lacks whose owner stage is valid is fetched with one `POST <lease>/files`. The coordinator streams these as an uncompressed zip (`stream_files`). The owner stage comes from `stage_reset.owner_stage`, the same mapping as per-stage redo.
   - Valid stages whose `pipeline.stage_artifacts` are still missing are marked pending in the task handed to the stage, so they re-run one per lease. Text results usually survive, because the GUI holds them.
3. **Run.** The worker snapshots session stats, then spawns `scripts/remote_job.py`. That script creates a fresh job SQLite under the lease folder, recreates the task in manual mode, runs `PipelineRunner` for exactly one stage in `workfolder/session`, and exits. Afterwards, `adopt_session` merges the download stage's video-named folder into `session`.
4. **Heartbeat.** Every 10 s a heartbeat thread posts progress from the job database plus up to 100 log entries `{t, m}` stamped with Colab time. `log_start` lets the coordinator drop re-sent batches. Heartbeats and output chunks renew the lease.
5. **Checkpoint and finish.**
   - The files whose stat changed become the stage's outputs. They are recorded in `state.json` before finish, and archived to Drive. A failed Drive write only logs a warning.
   - Only the GUI subset is zipped (stored, uncompressed) and `PUT` to `/output` in 64 MiB chunks. Chunks are offset-checked; a retried chunk is acknowledged and `offset=0` restarts the upload. Failed stages send nothing.
   - `POST /finish` carries the job-DB snapshot, `files` `{name: size}`, `deleted`, `ran` (the stages that executed, including re-runs), and a timing `report`.
   - In a threadpool, the coordinator unpacks into `_remote/<task>/.incoming-<lease>`, checks names and sizes, `os.replace`s the files into the session, updates statuses, and sets `remote_stage_versions[stage] = lease` for each stage in `ran`.
   - Finish is idempotent: the stored result is returned on retry, and `409 … in progress` means retry later. A failed commit marks the task failed with "Checkpoint commit failed: …" and never leaves a lease stuck in `committing`.
6. **Expiry.** `remote.expire()` fails any task whose lease lapsed ("Colab disconnected"), restores the stage snapshot taken at claim time, and deletes that lease's transfer files. GUI list and detail polling also call it. A worker that comes back late gets 409. `remote.init()` at startup deletes leftover `data/remote/*` and `.incoming-*` folders that belong to no active lease. Deleting or rerunning a task removes its `_remote/<task_id>` folder and its stage versions.

The worker retries network errors and 5xx/429 responses with backoff. An exception in one lease is logged and the worker moves on to the next claim instead of exiting. Because sessions move between machines, `PipelineRunner._uploaded_subtitle_path` falls back to `_uploads/<task_id>/subtitle/` when the absolute path recorded in `local_info.json` doesn't exist.

Other entry points:
- `scripts/colab_pipeline.py` with `notebooks/YouDub_Pipeline_Colab.ipynb`: the whole pipeline inside Colab, with no GUI and no transfer.
- `scripts/run_stt.py`: standalone STT and re-segmentation.
- `scripts/run_pipeline.py`: local CLI.
- The `youdub-*.zip` files in the root are gitignored outputs of `scripts/build_*_bundle.py`, left over from the older flow where a source bundle was uploaded to Colab.

### Frontend

Next.js App Router pages: `/` (submit a task, task history) and `/tasks/[id]` (stages, log, continue/resume/redo/rerun/delete, video player). There is no login screen: `/login` redirects to `/`, and `lib/auth.tsx` only fetches `/api/auth/session` to get the local CSRF token. All backend calls go through `lib/api.ts`. The browser calls same-origin `/api/*`, which `next.config.ts` rewrites to the backend, and mutations send `X-CSRF-Token`. `lib/use-serial-polling.ts` polls every 2 s without overlapping requests. `app/downloads/[id]/[kind]/route.ts` proxies downloads under descriptive filenames. `components/colab-dialog.tsx` shows Colab status and sets the worker password.

## Where logs and timing data actually go

- **Task log.** `data/logs/<task_id>.log` is written by `PipelineRunner.log` and `stage_message` as `[UTC ISO timestamp, 1-second precision] [stage] msg`, plus Colab lines relayed by `/heartbeat`. `GET /api/tasks/{id}/log` returns the whole file. The task page re-fetches it every 2 s and rewrites the leading timestamps to Pacific time (`apps/web/src/lib/log-time.ts`).
- **Stage durations.** These exist only as `task_stages.started_at` and `completed_at`, at 1-second precision. In colab mode they come from the Colab clock in the job database and exclude transfer and restore time.
- **Adapter output.** Adapters `print()` to stdout (`[download]`, `[tts]`, `[translate]`, `[merge_video]`). `openai_translate` uses `logging`, but nothing configures logging, so its INFO lines are dropped and WARNING and above go to stderr unformatted. FFmpeg and yt-dlp stderr is not captured into exceptions or the task log.
  - In local mode, stdout lands in the uvicorn console. That is `data/gui/backend.log`, which has no timestamps and is mostly access-log lines from GUI polling.
  - In colab mode, the subprocess output goes to `<workspace>/tasks/<task>/leases/<lease>/worker.log` on Colab disk, which disappears with the runtime. Only lines that pass `colab_worker.console_line()` are relayed to the GUI.
- **Relayed Colab lines** are written as `[<Colab UTC time>] [Colab] …`, one timestamp per line, with re-sent batches dropped. Each stage's fresh runner still re-logs `Task started`, `Device plan` and `Reused cached output` for every earlier stage.
- **Per-stage transfer timing** reaches the task log in two forms:
  - the worker's `[Colab] [transfer] Input: …` line (stages restored from Drive and files fetched from the GUI), `Output: …` line (GUI upload and Drive checkpoint), and `Re-running …` line;
  - one coordinator line per stage, `[transfer] stage <name>: input … ; process … ; GUI upload … ; Drive checkpoint … ; committed N files (X MiB), D deleted in T s`.

  Use these, not the GUI stage durations, to see where time goes.

## Known bottlenecks and fragility (observed 2026-10-09)

The user's main complaints are slowness, crashes, logs and timings that can't be traced, and wasted file transfer. Measure before and after any change, using the per-stage `[transfer]` lines.

**Baseline before the per-stage transfer (2026-10-09).** Task `hqMh33ft1Pw` was a 10.8-minute video on a T4 that took 42.3 minutes end to end. The breakdown was reconstructed from lease archive timestamps and stage times.
- **Stage compute: 22.6 minutes.**
  - TTS: 14.7 minutes for 288 clips, about 3 s per clip, sequential VoxCPM with `VOXCPM_OPTIMIZE=false`.
  - Demucs: 2.7 minutes. Translation: 2.2 minutes.
  - `merge_video`: 2.5 minutes. It used CPU libx264, because the NVENC probe failed on Colab.
- **Overhead between stages: 18.6 minutes.**
  - The coordinator was running pre-incremental code and never restarted, so every stage zipped the whole session and sent it both ways.
  - Uploads took 10 minutes in total: 2.8 GB at about 4.4 MB/s, sent as sequential, latency-bound 8 MiB PUTs. Downloads through the same tunnel ran at 12 MB/s or more (a lower bound from timestamps), with 2.2 GB in total. The final session was only about 0.6 GB zipped.
  - Zip and unzip on both sides took about 4.3 minutes.
  - Download plus subprocess start took about 3.8 minutes.

The per-stage transfer removes most of that overhead. With Drive checkpoints, only GUI files cross the tunnel: about 112 MiB of the 743 MiB session in that task, almost all of it the final video. What remains:
- **TTS dominates compute.** It took 14.7 of the 22.6 compute minutes in the baseline task. VoxCPM generates one clip at a time, and torch.compile is off on Colab (`VOXCPM_OPTIMIZE=false`).
- **NVENC is not used on Colab.** `merge_video` falls back to CPU libx264. Check why `ffmpeg_binary()` fails the `h264_nvenc` probe there.
- **One Python process per stage.** Each stage imports torch and validates the device again, which costs roughly 10–20 s per stage.
- **Legacy sessions are still full-size locally.** Tasks from before the Drive change keep their complete sessions under `workfolder/_remote/<task>/<lease>/session`, about 3.9 GB for 8 tasks. That's the only copy of their intermediates, so resuming one fetches its files from the GUI once per runtime.
- **The Drive checkpoint is on the critical path.** It is written before finish so the stage is durable. On Drive FUSE the write lands in a local cache first, and a runtime that dies before the background upload finishes loses that last checkpoint; that stage then re-runs. Deleted checkpoints go to Drive's trash, which still counts against the quota until it is emptied.
- **Redundant encoding.** `local_video._transcode_to_mp4` always re-encodes uploads with libx264. `merge_video` re-encodes the video even for dubbing-only output with no burned-in subtitles. `ytdlp._download_with_format_candidates` tries all four format selectors after any error, not only "format unavailable".
- **Long-video scaling.** `audio.merge_tts_audio` decodes each TTS clip up to three times with librosa and builds the track with `np.concatenate` in a loop, which is quadratic. `split_audio` and SenseVoice load the full vocals track into memory through pydub. SenseVoice reports no progress at all.
