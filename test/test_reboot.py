import unittest
from unittest.mock import PropertyMock, patch

from freshenmac.boot import Reboot
from freshenmac.computer import MacOS
from freshenmac.config import APP_INFO
from freshenmac.util import RunCMD


def make_cmd(stdout: str = "", stderr: str = "", errno: int = 0) -> RunCMD:
    """Creates a real RunCMD instance without executing a subprocess."""
    cmd = RunCMD.__new__(RunCMD)
    cmd.stdout = stdout
    cmd.stderr = stderr
    cmd.returncode = errno
    cmd.debug = False
    return cmd


def make_ascript(stdout: str = "", stderr: str = "", errno: int = 0):
    """Creates a mock RunAppleScript instance with __contains__ support."""
    res = unittest.mock.MagicMock()
    res.stdout = stdout
    res.stderr = stderr
    res.returncode = errno
    res.__contains__.side_effect = lambda s: s in stdout
    res.__bool__.return_value = (errno == 0)
    return res


class TestReboot(unittest.TestCase):
    """Unit tests for Reboot workflow and escalation logic."""

    def setUp(self):
        self.mac = MacOS(dry_run=True)
        self.reboot = Reboot(self.mac, dry_run=True)

        # Baseline safety shield: Never allow unmocked execution of AppleScripts, chimes, or subprocesses in TestReboot
        patch_ascript = patch('freshenmac.boot.util.RunAppleScript')
        self.mock_run_applescript = patch_ascript.start()
        self.addCleanup(patch_ascript.stop)

        patch_playsound = patch('freshenmac.boot.util.PlaySound')
        self.mock_playsound = patch_playsound.start()
        self.addCleanup(patch_playsound.stop)

        patch_sub_run = patch('subprocess.run')
        self.mock_sub_run = patch_sub_run.start()
        self.mock_sub_run.side_effect = RuntimeError("Real subprocess.run is blocked in TestReboot")
        self.addCleanup(patch_sub_run.stop)

        patch_sub_popen = patch('subprocess.Popen')
        self.mock_sub_popen = patch_sub_popen.start()
        self.mock_sub_popen.side_effect = RuntimeError("Real subprocess.Popen is blocked in TestReboot")
        self.addCleanup(patch_sub_popen.stop)

    def test_prompt_initial_dry_run(self):
        self.reboot.dry_run = True
        with patch.object(self.reboot, 'is_idle', return_value=False):
            choice = self.reboot.prompt_initial()
            self.assertEqual(choice, 'now')

    def test_remaining_idle_time(self):
        with patch.object(MacOS, 'all_user_idle_time', new_callable=PropertyMock, return_value=0):
            self.assertEqual(self.reboot.remaining_idle_time(), 3600)

        with patch.object(MacOS, 'all_user_idle_time', new_callable=PropertyMock, return_value=4000):
            self.assertEqual(self.reboot.remaining_idle_time(min_seconds=10), 10)

    def test_prompt_initial_restart_now(self):
        self.reboot.dry_run = False
        with patch.object(self.reboot, 'play_chime') as mock_chime, \
             patch.object(self.reboot, 'is_idle', return_value=False), \
             patch('freshenmac.boot.util.RunAppleScript.dialog', return_value=make_ascript(stdout='button returned:Restart Now')):
            choice = self.reboot.prompt_initial()
            self.assertEqual(choice, 'now')
            mock_chime.assert_called_once_with('Ping')

    def test_prompt_initial_snooze(self):
        self.reboot.dry_run = False
        self.reboot.snoozes_left = 3
        with patch.object(self.reboot, 'play_chime') as mock_chime, \
             patch.object(self.reboot, 'is_idle', return_value=False), \
             patch('freshenmac.boot.util.RunAppleScript.dialog', return_value=make_ascript(stdout='button returned:Snooze')):
            choice = self.reboot.prompt_initial()
            self.assertEqual(choice, 'snooze')
            self.assertEqual(self.reboot.snoozes_left, 2)
            mock_chime.assert_called_once_with('Ping')

    def test_prompt_initial_exhausted_snoozes(self):
        self.reboot.dry_run = False
        self.reboot.snoozes_left = 0
        with patch.object(self.reboot, 'play_chime') as mock_chime, \
             patch.object(self.reboot, 'is_idle', return_value=False):
            choice = self.reboot.prompt_initial()
            self.assertEqual(choice, 'now')
            mock_chime.assert_called_once_with('Ping')

    def test_prompt_retry_graceful_and_force(self):
        self.reboot.dry_run = False
        with patch.object(self.reboot, 'play_chime'), \
             patch.object(self.reboot, 'is_idle', return_value=False):
            with patch('freshenmac.boot.util.RunAppleScript.dialog') as mock_dialog:
                mock_dialog.return_value = make_ascript(stdout='button returned:Try Again Gracefully')
                choice = self.reboot.prompt_retry(1)
                self.assertEqual(choice, 'graceful')

                mock_dialog.return_value = make_ascript(stdout='button returned:Reboot Now (Force)')
                choice = self.reboot.prompt_retry(2)
                self.assertEqual(choice, 'force')

    def test_check_and_stop_backup_inactive(self):
        self.reboot.dry_run = False
        with patch(
            'freshenmac.boot.util.RunCMD',
            return_value=make_cmd(stdout='Backup session status:\n{\n    Running = 0;\n}'),
        ) as mock_cmd:
            self.reboot.check_and_stop_backup()
            for call_args in mock_cmd.call_args_list:
                args, _ = call_args
                self.assertNotIn('stopbackup', args[0])

    def test_check_and_stop_backup_active_stops_cleanly(self):
        self.reboot.dry_run = False
        call_count = 0

        def fake_runcmd(cmd, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if 'status' in cmd:
                if call_count == 1:
                    return make_cmd(stdout='Running = 1;')
                else:
                    return make_cmd(stdout='Running = 0;')
            return make_cmd(stdout='')

        with patch('freshenmac.boot.util.RunCMD', side_effect=fake_runcmd) as mock_cmd:
            with patch.object(self.reboot, '_sleep'):
                self.reboot.check_and_stop_backup()
                stopbackup_calls = [
                    c for c in mock_cmd.call_args_list
                    if 'stopbackup' in c[0][0]
                ]
                self.assertEqual(len(stopbackup_calls), 1)
                self.assertEqual(stopbackup_calls[0][0][0], ['tmutil', 'stopbackup'])

    def test_ensure_root_states(self):
        # 1. Dry run
        self.reboot.dry_run = True
        self.assertTrue(self.reboot.ensure_root())

        # 2. Sudo cached
        self.reboot.dry_run = False
        with patch('os.geteuid', return_value=501):
            with patch('freshenmac.boot.util.RunCMD', return_value=make_cmd(errno=0)):
                self.assertTrue(self.reboot.ensure_root())

            # 3. Interactive tty prompt
            with patch('freshenmac.boot.util.RunCMD') as mock_cmd:
                mock_cmd.side_effect = [make_cmd(errno=1), make_cmd(errno=0)]
                with patch('sys.stdin.isatty', return_value=True):
                    self.assertTrue(self.reboot.ensure_root())
                    self.assertEqual(mock_cmd.call_args[0][0], ['-v'])
                    self.assertTrue(mock_cmd.call_args[1].get('interactive'))
                    self.assertTrue(mock_cmd.call_args[1].get('need_root'))

            # 4. Non-interactive session without cached sudo
            with patch('freshenmac.boot.util.RunCMD', return_value=make_cmd(errno=1)):
                with patch('sys.stdin.isatty', return_value=False):
                    self.assertFalse(self.reboot.ensure_root())

    def test_attempt_authrestart_skips_when_no_password(self):
        self.reboot.dry_run = False
        self.reboot.computer.user_password = None
        with patch.object(MacOS, 'filevault_enabled', new_callable=PropertyMock, return_value=True), \
             patch.object(MacOS, 'supports_authrestart', new_callable=PropertyMock, return_value=True):
            result = self.reboot.attempt_authrestart()
            self.assertFalse(result)

    def test_attempt_authrestart_skips_when_no_sudo(self):
        self.reboot.dry_run = False
        self.reboot.computer.user_password = 'secret_password'
        with patch.object(MacOS, 'filevault_enabled', new_callable=PropertyMock, return_value=True), \
             patch.object(MacOS, 'supports_authrestart', new_callable=PropertyMock, return_value=True), \
             patch('freshenmac.boot.util.RunCMD', return_value=make_cmd(errno=1)):
            result = self.reboot.attempt_authrestart()
            self.assertFalse(result)

    def test_attempt_authrestart_success(self):
        self.reboot.dry_run = False
        self.reboot.computer.user_password = 'secret_password'
        with patch.object(MacOS, 'filevault_enabled', new_callable=PropertyMock, return_value=True), \
             patch.object(MacOS, 'supports_authrestart', new_callable=PropertyMock, return_value=True), \
             patch('freshenmac.boot.util.RunCMD', return_value=make_cmd(errno=0)):
            self.assertTrue(self.reboot.attempt_authrestart())

    def test_escalate_shutdown(self):
        self.reboot.dry_run = False
        executed_cmds = []

        def fake_runcmd(cmd, *args, **kwargs):
            executed_cmds.append(cmd)
            return make_cmd()

        # Root available
        with patch.object(self.reboot, 'ensure_root', return_value=True):
            with patch('freshenmac.boot.util.RunCMD', side_effect=fake_runcmd):
                self.reboot.escalate_shutdown()
                self.assertIn(['shutdown', '-r', '+2', f"[{APP_INFO['Name']}] Mandatory system restart in 2 minutes."], executed_cmds)

        # Non-root fallback to AppleScript
        executed_cmds.clear()
        with patch.object(self.reboot, 'ensure_root', return_value=False):
            with patch('freshenmac.boot.util.RunAppleScript') as mock_ascript:
                self.reboot.escalate_shutdown()
                mock_ascript.assert_called_once()
                call_arg = mock_ascript.call_args[0][0]
                self.assertIn('shutdown -r +2', call_arg)
                self.assertIn('with administrator privileges', call_arg)

    def test_turn_off_escalation(self):
        self.reboot.dry_run = False
        executed_cmds = []

        def fake_runcmd(cmd, *args, **kwargs):
            executed_cmds.append(cmd)
            return make_cmd()

        # Root available
        with patch.object(self.reboot, 'ensure_root', return_value=True):
            with patch('freshenmac.boot.util.RunCMD', side_effect=fake_runcmd):
                self.reboot.turn_off()
                self.assertIn(['shutdown', '-h', 'now'], executed_cmds)

        # Non-root fallback to AppleScript
        executed_cmds.clear()
        with patch.object(self.reboot, 'ensure_root', return_value=False):
            with patch('freshenmac.boot.util.RunCMD', side_effect=fake_runcmd), \
                 patch('freshenmac.boot.util.RunAppleScript') as mock_ascript:
                self.reboot.turn_off()
                cmd_match = any('shutdown -h now' in str(c) for c in executed_cmds)
                ascript_match = mock_ascript.called and any('shutdown -h now' in str(c) for c in mock_ascript.call_args_list)
                self.assertTrue(cmd_match or ascript_match)

    def test_notify_final_warning(self):
        self.reboot.dry_run = False
        with patch('freshenmac.boot.util.RunAppleScript.dialog') as mock_dialog:
            with patch.object(self.reboot, 'play_chime') as mock_chime:
                self.reboot.notify_final_warning()
                mock_chime.assert_called_once_with('Sosumi')
                mock_dialog.assert_called_once()
                args, kwargs = mock_dialog.call_args
                self.assertIn('The computer will force reboot in 2 minutes', args[0])
                self.assertEqual(kwargs.get('giving_up_after'), 120)
                self.assertEqual(kwargs.get('icon'), 'stop')

    def test_kill_programs_and_daemons_dry_run(self):
        self.reboot.dry_run = True
        with patch('freshenmac.boot.util.RunAppleScript') as mock_ascript, \
             patch('freshenmac.boot.util.RunCMD') as mock_cmd:
            self.reboot.kill_programs_and_daemons()
            mock_ascript.assert_not_called()
            mock_cmd.assert_not_called()

    def test_kill_programs_and_daemons_sudo(self):
        self.reboot.dry_run = False
        executed = []

        def fake_runcmd(cmd, **kwargs):
            executed.append((cmd, kwargs))
            m = unittest.mock.MagicMock()
            m.returncode = 0
            return m

        with patch('freshenmac.boot.util.RunAppleScript') as mock_ascript, \
             patch('freshenmac.boot.util.RunCMD', side_effect=fake_runcmd) as mock_cls:
            mock_cls.can_sudo.return_value = True
            self.reboot.kill_programs_and_daemons()
            mock_ascript.assert_called_once()
            self.assertIn('quit with saving no', mock_ascript.call_args[0][0])
            self.assertEqual(executed[0][0], ['killall', '-9', 'WindowServer'])
            self.assertTrue(executed[0][1].get('need_root'))

    def test_kill_programs_and_daemons_no_sudo(self):
        self.reboot.dry_run = False
        with patch('freshenmac.boot.util.RunAppleScript') as mock_ascript, \
             patch('freshenmac.boot.util.RunCMD') as mock_cls:
            mock_cls.can_sudo.return_value = False
            self.reboot.kill_programs_and_daemons()
            self.assertEqual(mock_ascript.call_count, 2)
            self.assertIn('quit with saving no', mock_ascript.call_args_list[0][0][0])
            self.assertIn('killall -9 WindowServer', mock_ascript.call_args_list[1][0][0])
            self.assertIn('with administrator privileges', mock_ascript.call_args_list[1][0][0])

    def test_graceful_restart_dry_run(self):
        self.reboot.dry_run = True
        with patch('freshenmac.boot.util.RunAppleScript') as mock_ascript:
            self.reboot.graceful_restart()
            mock_ascript.assert_not_called()

    def test_graceful_restart_standard(self):
        self.reboot.dry_run = False
        with patch.object(MacOS, 'filevault_enabled', new_callable=PropertyMock, return_value=False), \
             patch('freshenmac.boot.util.RunAppleScript') as mock_ascript:
            self.reboot.graceful_restart()
            self.assertEqual(mock_ascript.call_count, 2)
            self.assertIn('close every document saving yes', mock_ascript.call_args_list[0][0][0])
            self.assertEqual(mock_ascript.call_args_list[1][0][0], 'tell application "System Events" to restart')


if __name__ == '__main__':
    unittest.main()
