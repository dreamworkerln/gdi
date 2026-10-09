"""Canonical readable directory names for Git branch publications."""

import re

from .git import GdiError


def branch_directory(ref):
    if not isinstance(ref, str) or not ref.startswith('refs/heads/'):
        raise GdiError('invalid branch ref')
    name = ref[len('refs/heads/'):]
    if (not name or any(part in ('', '.', '..') for part in name.split('/')) or
            any(ord(char) < 32 or ord(char) == 127 for char in name) or '\\' in name):
        raise GdiError('invalid branch directory')
    return name.replace('%', '%25').replace('/', '%2F')


def directory_ref(directory):
    if not isinstance(directory, str) or '/' in directory or re.search(r'%(?!25|2F)', directory):
        raise GdiError('invalid branch directory')
    name = directory.replace('%2F', '/').replace('%25', '%')
    ref = 'refs/heads/' + name
    if branch_directory(ref) != directory:
        raise GdiError('noncanonical branch directory')
    return ref
