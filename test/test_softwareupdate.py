import io
import plistlib
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
    """
    Unit tests for the SoftwareUpdate class in freshenmac.softwareupdate.

    Covers two-stage macOS updates (download staging then installation), Apple Silicon
    Volume Owner authentication handoffs via System Settings & AppleScript dialogs,
    and inspection of staged update plists in /Library/Updates.
    """

    def setUp(self):
        """Initializes a dry-run SoftwareUpdate instance with a clean RebootState."""
        self.reboot = RebootState()
        with patch('freshenmac.softwareupdate.util.RunCMD') as mock_cmd:
            mock_cmd.return_value = make_cmd(stdout='15.0\n')
            self.swu = SoftwareUpdate(dry_run=True, reboot=self.reboot)

    def test_arm64_stdinpass_flags(self):
        """Verifies Apple Silicon CLI invocation includes --stdinpass and --user credentials."""
        # Arrange: SoftwareUpdate configured for Apple Silicon with credentials
        swu = SoftwareUpdate(
            arm_installed=True,
            console_user='testuser',
            user_password='secretpassword',
        )

        # Act & Assert: install_label should inject password via stdin and pass user flag
        with patch('freshenmac.softwareupdate.util.RunCMD') as mock_cmd:
            swu.install_label('macOS Sequoia 15.8.1-24H32', force_restart_now=True)
            mock_cmd.assert_called_once_with(
                [
                    'softwareupdate', '--install', 'macOS Sequoia 15.8.1-24H32', '--restart', '--stdinpass',
                    '--user', 'testuser',
                ],
                to_input='secretpassword\n',
                need_root=True,
                cancelable=True,
                debug=0,
                debug_limit=0,
                taskpolicy=None,
                timeout=1800,
            )

    def test_calculate_timeout_dynamic(self):
        """Verifies calculate_timeout scales timeout based on size, buffer, and speed."""
        fixture_path = Path(__file__).resolve().parent / 'fixtures/20261103-0742_softwareupdate__list/stdout.txt'
        content = fixture_path.read_text(encoding='utf-8')
        swu = SoftwareUpdate(download_speed_mbps=100.0)
        swu.softwareupdate_list = make_cmd(stdout=content)
        # Size: 1066742KiB = 1041.740234375 MB
        # Rate: 100.0 / 8.0 = 12.5 MB/s
        # Time: (1041.740234375 / 12.5) * 1.25 = 104.1740234375 s
        # Total: 300 + 104 = 404 s
        self.assertEqual(swu.calculate_timeout('macOS Sequoia 15.8.2-24H232'), 404)

    def test_calculate_timeout_fallback_to_defaults(self):
        """Verifies calculate_timeout falls back to default preferences when download_speed_mbps is 0."""
        fixture_path = Path(__file__).resolve().parent / 'fixtures/20261103-0742_softwareupdate__list/stdout.txt'
        content = fixture_path.read_text(encoding='utf-8')
        swu = SoftwareUpdate(download_speed_mbps=0.0)
        swu.softwareupdate_list = make_cmd(stdout=content)
        with patch('freshenmac.plist.load_preferences', return_value={}):
            # Default speed: 5.0 Mbps -> Rate: 0.625 MB/s
            # Time: (1041.740234375 / 0.625) * 1.25 = 2083.48046875 s
            # Total: 300 + 2083 = 2383 s
            self.assertEqual(swu.calculate_timeout('macOS Sequoia 15.8.2-24H232'), 2383)

    def test_calculate_timeout_unknown_size_returns_default_timeout(self):
        """Verifies calculate_timeout returns default timeout when update size cannot be determined."""
        swu = SoftwareUpdate(download_speed_mbps=50.0, timeout=1800)
        self.assertEqual(swu.calculate_timeout('NonExistentUpdate'), 1800)

    def test_check_staged_reboot(self):
        """Verifies detection of staged macOS updates waiting for logout/reboot in /Library/Updates/index.plist."""
        # Arrange: mock index.plist with verified payload directory
        raw_bytes = plistlib.dumps({'InstallLater': ['001-12345']})
        with patch.object(
            Path, 'is_file', autospec=True, return_value=True,
        ), patch(
            'freshenmac.softwareupdate.open', side_effect=lambda *args, **kwargs: io.BytesIO(raw_bytes),
        ), patch.object(
            Path, 'is_dir', autospec=True, side_effect=lambda p: p == Path('/Library/Updates/001-12345'),
        ):
            res = self.swu.check_staged_reboot()

            # Assert: Returns count 1 without mutating self.reboot.required (pure query)
            self.assertEqual(res, 1)
            self.assertEqual(self.reboot.required, [])

    def test_check_staged_reboot_multiple_payloads(self):
        """Verifies check_staged_reboot returns exact count when multiple distinct payloads exist."""
        payload_pkg2 = Path('/Library/Updates/002-67890.pkg')
        raw_bytes = plistlib.dumps({
            'InstallLater': ['001-12345'],
            'ProductPaths': {'002-67890': str(payload_pkg2)},
        })
        with patch.object(
            Path, 'is_file', autospec=True, return_value=True,
        ), patch(
            'freshenmac.softwareupdate.open', side_effect=lambda *args, **kwargs: io.BytesIO(raw_bytes),
        ), patch.object(
            Path, 'exists', autospec=True, side_effect=lambda p: p == payload_pkg2,
        ), patch.object(
            Path, 'is_dir', autospec=True, side_effect=lambda p: p == Path('/Library/Updates/001-12345'),
        ):
            res = self.swu.check_staged_reboot()

            self.assertEqual(res, 2)
            self.assertEqual(self.reboot.required, [])

    def test_check_staged_reboot_orphaned_key_ignored(self):
        """Verifies orphaned product keys in InstallLater without payloads on disk return 0."""
        raw_bytes = plistlib.dumps({
            'InstallAtLogout': [],
            'InstallLater': ['142-23583'],
            'ProductPaths': {},
        })
        with patch.object(
            Path, 'is_file', autospec=True, return_value=True,
        ), patch(
            'freshenmac.softwareupdate.open', side_effect=lambda *args, **kwargs: io.BytesIO(raw_bytes),
        ), patch.object(
            Path, 'is_dir', autospec=True, return_value=False,
        ):
            res = self.swu.check_staged_reboot()

            self.assertEqual(res, 0)
            self.assertEqual(self.reboot.required, [])

    def test_downloaded_ver_pattern(self):
        """Verifies PATTERN['Downloaded Ver'] matches Monterey through Tahoe across output formats."""
        from freshenmac.softwareupdate import PATTERN

        pattern = PATTERN['Downloaded Ver']
        # Monterey, Ventura, Sonoma, Sequoia, Tahoe
        self.assertEqual(pattern.search('Downloaded: macOS Sequoia 15.8.1').group('ver'), '15.8.1')
        self.assertEqual(pattern.search('Downloaded: 15.8.1').group('ver'), '15.8.1')
        self.assertEqual(pattern.search('Downloaded macOS Monterey 12.7.4').group('ver'), '12.7.4')
        self.assertEqual(pattern.search('Downloading macOS Big Sur 11.7.10').group('ver'), '11.7.10')
        self.assertEqual(pattern.search('Downloading macOS 15.8.2').group('ver'), '15.8.2')
        self.assertEqual(pattern.search('Downloading macOS Sonoma 14.7.1').group('ver'), '14.7.1')
        self.assertEqual(pattern.search('Downloading macOS Sequoia 15.8.2').group('ver'), '15.8.2')
        self.assertEqual(pattern.search('Downloading macOS Tahoe 16.0').group('ver'), '16.0')
        self.assertEqual(pattern.search('Downloaded: macOS Sequoia 15.8.2 (24H232)').group('ver'), '15.8.2')

        # Must not match progress percentages or single integer non-versions
        self.assertIsNone(pattern.search('Downloaded: 16'))
        self.assertIsNone(pattern.search('Downloading: 100.00%'))
        self.assertIsNone(pattern.search('Downloading: 0.10%'))
        self.assertIsNone(pattern.search('Downloading: 100%'))

    def test_get_update_size_mb_from_fixture(self):
        """Verifies get_update_size_mb extracts size in MB from softwareupdate --list output."""
        fixture_path = Path(__file__).resolve().parent / 'fixtures/20261103-0742_softwareupdate__list/stdout.txt'
        content = fixture_path.read_text(encoding='utf-8')
        swu = SoftwareUpdate()
        swu.softwareupdate_list = make_cmd(stdout=content)
        size_mb = swu.get_update_size_mb('macOS Sequoia 15.8.2-24H232')
        self.assertAlmostEqual(size_mb, 1066742 / 1024.0, places=3)
        self.assertEqual(swu.get_update_size_mb('Nonexistent Label'), 0.0)

    def test_get_update_size_mb_units(self):
        """Verifies get_update_size_mb converts KiB, MB, GB, and bytes correctly."""
        output = (
            "* Label: Package-MB\n"
            "\tTitle: Package MB, Version: 1.0, Size: 500MB, Recommended: YES\n"
            "* Label: Package-GB\n"
            "\tTitle: Package GB, Version: 2.0, Size: 1.5GB, Recommended: YES\n"
            "* Label: Package-KB\n"
            "\tTitle: Package KB, Version: 3.0, Size: 1024K, Recommended: YES\n"
            "* Label: Package-NoSize\n"
            "\tTitle: Package NoSize, Version: 4.0\n"
        )
        swu = SoftwareUpdate()
        swu.softwareupdate_list = make_cmd(stdout=output)
        self.assertEqual(swu.get_update_size_mb('Package-MB'), 500.0)
        self.assertEqual(swu.get_update_size_mb('Package-GB'), 1536.0)
        self.assertEqual(swu.get_update_size_mb('Package-KB'), 1.0)
        self.assertEqual(swu.get_update_size_mb('Package-NoSize'), 0.0)

    def test_init_defaults(self):
        """Verifies default values upon initialization."""
        self.assertTrue(self.swu.dry_run)
        self.assertEqual(self.swu.timeout, 1800)
        self.assertEqual(self.swu.download_speed_mbps, 0.0)
        self.assertFalse(self.swu.updates_performed)
        self.assertEqual(self.swu.summary, {'available': [], 'downloaded': []})
        self.assertEqual(self.swu.reboot.required, [])

    def test_notify_manual_dry_run(self):
        """Verifies notify_manual in dry-run mode skips opening settings and dialogs."""
        swu = SoftwareUpdate(dry_run=True)
        with patch(
            'freshenmac.softwareupdate.util.RunCMD'
        ) as mock_cmd, patch(
            'freshenmac.softwareupdate.util.RunAppleScript.dialog'
        ) as mock_dialog:
            swu.notify_manual('macOS 27.0.1-26A434')
            mock_cmd.assert_not_called()
            mock_dialog.assert_not_called()

    def test_notify_manual_opens_settings_and_dialog(self):
        """Verifies notify_manual opens System Settings pane and launches AppleScript alert dialog."""
        swu = SoftwareUpdate(dry_run=False)
        with patch(
            'freshenmac.softwareupdate.util.RunCMD'
        ) as mock_cmd, patch(
            'freshenmac.softwareupdate.util.RunAppleScript.dialog'
        ) as mock_dialog, patch(
            'builtins.print'
        ):
            swu.notify_manual('macOS 27.0.1-26A434')

            # 1. Opens the Software Update preference extension
            mock_cmd.assert_called_once_with(
                ['open', 'x-apple.systempreferences:com.apple.Software-Update-Settings.extension'],
                **swu.debug_vars,
            )

            # 2. Spawns persistent non-blocking alert dialog prompting Volume Owner authentication
            mock_dialog.assert_called_once()
            args, kwargs = mock_dialog.call_args
            self.assertIn('macOS 27.0.1-26A434', args[0])
            self.assertIn('Volume Owner authentication is required', args[0])
            self.assertTrue(kwargs.get('background'))

    def test_software_update_restart_required(self):
        """Verifies packages with 'Action: restart' increment reboot.required counter."""
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

        with patch(
            'freshenmac.softwareupdate.util.RunCMD', side_effect=fake_runcmd
        ), patch.object(
            self.swu, 'check_staged_reboot'
        ):
            self.swu.update()
            self.assertTrue(self.reboot.required)
            self.assertTrue(self.swu.updates_performed)
            self.assertIn('Safari18.0-24H123', self.swu.summary['downloaded'])

    def test_two_stage_macos_update_download_and_reboot(self):
        """Verifies Stage 1 download stages the update and marks reboot required without immediate installation."""
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

        with patch(
            'freshenmac.softwareupdate.util.RunCMD', side_effect=fake_runcmd
        ), patch.object(
            self.swu, 'check_staged_reboot'
        ):
            self.swu.update()
            self.assertTrue(self.reboot.required)
            self.assertTrue(self.swu.updates_performed)
            self.assertIn('macOS Sequoia 15.8.1-24H32', self.swu.summary['downloaded'])
            self.assertEqual(str(self.swu.os_ver['Downloaded']), '15.8.1')

    def test_update_macos_uses_calculated_timeout(self):
        """Verifies _update_macos passes calculated dynamic timeout to RunCMD."""
        swu = SoftwareUpdate(download_speed_mbps=100.0, dry_run=False)
        output = "* Label: macOS Sequoia 15.8.2-24H232\n\tTitle: macOS Sequoia 15.8.2, Version: 15.8.2, Size: 1066742KiB\n"
        swu.softwareupdate_list = make_cmd(stdout=output)

        with patch('freshenmac.softwareupdate.util.RunCMD') as mock_cmd:
            mock_cmd.return_value = make_cmd(stdout="Downloaded: macOS Sequoia 15.8.2\n")
            swu._update_macos('macOS Sequoia 15.8.2-24H232')
            mock_cmd.assert_called_once_with(
                ['softwareupdate', '-d', 'macOS Sequoia 15.8.2-24H232'],
                cancelable=True,
                debug=0,
                debug_limit=0,
                spinner=False,
                taskpolicy=None,
                timeout=404,
            )

    def test_update_os_failed_to_authenticate_notifies(self):
        """Verifies authentication failure during download triggers manual user notification."""
        self.swu.dry_run = False

        def fake_runcmd(cmd, *args, **kwargs):
            mock = MagicMock()
            mock.returncode = 0
            if '--list' in cmd:
                mock.std_all = "* Label: macOS 27.0.1-26A434\n\tTitle: macOS 27.0.1"
            elif '-d' in cmd:
                mock.std_all = "Downloaded: macOS 27.0.1\nFailed to authenticate"
            return mock

        with patch(
            'freshenmac.softwareupdate.util.RunCMD', side_effect=fake_runcmd
        ), patch.object(
            self.swu, 'notify_manual'
        ) as mock_notify, patch.object(
            self.swu, 'check_staged_reboot'
        ):
            self.swu.update()
            mock_notify.assert_called_once_with('macOS 27.0.1-26A434')
            self.assertIn('macOS 27.0.1-26A434', self.swu.summary['available'])

    def test_update_software_uses_calculated_timeout(self):
        """Verifies _update_software passes calculated dynamic timeout to RunCMD."""
        swu = SoftwareUpdate(download_speed_mbps=100.0, dry_run=False)
        output = "* Label: Command-Line-Tools\n\tTitle: Command Line Tools, Version: 16.0, Size: 1066742KiB\n"
        swu.softwareupdate_list = make_cmd(stdout=output)

        with patch('freshenmac.softwareupdate.util.RunCMD') as mock_cmd, patch.object(
            swu, 'install_label', return_value=make_cmd()
        ):
            mock_cmd.return_value = make_cmd(stdout="Downloaded Command-Line-Tools\n")
            swu._update_software('Command-Line-Tools')
            mock_cmd.assert_called_once_with(
                ['softwareupdate', '-d', 'Command-Line-Tools'],
                cancelable=True,
                debug=0,
                debug_limit=0,
                taskpolicy=None,
                timeout=404,
            )


if __name__ == '__main__':
    unittest.main()
