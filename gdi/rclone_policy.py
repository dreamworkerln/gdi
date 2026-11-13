"""Bounded, private rclone diagnostics and conservative request pacing."""

from datetime import datetime, timezone
import logging
import fcntl
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import time
import threading

_LOCK = threading.Lock()
_LOGS = {}


class PrivateRotatingFileHandler(RotatingFileHandler):
    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_SH)
        return os.fdopen(fd, 'a', encoding='utf-8')


def diagnostic_log(kind='cli'):
    with _LOCK:
        if kind in _LOGS:
            return _LOGS[kind]
        root = Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state'))) / 'gdi/logs'
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        if kind == 'cli':
            # Only our own CLI diagnostics are eligible for retention cleanup.
            candidates = []
            for path in root.glob('cli-rclone-*.log*'):
                try:
                    candidates.append((path.stat().st_mtime, path))
                except FileNotFoundError:
                    pass  # Another CLI may have just rotated/cleaned this log.
            candidates = [path for _, path in sorted(candidates, reverse=True)]
            total = 0
            for path in candidates:
                if path.is_symlink() or not path.is_file():
                    continue
                try:
                    info = path.stat()
                except FileNotFoundError:
                    continue
                total += info.st_size
                if time.time() - info.st_mtime > 7 * 86400 or total > 90 * 1024 * 1024:
                    # Never unlink the log of a currently running CLI process.
                    try:
                        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                    except FileNotFoundError:
                        continue
                    try:
                        try:
                            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            continue
                        path.unlink(missing_ok=True)
                    finally:
                        os.close(fd)
            name = 'cli-rclone-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + str(os.getpid()) + '.log'
        else:
            name = 'worker-rclone.log'
        path = root / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        handler = PrivateRotatingFileHandler(path, maxBytes=(10 if kind == 'worker' else 5) * 1024 * 1024, backupCount=4 if kind == 'worker' else 1, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
        logger = logging.Logger('gdi.rclone.' + kind, logging.DEBUG)
        logger.addHandler(handler)
        def write(line):
            # Proxy userinfo can occur in Go connection errors. Never dump HTTP.
            line = re.sub(r'(https?://|socks5h?://)[^\s/@]+:[^\s/@]+@', r'\1[redacted]@', line)
            logger.debug(line.rstrip())
        _LOGS[kind] = (path, write)
        return path, write


def flags(options):
    return ['--tpslimit', str(options.get('tpslimit', 1)), '--tpslimit-burst', '1',
            '--log-level', 'DEBUG']
