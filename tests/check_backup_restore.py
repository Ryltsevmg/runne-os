#!/usr/bin/python3
"""Disposable Borg encryption/restore drill; never touches a personal repo."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile


def main():
    with tempfile.TemporaryDirectory(prefix='runneos-borg-drill-',dir='/tmp/opencode' if os.access('/tmp/opencode',os.W_OK) else None) as directory:
        root=Path(directory);source=root/'source';restore=root/'restore';source.mkdir();restore.mkdir()
        payload='RunneOS: проверка восстановления\n'.encode()+os.urandom(4096)
        (source/'acceptance.txt').write_bytes(payload)
        env=dict(os.environ,BORG_PASSPHRASE=secrets.token_urlsafe(40),
                 BORG_CACHE_DIR=str(root/'cache'),BORG_SECURITY_DIR=str(root/'security'),
                 BORG_KEYS_DIR=str(root/'keys'))
        repo=str(root/'repo')
        steps=[(['borg','init','--encryption=repokey-blake2',repo],root),
               (['borg','create',repo+'::acceptance','acceptance.txt'],source),
               (['borg','check','--verify-data',repo],root),
               (['borg','extract',repo+'::acceptance'],restore)]
        for args,cwd in steps:
            result=subprocess.run(args,cwd=cwd,env=env,capture_output=True,timeout=90)
            if result.returncode:raise RuntimeError('Borg drill failed at '+args[1])
        assert (restore/'acceptance.txt').read_bytes()==payload
        print(json.dumps({'ok':True,'scope':'disposable encrypted local Borg repository, separate source/restore directories',
                          'verifiedBytes':len(payload),'sameSha256':hashlib.sha256(payload).digest()==hashlib.sha256((restore/'acceptance.txt').read_bytes()).digest(),
                          'notTested':['off-device backup','clean-machine restore','user recovery key custody','Btrfs system rollback']}))


if __name__=='__main__':main()
