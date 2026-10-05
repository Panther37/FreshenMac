import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

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
    """Unit tests for the standalone Xcode management class."""

    def test_init_defaults(self):
        xc = Xcode()
        self.assertFalse(xc.debug)
        self.assertFalse(xc.dry_run)

        xc_custom = Xcode(debug=True, dry_run=True)
        self.assertTrue(xc_custom.debug)
        self.assertTrue(xc_custom.dry_run)

    def test_is_installed_app_bundle(self):
        xc = Xcode()
        with patch('freshenmac.xcode.Path.is_file', return_value=True):
            self.assertTrue(xc.is_installed)
            self.assertEqual(xc.xcodebuild_path, Path('/Applications/Xcode.app/Contents/Developer/usr/bin/xcodebuild'))

    def test_is_installed_fallback_path(self):
        xc = Xcode()
        with patch('freshenmac.xcode.Path.is_file', return_value=False):
            with patch('freshenmac.xcode.shutil.which', return_value='/custom/bin/xcodebuild'):
                self.assertTrue(xc.is_installed)
                self.assertEqual(xc.xcodebuild_path, Path('/custom/bin/xcodebuild'))

    def test_not_installed(self):
        xc = Xcode()
        with patch('freshenmac.xcode.Path.is_file', return_value=False):
            with patch('freshenmac.xcode.shutil.which', return_value=None):
                self.assertFalse(xc.is_installed)
                self.assertIsNone(xc.xcodebuild_path)
                self.assertTrue(xc.check_and_fix_license())

    def test_is_license_accepted_true(self):
        xc = Xcode()
        with patch('freshenmac.xcode.Path.is_file', return_value=True):
            with patch('freshenmac.xcode.RunCMD') as mock_cmd:
                mock_cmd.return_value.returncode = 0
                self.assertTrue(xc.is_license_accepted)
                self.assertTrue(xc.check_and_fix_license())

    def test_is_license_accepted_false(self):
        xc = Xcode()
        with patch('freshenmac.xcode.Path.is_file', return_value=True):
            with patch('freshenmac.xcode.RunCMD', return_value=make_cmd(errno=69)):
                self.assertFalse(xc.is_license_accepted)

    def test_is_license_accepted_command_line_tools(self):
        xc = Xcode()
        with patch('freshenmac.xcode.Path.is_file', return_value=True):
            clt_cmd = make_cmd(
                errno=1,
                stderr="xcode-select: error: tool 'xcodebuild' requires Xcode, but active developer directory '/Library/Developer/CommandLineTools' is a command line tools instance",
            )
            with patch('freshenmac.xcode.RunCMD', return_value=clt_cmd):
                self.assertTrue(xc.is_license_accepted)

    def test_accept_license_dry_run(self):
        xc = Xcode(dry_run=True)
        with patch('freshenmac.xcode.Path.is_file', return_value=True):
            with patch('freshenmac.xcode.RunCMD', return_value=make_cmd(errno=69)):
                self.assertFalse(xc.accept_license())
                self.assertFalse(xc.check_and_fix_license())

    def test_accept_license_interactive_success(self):
        xc = Xcode(dry_run=False)
        with patch('freshenmac.xcode.Path.is_file', return_value=True):
            with patch('freshenmac.xcode.is_interactive', return_value=True):
                with patch('freshenmac.xcode.RunCMD') as mock_cmd:
                    mock_cmd.return_value.returncode = 0
                    self.assertTrue(xc.accept_license())

    def test_accept_license_cached_sudo_success(self):
        xc = Xcode(dry_run=False)
        with patch('freshenmac.xcode.Path.is_file', return_value=True):
            with patch('freshenmac.xcode.is_interactive', return_value=False):
                with patch('freshenmac.xcode.RunCMD') as mock_cmd:
                    accept_cmd = MagicMock(errno=0)
                    mock_cmd.return_value = accept_cmd
                    self.assertTrue(xc.accept_license())

    def test_accept_license_no_sudo(self):
        xc = Xcode(dry_run=False)
        with patch('freshenmac.xcode.Path.is_file', return_value=True):
            with patch('freshenmac.xcode.is_interactive', return_value=False):
                with patch('freshenmac.xcode.RunCMD') as mock_cmd:
                    fail_cmd = MagicMock(errno=1)
                    fail_cmd.__bool__.return_value = False
                    mock_cmd.return_value = fail_cmd
                    self.assertFalse(xc.accept_license())


if __name__ == '__main__':
    unittest.main()
