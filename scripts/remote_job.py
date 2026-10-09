"""Execute a single stage in an isolated job database; called by colab_worker."""
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.app import config, database, gpu_memory
from backend.app.pipeline import PipelineRunner

job_dir=Path(sys.argv[1])
task=json.loads((job_dir/'task.json').read_text(encoding='utf-8'))
config.ensure_runtime_dirs()
database.init_db()
database.create_task(task['url'],task_id=task['id'],execution_mode='manual',output_mode=task['output_mode'])
for stage in task['stages']:
    database.update_stage(task['id'],stage['name'],**{k:stage.get(k) for k in
        ('status','progress','started_at','completed_at','last_message','error_message')})
session=config.WORKFOLDER/'session'
if session.exists():
    database.update_task(task['id'],session_path=str(session),title=task.get('title'))

class ReportingRunner(PipelineRunner):
    def log(self,message):
        super().log(message)
        print(message,flush=True)

try:
    ReportingRunner(task['id']).run()
finally:
    gpu_memory.release_task_memory()
