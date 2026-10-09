"""Exec rclone with a Linux parent-death signal, without threaded preexec_fn."""

import ctypes
import os
import signal
import sys


def main():
    parent = int(sys.argv[1])
    libc = ctypes.CDLL(None, use_errno=True)
    # PR_SET_PDEATHSIG survives exec of a normal, non-setuid rclone binary.
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), 'cannot protect rclone parent lifetime')
    # Close the race where the parent died before prctl was installed.
    if os.getppid() != parent:
        return 1
    os.execvp(sys.argv[2], sys.argv[2:])


if __name__ == '__main__':
    sys.exit(main())
