"""Create a Colab venv with bundled pip; do not depend on system ensurepip."""
import os
from pathlib import Path
import subprocess
import sys


def run_visible(command, **kwargs):
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, **kwargs)
    if result.stdout:
        print(result.stdout, end='', flush=True)
    result.check_returncode()
    return result


def environment_ready(directory):
    python = directory / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if not python.is_file():
        return False
    probe = (
        'import sys,pip; from pathlib import Path; '
        'root=Path(sys.argv[1]).resolve(); '
        'assert Path(sys.prefix).resolve()==root; '
        'assert Path(pip.__file__).resolve().is_relative_to(root); '
        'assert "include-system-site-packages = true" in '
        '(root/"pyvenv.cfg").read_text().lower()'
    )
    try:
        result = subprocess.run([str(python), '-c', probe, str(directory)],
                                capture_output=True, text=True)
        return result.returncode == 0
    except OSError:
        return False


def ensure_environment(root):
    root = Path(root).resolve()
    directory = root / '.venv-colab'
    if not environment_ready(directory):
        # The bootstrap is kept outside both the notebook kernel's packages and
        # the target venv. virtualenv supplies pip without invoking ensurepip.
        bootstrap = root / '.virtualenv-bootstrap'
        run_visible([sys.executable, '-m', 'pip', 'install', '--upgrade',
                     '--target', str(bootstrap), 'virtualenv==20.35.1'])
        env = os.environ.copy()
        env['PYTHONPATH'] = str(bootstrap)
        run_visible([sys.executable, '-m', 'virtualenv', '--system-site-packages',
                     '--seeder', 'app-data', str(directory)], env=env)
        if not environment_ready(directory):
            raise RuntimeError('Environment creation finished but isolated pip/system-site-packages validation failed')
    python = directory / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    print('Environment ready:', python, flush=True)
    return python


if __name__ == '__main__':
    ensure_environment(Path(sys.argv[1]))
