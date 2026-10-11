"""User command aliases, expanded as arguments without a shell."""

import configparser
import io
import os
from pathlib import Path
import re
import shlex

from .git import GdiError


BUILTIN_COMMANDS = frozenset(('remote', 'status', 'cache', 'gc', 'push', 'fetch',
                              'pull', 'log', 'ci', 'agent', 'worker', 'alias'))


def config_path():
    base = os.environ.get('XDG_CONFIG_HOME')
    return (Path(base) if base else Path.home() / '.config') / 'gdi' / 'aliases.ini'


def ensure_config():
    """Create the initial user aliases without overwriting an existing file."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open('x', encoding='utf-8') as handle:
            handle.write('[alias]\ns = status\n')
    except FileExistsError:
        pass


def load():
    path = config_path()
    config = configparser.ConfigParser(interpolation=None, delimiters=('=',),
                                       empty_lines_in_values=False)
    config.optionxform = str
    try:
        with path.open(encoding='utf-8') as handle:
            config.read_file(handle)
    except FileNotFoundError:
        config.add_section('alias')
    except configparser.Error as exc:
        raise GdiError(f'invalid alias config {path}: {exc}') from exc
    if config.defaults() or config.sections() != ['alias']:
        raise GdiError(f'invalid alias config {path}: expected only an [alias] section')
    return config


def validate_name(name):
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]*', name):
        raise GdiError('alias names must start with an ASCII letter and use letters, digits, _ or -')
    if name in BUILTIN_COMMANDS:
        raise GdiError(f'cannot override built-in gdi command: {name}')


def arguments(name, value):
    validate_name(name)
    try:
        result = shlex.split(value)
    except ValueError as exc:
        raise GdiError(f'invalid alias {name}: {exc}') from exc
    if not result or result[0] not in BUILTIN_COMMANDS:
        raise GdiError(f'alias {name} must start with a built-in gdi command (without the gdi prefix)')
    if any('\x00' in argument for argument in result):
        raise GdiError(f'invalid alias {name}: NUL characters are not allowed')
    return result


def command_index(argv):
    """Skip global options, leaving normal argparse errors/help untouched."""
    options = ('--profile-log', '--progress', '--no-progress')
    index = 0
    while index < len(argv) and argv[index].startswith('-'):
        option, separator, value = argv[index].partition('=')
        matches = [candidate for candidate in options if candidate.startswith(option)]
        if not option.startswith('--') or len(matches) != 1:
            return None
        if matches[0] == '--profile-log':
            index += 1 if separator else 2
        elif separator:
            return None
        else:
            index += 1
    return index if index < len(argv) else None


def expand(argv):
    argv = list(argv)
    index = command_index(argv)
    if index is None or argv[index] in BUILTIN_COMMANDS:
        return argv
    config = load()
    name = argv[index]
    if name not in config['alias']:
        return argv
    return argv[:index] + arguments(name, config['alias'][name]) + argv[index + 1:]


def save(config):
    from .ci_protocol import atomic_write
    output = io.StringIO()
    config.write(output)
    atomic_write(config_path(), output.getvalue().encode('utf-8'))


def set_alias(name, value):
    arguments(name, value)
    config = load()
    config['alias'][name] = value
    save(config)


def remove_alias(name):
    config = load()
    if name not in config['alias']:
        raise GdiError(f'no such alias: {name}')
    del config['alias'][name]
    save(config)


def execute(args):
    if args.operation == 'set':
        set_alias(args.name, args.expansion)
        print(f'{args.name} = {args.expansion}')
    elif args.operation == 'remove':
        remove_alias(args.name)
        print(f'Removed alias {args.name}')
    else:
        for name, value in sorted(load()['alias'].items()):
            print(f'{name} = {value}')
    return 0
