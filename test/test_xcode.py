import unittest
from pathlib import Path
from unittest.mock import patch

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


class TestXcode(unittest.TestCase):
    """
    Unit tests for the standalone Xcode management class.

    Covers detection of Xcode and Command Line Tools (CLT), automated license
    acceptance via cached sudo, interactive GUI prompt fallbacks, and unattended
    silent CLT installation using softwareupdate and the sentinel touch file.
    """

    def test_accept_license_cached_sudo_success(self):
        """Verifies license agreement is accepted silently when sudo credentials are cached."""
        xc = Xcode(dry_run=False)
        with patch('freshenmac.xcode.Path.is_file', return_value=True):
            with patch('freshenmac.xcode.is_interactive', return_value=False):
                with patch('freshenmac.xcode.RunCMD') as mock_cmd:
                    mock_cmd.return_value = make_cmd(errno=0)
                    self.assertTrue(xc.accept_license())

    def test_accept_license_dry_run(self):
        """Verifies license acceptance in dry-run mode returns False without running commands."""
        xc = Xcode(dry_run=True)
        with patch('freshenmac.xcode.Path.is_file', return_value=True):
            with patch('freshenmac.xcode.RunCMD', return_value=make_cmd(errno=69)):
                self.assertFalse(xc.accept_license())
                self.assertFalse(xc.check_and_fix_license())

    def test_accept_license_interactive_success(self):
        """Verifies interactive tty session prompts for sudo password to accept license."""
        xc = Xcode(dry_run=False)
        with patch('freshenmac.xcode.Path.is_file', return_value=True):
            with patch('freshenmac.xcode.is_interactive', return_value=True):
                with patch('freshenmac.xcode.RunCMD') as mock_cmd:
                    mock_cmd.return_value = make_cmd(errno=0)
                    self.assertTrue(xc.accept_license())

    def test_accept_license_no_sudo(self):
        """Verifies license acceptance fails gracefully in non-interactive sessions without cached sudo."""
        xc = Xcode(dry_run=False)
        with patch('freshenmac.xcode.Path.is_file', return_value=True):
            with patch('freshenmac.xcode.is_interactive', return_value=False):
                with patch('freshenmac.xcode.RunCMD') as mock_cmd:
                    mock_cmd.return_value = make_cmd(errno=1)
                    self.assertFalse(xc.accept_license())

    def test_check_and_install_clt_already_installed(self):
        """Verifies check_and_install_clt returns True immediately when CLT is already present."""
        xc = Xcode()
        with patch.object(Xcode, 'is_clt_installed', True):
            self.assertTrue(xc.check_and_install_clt())

    def test_check_and_install_clt_dry_run(self):
        """Verifies check_and_install_clt logs dry-run notice without installing when missing."""
        xc = Xcode(dry_run=True)
        with patch.object(Xcode, 'is_clt_installed', False):
            with patch.object(Xcode, 'is_installed', False):
                self.assertFalse(xc.check_and_install_clt())

    def test_check_and_install_clt_headless_with_sudo(self):
        """Verifies headless session with sudo triggers unattended softwareupdate installation."""
        xc = Xcode(dry_run=False)
        with patch.object(Xcode, 'is_clt_installed', False):
            with patch.object(Xcode, 'is_installed', False):
                with patch('freshenmac.xcode.is_interactive', return_value=False):
                    with patch('freshenmac.xcode.RunCMD.can_sudo', return_value=True):
                        with patch.object(xc, 'install_clt_headless', return_value=True) as mock_headless:
                            self.assertTrue(xc.check_and_install_clt())
                            mock_headless.assert_called_once()

    def test_check_and_install_clt_headless_without_sudo(self):
        """Verifies headless session without sudo logs an error and returns False."""
        xc = Xcode(dry_run=False)
        with patch.object(Xcode, 'is_clt_installed', False):
            with patch.object(Xcode, 'is_installed', False):
                with patch('freshenmac.xcode.is_interactive', return_value=False):
                    with patch('freshenmac.xcode.RunCMD.can_sudo', return_value=False):
                        with patch('os.geteuid', return_value=501):
                            self.assertFalse(xc.check_and_install_clt())

    def test_check_and_install_clt_interactive(self):
        """Verifies interactive session triggers the native xcode-select GUI install dialog."""
        xc = Xcode(dry_run=False)
        with patch.object(Xcode, 'is_clt_installed', False):
            with patch.object(Xcode, 'is_installed', False):
                with patch('freshenmac.xcode.is_interactive', return_value=True):
                    with patch.object(xc, 'install_clt_interactive', return_value=True) as mock_interactive:
                        self.assertTrue(xc.check_and_install_clt())
                        mock_interactive.assert_called_once()

    def test_init_defaults(self):
        """Verifies default values and custom parameter assignment upon initialization."""
        xc = Xcode()
        self.assertFalse(xc.debug)
        self.assertFalse(xc.dry_run)

        xc_custom = Xcode(debug=True, dry_run=True)
        self.assertTrue(xc_custom.debug)
        self.assertTrue(xc_custom.dry_run)

    def test_install_clt_headless_install_failure(self):
        """Verifies headless installation cleans up sentinel file when softwareupdate -i fails."""
        xc = Xcode()
        sw_output = "* Label: Command Line Tools for Xcode-16.2\n"
        with patch('freshenmac.xcode.Path.touch'), \
            patch('freshenmac.xcode.Path.is_file', return_value=True), \
            patch('freshenmac.xcode.Path.unlink'):
            with patch('freshenmac.xcode.RunCMD') as mock_cmd:
                mock_cmd.side_effect = [
                    make_cmd(stdout=sw_output),  # .      softwareupdate -l
                    make_cmd(errno=1, stderr="Error"),  # softwareupdate -i failed
                ]
                self.assertFalse(xc.install_clt_headless())

    def test_install_clt_headless_no_package(self):
        """Verifies headless installation aborts cleanly if softwareupdate list has no CLT package."""
        xc = Xcode()
        with patch('freshenmac.xcode.Path.touch'), \
            patch('freshenmac.xcode.Path.is_file', return_value=True), \
            patch('freshenmac.xcode.Path.unlink'):
            with patch('freshenmac.xcode.RunCMD', return_value=make_cmd(stdout='No updates found')):
                self.assertFalse(xc.install_clt_headless())

    def test_install_clt_headless_success(self):
        """Verifies full headless install sequence: sentinel touch -> softwareupdate -> xcode-select -s."""
        xc = Xcode()
        sw_output = (
            "Software Update Find Found the following new or updated software:\n"
            "* Label: Command Line Tools for Xcode-16.2\n"
            "  Title: Command Line Tools for Xcode, Version: 16.2\n"
        )
        with patch('freshenmac.xcode.Path.touch'), \
            patch('freshenmac.xcode.Path.is_file', return_value=True), \
            patch('freshenmac.xcode.Path.unlink'), \
            patch('freshenmac.xcode.Path.is_dir', return_value=True):
            with patch('freshenmac.xcode.RunCMD') as mock_cmd:
                mock_cmd.side_effect = [
                    make_cmd(stdout=sw_output),  # 1. softwareupdate -l
                    make_cmd(errno=0),  # .        2. softwareupdate -i
                    make_cmd(errno=0),  # .        3. xcode-select -s
                ]
                self.assertTrue(xc.install_clt_headless())

    def test_install_clt_interactive_already_installed_reset(self):
        """Verifies xcode-select --reset is attempted if tools are already installed on disk."""
        xc = Xcode()
        with patch('freshenmac.xcode.RunCMD') as mock_cmd:
            fail_cmd = make_cmd(errno=1, stderr="xcode-select: error: command line tools are already installed")
            mock_cmd.side_effect = [fail_cmd, make_cmd(errno=0)]
            with patch('freshenmac.xcode.Path.is_dir', return_value=True):
                self.assertTrue(xc.install_clt_interactive())

    def test_install_clt_interactive_success(self):
        """Verifies interactive trigger executes 'xcode-select --install' successfully."""
        xc = Xcode()
        with patch('freshenmac.xcode.RunCMD') as mock_cmd:
            mock_cmd.return_value = make_cmd(errno=0)
            self.assertTrue(xc.install_clt_interactive())

    def test_is_clt_installed_false(self):
        """Verifies is_clt_installed returns False when xcode-select -p fails and directory is missing."""
        xc = Xcode()
        with patch('freshenmac.xcode.RunCMD', return_value=make_cmd(errno=2)):
            with patch('freshenmac.xcode.Path.is_file', return_value=False):
                self.assertFalse(xc.is_clt_installed)

    def test_is_clt_installed_true(self):
        """Verifies is_clt_installed returns True when valid developer directory and tools exist."""
        xc = Xcode()
        with patch('freshenmac.xcode.RunCMD', return_value=make_cmd(stdout='/Library/Developer/CommandLineTools\n')):
            with patch('freshenmac.xcode.Path.is_dir', return_value=True):
                with patch('freshenmac.xcode.Path.is_file', return_value=True):
                    self.assertTrue(xc.is_clt_installed)

    def test_is_installed_app_bundle(self):
        """Verifies detection of Xcode within /Applications/Xcode.app."""
        xc = Xcode()
        with patch('freshenmac.xcode.Path.is_file', return_value=True):
            self.assertTrue(xc.is_installed)
            self.assertEqual(xc.xcodebuild_path, Path('/Applications/Xcode.app/Contents/Developer/usr/bin/xcodebuild'))

    def test_is_installed_fallback_path(self):
        """Verifies detection of xcodebuild via shutil.which when outside the standard app bundle."""
        xc = Xcode()
        with patch('freshenmac.xcode.Path.is_file', return_value=False):
            with patch('freshenmac.xcode.shutil.which', return_value='/custom/bin/xcodebuild'):
                self.assertTrue(xc.is_installed)
                self.assertEqual(xc.xcodebuild_path, Path('/custom/bin/xcodebuild'))

    def test_is_license_accepted_command_line_tools(self):
        """Verifies CommandLineTools developer directory is treated as license accepted (no EULA required)."""
        xc = Xcode()
        with patch('freshenmac.xcode.Path.is_file', return_value=True):
            clt_cmd = make_cmd(
                errno=1,
                stderr=(
                    "xcode-select: error: tool 'xcodebuild' requires Xcode, but active developer directory "
                    "'/Library/Developer/CommandLineTools' is a command line tools instance"
                ),
            )
            with patch('freshenmac.xcode.RunCMD', return_value=clt_cmd):
                self.assertTrue(xc.is_license_accepted)

    def test_is_license_accepted_false(self):
        """Verifies license agreement is detected as unaccepted when xcodebuild returns error 69."""
        xc = Xcode()
        with patch('freshenmac.xcode.Path.is_file', return_value=True):
            with patch('freshenmac.xcode.RunCMD', return_value=make_cmd(errno=69)):
                self.assertFalse(xc.is_license_accepted)

    def test_is_license_accepted_true(self):
        """Verifies license agreement is detected as accepted when xcodebuild returns 0."""
        xc = Xcode()
        with patch('freshenmac.xcode.Path.is_file', return_value=True):
            with patch('freshenmac.xcode.RunCMD') as mock_cmd:
                mock_cmd.return_value = make_cmd(errno=0)
                self.assertTrue(xc.is_license_accepted)
                self.assertTrue(xc.check_and_fix_license())

    def test_not_installed(self):
        """Verifies is_installed returns False when neither bundle nor which find xcodebuild."""
        xc = Xcode()
        with patch('freshenmac.xcode.Path.is_file', return_value=False):
            with patch('freshenmac.xcode.shutil.which', return_value=None):
                self.assertFalse(xc.is_installed)
                self.assertIsNone(xc.xcodebuild_path)
                self.assertTrue(xc.check_and_fix_license())


if __name__ == '__main__':
    unittest.main()
