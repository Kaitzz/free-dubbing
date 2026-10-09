"""Content-verified incremental checkpoints; reconstruction never mutates the base."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
from zipfile import ZipFile, ZIP_DEFLATED
from .remote_archive import unpack, MAX_ARCHIVE_BYTES

MANIFEST = 'session/.checkpoint-manifest.json'
MAX_MANIFEST = 16 * 1024 * 1024


def digest(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def validate_manifest(value):
    if not isinstance(value, dict) or len(value) > 100000:
        raise ValueError('Invalid checkpoint manifest')
    seen = set()
    total = 0
    for name, entry in value.items():
        path = PurePosixPath(name)
        if (len(path.parts) < 2 or path.parts[0] not in {'session', 'uploads'}
                or path.as_posix() != name or '\\' in name or ':' in name
                or any(p in {'.', '..'} or p.endswith((' ', '.'))
                       or re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?', p)
                       for p in path.parts) or name == MANIFEST or name.casefold() in seen):
            raise ValueError('Unsafe checkpoint path')
        seen.add(name.casefold())
        if (not isinstance(entry, dict) or type(entry.get('size')) is not int
                or entry['size'] < 0 or not isinstance(entry.get('sha256'), str)
                or not re.fullmatch('[a-f0-9]{64}', entry['sha256'])):
            raise ValueError('Invalid checkpoint fingerprint')
        total += entry['size']
    if total > MAX_ARCHIVE_BYTES:
        raise ValueError('Checkpoint exceeds size budget')
    return value


def source_path(roots, name):
    prefix, relative = name.split('/', 1)
    root = roots.get(prefix)
    if root is None:
        raise ValueError('Missing checkpoint base')
    root = Path(root).resolve()
    path = root / relative
    if not path.resolve().is_relative_to(root) or path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError('Unsafe checkpoint base file')
    return path


def manifest(roots):
    result = {}
    for prefix, root in roots.items():
        if root is None or not Path(root).exists():
            continue
        root = Path(root)
        if root.is_symlink():
            raise ValueError('Linked checkpoint root')
        for path in sorted(root.rglob('*')):
            if path.is_symlink():
                raise ValueError('Linked checkpoint file')
            if path.is_file():
                result[f'{prefix}/{path.relative_to(root).as_posix()}'] = {
                    'size': path.stat().st_size, 'sha256': digest(path)}
    return validate_manifest(result)


def pack_delta(destination, roots, base):
    validate_manifest(base)
    current = manifest(roots)
    encoded = json.dumps(current, ensure_ascii=True).encode()
    if len(encoded) > MAX_MANIFEST:
        raise ValueError('Checkpoint manifest too large')
    with ZipFile(destination, 'w', compression=ZIP_DEFLATED, compresslevel=1) as z:
        z.writestr(MANIFEST, encoded)
        for name, entry in current.items():
            if base.get(name) != entry:
                z.write(source_path(roots, name), name)
    return current


def restore(archive, destination, base_roots):
    with ZipFile(archive) as z:
        if MANIFEST not in z.namelist():
            unpack(archive, destination)
            return
        if z.getinfo(MANIFEST).file_size > MAX_MANIFEST:
            raise ValueError('Checkpoint manifest too large')
        expected = validate_manifest(json.loads(z.read(MANIFEST)))
        if any(not i.is_dir() and i.filename != MANIFEST and i.filename not in expected for i in z.infolist()):
            raise ValueError('Unexpected checkpoint file')
    destination = Path(destination)
    unpack(archive, destination)
    (destination / MANIFEST).unlink()
    if not any(name.startswith('session/') for name in expected):
        (destination / 'session').rmdir()
    for name, entry in expected.items():
        target = destination / name
        if not target.exists():
            source = source_path(base_roots, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        if target.stat().st_size != entry['size'] or digest(target) != entry['sha256']:
            raise ValueError('Checkpoint checksum mismatch: ' + name)
