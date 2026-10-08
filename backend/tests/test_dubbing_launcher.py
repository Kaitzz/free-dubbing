import io
import json
import subprocess
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_launcher_uses_one_commit_and_stops_on_working_cell_failure(monkeypatch):
    nb = json.loads((ROOT/'notebooks/Dubbing_Launcher.ipynb').read_text(encoding='utf-8'))
    sha = 'a'*40
    monkeypatch.setattr(subprocess, 'check_output', lambda *a, **kw: sha+' refs/heads/main')
    fetched = []
    working = {'cells': [
        {'cell_type':'markdown', 'source':['raise AssertionError()']},
        {'cell_type':'code', 'source':["executed = [DUBBING_SOURCE_COMMIT]"]},
        {'cell_type':'code', 'source':["raise ValueError('installation failed')"]},
        {'cell_type':'code', 'source':["executed.append('must not run')"]},
    ]}
    def fetch(url, **kw):
        fetched.append(url)
        return io.BytesIO(json.dumps(working).encode())
    monkeypatch.setattr(urllib.request, 'urlopen', fetch)
    scope = {'DUBBING_PASSWORD':'01234567', 'DUBBING_VERSION':'main'}
    with pytest.raises(RuntimeError, match='aaaaaaa'):
        exec(''.join(nb['cells'][2]['source']), scope)
    assert scope['executed'] == [sha]
    assert f'/{sha}/notebooks/' in fetched[0]


def test_all_notebook_cells_compile_without_saved_output():
    for p in (ROOT/'notebooks').glob('*.ipynb'):
        nb=json.loads(p.read_text(encoding='utf-8'))
        for c in nb['cells']:
            if c['cell_type']=='code':
                compile(''.join(c['source']), str(p), 'exec')
                assert not c.get('outputs')
