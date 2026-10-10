"""Git operations with explicit arguments and no inherited repository overrides."""

from contextlib import contextmanager
import fcntl
import os
import re
from pathlib import Path
import subprocess


class GdiError(RuntimeError):
    """An actionable command, transport, or protocol failure."""


def run(args, *, cwd=None, allowed=(0,), isolated_git=False, stderr_line=None):
    from .diagnostics import subprocess_call
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    if isolated_git:
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
                   GIT_CONFIG_NOSYSTEM="1")
    with subprocess_call(args) as timing:
        try:
            if stderr_line is None:
                result = subprocess.run(args, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                        errors="strict")
            else:
                # A temporary stdout avoids pipe deadlocks while we consume stats
                # on stderr; no payload or raw rclone log is sent to the terminal.
                import tempfile
                with tempfile.TemporaryFile() as output:
                    process = subprocess.Popen(args, cwd=cwd, env=env, stdout=output,
                                               stderr=subprocess.PIPE)
                    lines = []
                    try:
                        for line in iter(process.stderr.readline, b''):
                            text = line.decode('utf-8', errors='strict')
                            lines.append(text)
                            stderr_line(text)
                        code = process.wait()
                        output.seek(0)
                        result = subprocess.CompletedProcess(args, code, output.read().decode('utf-8'), ''.join(lines))
                    except BaseException:
                        process.kill()
                        process.wait()
                        raise
                    finally:
                        process.stderr.close()
        except FileNotFoundError as exc:
            raise GdiError(f"{args[0]} not found in PATH") from exc
        timing['result'] = result
        if result.returncode not in allowed:
            detail = result.stderr.strip() or result.stdout.strip()
            raise GdiError(f"{args[0]} failed (exit {result.returncode}): {detail}")
        return result


class Git:
    def __init__(self, path, *, isolated=False):
        self.path = Path(path).resolve()
        self.isolated = isolated

    def call(self, *args, allowed=(0,)):
        return run(["git", "--no-replace-objects", "-c", "core.hooksPath=/dev/null",
                    "-c", "core.fsmonitor=false", "-c", "merge.autostash=false",
                    "-c", "gc.auto=0", "-c", "maintenance.auto=false",
                    *args], cwd=self.path, allowed=allowed, isolated_git=self.isolated)

    def text(self, *args):
        return self.call(*args).stdout.strip()

    @classmethod
    def discover(cls, path="."):
        git = cls(path)
        if git.text("rev-parse", "--is-bare-repository") != "false":
            raise GdiError("gdi requires a git worktree; bare user repositories are unsupported")
        git.path = Path(git.text("rev-parse", "--show-toplevel"))
        if git.text("rev-parse", "--show-object-format") != "sha1":
            raise GdiError("gdi supports only git SHA-1 repositories")
        if git.text("rev-parse", "--is-shallow-repository") != "false":
            raise GdiError("shallow repositories are unsupported; obtain the full history first")
        partial = git.call("config", "--get-regexp", r"^(extensions\.partialclone|remote\..*\.promisor)$",
                           allowed=(0, 1))
        if partial.stdout.strip():
            raise GdiError("partial clones are unsupported; use a full clone")
        if git.text("for-each-ref", "--format=%(refname)", "refs/replace/"):
            raise GdiError("replace refs are unsupported")
        grafts = Path(git.text("rev-parse", "--git-path", "info/grafts"))
        if not grafts.is_absolute():
            grafts = git.path / grafts
        if grafts.exists() and grafts.stat().st_size:
            raise GdiError("git grafts are unsupported")
        return git

    @contextmanager
    def lock(self):
        common = self.gdi_dir()
        with (common / "lock").open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise GdiError("another gdi command is using this repository") from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def common_dir(self):
        common = Path(self.text("rev-parse", "--git-common-dir"))
        return common if common.is_absolute() else self.path / common

    def gdi_dir(self):
        common = self.common_dir().resolve()
        base = common.parent if common.name == ".git" else self.path
        path = base / ".gdi"
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise GdiError(".gdi must be a local directory, not a symlink")
        path.mkdir(exist_ok=True, mode=0o700)
        return path

    def config(self, key):
        result = self.call("config", "--local", "--get-all", key, allowed=(0, 1))
        values = result.stdout.splitlines()
        if len(values) > 1:
            raise GdiError(f"duplicate local config key: {key}")
        return values[0] if values else None

    def branch(self):
        result = self.call("symbolic-ref", "--quiet", "HEAD", allowed=(0, 1))
        if result.returncode or not result.stdout.startswith("refs/heads/"):
            raise GdiError("detached HEAD: switch to a branch or specify a branch for push/fetch")
        return result.stdout.strip()[len("refs/heads/"):]

    def github_repository(self):
        """Read the public namespace, never copy an origin URL or its credentials."""
        origin = self.config("remote.origin.url") or ""
        match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?", origin)
        return match[1] + "/" + match[2] if match else ""

    def ref(self, branch):
        if not branch or branch.startswith("-"):
            raise GdiError("invalid branch name")
        ref = "refs/heads/" + branch
        self.call("check-ref-format", ref)
        return ref

    def oid(self, ref):
        result = self.call("rev-parse", "--verify", "--quiet", ref, allowed=(0, 1))
        return result.stdout.strip() if result.returncode == 0 else None

    def has_commit(self, oid):
        return self.call("cat-file", "-e", oid + "^{commit}", allowed=(0, 1, 128)).returncode == 0

    def snapshot(self):
        return self.branch(), self.oid("HEAD"), self.call("status", "--porcelain=v1", "--untracked-files=all", "--", ".", ":(exclude).gdi").stdout

    def require_clean(self, snapshot):
        if snapshot[2]:
            raise GdiError("working tree is dirty; commit, stash, or remove changes before pull")
        for name in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "sequencer"):
            path = Path(self.text("rev-parse", "--git-path", name))
            if not path.is_absolute():
                path = self.path / path
            if path.exists():
                raise GdiError(f"unfinished git operation ({name}); complete it before pull")

    def ancestor(self, old, new):
        return self.call("merge-base", "--is-ancestor", old, new, allowed=(0, 1)).returncode == 0

    def check_payload(self, oid):
        tree = self.call("ls-tree", "-r", "-z", oid).stdout
        if self.call("ls-tree", "-r", "--name-only", oid, "--", ".gdi").stdout:
            raise GdiError(".gdi contains local metadata and must not be committed")
        if any(entry.startswith("160000 ") for entry in tree.split("\0")):
            raise GdiError("submodules are unsupported (bundle does not contain their data)")
        pointers = self.call("grep", "-I", "-l", "-e",
                             "^version https://git-lfs.github.com/spec/v1$", oid, "--", allowed=(0, 1))
        if pointers.returncode == 0:
            raise GdiError("git LFS pointers are unsupported (bundle has no LFS payload)")

    def import_objects(self, repository, oid):
        self.call("-c", "fetch.fsckObjects=true", "fetch", "--no-tags",
                  "--no-write-fetch-head", "--", str(repository), oid)
