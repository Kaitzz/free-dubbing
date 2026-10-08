"""Bounded, path-checked transport archives shared by coordinator and worker."""
from pathlib import Path, PurePosixPath
import json
import re
import stat
from zipfile import ZipFile, ZIP_DEFLATED

MAX_ARCHIVE_BYTES = 16 * 1024**3

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
            if any(re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?', p)
                   or p.endswith((' ', '.')) for p in path.parts):
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
            with z.open(item) as source, target.open('xb') as output:
                while chunk := source.read(1024*1024):
                    output.write(chunk)

def pack(destination, roots):
    with ZipFile(destination, 'w', compression=ZIP_DEFLATED, compresslevel=1) as z:
        for prefix, root in roots.items():
            if not root or not Path(root).exists():
                continue
            root = Path(root).resolve()
            for path in sorted(root.rglob('*')):
                if path.is_symlink():
                    raise ValueError('Cannot transport symlinks')
                if path.is_file():
                    z.write(path, f'{prefix}/{path.relative_to(root).as_posix()}')

def rebase_local_info(session, uploads):
    path = session/'metadata/local_info.json'
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding='utf-8'))
    subtitles = list((uploads/'subtitle').glob('*'))
    videos = list((uploads/'video').glob('*'))
    if data.get('subtitle_path'):
        if len(subtitles) != 1:
            raise ValueError('Checkpoint is missing uploaded subtitles')
        data['subtitle_path'] = str(subtitles[0])
    if videos:
        data['original_path'] = str(videos[0])
    path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
