import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from freshenmac.boot import RebootState
from freshenmac.softwareupdate import SoftwareUpdate
from freshenmac.util import RunCMD, parse_version


def make_cmd(stdout: str = "", stderr: str = "", errno: int = 0) -> RunCMD:
    """Creates a mock RunCMD result without executing a subprocess."""
    cmd = RunCMD.__new__(RunCMD)
    cmd.stdout = stdout
    cmd.stderr = stderr
    cmd.returncode = errno
    cmd.debug = False
    return cmd


class TestSoftwareUpdate(unittest.TestCase):
    """Unit tests for the SoftwareUpdate class in freshenmac.softwareupdate."""

    def setUp(self):
        self.reboot = RebootState()
        with patch('freshenmac.softwareupdate.RunCMD') as mock_cmd:
            mock_cmd.return_value = make_cmd(stdout='15.0\n')
            self.swu = SoftwareUpdate(dry_run=True, reboot=self.reboot)

    def test_init_defaults(self):
        self.assertTrue(self.swu.dry_run)
        self.assertEqual(self.swu.timeout, 1800)
        self.assertFalse(self.swu.updates_performed)
        self.assertEqual(self.swu.summary, {'available': [], 'downloaded': []})
        self.assertEqual(self.swu.reboot.required, 0)

    def test_two_stage_macos_update_download_and_reboot(self):
        self.swu.dry_run = False
        self.swu.os_ver['Installed'] = parse_version('15.0')
        executed_cmds = []

        def fake_runcmd(cmd, *args, **kwargs):
            executed_cmds.append(cmd)
            mock = MagicMock()
            mock.returncode = 0
            mock.stderr = ''
            if '--list' in cmd:
                mock.std_all = "* Label: macOS Sequoia 15.8.1-24H32\n\tTitle: macOS Sequoia 15.8.1, Version: 15.8.1"
            elif '-d' in cmd:
                mock.std_all = "Downloaded: macOS Sequoia 15.8.1\nDone."
            else:
                mock.std_all = "Success"
            return mock

        with patch('freshenmac.softwareupdate.RunCMD', side_effect=fake_runcmd), \
             patch.object(self.swu, 'check_staged_reboot'):
            self.swu.update()
            self.assertEqual(self.reboot.required, 1)
            self.assertTrue(self.swu.updates_performed)
            self.assertIn('macOS Sequoia 15.8.1-24H32', self.swu.summary['downloaded'])
            self.assertEqual(str(self.swu.os_ver['Downloaded']), '15.8.1')

    def test_software_update_restart_required(self):
        self.swu.dry_run = False
        executed_cmds = []

        def fake_runcmd(cmd, *args, **kwargs):
            executed_cmds.append(cmd)
            mock = MagicMock()
            mock.returncode = 0
            mock.stderr = ''
            if '--list' in cmd:
                mock.std_all = "* Label: Safari18.0-24H123\n\tTitle: Safari, Action: restart\n"
            elif '-d' in cmd:
                mock.std_all = "Downloaded Safari"
            elif '--install' in cmd:
                mock.std_all = "Installing... Action: restart"
            return mock

        with patch('freshenmac.softwareupdate.RunCMD', side_effect=fake_runcmd), \
             patch.object(self.swu, 'check_staged_reboot'):
            self.swu.update()
            self.assertGreaterEqual(self.reboot.required, 1)
            self.assertTrue(self.swu.updates_performed)
            self.assertIn('Safari18.0-24H123', self.swu.summary['downloaded'])

    def test_notify_manual_opens_settings_and_dialog(self):
        swu = SoftwareUpdate(dry_run=False)
        with patch('freshenmac.softwareupdate.RunCMD') as mock_cmd, \
             patch('freshenmac.softwareupdate.subprocess.Popen') as mock_popen, \
             patch('builtins.print'):
            swu.notify_manual('macOS 27.0.1-26A434')
            mock_cmd.assert_called_once_with(
                ['open', 'x-apple.systempreferences:com.apple.Software-Update-Settings.extension'],
                **swu.debug_vars,
            )
            mock_popen.assert_called_once()
            args = mock_popen.call_args[0][0]
            self.assertEqual(args[0], 'osascript')
            self.assertEqual(args[1], '-e')
            self.assertIn('macOS 27.0.1-26A434', args[2])
            self.assertIn('Volume Owner authentication is required', args[2])

    def test_notify_manual_dry_run(self):
        swu = SoftwareUpdate(dry_run=True)
        with patch('freshenmac.softwareupdate.RunCMD') as mock_cmd, \
             patch('freshenmac.softwareupdate.subprocess.Popen') as mock_popen:
            swu.notify_manual('macOS 27.0.1-26A434')
            mock_cmd.assert_not_called()
            mock_popen.assert_not_called()

    def test_update_os_failed_to_authenticate_notifies(self):
        self.swu.dry_run = False
        def fake_runcmd(cmd, *args, **kwargs):
            mock = MagicMock()
            mock.returncode = 0
            if '--list' in cmd:
                mock.std_all = "* Label: macOS 27.0.1-26A434\n\tTitle: macOS 27.0.1"
            elif '-d' in cmd:
                mock.std_all = "Downloaded: macOS 27.0.1\nFailed to authenticate"
            return mock

        with patch('freshenmac.softwareupdate.RunCMD', side_effect=fake_runcmd), \
             patch.object(self.swu, 'notify_manual') as mock_notify, \
             patch.object(self.swu, 'check_staged_reboot'):
            self.swu.update()
            mock_notify.assert_called_once_with('macOS 27.0.1-26A434')
            self.assertIn('macOS 27.0.1-26A434', self.swu.summary['available'])

    def test_arm64_stdinpass_flags(self):
        swu = SoftwareUpdate(
            arm_installed=True,
            console_user='testuser',
            user_password='secretpassword',
        )
        with patch('freshenmac.softwareupdate.RunCMD') as mock_cmd:
            swu._install_label('macOS Sequoia 15.8.1-24H32', force_restart=True)
            mock_cmd.assert_called_once_with(
                ['softwareupdate', '--install', 'macOS Sequoia 15.8.1-24H32', '--restart', '--force', '--stdinpass', '--user', 'testuser'],
                to_input='secretpassword\n',
                need_root=True,
                cancelable=True,
                debug=0,
                debug_limit=0,
                taskpolicy=None,
                timeout=1800,
            )

    def test_check_staged_reboot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fake_index = Path(temp_dir) / 'index.plist'
            with open(fake_index, 'wb') as f:
                plistlib.dump({'InstallLater': True}, f)

            with patch('freshenmac.softwareupdate.Path', side_effect=lambda *args, **kwargs: fake_index if any('/Library/Updates' in str(a) for a in args) else Path(*args, **kwargs)):
                res = self.swu.check_staged_reboot()
                self.assertTrue(res)
                self.assertGreaterEqual(self.reboot.required, 1)


if __name__ == '__main__':
    unittest.main()
