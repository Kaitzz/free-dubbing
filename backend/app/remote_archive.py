"""Bounded, path-checked transport archives shared by coordinator and worker."""
from pathlib import Path, PurePosixPath
import json
import re
import stat
from zipfile import ZipFile, ZipInfo, ZIP_STORED

MAX_ARCHIVE_BYTES = 16 * 1024**3
_RESERVED = re.compile(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?')


def safe_name(name):
    """Return name if it is a portable relative path under session/ or uploads/."""
    path = PurePosixPath(name) if isinstance(name, str) else None
    if (path is None or len(path.parts) < 2 or path.parts[0] not in {'session', 'uploads'}
            or path.as_posix() != name or '\\' in name or ':' in name
            or any(p in {'.', '..'} or p.endswith((' ', '.')) or _RESERVED.fullmatch(p) for p in path.parts)):
        raise ValueError('Unsafe transport path')
    return name


def unpack(archive, destination):
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    with ZipFile(archive) as z:
        total = 0
        seen = set()
        for item in z.infolist():
            path = PurePosixPath(item.filename)
            if (not path.parts or path.is_absolute() or any(p in {'.','..'} for p in path.parts)
                    or chr(92) in item.filename or ':' in item.filename or item.filename.startswith('/')
                    or path.parts[0] not in {'session', 'uploads'}
                    or stat.S_ISLNK(item.external_attr >> 16)):
                raise ValueError('Unsafe archive path')
            if any(_RESERVED.fullmatch(p) or p.endswith((' ', '.')) for p in path.parts):
                raise ValueError('Unsafe platform filename')
            key = item.filename.casefold()
            if key in seen:
                raise ValueError('Duplicate archive path')
            seen.add(key)
            total += item.file_size
            if total > MAX_ARCHIVE_BYTES or len(seen) > 100000:
                raise ValueError('Archive exceeds extraction budget')
        for item in z.infolist():
            target = (destination/item.filename).resolve()
            if not target.is_relative_to(destination):
                raise ValueError('Archive escapes destination')
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            # Reading to EOF verifies each entry's CRC-32.
            with z.open(item) as source, target.open('xb') as output:
                while chunk := source.read(1024*1024):
                    output.write(chunk)


def listing(roots, exclude=()):
    """Map transport names to (path, size, mtime_ns) using stat only, never file contents."""
    files = {}
    for prefix, root in roots.items():
        if root is None or not Path(root).exists():
            continue
        root = Path(root)
        if root.is_symlink():
            raise ValueError('Cannot transport symlinks')
        for path in sorted(root.rglob('*')):
            if path.is_symlink():
                raise ValueError('Cannot transport symlinks')
            if not path.is_file():
                continue
            name = safe_name(f'{prefix}/{path.relative_to(root).as_posix()}')
            if not name.startswith(exclude):
                metadata = path.stat()
                files[name] = (path, metadata.st_size, metadata.st_mtime_ns)
    if len(files) > 100000 or sum(size for _, size, _ in files.values()) > MAX_ARCHIVE_BYTES:
        raise ValueError('Checkpoint exceeds size budget')
    return files


def pack_files(destination, files):
    """Store entries uncompressed: audio and video dominate and do not shrink."""
    with ZipFile(destination, 'w', compression=ZIP_STORED, allowZip64=True) as z:
        for name, path in files:
            z.write(path, safe_name(name))


class _Chunks:
    def __init__(self):
        self.parts = []

    def write(self, data):
        self.parts.append(bytes(data))
        return len(data)

    def flush(self):
        pass

    def take(self):
        data = b''.join(self.parts)
        self.parts.clear()
        return data


def stream_files(files, block=1024*1024):
    """Yield an uncompressed zip of files without staging it on disk."""
    sink = _Chunks()
    with ZipFile(sink, 'w', compression=ZIP_STORED, allowZip64=True) as z:
        for name, path in files:
            info = ZipInfo(safe_name(name))
            info.file_size = path.stat().st_size
            with path.open('rb') as source, z.open(info, 'w') as target:
                while data := source.read(block):
                    target.write(data)
                    if sink.parts:
                        yield sink.take()
    yield sink.take()


def rebase_local_info(session, uploads):
    path = session/'metadata/local_info.json'
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding='utf-8'))
    original = dict(data)
    subtitles = list((uploads/'subtitle').glob('*'))
    videos = list((uploads/'video').glob('*'))
    if data.get('subtitle_path'):
        if len(subtitles) != 1:
            raise ValueError('Checkpoint is missing uploaded subtitles')
        data['subtitle_path'] = str(subtitles[0])
    if videos:
        data['original_path'] = str(videos[0])
    # Rewriting an unchanged file would make it look modified to the transfer.
    if data != original:
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
