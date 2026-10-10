"""Explicit systemd --user integration; no login-shell or working-directory dependency."""

import os
from pathlib import Path
import sys
import subprocess

from .git import GdiError, run

UNIT = 'gdi-worker.service'


def quote(value):
    if '\n' in value or '\r' in value or '\x00' in value:
        raise GdiError('invalid systemd argument')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'


def unit(config_path):
    return f'''[Unit]
Description=GDI autonomous CI worker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={quote(str(Path(sys.executable).absolute()))} -m gdi worker run --config {quote(str(Path(config_path).expanduser().resolve()))}
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=10
TimeoutStopSec=120
KillMode=mixed
UMask=0077
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
'''


def install(config_path):
    from .worker_config import load_config
    load_config(config_path)
    base = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config')))
    path = base / 'systemd/user' / UNIT
    path.parent.mkdir(parents=True, exist_ok=True)
    from .ci_protocol import atomic_write
    atomic_write(path, unit(config_path).encode())
    run(['systemctl', '--user', 'daemon-reload'])
    return path


def action(operation):
    if operation == 'start':
        return run(['systemctl', '--user', 'enable', '--now', UNIT]).stdout
    if operation == 'stop':
        return run(['systemctl', '--user', 'stop', UNIT]).stdout
    if operation == 'restart':
        run(['systemctl', '--user', 'daemon-reload'])
        return run(['systemctl', '--user', 'restart', UNIT]).stdout
    result = run(['systemctl', '--user', 'show', UNIT, '--property=ActiveState,SubState,ExecMainStatus,UnitFileState'])
    return dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)


def logs(*, lines=100, follow=False, boot=False):
    """Stream the user service journal directly, including journalctl errors."""
    if type(lines) is not int or lines < 0:
        raise GdiError('log line count must be a nonnegative integer')
    args = ['journalctl', '--user', '--unit=' + UNIT, '--no-pager', '--lines=' + str(lines)]
    if boot:
        args.append('--boot')
    if follow:
        args.append('--follow')
    from .diagnostics import subprocess_call
    with subprocess_call(args) as timing:
        try:
            with subprocess.Popen(args) as process:
                try:
                    code = process.wait()
                except KeyboardInterrupt:
                    process.terminate()
                    process.wait()
                    raise
        except FileNotFoundError as exc:
            raise GdiError('journalctl not found in PATH') from exc
        timing['result'] = subprocess.CompletedProcess(args, code, stdout='', stderr='')
    return code if code >= 0 else 128 - code
