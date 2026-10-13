import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

FILES=Path(__file__).parents[1]/'packages/runneos-software/files'
sys.path.insert(0,str(FILES))
from operations import OperationRunner,read_operation,write_operation


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='runneos-journal-',dir='/tmp/opencode' if os.access('/tmp/opencode',os.W_OK) else None);self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.state=self.root/'state'

    def run_plan(self,steps,callback=lambda line:None):return OperationRunner(self.state).run(steps,callback)

    def fake_snapshot(self,output,code=0):
        script=self.root/'snapper';script.write_text(f'#!/bin/sh\nprintf "%s\\n" "{output}"\nexit {code}\n');script.chmod(0o755)
        return [str(script),'create','--print-number']

    def marker_step(self):
        return [sys.executable,'-c',f'from pathlib import Path;Path({str(self.root/"marker")!r}).touch()']

    def test_success_is_durable_private_and_contains_no_output_or_arguments(self):
        self.assertEqual(self.run_plan([[sys.executable,'-c','print("TEST_SECRET")']]),0)
        data=read_operation(self.state)
        self.assertEqual(data['status'],'succeeded');self.assertEqual(data['completedSteps'],1)
        self.assertEqual((self.state/'operation.json').stat().st_mode & 0o777,0o600)
        self.assertNotIn('TEST_SECRET',(self.state/'operation.json').read_text())
        self.assertNotIn('print(',str(data))

    def test_snapshot_failure_stops_before_package_step(self):
        self.assertEqual(self.run_plan([self.fake_snapshot('',1),self.marker_step()]),1)
        self.assertFalse((self.root/'marker').exists())
        self.assertEqual(read_operation(self.state)['completedSteps'],0)

    def test_success_without_snapshot_number_is_not_accepted(self):
        self.assertEqual(self.run_plan([self.fake_snapshot('not-a-snapshot'),self.marker_step()]),65)
        self.assertFalse((self.root/'marker').exists())

    def test_snapshot_number_recorded_before_next_step(self):
        self.assertEqual(self.run_plan([self.fake_snapshot('123'),self.marker_step()]),0)
        self.assertEqual(read_operation(self.state)['snapshotNumber'],123)
        self.assertTrue((self.root/'marker').exists())

    def test_missing_executable_is_persisted_failure(self):
        self.assertEqual(self.run_plan([[str(self.root/'missing')]]),127)
        self.assertEqual(read_operation(self.state)['status'],'failed')

    def test_unfinished_journal_blocks_replay_and_is_not_overwritten(self):
        self.state.mkdir();old={'schemaVersion':1,'status':'running','id':'prior'};write_operation(self.state,old)
        with self.assertRaisesRegex(RuntimeError,'не завершена'):self.run_plan([self.marker_step()])
        self.assertEqual(read_operation(self.state),old);self.assertFalse((self.root/'marker').exists())

    def test_broken_ui_output_callback_does_not_abort_transaction(self):
        def broken(line):raise BrokenPipeError()
        self.assertEqual(self.run_plan([self.marker_step()],broken),0)
        self.assertTrue((self.root/'marker').exists())

    def test_empty_plan_never_replaces_journal(self):
        with self.assertRaises(ValueError):self.run_plan([])
        self.assertIsNone(read_operation(self.state))

    def test_unknown_journal_state_blocks_new_operation(self):
        self.state.mkdir();write_operation(self.state,{'schemaVersion':1,'status':'unrecognized'})
        with self.assertRaises(ValueError):self.run_plan([self.marker_step()])
        self.assertFalse((self.root/'marker').exists())

    def test_real_process_lock_and_deferred_sigterm(self):
        release=self.root/'release';done=self.root/'done'
        child_code=f'import time;from pathlib import Path\nwhile not Path({str(release)!r}).exists():time.sleep(.01)\nPath({str(done)!r}).touch()'
        worker_code=f'import sys;sys.path.insert(0,{str(FILES)!r});from operations import OperationRunner;raise SystemExit(OperationRunner({str(self.state)!r}).run({[[sys.executable,"-c",child_code],self.marker_step()]!r},lambda line:None))'
        proc=subprocess.Popen([sys.executable,'-c',worker_code],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                data=read_operation(self.state)
                if data and data.get('childPid'):break
                time.sleep(.01)
            else:self.fail('Test runner never started')
            with self.assertRaisesRegex(RuntimeError,'ещё выполняется'):self.run_plan([self.marker_step()])
            proc.send_signal(signal.SIGTERM)
            release.touch()
            proc.communicate(timeout=5)
            self.assertEqual(proc.returncode,130)
            self.assertTrue(done.exists());self.assertFalse((self.root/'marker').exists())
            self.assertEqual(read_operation(self.state)['status'],'stopped_after_step')
        finally:
            release.touch()
            if proc.poll() is None:proc.kill();proc.wait()
            proc.stdout.close();proc.stderr.close()

    def test_journal_failure_after_spawn_does_not_leave_child_unobserved(self):
        import operations
        original=operations.write_operation;calls=0
        def failing(root,data):
            nonlocal calls
            calls+=1
            if calls==3:raise OSError('simulated disk failure after spawn')
            original(root,data)
        with patch.object(operations,'write_operation',side_effect=failing):
            result=self.run_plan([self.marker_step()])
        self.assertEqual(result,127)
        self.assertTrue((self.root/'marker').exists())
        self.assertEqual(read_operation(self.state)['status'],'interrupted')

    def test_killed_controller_does_not_allow_automatic_replay(self):
        release=self.root/'release';done=self.root/'done'
        child_code=f'import time;from pathlib import Path\nwhile not Path({str(release)!r}).exists():time.sleep(.01)\nPath({str(done)!r}).touch()'
        worker=f'import sys;sys.path.insert(0,{str(FILES)!r});from operations import OperationRunner;OperationRunner({str(self.state)!r}).run({[[sys.executable,"-c",child_code]]!r},lambda line:None)'
        proc=subprocess.Popen([sys.executable,'-c',worker],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                data=read_operation(self.state)
                if data and data.get('childPid'):break
                time.sleep(.01)
            else:self.fail('Controller not ready')
            proc.kill();proc.wait(timeout=5)
            with self.assertRaisesRegex(RuntimeError,'не завершена'):self.run_plan([self.marker_step()])
            self.assertFalse((self.root/'marker').exists())
            release.touch()
            deadline=time.monotonic()+5
            while not done.exists() and time.monotonic()<deadline:time.sleep(.01)
            self.assertTrue(done.exists())
            self.assertEqual(read_operation(self.state)['status'],'running')
        finally:
            release.touch()
            if proc.poll() is None:proc.kill();proc.wait()


if __name__=='__main__':unittest.main()
