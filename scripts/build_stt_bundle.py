"""Package only STT source files for Colab; never include .env, caches or credentials."""
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parents[1]
files = sorted((root / "backend/app").rglob("*.py"))
files += [root / name for name in (
    "requirements-stt.txt", "scripts/run_stt.py", "COLAB_STT.md",
    "notebooks/SenseVoice_Colab.ipynb",
)]
target = root / "youdub-stt-colab.zip"
with ZipFile(target, "w", compression=ZIP_DEFLATED) as archive:
    for path in files:
        archive.write(path, path.relative_to(root).as_posix())
print(f"Created {target.name}: {len(files)} source files; no environment or runtime data")
