"""Package reviewed application source, excluding secrets and runtime data."""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

def build(root, target):
    paths = sorted((root / 'backend/app').rglob('*.py'))
    paths += [root / name for name in (
        'requirements.txt', 'requirements-stt.txt', 'requirements-colab.txt',
        'scripts/colab_worker.py', 'scripts/remote_job.py', 'notebooks/YouDub_GUI_Colab.ipynb',
        'scripts/colab_pipeline.py', 'scripts/colab_preflight.py', 'scripts/colab_environment.py', 'scripts/run_colab_task_cell.py',
        'notebooks/YouDub_Pipeline_Colab.ipynb', 'COLAB_PIPELINE.md', 'LICENSE',
    )]
    with ZipFile(target, 'w', compression=ZIP_DEFLATED) as z:
        for p in paths:
            if p.is_symlink() or not p.resolve().is_relative_to(root.resolve()):
                raise ValueError('Unexpected source path')
            z.write(p, p.relative_to(root).as_posix())
    return len(paths)

if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]
    print('Packaged', build(root, root/'youdub-pipeline-colab.zip'), 'source files; no credentials or runtime data')
