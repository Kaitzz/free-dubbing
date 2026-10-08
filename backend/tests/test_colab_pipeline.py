import json
from pathlib import Path
from zipfile import ZipFile
import pytest
from scripts import colab_pipeline as cli
from scripts.build_pipeline_bundle import build
from backend.app import database, config, pipeline, gpu_memory


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DB_PATH', tmp_path/'tasks.sqlite')
    monkeypatch.setattr(config, 'LOG_DIR', tmp_path/'logs')
    (tmp_path/'logs').mkdir()
    monkeypatch.setattr(config, 'WORKFOLDER', tmp_path/'work')
    monkeypatch.setattr(config, 'ensure_runtime_dirs', lambda: None)
    database.init_db()
    return tmp_path


def test_local_registration_and_resuming_same_task(isolated_db):
    root = isolated_db
    video = root/'lecture.mp4'
    video.write_bytes(b'video')
    state = root/'state.json'
    task_id = cli.create_local_task(video, 'en-zh', 'both', state)
    assert database.get_task(task_id)['output_mode'] == 'both'
    assert 'direction=en-zh' in database.get_task(task_id)['url']
    assert (root/'work/_uploads'/task_id/'video/input.mp4').read_bytes() == b'video'
    with pytest.raises(ValueError, match='already exists'):
        cli.create_local_task(video, 'en-zh', 'both', state)
    database.update_stage(task_id, 'download', status='succeeded')
    database.update_stage(task_id, 'asr', status='failed')
    database.update_task(task_id, status='failed')
    assert cli.resume_task(state) == task_id
    assert database.get_task(task_id)['status'] == 'queued'
    assert {s['name']:s['status'] for s in database.get_task(task_id)['stages']}['download'] == 'succeeded'
    database.update_task(task_id, status='running')
    with pytest.raises(ValueError, match='second process'):
        cli.resume_task(state)


def test_export_whitelist_excludes_secrets_and_failed_results(tmp_path):
    session = tmp_path/'session'
    (session/'metadata').mkdir(parents=True)
    (session/'media').mkdir()
    video = session/'media/video_final.mp4'
    video.write_bytes(b'video')
    (session/'.env').write_text('SECRET')
    (session/'metadata/private.json').write_text('SECRET')
    (session/'metadata/asr.json').write_text('{}')
    task = dict(status='succeeded', session_path=str(session), final_video_path=str(video))
    destination = tmp_path/'result.zip'
    cli.export_result(task, destination)
    with ZipFile(destination) as z:
        assert set(z.namelist()) == {'media/video_final.mp4','metadata/asr.json'}
    task['status'] = 'failed'
    with pytest.raises(ValueError, match='incomplete'):
        cli.export_result(task, tmp_path/'failed.zip')


def test_cli_runs_all_real_pipeline_stage_transitions(isolated_db, monkeypatch):
    import sys
    root = isolated_db
    video = root/'input.mp4'; video.write_bytes(b'video')
    session = root/'session'; (session/'media').mkdir(parents=True)
    final = session/'media/video_final.mp4'; final.write_bytes(b'result')
    visited = []
    from backend.app.stages import STAGES
    def handler(name):
        def run(self, task):
            visited.append(name)
            self.artifacts.session = session
            self.artifacts.final_video = final
            database.update_task(self.task_id, session_path=str(session))
        return run
    for stage in STAGES:
        monkeypatch.setattr(pipeline.PipelineRunner, '_'+stage.name, handler(stage.name))
    monkeypatch.setattr(pipeline, 'validate_runtime_device', lambda: None)
    monkeypatch.setattr(gpu_memory, 'release_task_memory', lambda: {})
    monkeypatch.setattr(database, 'get_openai_settings', lambda: {'api_key':'test'})
    monkeypatch.setattr(sys, 'argv', ['colab_pipeline.py', '--video', str(video),
        '--state', str(root/'state.json'), '--result', str(root/'result.zip')])
    assert cli.main() == 0
    assert visited == [s.name for s in STAGES]
    task_id = json.loads((root/'state.json').read_text())['task_id']
    assert database.get_task(task_id)['status'] == 'succeeded'
    assert (root/'result.zip').is_file()


def test_bundle_and_notebook_cells(tmp_path):
    root = Path(__file__).resolve().parents[2]
    bundle = tmp_path/'source.zip'
    build(root, bundle)
    with ZipFile(bundle) as z:
        names = z.namelist()
        assert 'scripts/colab_pipeline.py' in names
        assert not any(p.endswith('.env') or '/data/' in p or 'workfolder' in p for p in names)
        notebook = json.loads(z.read('notebooks/YouDub_Pipeline_Colab.ipynb'))
        for cell in notebook['cells']:
            if cell['cell_type'] == 'code':
                compile(''.join(cell['source']), '<colab-cell>', 'exec')


def test_killed_task_recovery_preserves_completed_stages(isolated_db, monkeypatch):
    root = isolated_db
    task_id = database.create_task('https://www.youtube.com/watch?v=abcdefghijk')
    database.update_task(task_id, status='running', current_stage='tts')
    database.update_stage(task_id, 'translate', status='succeeded')
    database.update_stage(task_id, 'tts', status='running')
    state = root/'state.json';state.write_text(json.dumps({'task_id':task_id}))
    monkeypatch.setattr(cli.os, 'kill', lambda *a: None)
    with pytest.raises(ValueError, match='still exists'):
        cli.resume_task(state, dead_pid=1234)
    def absent(*a):raise ProcessLookupError()
    monkeypatch.setattr(cli.os, 'kill', absent)
    assert cli.resume_task(state, dead_pid=1234) == task_id
    task = database.get_task(task_id)
    stages = {s['name']:s['status'] for s in task['stages']}
    assert task['status'] == 'queued'
    assert stages['translate'] == 'succeeded' and stages['tts'] == 'pending'
