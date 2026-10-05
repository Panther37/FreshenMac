import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

from freshenmac.boot import RebootState
from freshenmac.computer import MacOS
from freshenmac.homebrew import HomeBrew
from freshenmac.util import RunCMD
from freshenmac.xcode import Xcode


def make_cmd(stdout: str = "", stderr: str = "", errno: int = 0) -> RunCMD:
    """Creates a real RunCMD instance without executing a subprocess."""
    cmd = RunCMD.__new__(RunCMD)
    cmd.stdout = stdout
    cmd.stderr = stderr
    cmd.returncode = errno
    cmd.debug = False
    return cmd


class TestMacOS(unittest.TestCase):
    """Unit tests for MacOS class."""

    def setUp(self):
        self.mac = MacOS(dry_run=True)

    def test_init_dry_run(self):
        self.assertTrue(self.mac.dry_run)
        self.assertIsInstance(self.mac.reboot, RebootState)
        self.assertEqual(self.mac.reboot.required, 0)
        self.assertEqual(self.mac.reboot.suggested, 0)

    def test_is_app_running_real_finder(self):
        # Finder is virtually always running on macOS GUI
        if self.mac.is_mac and self.mac.is_app_running('Finder'):
            self.assertTrue(self.mac.is_app_running('Finder'))
        self.assertFalse(self.mac.is_app_running('TotallyNonExistentApp123xyz'))

    def test_is_app_running_mocked(self):
        with patch('freshenmac.computer.RunCMD') as mock_cmd:
            # First check pgrep -f matches
            mock_cmd.return_value = make_cmd(errno=0)
            self.assertTrue(self.mac.is_app_running('Logic Pro'))

            # None match
            mock_cmd.return_value = make_cmd(errno=1)
            self.assertFalse(self.mac.is_app_running('Logic Pro'))

    def test_schedule_and_cleanup_startup_run(self):
        self.mac.dry_run = False
        with tempfile.TemporaryDirectory() as temp_dir:
            fake_home = Path(temp_dir)
            with patch('freshenmac.plist.Path.home', return_value=fake_home):
                with patch('freshenmac.plist.RunCMD') as mock_cmd:
                    mock_cmd.return_value.returncode = 0

                    scheduled = self.mac.schedule_startup_run()
                    self.assertTrue(scheduled)

                    expected_plist = fake_home / 'Library/LaunchAgents/com.panther37.FreshenMac_runonce.plist'
                    self.assertTrue(expected_plist.is_file())

                    with open(expected_plist, 'rb') as f:
                        plist_data = plistlib.load(f)
                    self.assertEqual(plist_data.get('Label'), 'com.panther37.FreshenMac_runonce')
                    self.assertTrue(plist_data.get('RunAtLoad'))

                    # Test cleanup
                    MacOS.cleanup_startup_run()
                    self.assertFalse(expected_plist.exists())

    def test_check_staged_reboot_missing(self):
        with patch('freshenmac.computer.Path.is_file', return_value=False):
            result = self.mac.check_staged_reboot()
            self.assertFalse(result)
            self.assertEqual(self.mac.reboot.required, 0)

    def test_check_staged_reboot_with_install_at_logout(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fake_index = Path(temp_dir) / 'index.plist'
            with open(fake_index, 'wb') as f:
                plistlib.dump({'InstallAtLogout': True}, f)

            with patch(
                'freshenmac.computer.Path',
                side_effect=lambda *args, **kwargs: fake_index if any('/Library/Updates' in str(a) for a in args) else Path(*args, **kwargs),
            ):
                result = self.mac.check_staged_reboot()
                self.assertTrue(result)
                self.assertGreaterEqual(self.mac.reboot.required, 1)

    def test_check_reboot_status_suggested(self):
        self.mac._updates_performed = True
        with patch.object(MacOS, 'uptime_days', new_callable=PropertyMock, return_value=16):
            with patch.object(MacOS, 'check_staged_reboot', return_value=False):
                status = self.mac.check_reboot_status()
                self.assertGreaterEqual(status.suggested, 1)

    def test_update_mas_skips_running_gui_app(self):
        self.mac.dry_run = False
        outdated_output = "634148309 Logic Pro (12.3 -> 12.3.1)"

        def fake_runcmd(cmd, *args, **kwargs):
            cmd_str = " ".join(cmd)
            mock = MagicMock()
            if 'mas outdated' in cmd_str:
                mock.stdout = outdated_output
                mock.returncode = 0
                mock.stderr = ''
            elif 'pgrep -f Logic Pro.app' in cmd_str:
                mock.returncode = 0
                mock.stdout = '12345'
            else:
                mock.returncode = 0
                mock.stdout = ''
                mock.stderr = ''
            return mock

        with patch('freshenmac.computer.RunCMD', side_effect=fake_runcmd), \
             patch('freshenmac.mas.RunCMD', side_effect=fake_runcmd) as mock_cmd:
            with patch.object(self.mac, 'schedule_startup_run') as mock_schedule:
                self.mac._update_mas()
                self.assertEqual(self.mac.reboot.required, 1)
                mock_schedule.assert_called_once()
                # Verify mas upgrade was not called for Logic Pro
                for call_args in mock_cmd.call_args_list:
                    args, _ = call_args
                    cmd_list = args[0]
                    self.assertNotIn('upgrade', cmd_list)

    def test_update_mas_upgrades_non_running_app(self):
        self.mac.dry_run = False
        outdated_output = "497799835 Xcode (15.0 -> 15.1)"

        executed_commands = []

        def fake_runcmd(cmd, *args, **kwargs):
            cmd_str = " ".join(cmd)
            executed_commands.append((cmd, kwargs))
            mock = MagicMock()
            if 'mas outdated' in cmd_str:
                mock.stdout = outdated_output
                mock.returncode = 0
                mock.stderr = ''
            elif 'pgrep' in cmd_str:
                mock.returncode = 1
                mock.stdout = ''
            else:
                mock.returncode = 0
                mock.stdout = ''
                mock.stderr = ''
            return mock

        with patch('freshenmac.computer.RunCMD', side_effect=fake_runcmd), \
             patch('freshenmac.mas.RunCMD', side_effect=fake_runcmd):
            with patch.object(self.mac, 'is_app_running', return_value=False):
                self.mac._update_mas()
                self.assertEqual(self.mac.reboot.required, 0)
                # Verify mas upgrade 497799835 was called with utility taskpolicy
                upgraded = [(c, kw) for c, kw in executed_commands if 'upgrade' in c and '497799835' in c]
                self.assertTrue(upgraded)
                self.assertEqual(upgraded[0][1].get('taskpolicy'), 'utility')

    def test_ignored_mas_apps_property(self):
        mac = MacOS(ignore_mas=['Slack', '497799835'])
        with patch('freshenmac.plist.load_preferences', return_value={'Ignore MAS': ['Xcode', 'Logic Pro']}):
            self.assertEqual(mac.ignored_mas_apps, {'xcode', 'logic pro', 'slack', '497799835'})

        mac2 = MacOS()
        with patch('freshenmac.plist.load_preferences', return_value={'Ignore MAS': 12345}):
            self.assertEqual(mac2.ignored_mas_apps, {'12345'})

    def test_update_mas_skips_ignored_app(self):
        mac = MacOS(dry_run=False, ignore_mas=['Xcode'])
        outdated_output = "497799835 Xcode (15.0 -> 15.1)\n634148309 Logic Pro (12.3 -> 12.3.1)"
        executed_commands = []

        def fake_runcmd(cmd, *args, **kwargs):
            executed_commands.append(cmd)
            mock = MagicMock()
            if 'mas outdated' in " ".join(cmd):
                mock.stdout = outdated_output
                mock.returncode = 0
            else:
                mock.returncode = 0
                mock.stdout = ''
                mock.stderr = ''
            return mock

        with patch('freshenmac.computer.RunCMD', side_effect=fake_runcmd), \
             patch('freshenmac.mas.RunCMD', side_effect=fake_runcmd):
            with patch.object(mac, 'is_app_running', return_value=False):
                with patch.object(mac, 'schedule_startup_run') as mock_schedule:
                    mac._update_mas()
                    upgraded_ids = [
                        c[2] for c in executed_commands
                        if len(c) >= 3 and c[0].endswith('mas') and c[1] == 'upgrade'
                    ]
                    self.assertNotIn('497799835', upgraded_ids)
                    self.assertIn('634148309', upgraded_ids)
                    self.assertEqual(mac.reboot.required, 0)
                    mock_schedule.assert_not_called()

    def test_update_os_two_stage(self):
        self.mac.dry_run = False
        def fake_runcmd(cmd, *args, **kwargs):
            mock = MagicMock()
            mock.returncode = 0
            mock.stderr = ''
            if 'sw_vers' in cmd:
                mock.stdout = '15.8\n'
                mock.strip.return_value = '15.8'
                mock.std_all = '15.8'
            elif '--list' in cmd:
                mock.stdout = '* Label: macOS Sequoia 15.8.1-24H32\n\tTitle: macOS Sequoia 15.8.1, Action: restart,'
                mock.std_all = mock.stdout
            else:
                mock.stdout = 'Downloaded: macOS Sequoia 15.8.1\nDone.'
                mock.std_all = mock.stdout
            return mock

        with patch('freshenmac.computer.RunCMD', side_effect=fake_runcmd), \
             patch('freshenmac.softwareupdate.RunCMD', side_effect=fake_runcmd) as mock_cmd:
            with patch.object(self.mac, 'check_staged_reboot'):
                self.mac._update_os()
                mock_cmd.assert_any_call(
                    ['softwareupdate', '-d', 'macOS Sequoia 15.8.1-24H32'],
                    cancelable=True,
                    debug=self.mac.debug_vars['debug'],
                    debug_limit=self.mac.debug_vars['debug_limit'],
                    taskpolicy=None,
                    spinner=False,
                    timeout=self.mac.timeout,
                )

    def test_update_os_downloaded_higher_version_triggers_reboot(self):
        self.mac.dry_run = False
        self.mac.reboot.required = 0
        def fake_runcmd(cmd, *args, **kwargs):
            mock = MagicMock()
            mock.returncode = 0
            mock.stderr = ''
            if 'sw_vers' in cmd:
                mock.stdout = '15.8\n'
                mock.strip.return_value = '15.8'
                mock.std_all = '15.8'
            elif '--list' in cmd:
                mock.stdout = '* Label: macOS Sequoia 15.8.1-24H32\n\tTitle: macOS Sequoia 15.8.1'
                mock.std_all = mock.stdout
            else:
                mock.stdout = 'Downloaded: macOS Sequoia 15.8.1\nDone.'
                mock.std_all = mock.stdout
                mock.__contains__.side_effect = lambda s: s in mock.stdout
            return mock

        with patch('freshenmac.computer.RunCMD', side_effect=fake_runcmd), \
             patch('freshenmac.softwareupdate.RunCMD', side_effect=fake_runcmd):
            with patch.object(self.mac, 'check_staged_reboot'):
                self.mac._update_os()
                self.assertGreaterEqual(self.mac.reboot.required, 1)
                self.assertTrue(self.mac._updates_performed)

    def test_mas_timeout_default_is_1800(self):
        self.assertEqual(self.mac.mas_timeout, 1800)
        self.assertEqual(self.mac.mas_cmd_options['timeout'], 1800)

    def test_brew_property(self):
        self.assertIsInstance(self.mac.brew, HomeBrew)
        self.assertEqual(self.mac.brew.is_arm, self.mac.arm_installed)
        self.assertEqual(self.mac.brew.dry_run, self.mac.dry_run)
        self.assertEqual(self.mac.brew.debug, self.mac.debug_vars['debug'])

    def test_xcode_property(self):
        self.assertIsInstance(self.mac.xcode, Xcode)
        self.assertEqual(self.mac.xcode.dry_run, self.mac.dry_run)
        self.assertEqual(self.mac.xcode.debug, self.mac.debug_vars['debug'])

    def test_update_brew_delegation(self):
        with patch.object(self.mac.brew, 'update') as mock_brew_update:
            self.mac.brew.updates_performed = True
            self.mac.update('brew')
            mock_brew_update.assert_called_once()
            self.assertTrue(self.mac._updates_performed)

    def test_idle_time_returns_int(self):
        with patch('freshenmac.computer.RunCMD') as mock_cmd:
            mock_cmd.return_value.stdout = "'HIDIdleTime' = 12500000000"
            mock_cmd.return_value.returncode = 0
            res = self.mac.idle_time
            self.assertIsInstance(res, int)
            self.assertEqual(res, 12)

    def test_all_user_idle_time_returns_int(self):
        with patch.object(MacOS, 'idle_time', new_callable=PropertyMock, return_value=300):
            with patch('freshenmac.computer.RunCMD') as mock_cmd:
                mock_cmd.return_value.stdout = (
                    "user1 console  Sep 16 10:00  01:30  1234\nuser2 ttys001  Sep 16 11:00  00:05  5678"
                )
                mock_cmd.return_value.returncode = 0
                mock_cmd.return_value.__bool__.return_value = True
                mock_cmd.return_value.__iter__.return_value = mock_cmd.return_value.stdout.splitlines()
                res = self.mac.all_user_idle_time
                self.assertIsInstance(res, int)
                self.assertEqual(res, 300)

    def test_cmd_options(self):
        self.mac.timeout = 1500
        self.mac.debug_vars['debug'] = True
        opts = self.mac.cmd_options()
        self.assertEqual(opts, {'cancelable': True, 'debug': True, 'debug_limit': 0, 'taskpolicy': 'utility', 'timeout': 1500})

    def test_has_mas_dry_run_returns_false_without_installing(self):
        mac = MacOS(dry_run=True)
        mac.__dict__['mas_path'] = Path('')
        with patch('freshenmac.computer.RunCMD') as mock_cmd, \
             patch('freshenmac.mas.RunCMD') as mock_mas_cmd:
            self.assertFalse(mac.has_mas)
            mock_cmd.assert_not_called()
            mock_mas_cmd.assert_not_called()

    def test_has_mas_installs_when_not_dry_run(self):
        mac = MacOS(dry_run=False)
        mac.__dict__['arm_installed'] = False
        mac.__dict__['mas_path'] = Path('')
        with patch('freshenmac.mas.RunCMD') as mock_cmd:
            mock_cmd.return_value.returncode = 0
            mock_cmd.return_value.__bool__.return_value = True
            with patch('freshenmac.mas.shutil.which', return_value='/opt/homebrew/bin/mas'):
                self.assertTrue(mac.has_mas)
                mock_cmd.assert_called_once()

    def test_notify_manual_os_update(self):
        mac = MacOS(dry_run=False)
        with patch('freshenmac.computer.RunCMD') as mock_cmd, \
             patch('freshenmac.computer.subprocess.Popen') as mock_popen, \
             patch('builtins.print'):
            mac.notify_manual_os_update('macOS 27.0.1-26A434')
            mock_cmd.assert_called_once_with(
                ['open', 'x-apple.systempreferences:com.apple.Software-Update-Settings.extension'],
                **mac.debug_vars,
            )
            mock_popen.assert_called_once()
            args = mock_popen.call_args[0][0]
            self.assertEqual(args[0], 'osascript')
            self.assertEqual(args[1], '-e')
            self.assertIn('macOS 27.0.1-26A434', args[2])
            self.assertIn('Volume Owner authentication is required', args[2])

    def test_notify_manual_os_update_dry_run(self):
        mac = MacOS(dry_run=True)
        with patch('freshenmac.computer.RunCMD') as mock_cmd, \
             patch('freshenmac.computer.subprocess.Popen') as mock_popen, \
             patch('builtins.print'):
            mac.notify_manual_os_update('macOS 27.0.1-26A434')
            mock_cmd.assert_not_called()
            mock_popen.assert_not_called()

    def test_update_os_failed_to_authenticate_triggers_notify(self):
        mac = MacOS(dry_run=False)
        mac.__dict__['arm_installed'] = True
        mac.user_password = None

        def fake_runcmd(cmd, **kwargs):
            mock = MagicMock()
            mock.returncode = 0
            if '--list' in cmd:
                mock.std_all = "* Label: macOS 27.0.1-26A434\n\tTitle: macOS 27.0.1, Version: 27.0.1, Action: restart\n"
            elif '-d' in cmd:
                mock.std_all = "Downloaded: macOS 27.0.1\nFailed to authenticate\n"
            elif '--install' in cmd:
                mock.std_all = "Failed to authenticate\n"
            return mock

        with patch('freshenmac.computer.RunCMD', side_effect=fake_runcmd), \
             patch('freshenmac.softwareupdate.RunCMD', side_effect=fake_runcmd), \
             patch.object(mac, 'notify_manual_os_update') as mock_notify, \
             patch.object(mac, 'check_staged_reboot'):
            mac._update_os()
            mock_notify.assert_called_once_with('macOS 27.0.1-26A434')
            self.assertIn('macOS 27.0.1-26A434', mac.os_summary['available'])


if __name__ == '__main__':
    unittest.main()
