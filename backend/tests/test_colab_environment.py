import subprocess
from types import SimpleNamespace
import pytest
from scripts import colab_environment as env


def test_incomplete_environment_is_repaired_without_deletion(tmp_path, monkeypatch):
    venv = tmp_path/'.venv-colab'
    venv.mkdir()
    marker = venv/'keep.txt'; marker.write_text('existing data')
    readiness = iter([False, True])
    monkeypatch.setattr(env, 'environment_ready', lambda p: next(readiness))
    calls = []
    monkeypatch.setattr(env, 'run_visible', lambda command, **kw: calls.append((command, kw)))
    env.ensure_environment(tmp_path)
    assert len(calls) == 2
    assert '--target' in calls[0][0]
    assert '--seeder' in calls[1][0] and 'app-data' in calls[1][0]
    assert '--system-site-packages' in calls[1][0]
    assert '--clear' not in calls[1][0]
    assert marker.read_text() == 'existing data'


def test_healthy_environment_is_reused(tmp_path, monkeypatch):
    monkeypatch.setattr(env, 'environment_ready', lambda p: True)
    monkeypatch.setattr(env, 'run_visible', lambda *a, **k: pytest.fail('Unexpected install'))
    assert env.ensure_environment(tmp_path).parent.parent == tmp_path/'.venv-colab'


def test_python_alone_does_not_mean_healthy(tmp_path, monkeypatch):
    directory = tmp_path/'.venv-colab'
    python = directory/('Scripts/python.exe' if env.os.name=='nt' else 'bin/python')
    python.parent.mkdir(parents=True);python.write_bytes(b'partial')
    monkeypatch.setattr(env.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1))
    assert not env.environment_ready(directory)


def test_failure_prints_underlying_error(monkeypatch, capsys):
    monkeypatch.setattr(env.subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(a[0], 1, 'actual failure reason'))
    with pytest.raises(subprocess.CalledProcessError):
        env.run_visible(['failed-command'])
    assert 'actual failure reason' in capsys.readouterr().out
