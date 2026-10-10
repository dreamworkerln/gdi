"""Terminal colours with explicit pipe opt-in and NO_COLOR taking precedence."""

import os
import sys


def colorize(text, code):
    force = os.environ.get('FORCE_COLOR', '') not in ('', '0')
    enabled = 'NO_COLOR' not in os.environ and (force or (
        sys.stdout.isatty() and os.environ.get('TERM') != 'dumb'))
    return f'\033[{code}m{text}\033[0m' if enabled else text
