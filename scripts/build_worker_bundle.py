from pathlib import Path
from build_pipeline_bundle import build
root=Path(__file__).resolve().parents[1]
print('Packaged',build(root,root/'youdub-colab-worker.zip'),'source files; no task media, cookies or credentials')
