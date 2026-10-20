#!/usr/bin/env python3
"""Install exact CI tool versions, reusing cached executables without downloads."""

import io
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import tempfile
from urllib.request import urlopen
import zipfile


def matches(path, expected):
    try:
        result = subprocess.run([str(path), '--version'], capture_output=True, text=True, timeout=30)
        return result.returncode == 0 and result.stdout.splitlines()[0] == expected
    except (OSError, subprocess.TimeoutExpired, IndexError):
        return False


def install(name, version, root, architecture):
    expected = ('act version ' if name == 'act' else 'rclone v') + version
    target = root / (name + '-' + version) / name
    if matches(target, expected):
        print(f'ci-tools: cache hit: {name} {version}', flush=True)
        return target
    if name == 'act':
        archive_arch = {'amd64': 'x86_64', 'arm64': 'arm64'}[architecture]
        url = f'https://github.com/nektos/act/releases/download/v{version}/act_Linux_{archive_arch}.tar.gz'
    else:
        url = f'https://downloads.rclone.org/v{version}/rclone-v{version}-linux-{architecture}.zip'
    print(f'ci-tools: downloading {name} {version}', flush=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Publish only a complete executable whose actual version was checked.
    with tempfile.TemporaryDirectory(prefix='.install-', dir=target.parent) as temporary:
        pending = Path(temporary) / name
        with urlopen(url, timeout=120) as response:
            data = io.BytesIO(response.read())
        with pending.open('wb') as output:
            if name == 'act':
                with tarfile.open(fileobj=data, mode='r:gz') as archive:
                    member = archive.getmember('act')
                    if not member.isfile():
                        raise ValueError('act archive does not contain a regular executable')
                    with archive.extractfile(member) as source:
                        shutil.copyfileobj(source, output)
            else:
                with zipfile.ZipFile(data) as archive:
                    with archive.open(f'rclone-v{version}-linux-{architecture}/rclone') as source:
                        shutil.copyfileobj(source, output)
        pending.chmod(0o755)
        if not matches(pending, expected):
            raise ValueError(f'downloaded {name} version does not match {version}')
        os.replace(pending, target)
    return target


def main():
    architectures = {'x86_64': 'amd64', 'aarch64': 'arm64'}
    if platform.system() != 'Linux' or platform.machine() not in architectures:
        raise ValueError('CI tools require Linux amd64 or arm64')
    architecture = architectures[platform.machine()]
    root = Path.home() / '.cache/gdi-ci-tools' / ('linux-' + architecture)
    tools = {}
    for name in ('act', 'rclone'):
        version = os.environ['GDI_CI_' + name.upper() + '_VERSION']
        if not version or any(part == '' or not part.isascii() or not part.isdigit() for part in version.split('.')):
            raise ValueError('invalid ' + name + ' version')
        tools[name] = install(name, version, root, architecture)
    with Path(os.environ['GITHUB_PATH']).open('a') as output:
        for target in tools.values():
            output.write(str(target.parent) + '\n')
    with Path(os.environ['GITHUB_ENV']).open('a') as output:
        output.write('GDI_TEST_ACT=' + str(tools['act']) + '\n')


if __name__ == '__main__':
    main()
