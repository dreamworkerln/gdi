"""Command timings and subprocess events; payloads and credentials are not logged."""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
import json
import os
from pathlib import Path
import sys
import threading
import time
import uuid


_active = None
_phase = ContextVar('gdi_phase', default='command')


class Session:
    def __init__(self, command, path=None, progress=False):
        self.command = command
        self.path = Path(path).expanduser() if path else None
        self.progress = progress
        self.started = time.monotonic()
        self.session_id = uuid.uuid4().hex
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.running = {}
        self.counts = {}
        self.seconds = {}
        self.rc_count = 0
        self.rc_seconds = 0
        self.sequence = 0
        self.handle = None
        self.thread = None
        self.exit_code = None
        self.error = None

    def event(self, event, **fields):
        if self.handle is not None:
            with self.lock:
                value = {'event': event, 'session_id': self.session_id,
                         'time': datetime.now(timezone.utc).isoformat(),
                         'elapsed_seconds': time.monotonic() - self.started, **fields}
                self.handle.write(json.dumps(value, ensure_ascii=False) + '\n')
                self.handle.flush()

    def show(self, message):
        if self.progress:
            with self.lock:
                print('gdi: ' + message, file=sys.stderr, flush=True)

    def tick(self):
        while not self.stop.wait(2):
            with self.lock:
                for argv, started in self.running.values():
                    if argv[0] == 'RC' or argv[0] == 'rclone' and argv[1] != 'rcd':
                        self.show(f"waiting for {argv[0]} {argv[1]}: {time.monotonic() - started:.1f}s "
                                  f"(total {time.monotonic() - self.started:.1f}s)")

    def begin_call(self, argv):
        started = time.monotonic()
        program = Path(argv[0]).name
        with self.lock:
            self.sequence += 1
            call_id = self.sequence
            self.running[call_id] = (argv, started)
        fields = {'call_id': call_id, 'program': program, 'phase': _phase.get()}
        # Git config and CI arguments can contain private values. Only rclone's
        # controlled transport arguments are recorded, never subprocess output.
        if program == 'rclone':
            fields['argv'] = [str(arg) for arg in argv]
            if self.path is not None:
                self.show(f"rclone {argv[1]} ({_phase.get()})")
        self.event('subprocess_start', **fields)
        return call_id, started, program

    def end_call(self, call, result, error):
        call_id, started, program = call
        duration = time.monotonic() - started
        with self.lock:
            argv, _ = self.running.pop(call_id)
            self.counts[program] = self.counts.get(program, 0) + 1
            self.seconds[program] = self.seconds.get(program, 0) + duration
            fields = {}
            if program == 'rclone' and len(argv) > 3 and argv[1] == 'copyto':
                for direction, candidate in (('source_file_bytes', argv[2]), ('target_file_bytes', argv[3])):
                    local = Path(candidate)
                    try:
                        if local.is_file():
                            fields[direction] = local.stat().st_size
                    except OSError:
                        pass
            self.event('subprocess_finish', call_id=call_id, program=program,
                       phase=_phase.get(), duration_seconds=duration,
                       returncode=result.returncode if result is not None else None,
                       error=type(error).__name__ if error is not None else None,
                       stdout_bytes=len(result.stdout.encode('utf-8')) if result is not None else 0,
                       **fields)
        if program == 'rclone' and self.path is not None:
            self.show(f"rclone finished in {duration:.3f}s")


@contextmanager
def command_session(command, *, path=None, progress=None):
    global _active
    path = path if path is not None else os.environ.get('GDI_PROFILE_LOG')
    if progress is None:
        progress = os.environ.get('GDI_PROGRESS', '').lower() in ('1', 'true', 'yes') or sys.stderr.isatty()
    if not path and not progress:
        yield None
        return
    session = Session(command, path, progress)
    previous = _active
    try:
        if session.path is not None:
            session.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(session.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            session.handle = os.fdopen(fd, 'a', encoding='utf-8')
        _active = session
        session.event('command_start', command=command)
        if progress and session.path is not None:
            session.thread = threading.Thread(target=session.tick, daemon=True)
            session.thread.start()
        yield session
    except BaseException as exc:
        session.error = type(exc).__name__
        raise
    finally:
        session.stop.set()
        if session.thread is not None:
            session.thread.join()
        _active = previous
        if session.handle is not None:
            duration = time.monotonic() - session.started
            session.event('command_finish', command=command, duration_seconds=duration,
                          exit_code=session.exit_code, error=session.error,
                          subprocess_counts=session.counts, subprocess_seconds=session.seconds,
                          rc_calls=session.rc_count, rc_seconds=session.rc_seconds)
            print(f"gdi: total {duration:.3f}s; rclone {session.counts.get('rclone', 0)} processes, "
                  f"{session.seconds.get('rclone', 0):.3f}s" +
                  f"; RC {session.rc_count} calls, {session.rc_seconds:.3f}s" +
                  (f"; profile {session.path}" if session.path is not None else ''), file=sys.stderr)
        if session.handle is not None:
            session.handle.close()


@contextmanager
def subprocess_call(argv):
    session = _active
    result = {'result': None, 'error': None}
    call = session.begin_call(argv) if session is not None else None
    try:
        yield result
    except BaseException as exc:
        result['error'] = exc
        raise
    finally:
        if session is not None:
            session.end_call(call, result['result'], result['error'])


@contextmanager
def rc_call(operation, parameters=None):
    """Record native RC operations separately from processes and Drive HTTP traffic."""
    session = _active
    result = {'status': None}
    started = time.monotonic()
    error = None
    call_id = None
    if session is not None:
        with session.lock:
            session.sequence += 1
            call_id = session.sequence
            session.running[call_id] = (['RC', operation], started)
        session.event('rc_start', call_id=call_id, operation=operation,
                      phase=_phase.get(), parameters=parameters)
        if session.path is not None:
            session.show('RC ' + operation + ' (' + _phase.get() + ')')
    try:
        yield result
    except BaseException as exc:
        error = type(exc).__name__
        raise
    finally:
        if session is not None:
            duration = time.monotonic() - started
            with session.lock:
                session.running.pop(call_id)
                session.rc_count += 1
                session.rc_seconds += duration
                session.event('rc_finish', call_id=call_id, operation=operation,
                              phase=_phase.get(), duration_seconds=duration,
                              status=result['status'], error=error)


@contextmanager
def phase(name):
    session = _active
    if session is None:
        yield
        return
    token = _phase.set(name)
    started = time.monotonic()
    session.event('phase_start', phase=name)
    session.show(name)
    try:
        yield
    finally:
        session.event('phase_finish', phase=name, duration_seconds=time.monotonic() - started)
        _phase.reset(token)


def timed(name):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with phase(name):
                return function(*args, **kwargs)
        return wrapped
    return decorate


def note(message, **fields):
    session = _active
    if session is not None:
        session.event('note', phase=_phase.get(), message=message, **fields)
        if session.path is not None:
            session.show(message)
