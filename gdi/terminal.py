"""Terminal colours with explicit pipe opt-in and NO_COLOR taking precedence."""

import json
import os
import sys


def display_path(path):
    """Keep Unicode readable while escaping terminal controls and ambiguous quotes."""
    if all(char.isprintable() and char not in '\\"' for char in path):
        return path
    quoted = json.dumps(path, ensure_ascii=False)
    return ''.join(char if char.isprintable() else char.encode('unicode_escape').decode('ascii')
                   for char in quoted)


def colorize(text, code, *, stream=None):
    stream = sys.stdout if stream is None else stream
    force = os.environ.get('FORCE_COLOR', '') not in ('', '0')
    enabled = 'NO_COLOR' not in os.environ and (force or (
        stream.isatty() and os.environ.get('TERM') != 'dumb'))
    return f'\033[{code}m{text}\033[0m' if enabled else text
