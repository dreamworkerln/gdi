"""Read the latest started job's local console without worker or remote access."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import time

from .exchange import hex_value
from .git import GdiError


def started(spool):
    # Rejected and cancelled-before-start requests also have a build.log.
    paths = [spool / 'status.json', spool / 'result.json']
    paths.extend(sorted((spool / 'events').glob('*.json')))
    for path in paths:
        if not path.exists():
            continue
        try:
            value = json.loads(path.read_bytes())
        except (ValueError, UnicodeError) as exc:
            raise GdiError('invalid local job metadata: ' + str(path)) from exc
        if not isinstance(value, dict):
            raise GdiError('invalid local job metadata: ' + str(path))
        if value.get('state') == 'RUNNING' or value.get('stages'):
            return True
    return False


def latest_job(config):
    root = Path(config['state_dir']) / config['worker_id']
    ledger = root / 'ledger.sqlite3'
    if ledger.is_file():
        try:
            # Do not acquire the worker lock, create a database, or migrate it.
            with closing(sqlite3.connect(ledger.resolve().as_uri() + '?mode=ro', uri=True)) as db:
                jobs = db.execute('SELECT job_id FROM jobs ORDER BY sequence DESC').fetchall()
        except sqlite3.Error as exc:
            raise GdiError('cannot read local worker ledger: ' + str(exc)) from exc
        for (job_id,) in jobs:
            if not hex_value(job_id, 32):
                raise GdiError('invalid job ID in local worker ledger')
            spool = root / 'jobs' / job_id
            if started(spool):
                return job_id, spool
    raise GdiError('no locally started jobs for worker ' + config['worker_id'])


def tail_start(handle, end, lines):
    """Find the history boundary in bounded blocks, preserving partial lines."""
    if not lines or not end:
        return end
    handle.seek(end - 1)
    needed = lines + (handle.read(1) == b'\n')
    position = end
    while position:
        beginning = max(0, position - 8192)
        handle.seek(beginning)
        block = handle.read(position - beginning)
        count = block.count(b'\n')
        if count >= needed:
            index = len(block)
            for _ in range(needed):
                index = block.rfind(b'\n', 0, index)
            return beginning + index + 1
        needed -= count
        position = beginning
    return 0


def logs(config, *, lines=10, follow=False):
    if type(lines) is not int or lines < 0:
        raise GdiError('log line count must be a nonnegative integer')
    job_id, spool = latest_job(config)
    with (spool / 'build.log').open('rb') as handle:
        handle.seek(0, 2)
        end = handle.tell()
        handle.seek(tail_start(handle, end, lines))
        print('gdi: local job ' + job_id, file=sys.stderr, flush=True)
        output = sys.stdout.buffer
        while handle.tell() < end:
            output.write(handle.read(min(65536, end - handle.tell())))
            output.flush()
        if not follow:
            return 0
        while True:
            data = handle.read(65536)
            if data:
                output.write(data)
                output.flush()
                continue
            if (spool / 'result.json').is_file():
                # The final append may have raced the preceding EOF read.
                data = handle.read(65536)
                if data:
                    output.write(data)
                    output.flush()
                    continue
                return 0
            time.sleep(.1)
