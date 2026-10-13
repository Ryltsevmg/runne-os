#!/usr/bin/python3
"""RunneOS update entry point, shared with the graphical software center."""
import argparse
import json
import os
import sys

from backend import Backend
from operations import OperationRunner,read_operation,update_commands


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    action=parser.add_mutually_exclusive_group()
    action.add_argument('--check',action='store_true',help='Check available updates without changing package databases')
    action.add_argument('--plan',action='store_true',help='Print the exact update steps without running them (default)')
    action.add_argument('--status',action='store_true',help='Read the last operation journal')
    action.add_argument('--apply',action='store_true',help='Explicitly execute the full snapshot/Arch/AUR/Flatpak sequence')
    args=parser.parse_args()
    if args.status:
        try:data=read_operation()
        except (OSError,ValueError):
            print('Журнал операции недоступен или повреждён.',file=sys.stderr);return 1
        print(json.dumps(data or {'status':'no_operations'},ensure_ascii=False,indent=2))
        return 0
    if args.check:
        updates,errors=Backend().updates()
        print(json.dumps({'updates':updates,'errors':errors},ensure_ascii=False,indent=2))
        return 1 if errors else 0
    steps=update_commands()
    if not args.apply:
        print(json.dumps({'scope':'Root snapshot excludes separate /home and /boot',
                          'steps':steps,'execution':'Only --apply runs the plan'},ensure_ascii=False,indent=2))
        return 0
    if os.geteuid()==0:
        print('Запустите runneos-update от обычного пользователя: права запрашиваются для отдельных системных шагов.',file=sys.stderr)
        return 2
    try:return OperationRunner().run(steps,lambda line:print(line,end='',flush=True))
    except (RuntimeError,ValueError) as error:
        print(str(error),file=sys.stderr);return 1


if __name__=='__main__':raise SystemExit(main())
