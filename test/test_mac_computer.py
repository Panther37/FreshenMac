import io
import plistlib
import unittest
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

from freshenmac import config, util
from freshenmac.boot import RebootState
from freshenmac.computer import MacOS
from freshenmac.homebrew import HomeBrew

_RunCMD = util.RunCMD


def make_cmd(stdout: str = "", stderr: str = "", errno: int = 0) -> util.RunCMD:
    """Creates a real RunCMD instance without executing an actual subprocess."""
    cmd = _RunCMD.__new__(_RunCMD)
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
            with patch('freshenmac.computer.util.RunCMD') as mock_cmd:
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

    def test_arm_installed(self):
        """Verifies arm_installed checks sysctl hw.optional.arm64 via util.RunCMD."""
        mac = MacOS(dry_run=True)
        with patch('freshenmac.computer.util.RunCMD') as mock_cmd:
            mock_cmd.return_value = make_cmd(stdout='1\n')
            self.assertTrue(mac.arm_installed)

        mac_intel = MacOS(dry_run=True)
        with patch('freshenmac.computer.util.RunCMD') as mock_cmd:
            mock_cmd.return_value = make_cmd(stdout='0\n')
            self.assertFalse(mac_intel.arm_installed)

    def test_brew_property(self):
        """Verifies brew property initializes and caches a HomeBrew instance with matching flags."""
        # Arrange & Act
        brew_inst = self.mac.brew

        # Assert
        self.assertIsInstance(brew_inst, HomeBrew)
        self.assertEqual(brew_inst.is_arm, self.mac.arm_installed)
        self.assertEqual(brew_inst.dry_run, self.mac.dry_run)
        self.assertEqual(brew_inst.debug_vars, self.mac.debug_vars)
        self.assertEqual(brew_inst.download_speed_mbps, self.mac.download_speed)

    def test_check_reboot_status_suggested(self):
        """Verifies check_reboot_status flags suggested reboot when uptime exceeds threshold."""
        # Arrange: updates were performed and system uptime is 16 days
        self.mac._updates_performed = True
        with patch.object(
            MacOS, 'uptime_days', new_callable=PropertyMock, return_value=config.IDLE_THRESHOLD['Uptime in Days'] + 1,
        ), patch(
            'freshenmac.softwareupdate.SoftwareUpdate.check_staged_reboot', return_value=0,
        ):
            # Act
            status = self.mac.check_reboot_status()

            # Assert
            self.assertTrue(status.suggested)

    def test_check_staged_reboot_with_install_at_logout(self):
        """Verifies check_staged_reboot detects staged update in index.plist with InstallAtLogout."""
        # Arrange: mock index.plist containing InstallAtLogout with verified payload directory
        raw_bytes = plistlib.dumps({'InstallAtLogout': ['001-12345']})
        with patch.object(
            Path, 'is_file', autospec=True, return_value=True,
        ), patch(
            'freshenmac.softwareupdate.open', side_effect=lambda *args, **kwargs: io.BytesIO(raw_bytes),
        ), patch.object(
            Path, 'is_dir', autospec=True, side_effect=lambda p: p == Path('/Library/Updates/001-12345'),
        ):
            # Act: query directly
            result = self.mac.softwareupdate.check_staged_reboot()

            # Assert: query returns 1 without mutating state
            self.assertEqual(result, 1)
            self.assertEqual(self.mac.reboot.required, [])

            # Act: orchestrator evaluates check_staged_reboot and updates reboot state
            self.mac.check_reboot_status()
            self.assertIn('Staged Apple Software Update (1)', self.mac.reboot.required)

    def test_download_speed_initialization(self):
        """Verifies download_speed respects explicit argument or falls back to preferences/defaults."""
        # Case 1: Explicit download_speed provided
        mac_explicit = MacOS(download_speed=25.0)
        self.assertEqual(mac_explicit.download_speed, 25.0)
        self.assertEqual(mac_explicit.app_store.download_speed_mbps, 25.0)
        self.assertEqual(mac_explicit.softwareupdate.download_speed_mbps, 25.0)

        # Case 2: Unspecified (0.0), loaded from preferences
        with patch('freshenmac.plist.load_preferences', return_value={'download-speed': 15.0}):
            mac_pref = MacOS(download_speed=0.0)
            self.assertEqual(mac_pref.download_speed, 15.0)
            self.assertEqual(mac_pref.app_store.download_speed_mbps, 15.0)
            self.assertEqual(mac_pref.softwareupdate.download_speed_mbps, 15.0)

        # Case 3: Unspecified (0.0) and absent from preferences -> config default
        with patch('freshenmac.plist.load_preferences', return_value={}):
            mac_default = MacOS(download_speed=0.0)
            self.assertEqual(mac_default.download_speed, config.DEFAULTS['Download Speed Mbps'])
            self.assertEqual(mac_default.app_store.download_speed_mbps, config.DEFAULTS['Download Speed Mbps'])
            self.assertEqual(
                mac_default.softwareupdate.download_speed_mbps,
                config.DEFAULTS['Download Speed Mbps'],
            )

    def test_filevault_enabled(self):
        """Verifies filevault_enabled inspects fdesetup status output via util.RunCMD."""
        mac = MacOS(dry_run=True)
        with patch.object(MacOS, 'is_mac', new_callable=PropertyMock, return_value=True):
            with patch('freshenmac.computer.util.RunCMD') as mock_cmd:
                mock_cmd.return_value = make_cmd(stdout='FileVault is On.\n')
                self.assertTrue(mac.filevault_enabled)

        mac_off = MacOS(dry_run=True)
        with patch.object(MacOS, 'is_mac', new_callable=PropertyMock, return_value=True):
            with patch('freshenmac.computer.util.RunCMD') as mock_cmd:
                mock_cmd.return_value = make_cmd(stdout='FileVault is Off.\n')
                self.assertFalse(mac_off.filevault_enabled)

    def test_get_running_gui_apps(self):
        """Verifies get_running_gui_apps queries System Events and parses process names."""
        with patch('freshenmac.util.RunAppleScript') as mock_osa:
            mock_osa.return_value = MagicMock(returncode=0, stdout='Finder, Logic Pro, Xcode')
            apps = util.get_running_gui_apps()
            self.assertEqual(apps, {'finder', 'logic pro', 'xcode'})

            # Exception fallback
            mock_osa.side_effect = RuntimeError('AppleScript failed')
            self.assertEqual(util.get_running_gui_apps(), set())

    def test_idle_time_returns_int(self):
        """Verifies idle_time queries IOHIDSystem and converts nanoseconds to seconds."""
        # Arrange: mock ioreg returning HIDIdleTime in nanoseconds (12.5s)
        with patch('freshenmac.computer.util.RunCMD') as mock_cmd:
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
        self.assertEqual(self.mac.ignore_mas, set())
        self.assertIsInstance(self.mac.ignore_mas, set)

    def test_init_loads_download_speed_from_prefs(self):
        """Verifies MacOS initializes download_speed from loaded plist preferences when 0.0."""
        with patch('freshenmac.plist.load_preferences', return_value={'download-speed': 25.0}):
            mac = MacOS(dry_run=True)
            self.assertEqual(mac.download_speed, 25.0)

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

            # Case 4: pre-fetched running_gui_apps set
            mock_cmd.return_value = make_cmd(errno=1)
            self.assertTrue(util.is_app_running('Logic Pro', running_gui_apps={'logic pro', 'finder'}))
            self.assertFalse(util.is_app_running('Logic Pro', running_gui_apps={'safari', 'terminal'}))

    def test_is_idle(self):
        """Verifies is_idle returns True when all_user_idle_time >= IDLE_THRESHOLD['Seconds']."""
        # Case 1: Idle time below threshold (e.g. 1000s < 3600s)
        with patch.object(MacOS, 'all_user_idle_time', new_callable=PropertyMock, return_value=1000):
            self.assertFalse(self.mac.is_idle)

        # Case 2: Idle time at or above threshold (e.g. 3600s >= 3600s)
        with patch.object(MacOS, 'all_user_idle_time', new_callable=PropertyMock, return_value=3600):
            self.assertTrue(self.mac.is_idle)

    def test_lazy_properties_not_initialized_on_init(self):
        """Verifies workers (brew, app_store, softwareupdate) are lazy cached properties and not created on init."""
        mac = MacOS(dry_run=True)
        self.assertNotIn('app_store', mac.__dict__)
        self.assertNotIn('brew', mac.__dict__)
        self.assertNotIn('softwareupdate', mac.__dict__)

        # Evaluate app_store
        store = mac.app_store
        self.assertIn('app_store', mac.__dict__)
        self.assertIs(store, mac.app_store)

        # Evaluate softwareupdate
        swu = mac.softwareupdate
        self.assertIn('softwareupdate', mac.__dict__)
        self.assertIs(swu, mac.softwareupdate)

        # Evaluate brew
        hb = mac.brew
        self.assertIn('brew', mac.__dict__)
        self.assertIs(hb, mac.brew)

    def test_log_summary_staged_macos_does_not_report_up_to_date(self):
        """Verifies print_summary excludes '(up to date)' when a macOS update is staged."""
        mac = MacOS(dry_run=True)
        mac.softwareupdate.summary['downloaded'] = ['Safari27.2SequoiaAuto-27.2', 'macOS Sequoia 15.8.2-24H232']
        mac.softwareupdate.os_ver['Downloaded'] = util.parse_version('15.8.2')
        mac.softwareupdate.os_ver['Installed'] = util.parse_version('15.8.1')

        captured_summary = []

        with patch.object(mac.log, 'print_store', side_effect=lambda msg, comp='': captured_summary.append(msg)):
            mac.print_summary()

            summary_text = '\n'.join(captured_summary)
            self.assertIn('✔︎ [Staged] macOS Sequoia 15.8.2-24H232', summary_text)
            self.assertNotIn('macOS 15.8.1 (up to date)', summary_text)

    def test_mas_timeout_default_is_1800(self):
        """Verifies mas_timeout defaults to 1800s to allow large App Store downloads."""
        self.assertEqual(self.mac.mas_timeout, 1800)

    def test_supports_authrestart(self):
        """Verifies supports_authrestart queries fdesetup supportsauthrestart via util.RunCMD."""
        mac = MacOS(dry_run=True)
        with patch.object(MacOS, 'is_mac', new_callable=PropertyMock, return_value=True):
            with patch('freshenmac.computer.util.RunCMD') as mock_cmd:
                mock_cmd.return_value = make_cmd(stdout='true\n')
                self.assertTrue(mac.supports_authrestart)

        mac_unsupported = MacOS(dry_run=True)
        with patch.object(MacOS, 'is_mac', new_callable=PropertyMock, return_value=True):
            with patch('freshenmac.computer.util.RunCMD') as mock_cmd:
                mock_cmd.return_value = make_cmd(stdout='false\n')
                self.assertFalse(mac_unsupported.supports_authrestart)

    def test_update_brew_delegation(self):
        """Verifies mac.update('brew') delegates directly to brew.update() and flags updates."""
        with patch.object(self.mac.brew, 'update', return_value=True) as mock_brew_update:
            # Act
            result = self.mac.update('brew')

            # Assert
            mock_brew_update.assert_called_once()
            self.assertTrue(result)
            self.assertTrue(self.mac._updates_performed)

    def test_update_mas_delegation(self):
        """Verifies mac.update('mas') delegates directly to app_store.update() and flags updates."""
        with patch.object(self.mac.app_store, 'update', return_value=True) as mock_mas_update:
            # Act
            result = self.mac.update('mas')

            # Assert
            mock_mas_update.assert_called_once()
            self.assertTrue(result)
            self.assertTrue(self.mac._updates_performed)

    def test_update_mas_merges_no_mas_from_prefs(self):
        """Verifies _update_mas merges no-mas items from plist preferences into ignore_mas set."""
        with patch('freshenmac.plist.load_preferences', return_value={'no-mas': ['iPhoto', 'Xcode']}):
            mac = MacOS(dry_run=True, ignore_mas={'Slack'})
            with patch.object(mac.app_store, 'update', return_value=True):
                mac._update_mas()
                self.assertIn('iphoto', {x.lower() for x in mac.ignore_mas})
                self.assertIn('xcode', {x.lower() for x in mac.ignore_mas})
                self.assertIn('slack', {x.lower() for x in mac.ignore_mas})

    def test_update_os_delegation(self):
        """Verifies mac.update('os') delegates directly to softwareupdate.update() and flags updates."""
        with patch.object(self.mac.softwareupdate, 'update', return_value=True) as mock_os_update:
            # Act
            result = self.mac.update('os')

            # Assert
            mock_os_update.assert_called_once()
            self.assertTrue(result)
            self.assertTrue(self.mac._updates_performed)

    def test_uptime_seconds(self):
        """Verifies uptime_seconds parses kern.boottime epoch via util.RunCMD."""
        mac = MacOS(dry_run=True)
        with patch('time.time', return_value=1730000100.0), patch(
            'freshenmac.computer.util.RunCMD',
        ) as mock_cmd:
            mock_cmd.return_value = make_cmd(stdout="{ sec = 1730000000, usec = 0 } Mon Oct 27 00:00:00 2026\n")
            self.assertEqual(mac.uptime_seconds, 100)


if __name__ == '__main__':
    unittest.main()
