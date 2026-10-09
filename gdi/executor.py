"""Binary console capture and process-group timeouts without network in the reader."""

import os
from pathlib import Path
import selectors
import signal
import subprocess
import time

from .git import GdiError


def process_identity(pid):
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
        return {"pid": pid, "start_time": text.rsplit(')', 1)[1].split()[19],
                "boot_id": Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
    except (OSError, IndexError):
        return None


def kill_owned(identity):
    if identity and process_identity(identity["pid"]) == identity:
        try:
            os.killpg(identity["pid"], signal.SIGKILL)
        except ProcessLookupError:
            pass


def inside(root, relative):
    target = (root / relative).resolve()
    if not target.is_relative_to(root.resolve()):
        raise GdiError("profile path/symlink escapes the job checkout")
    return target


def execute(checkout, profile, log, update, process_changed):
    started = time.monotonic()
    results, warnings = [], []
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env.update(profile["env"])
    env.update(profile.get("process_env", {}))
    env["PYTHONUNBUFFERED"] = "1"
    with log.open('ab', buffering=0) as output:
        for stage in profile["stages"]:
            update("RUNNING", stage["name"])
            output.write(f"\n=== gdi stage: {stage['name']} ===\n".encode())
            cwd = log.parent if "workflow" in profile else inside(checkout.path, stage["cwd"])
            deadline = min(started + profile["timeout_seconds"], time.monotonic() + stage["timeout_seconds"])
            if time.monotonic() >= deadline:
                return "TIMEOUT", 124, stage["name"], results, warnings
            process = subprocess.Popen(stage["argv"], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
            identity = process_identity(process.pid)
            timed_out = False
            force_kill_at = None
            heartbeat = time.monotonic()
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            os.set_blocking(process.stdout.fileno(), False)
            try:
                process_changed(identity)
                while selector.get_map() or process.poll() is None:
                    if time.monotonic() >= deadline and not timed_out:
                        timed_out = True
                        # The leader may have exited while descendants still hold stdout.
                        try:
                            os.killpg(process.pid, signal.SIGINT if "workflow" in profile else signal.SIGKILL)
                            if "workflow" in profile:
                                force_kill_at = time.monotonic() + 10
                        except ProcessLookupError:
                            pass
                    if force_kill_at is not None and time.monotonic() >= force_kill_at:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        force_kill_at = None
                    for key, _ in selector.select(timeout=0.2):
                        block = os.read(key.fd, 65536)
                        if block:
                            output.write(block)
                        else:
                            selector.unregister(key.fileobj)
                    if time.monotonic() - heartbeat >= 5:
                        update("RUNNING", stage["name"])
                        heartbeat = time.monotonic()
                code = process.wait()
                # Do not leave detached children of a completed stage running on the host.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            except BaseException:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                raise
            finally:
                selector.close()
                process.stdout.close()
                process_changed(None)
            state = "TIMEOUT" if timed_out else "PASS" if code == 0 else "FAIL"
            results.append({"name": stage["name"], "blocking": stage["blocking"], "state": state,
                            "exit_code": 124 if timed_out else code})
            if timed_out:
                return "TIMEOUT", 124, stage["name"], results, warnings
            if code:
                if stage["blocking"]:
                    return "FAIL", code, stage["name"], results, warnings
                warnings.append(f"non-blocking stage {stage['name']} failed: {code}")
        os.fsync(output.fileno())
    if checkout.oid("HEAD") is None or checkout.call('diff', '--quiet', 'HEAD', '--', allowed=(0, 1)).returncode:
        raise GdiError("CI modified tracked sources")
    return "PASS", 0, None, results, warnings
