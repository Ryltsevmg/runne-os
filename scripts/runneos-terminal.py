#!/usr/bin/env python3
"""RunneOS terminal entry point: Ghostty with argv-preserving commands."""
import os
import sys
from pathlib import Path


def prepare(arguments, cwd):
    args = list(arguments)
    command = None
    while args:
        option = args.pop(0)
        if option in ('-e', '--'):
            if not args:
                raise ValueError('a command is required after -e or --')
            command = args
            break
        if option in ('--working-directory', '--cwd', '-d'):
            if not args:
                raise ValueError('a directory is required')
            cwd = args.pop(0)
        elif option.startswith('--working-directory='):
            cwd = option.split('=', 1)[1]
        else:
            raise ValueError('unsupported terminal option: ' + option)
    cwd = str(Path(cwd).expanduser().resolve())
    launch = ['ghostty', '--working-directory=' + cwd]
    if command is not None:
        launch.extend(['-e', *command])
    return launch


if __name__ == '__main__':
    try:
        launch = prepare(sys.argv[1:], os.getcwd())
    except ValueError as error:
        raise SystemExit(str(error))
    os.execvp(launch[0], launch)
