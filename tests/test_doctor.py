import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('doctor',Path(__file__).parents[1]/'packages/runneos-tools/files/doctor.py')
doctor=importlib.util.module_from_spec(spec);spec.loader.exec_module(doctor)


class DoctorTests(unittest.TestCase):
    def test_exit_zero_not_enough_for_expected_service_state(self):
        with patch.object(doctor,'command',return_value=(0,'inactive')):
            self.assertEqual(doctor.check('service',['test'],'active')['status'],'attention')

    def test_raw_diagnostics_not_exposed_for_validation(self):
        with patch.object(doctor,'command',return_value=(1,'private path or log text')):
            result=doctor.check('config',['test'])
        self.assertNotIn('private',str(result))
        self.assertEqual(result['status'],'attention')

    def test_timeout_is_not_success(self):
        with patch.object(doctor.subprocess,'run',side_effect=subprocess.TimeoutExpired('test',1)):
            self.assertEqual(doctor.command(['test']), (127,''))

    def test_user_manager_query_falls_back_to_host_machine(self):
        seen=[]
        def fake(args,timeout=8):
            seen.append(args)
            return (0,'active') if any(str(arg).startswith('--machine=') for arg in args) else (1,'')
        with patch.object(doctor,'command',side_effect=fake):
            self.assertEqual(doctor.user_manager_command('is-active','niri.service'),(0,'active'))
        self.assertEqual(len(seen),2)
        machine=[arg for arg in seen[1] if str(arg).startswith('--machine=')]
        self.assertTrue(machine and str(machine[0]).endswith('@.host'))

    def test_healthy_local_answer_is_not_retried(self):
        seen=[]
        with patch.object(doctor,'command',side_effect=lambda args,timeout=8:(seen.append(args),(0,'active'))[1]):
            self.assertEqual(doctor.user_manager_command('is-active','niri.service'),(0,'active'))
        self.assertEqual(len(seen),1)

    def test_inactive_unit_is_not_retried(self):
        seen=[]
        with patch.object(doctor,'command',side_effect=lambda args,timeout=8:(seen.append(args),(3,'inactive'))[1]):
            self.assertEqual(doctor.user_manager_command('is-active','niri.service'),(3,'inactive'))
        self.assertEqual(len(seen),1)


if __name__=='__main__':unittest.main()
