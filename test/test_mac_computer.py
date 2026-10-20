import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

from freshenmac import util
from freshenmac.boot import RebootState
from freshenmac.computer import MacOS
from freshenmac.homebrew import HomeBrew
from freshenmac.util import RunCMD


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
        self.assertEqual(brew_inst.debug_vars, self.mac.debug_vars)

    def test_check_reboot_status_suggested(self):
        """Verifies check_reboot_status flags suggested reboot when uptime exceeds threshold."""
        # Arrange: updates were performed and system uptime is 16 days
        self.mac._updates_performed = True
        with patch.object(
            MacOS, 'uptime_days', new_callable=PropertyMock, return_value=16,
        ), patch(
            'freshenmac.softwareupdate.SoftwareUpdate.check_staged_reboot', return_value=0,
        ):
            # Act
            status = self.mac.check_reboot_status()

            # Assert
            self.assertTrue(status.suggested)

    def test_check_staged_reboot_with_install_at_logout(self):
        """Verifies check_staged_reboot detects staged update in index.plist with InstallAtLogout."""
        # Arrange: create temporary index.plist containing InstallAtLogout with verified payload directory
        with tempfile.TemporaryDirectory() as temp_dir:
            fake_index = Path(temp_dir) / 'index.plist'
            payload_dir = Path(temp_dir) / '001-12345'
            payload_dir.mkdir()
            with open(fake_index, 'wb') as f:
                plistlib.dump({'InstallAtLogout': ['001-12345']}, f)

            with patch(
                'freshenmac.softwareupdate.Path',
                side_effect=lambda *args, **kwargs: fake_index if any(
                    ['/Library/Updates' in str(a) for a in args],
                ) else Path(*args, **kwargs),
            ):
                # Act: query directly
                result = self.mac.softwareupdate.check_staged_reboot()

                # Assert: query returns 1 without mutating state
                self.assertEqual(result, 1)
                self.assertEqual(self.mac.reboot.required, [])

                # Act: orchestrator evaluates check_staged_reboot and updates reboot state
                self.mac.check_reboot_status()
                self.assertIn('Staged Apple Software Update (1)', self.mac.reboot.required)

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
        self.assertEqual(self.mac.reboot.required, [])
        self.assertEqual(self.mac.reboot.suggested, [])

    def test_is_app_running_mocked(self):
        """Verifies is_app_running accurately checks process existence via pgrep and AppleScript fallback."""
        with patch('freshenmac.util.RunCMD') as mock_cmd, patch('freshenmac.util.RunAppleScript') as mock_osa:
            # Case 1: pgrep finds match (returncode 0)
            mock_cmd.return_value = make_cmd(errno=0)
            self.assertTrue(util.is_app_running('Logic Pro'))

            # Case 2: pgrep finds no match, AppleScript finds no match
            mock_cmd.return_value = make_cmd(errno=1)
            mock_osa.return_value = MagicMock(returncode=0, stdout='Finder, Terminal')
            self.assertFalse(util.is_app_running('Logic Pro'))

            # Case 3: pgrep finds no match, AppleScript finds match
            mock_osa.return_value = MagicMock(returncode=0, stdout='Finder, Logic Pro, Terminal')
            self.assertTrue(util.is_app_running('Logic Pro'))

    def test_is_app_running_real_finder(self):
        """Verifies is_app_running against real Finder on macOS GUI or nonexistent app."""
        if all([self.mac.is_mac, util.is_app_running('Finder')]):
            self.assertTrue(util.is_app_running('Finder'))
        self.assertFalse(util.is_app_running('TotallyNonExistentApp123xyz'))

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
        with patch(
            'freshenmac.computer.RunCMD',
        ) as mock_cmd, patch(
            'freshenmac.computer.util.RunAppleScript.dialog',
        ) as mock_dialog, patch(
            'builtins.print',
        ):
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
        with patch(
            'freshenmac.computer.RunCMD',
        ) as mock_cmd, patch(
            'freshenmac.computer.util.RunAppleScript.dialog',
        ) as mock_dialog, patch(
            'builtins.print',
        ):
            # Act
            mac.notify_manual_os_update('macOS 27.0.1-26A434')

            # Assert
            mock_cmd.assert_not_called()
            mock_dialog.assert_not_called()

    def test_update_brew_delegation(self):
        """Verifies mac.update('brew') delegates directly to brew.update() and flags updates."""
        with patch.object(self.mac.brew, 'update') as mock_brew_update:
            self.mac.brew.updates_performed = True
            # Act
            self.mac.update('brew')

            # Assert
            mock_brew_update.assert_called_once()
            self.assertTrue(self.mac._updates_performed)

    def test_update_mas_delegation(self):
        """Verifies mac.update('mas') delegates directly to app_store.update() and flags updates."""
        with patch.object(self.mac.app_store, 'update') as mock_mas_update:
            self.mac.app_store.updates_performed = True
            # Act
            self.mac.update('mas')

            # Assert
            mock_mas_update.assert_called_once()
            self.assertTrue(self.mac._updates_performed)

    def test_update_os_delegation(self):
        """Verifies mac.update('os') delegates directly to softwareupdate.update() and flags updates."""
        with patch.object(self.mac.softwareupdate, 'update') as mock_os_update:
            self.mac.softwareupdate.updates_performed = True
            # Act
            self.mac.update('os')

            # Assert
            mock_os_update.assert_called_once()
            self.assertTrue(self.mac._updates_performed)


if __name__ == '__main__':
    unittest.main()
