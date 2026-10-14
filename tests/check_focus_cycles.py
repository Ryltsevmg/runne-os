#!/usr/bin/python3
"""Explicit live DMS focus-return drill; no keyboard injection or private text."""
import argparse
import json
import subprocess
import time


def run(*argv):return subprocess.run(argv,capture_output=True,text=True,check=True,timeout=5).stdout


def focused():return json.loads(run('niri','msg','--json','focused-window'))


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--window-id',type=int,required=True);parser.add_argument('--cycles',type=int,default=30)
    args=parser.parse_args()
    if not 1<=args.cycles<=30:raise SystemExit('Use 1..30 cycles')
    original=focused();failures=[]
    try:
        run('niri','msg','action','focus-window','--id',str(args.window_id))
        for cycle in range(args.cycles):
            run('dms','ipc','call','spotlight','openQuery','RunneOS focus check')
            time.sleep(.08)
            run('dms','ipc','call','spotlight','close')
            time.sleep(.12)
            window=focused()
            if not window or window.get('id')!=args.window_id:failures.append(cycle+1)
        print(json.dumps({'cycles':args.cycles,'focusFailures':failures,
                          'scope':'IPC open/close and compositor focus; no physical typing, not latency-to-pixels measurement'}))
    finally:
        run('dms','ipc','call','spotlight','close')
        if original:run('niri','msg','action','focus-window','--id',str(original['id']))
    return bool(failures)


if __name__=='__main__':raise SystemExit(main())
