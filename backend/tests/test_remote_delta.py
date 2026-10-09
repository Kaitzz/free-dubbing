import json
from zipfile import ZipFile
import pytest
from backend.app.remote_delta import manifest, pack_delta, restore, MANIFEST, validate_manifest


def test_delta_restores_changed_deleted_and_cached_files(tmp_path):
    base = tmp_path/'base'; base.mkdir()
    (base/'video.mp4').write_bytes(b'video' * 100000)
    (base/'old.txt').write_text('obsolete')
    before = manifest({'session':base})
    current = tmp_path/'current'; current.mkdir()
    (current/'video.mp4').write_bytes((base/'video.mp4').read_bytes())
    (current/'translation.json').write_text('{"text":"new"}')
    archive = tmp_path/'delta.zip'
    expected = pack_delta(archive, {'session':current}, before)
    with ZipFile(archive) as z:
        assert set(z.namelist()) == {MANIFEST, 'session/translation.json'}
    restore(archive, tmp_path/'out', {'session':base})
    assert manifest({'session':tmp_path/'out/session'}) == expected
    assert not (tmp_path/'out/session/old.txt').exists()
    assert (base/'old.txt').read_text() == 'obsolete'


def test_changed_file_and_empty_cache_recovery(tmp_path):
    source = tmp_path/'source'; source.mkdir()
    (source/'a').write_text('old')
    before = manifest({'session':source})
    (source/'a').write_text('new')
    for name, baseline in [('changed', before), ('restart', {})]:
        archive = tmp_path/(name+'.zip')
        expected = pack_delta(archive, {'session':source}, baseline)
        restore(archive, tmp_path/name, {})
        assert manifest({'session':tmp_path/name/'session'}) == expected


@pytest.mark.parametrize('missing', [False, True])
def test_corrupt_or_missing_base_is_rejected(tmp_path, missing):
    source = tmp_path/'source'; source.mkdir()
    (source/'a').write_text('original')
    archive = tmp_path/'delta.zip'
    pack_delta(archive, {'session':source}, manifest({'session':source}))
    if missing: (source/'a').unlink()
    else: (source/'a').write_text('corrupted')
    with pytest.raises((ValueError, OSError)):
        restore(archive, tmp_path/'out', {'session':source})


@pytest.mark.parametrize('name', ['session/../outside','/session/a','session/CON','session/a:b','session/a\\b'])
def test_manifest_paths_rejected(name):
    with pytest.raises(ValueError):
        validate_manifest({name:{'size':0,'sha256':'0'*64}})


def test_empty_checkpoint_does_not_create_fake_session(tmp_path):
    archive = tmp_path/'empty.zip'
    pack_delta(archive, {}, {})
    restore(archive, tmp_path/'out', {})
    assert not (tmp_path/'out/session').exists()


def test_tampered_transferred_file_rejected(tmp_path):
    archive = tmp_path/'bad.zip'
    with ZipFile(archive, 'w') as z:
        z.writestr(MANIFEST, json.dumps({'session/a':{'size':3,'sha256':'0'*64}}))
        z.writestr('session/a', 'bad')
    with pytest.raises(ValueError, match='checksum'):
        restore(archive, tmp_path/'out', {})
