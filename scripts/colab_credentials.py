"""Handle Colab-only cookies outside task checkpoints and logs."""
from pathlib import Path
from backend.app import runtime_security


def write_youtube_cookie(value: str, data_dir: Path) -> Path | None:
    if not value or not value.strip():
        return None
    error = 'YOUTUBE_COOKIES must contain Netscape cookies.txt content exported for YouTube'
    if len(value) > 1024*1024:
        raise ValueError(error)
    text = value.lstrip('\ufeff').replace('\r\n', '\n').replace('\r', '\n')
    if not text.startswith(('# Netscape HTTP Cookie File', '# HTTP Cookie File')):
        raise ValueError(error)
    records = []
    for line in text.splitlines():
        if not line.strip() or (line.startswith('#') and not line.startswith('#HttpOnly_')):
            continue
        fields = line.removeprefix('#HttpOnly_').split('\t')
        if len(fields) != 7:
            raise ValueError(error)
        domain, include, path, secure, expires, name, cookie_value = fields
        if (include not in {'TRUE','FALSE'} or secure not in {'TRUE','FALSE'}
                or not path.startswith('/') or not expires.isdigit() or not name):
            raise ValueError(error)
        host = domain.lstrip('.').lower()
        # Ignore unrelated sites from broader browser exports.
        if host == 'youtube.com' or host.endswith('.youtube.com'):
            records.append(line)
    if not records:
        raise ValueError(error)
    target = data_dir/'cookies/youtube.txt'
    runtime_security.ensure_private_directory(target.parent)
    runtime_security.atomic_write_private_text(target, '# Netscape HTTP Cookie File\n'+'\n'.join(records)+'\n')
    return target
