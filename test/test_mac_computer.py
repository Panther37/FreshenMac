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
    """Creates a real RunCMD instance without executing an actual subprocess."""
    cmd = RunCMD.__new__(RunCMD)
    cmd.stdout = stdout
    cmd.stderr = stderr
    cmd.returncode = errno
    cmd.debug = False
    return cmd


class TestMacOS(unittest.TestCase):
    """
    Unit tests for MacOS system orchestrator class.

    Tests hardware/architecture detection, app running status, LaunchAgent startup
    scheduling, idle time calculations, and updates delegation (Homebrew, MAS, OS, Xcode).
    """

    def setUp(self):
        """Set up test fixtures before each test method."""
        self.mac = MacOS(dry_run=True)

    def test_all_user_idle_time_returns_int(self):
        """Verifies all_user_idle_time calculates minimum idle seconds across logged in users."""
        # Arrange: mock idle_time and w -h output reporting user sessions
        with patch.object(MacOS, 'idle_time', new_callable=PropertyMock, return_value=300):
            with patch('freshenmac.computer.RunCMD') as mock_cmd:
                mock_cmd.return_value.stdout = (
                    "user1 console  Sep 16 10:00  01:30  1234\nuser2 ttys001  Sep 16 11:00  00:05  5678"
                )
                mock_cmd.return_value.returncode = 0
                mock_cmd.return_value.__bool__.return_value = True
                mock_cmd.return_value.__iter__.return_value = mock_cmd.return_value.stdout.splitlines()

                # Act
                res = self.mac.all_user_idle_time

                # Assert
                self.assertIsInstance(res, int)
                self.assertEqual(res, 300)

    def test_brew_property(self):
        """Verifies brew property initializes and caches a HomeBrew instance with matching flags."""
        # Arrange & Act
        brew_inst = self.mac.brew

        # Assert
        self.assertIsInstance(brew_inst, HomeBrew)
        self.assertEqual(brew_inst.is_arm, self.mac.arm_installed)
        self.assertEqual(brew_inst.dry_run, self.mac.dry_run)
        self.assertEqual(brew_inst.debug, self.mac.debug_vars['debug'])

    def test_check_reboot_status_suggested(self):
        """Verifies check_reboot_status flags suggested reboot when uptime exceeds threshold."""
        # Arrange: updates were performed and system uptime is 16 days
        self.mac._updates_performed = True
        with patch.object(MacOS, 'uptime_days', new_callable=PropertyMock, return_value=16), \
            patch.object(MacOS, 'check_staged_reboot', return_value=False):
            # Act
            status = self.mac.check_reboot_status()

            # Assert
            self.assertGreaterEqual(status.suggested, 1)

    def test_check_staged_reboot_missing(self):
        """Verifies check_staged_reboot returns False when index.plist does not exist."""
        # Arrange: updates index.plist does not exist
        with patch('freshenmac.computer.Path.is_file', return_value=False):
            # Act
            result = self.mac.check_staged_reboot()

            # Assert
            self.assertFalse(result)
            self.assertEqual(self.mac.reboot.required, 0)

    def test_check_staged_reboot_with_install_at_logout(self):
        """Verifies check_staged_reboot detects staged update in index.plist with InstallAtLogout."""
        # Arrange: create temporary index.plist containing InstallAtLogout=True
        with tempfile.TemporaryDirectory() as temp_dir:
            fake_index = Path(temp_dir) / 'index.plist'
            with open(fake_index, 'wb') as f:
                plistlib.dump({'InstallAtLogout': True}, f)

            with patch(
                'freshenmac.computer.Path',
                side_effect=lambda *args, **kwargs: fake_index if any(
                    '/Library/Updates' in str(a) for a in args
                ) else Path(*args, **kwargs),
            ):
                # Act
                result = self.mac.check_staged_reboot()

                # Assert
                self.assertTrue(result)
                self.assertGreaterEqual(self.mac.reboot.required, 1)

    def test_cmd_options(self):
        """Verifies cmd_options compiles execution options dictionary with defaults."""
        # Arrange
        self.mac.timeout = 1500
        self.mac.debug_vars['debug'] = True

        # Act
        opts = self.mac.cmd_options()

        # Assert
        self.assertEqual(
            opts,
            {'cancelable': True, 'debug': True, 'debug_limit': 0, 'taskpolicy': 'utility', 'timeout': 1500},
        )

    def test_has_mas_dry_run_returns_false_without_installing(self):
        """Verifies has_mas returns False in dry run without invoking installation commands."""
        # Arrange
        mac = MacOS(dry_run=True)
        mac.__dict__['mas_path'] = Path('')

        with patch('freshenmac.computer.RunCMD') as mock_cmd, \
            patch('freshenmac.mas.RunCMD') as mock_mas_cmd:
            # Act
            result = mac.has_mas

            # Assert
            self.assertFalse(result)
            mock_cmd.assert_not_called()
            mock_mas_cmd.assert_not_called()

    def test_has_mas_installs_when_not_dry_run(self):
        """Verifies has_mas invokes Homebrew to install mas CLI when missing in normal run."""
        # Arrange
        mac = MacOS(dry_run=False)
        mac.__dict__['arm_installed'] = False
        mac.__dict__['mas_path'] = Path('')

        with patch('freshenmac.mas.RunCMD') as mock_cmd:
            mock_cmd.return_value.returncode = 0
            mock_cmd.return_value.__bool__.return_value = True
            with patch('freshenmac.mas.shutil.which', return_value='/opt/homebrew/bin/mas'):
                # Act
                result = mac.has_mas

                # Assert
                self.assertTrue(result)
                mock_cmd.assert_called_once()

    def test_idle_time_returns_int(self):
        """Verifies idle_time queries IOHIDSystem and converts nanoseconds to seconds."""
        # Arrange: mock ioreg returning HIDIdleTime in nanoseconds (12.5s)
        with patch('freshenmac.computer.RunCMD') as mock_cmd:
            mock_cmd.return_value.stdout = "'HIDIdleTime' = 12500000000"
            mock_cmd.return_value.returncode = 0

            # Act
            res = self.mac.idle_time

            # Assert
            self.assertIsInstance(res, int)
            self.assertEqual(res, 12)

    def test_init_dry_run(self):
        """Verifies initialization properties and reboot state defaults."""
        # Assert
        self.assertTrue(self.mac.dry_run)
        self.assertIsInstance(self.mac.reboot, RebootState)
        self.assertEqual(self.mac.reboot.required, 0)
        self.assertEqual(self.mac.reboot.suggested, 0)

    def test_is_app_running_mocked(self):
        """Verifies is_app_running accurately checks process existence via pgrep and AppleScript fallback."""
        with patch('freshenmac.computer.RunCMD') as mock_cmd, \
            patch('freshenmac.computer.util.RunAppleScript') as mock_osa:
            # Case 1: pgrep finds match (returncode 0)
            mock_cmd.return_value = make_cmd(errno=0)
            self.assertTrue(self.mac.is_app_running('Logic Pro'))

            # Case 2: pgrep finds no match, AppleScript finds no match
            mock_cmd.return_value = make_cmd(errno=1)
            mock_osa.return_value = MagicMock(returncode=0, stdout='Finder, Terminal')
            self.assertFalse(self.mac.is_app_running('Logic Pro'))

            # Case 3: pgrep finds no match, AppleScript finds match
            mock_osa.return_value = MagicMock(returncode=0, stdout='Finder, Logic Pro, Terminal')
            self.assertTrue(self.mac.is_app_running('Logic Pro'))

    def test_is_app_running_real_finder(self):
        """Verifies is_app_running against real Finder on macOS GUI or nonexistent app."""
        if all([self.mac.is_mac, self.mac.is_app_running('Finder')]):
            self.assertTrue(self.mac.is_app_running('Finder'))
        self.assertFalse(self.mac.is_app_running('TotallyNonExistentApp123xyz'))

    def test_is_idle(self):
        """Verifies is_idle returns True when all_user_idle_time >= IDLE_THRESHOLD['Seconds']."""
        # Case 1: Idle time below threshold (e.g. 1000s < 3600s)
        with patch.object(MacOS, 'all_user_idle_time', new_callable=PropertyMock, return_value=1000):
            self.assertFalse(self.mac.is_idle)

        # Case 2: Idle time at or above threshold (e.g. 3600s >= 3600s)
        with patch.object(MacOS, 'all_user_idle_time', new_callable=PropertyMock, return_value=3600):
            self.assertTrue(self.mac.is_idle)

    def test_mas_timeout_default_is_1800(self):
        """Verifies mas_timeout defaults to 1800s to allow large App Store downloads."""
        self.assertEqual(self.mac.mas_timeout, 1800)

    def test_notify_manual_os_update(self):
        """Verifies notify_manual_os_update opens System Settings and triggers AppleScript alert."""
        # Arrange
        mac = MacOS(dry_run=False)
        with patch('freshenmac.computer.RunCMD') as mock_cmd, \
            patch('freshenmac.computer.util.RunAppleScript.dialog') as mock_dialog, \
            patch('builtins.print'):
            # Act
            mac.notify_manual_os_update('macOS 27.0.1-26A434')

            # Assert: opened settings pane and spawned alert dialog
            mock_cmd.assert_called_once_with(
                ['open', 'x-apple.systempreferences:com.apple.Software-Update-Settings.extension'],
                **mac.debug_vars,
            )
            mock_dialog.assert_called_once()
            args, kwargs = mock_dialog.call_args
            self.assertIn('macOS 27.0.1-26A434', args[0])
            self.assertIn('Volume Owner authentication is required', args[0])
            self.assertTrue(kwargs.get('background'))

    def test_notify_manual_os_update_dry_run(self):
        """Verifies notify_manual_os_update does not trigger commands or alerts in dry run."""
        # Arrange
        mac = MacOS(dry_run=True)
        with patch('freshenmac.computer.RunCMD') as mock_cmd, \
            patch('freshenmac.computer.util.RunAppleScript.dialog') as mock_dialog, \
            patch('builtins.print'):
            # Act
            mac.notify_manual_os_update('macOS 27.0.1-26A434')

            # Assert
            mock_cmd.assert_not_called()
            mock_dialog.assert_not_called()

    def test_schedule_and_cleanup_startup_run(self):
        """Verifies scheduling run-once LaunchAgent plist and subsequent cleanup."""
        # Arrange
        self.mac.dry_run = False
        with tempfile.TemporaryDirectory() as temp_dir:
            fake_home = Path(temp_dir)
            with patch('freshenmac.plist.Path.home', return_value=fake_home):
                with patch('freshenmac.plist.RunCMD') as mock_cmd:
                    mock_cmd.return_value.returncode = 0

                    # Act: schedule startup run
                    scheduled = self.mac.schedule_startup_run()
                    self.assertTrue(scheduled)

                    # Assert: plist written correctly
                    expected_plist = fake_home / 'Library/LaunchAgents/com.panther37.FreshenMac_runonce.plist'
                    self.assertTrue(expected_plist.is_file())

                    with open(expected_plist, 'rb') as f:
                        plist_data = plistlib.load(f)
                    self.assertEqual(plist_data.get('Label'), 'com.panther37.FreshenMac_runonce')
                    self.assertTrue(plist_data.get('RunAtLoad'))

                    # Act & Assert: cleanup startup run removes plist file
                    MacOS.cleanup_startup_run()
                    self.assertFalse(expected_plist.exists())

    def test_update_brew_delegation(self):
        """Verifies mac.update('brew') delegates directly to brew.update() and flags updates."""
        with patch.object(self.mac.brew, 'update') as mock_brew_update:
            self.mac.brew.updates_performed = True
            # Act
            self.mac.update('brew')

            # Assert
            mock_brew_update.assert_called_once()
            self.assertTrue(self.mac._updates_performed)

    def test_update_mas_skips_ignored_app(self):
        """Verifies _update_mas skips upgrading apps designated in ignored_mas_apps."""
        # Arrange: Xcode is in ignore list; Logic Pro is not
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
                    # Act
                    mac._update_mas()

                    # Assert: Xcode skipped, Logic Pro upgraded
                    upgraded_ids = [
                        c[2] for c in executed_commands
                        if all(
                            [
                                len(c) >= 3,
                                c[0].endswith('mas') if c else False,
                                c[1] == 'upgrade' if len(c) > 1 else False,
                            ],
                        )
                    ]
                    self.assertNotIn('497799835', upgraded_ids)
                    self.assertIn('634148309', upgraded_ids)
                    self.assertEqual(mac.reboot.required, 0)
                    mock_schedule.assert_not_called()

    def test_update_mas_skips_running_gui_app(self):
        """Verifies _update_mas defers upgrading currently running GUI apps to next reboot."""
        # Arrange: Logic Pro is outdated but currently running
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
                # Act
                self.mac._update_mas()

                # Assert: deferred to startup run, reboot required incremented, upgrade skipped
                self.assertEqual(self.mac.reboot.required, 1)
                mock_schedule.assert_called_once()
                for call_args in mock_cmd.call_args_list:
                    args, _ = call_args
                    self.assertNotIn('upgrade', args[0])

    def test_update_mas_upgrades_non_running_app(self):
        """Verifies _update_mas executes mas upgrade with utility taskpolicy on idle apps."""
        # Arrange: Xcode is outdated and not currently running
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
                # Act
                self.mac._update_mas()

                # Assert: upgraded successfully with utility taskpolicy
                self.assertEqual(self.mac.reboot.required, 0)
                upgraded = [(c, kw) for c, kw in executed_commands if all(['upgrade' in c, '497799835' in c])]
                self.assertTrue(upgraded)
                self.assertEqual(upgraded[0][1].get('taskpolicy'), 'utility')

    def test_update_os_downloaded_higher_version_triggers_reboot(self):
        """Verifies _update_os increments reboot requirement when higher macOS version downloaded."""
        # Arrange
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
            patch('freshenmac.softwareupdate.util.RunCMD', side_effect=fake_runcmd):
            with patch.object(self.mac, 'check_staged_reboot'):
                # Act
                self.mac._update_os()

                # Assert: reboot flagged and updates recorded
                self.assertGreaterEqual(self.mac.reboot.required, 1)
                self.assertTrue(self.mac._updates_performed)

    def test_update_os_failed_to_authenticate_triggers_notify(self):
        """Verifies _update_os alerts user via notify_manual_os_update when authentication fails."""
        # Arrange: Apple Silicon machine where softwareupdate requires Volume Owner credentials
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
            patch('freshenmac.softwareupdate.util.RunCMD', side_effect=fake_runcmd), \
            patch.object(mac, 'notify_manual_os_update') as mock_notify, \
            patch.object(mac, 'check_staged_reboot'):
            # Act
            mac._update_os()

            # Assert: alert dispatched and update registered in os_summary
            mock_notify.assert_called_once_with('macOS 27.0.1-26A434')
            self.assertIn('macOS 27.0.1-26A434', mac.os_summary['available'])

    def test_update_os_two_stage(self):
        """Verifies _update_os executes two-stage update: download followed by staging."""
        # Arrange
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
            patch('freshenmac.softwareupdate.util.RunCMD', side_effect=fake_runcmd) as mock_cmd:
            with patch.object(self.mac, 'check_staged_reboot'):
                # Act
                self.mac._update_os()

                # Assert: softwareupdate called with -d flag and options
                mock_cmd.assert_any_call(
                    ['softwareupdate', '-d', 'macOS Sequoia 15.8.1-24H32'],
                    cancelable=True,
                    debug=self.mac.debug_vars['debug'],
                    debug_limit=self.mac.debug_vars['debug_limit'],
                    taskpolicy=None,
                    spinner=False,
                    timeout=self.mac.timeout,
                )

    def test_xcode_property(self):
        """Verifies xcode property creates and caches an Xcode instance with matching settings."""
        # Arrange & Act
        xcode_inst = self.mac.xcode

        # Assert
        self.assertIsInstance(xcode_inst, Xcode)
        self.assertEqual(xcode_inst.dry_run, self.mac.dry_run)
        self.assertEqual(xcode_inst.debug, self.mac.debug_vars['debug'])


if __name__ == '__main__':
    unittest.main()
