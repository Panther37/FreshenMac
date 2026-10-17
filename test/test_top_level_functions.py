import argparse
import io
import unittest
from unittest.mock import ANY, call, patch

from freshenmac.config import APP_INFO
from freshenmac.main import (
    actions,
    main,
    yield_arguments,
)


class TestTopLevelFunctions(unittest.TestCase):
    """
    Unit tests for top-level entry point, CLI argument parsing, and system setup functions.

    Tests main() execution paths, sudo credential priming, argument parsing flags,
    LaunchAgent installation/uninstallation, and self-updating script builds.
    """

    def setUp(self):
        """Install global patches for sudo and password prompts during testing."""
        self._can_sudo_patcher = patch('freshenmac.util.RunCMD.can_sudo', return_value=True)
        self._can_sudo_patcher.start()
        self._getpass_patcher = patch('getpass.getpass', return_value='')
        self._getpass_patcher.start()

    def tearDown(self):
        """Clean up active test patches."""
        self._getpass_patcher.stop()
        self._can_sudo_patcher.stop()

    def test_actions(self):
        """Verifies actions handles Install, Update, Uninstall, and unknown verbs."""
        # Install
        with patch('freshenmac.plist.LaunchAgent.check_and_install', return_value=True) as mock_agent, \
            patch('freshenmac.plist.PackageSync.update_files', return_value=True) as mock_sync:
            self.assertTrue(actions(argparse.Namespace(action='Install', debug=0, debug_limit=0)))
            mock_agent.assert_called_once()
            mock_sync.assert_called_once()

        # Update
        with patch('freshenmac.plist.PackageSync.update_files', return_value=True) as mock_sync:
            self.assertTrue(actions(argparse.Namespace(action='Update', debug=0, debug_limit=0)))
            mock_sync.assert_called_once()

        # Uninstall
        with patch('freshenmac.plist.LaunchAgent.uninstall', return_value=True) as mock_uninstall:
            self.assertTrue(actions(argparse.Namespace(action='Uninstall', debug=0, debug_limit=0)))
            mock_uninstall.assert_called_once()

        # Unknown action
        self.assertFalse(actions(argparse.Namespace(action='NonExistent', dry_run=True)))

    def test_main_action(self):
        """Verifies main delegates directly to actions() when --action is provided."""
        # Arrange
        with patch('freshenmac.main.actions', return_value=True) as mock_actions, \
            patch('sys.argv', ['freshenmac.py', '--action', 'Install']):

            # Act
            res = main()

            # Assert
            self.assertTrue(res)
            mock_actions.assert_called_once()

    def test_main_forwards_cli_flags_to_macos(self):
        """Verifies main forwards force_reboot and debug options to MacOS constructor."""
        # Arrange
        with patch('freshenmac.computer.MacOS') as mock_mac_cls, \
            patch('freshenmac.plist.check_plist'), \
            patch('freshenmac.plist.update_installed_script'), \
            patch('sys.argv', ['freshenmac.py', '-f', '-l', '500']):
            mock_mac = mock_mac_cls.return_value

            # Act
            res = main()

            # Assert: CLI flags properly passed to MacOS instance
            self.assertTrue(res)
            mock_mac_cls.assert_called_once_with(
                console_user=ANY,
                force_reboot=True,
                ignore_mas=None,
                logger=ANY,
                user_password=ANY,
                debug=2,
                debug_limit=500,
            )
            mock_mac.update.assert_called_once_with('all')

    def test_main_forwards_ignore_mas_to_macos(self):
        """Verifies main forwards --ignore-mas list to MacOS constructor."""
        # Arrange
        with patch('freshenmac.computer.MacOS') as mock_mac_cls, \
            patch('freshenmac.plist.check_plist'), \
            patch('freshenmac.plist.update_installed_script'), \
            patch('sys.argv', ['freshenmac.py', '--ignore-mas', 'Xcode', '497799835']):
            mock_mac = mock_mac_cls.return_value

            # Act
            res = main()

            # Assert: ignored apps list passed through
            self.assertTrue(res)
            mock_mac_cls.assert_called_once_with(
                console_user=ANY,
                force_reboot=False,
                ignore_mas=['Xcode', '497799835'],
                logger=ANY,
                user_password=ANY,
                debug=0,
                debug_limit=0,
            )
            mock_mac.update.assert_called_once_with('all')

    def test_main_primes_sudo_when_interactive(self):
        """Verifies main prompts for sudo -v when running in an interactive terminal session."""
        # Arrange
        with patch('freshenmac.main.util.is_interactive', return_value=True), \
            patch('freshenmac.main.util.RunCMD') as mock_cmd, \
            patch('freshenmac.main.perform_updates', return_value=True), \
            patch('freshenmac.main.util.SudoKeepAlive'):
            mock_cmd.can_sudo.return_value = False

            # Act
            main([])

            # Assert: primed credentials with 120s timeout
            mock_cmd.assert_called_with(['sudo', '-v'], interactive=True, timeout=120)

    def test_main_runs_update_and_continues(self):
        """Verifies main checks for self-updates and proceeds to perform system updates."""
        # Arrange
        with patch('freshenmac.plist.update_installed_script') as mock_update_script, \
            patch('freshenmac.computer.MacOS') as mock_mac_cls, \
            patch('freshenmac.plist.check_plist'), \
            patch('sys.argv', ['freshenmac.py']), \
            patch('sys.stdin.isatty', return_value=True):
            mock_mac = mock_mac_cls.return_value

            # Act
            res = main()

            # Assert
            self.assertTrue(res)
            mock_update_script.assert_called_once_with(debug=False, debug_limit=0)
            mock_mac_cls.assert_called_once_with(
                console_user=ANY,
                force_reboot=False,
                ignore_mas=None,
                logger=ANY,
                user_password=ANY,
                debug=0,
                debug_limit=0,
            )
            mock_mac.update.assert_called_once_with('all')

    def test_main_schedule(self):
        """Verifies main saves LaunchAgent schedule when valid cron expression is passed."""
        # Arrange & Act
        with patch('freshenmac.plist.SavePreferences.save', return_value=True) as mock_save, \
            patch('sys.argv', ['freshenmac.py', '--schedule', '0 7 * * 1']):
            res = main()

            # Assert
            self.assertTrue(res)
            mock_save.assert_called_once()

    def test_main_schedule_invalid(self):
        """Verifies main rejects invalid cron expressions and exits with False."""
        # Arrange & Act
        with patch('sys.argv', ['freshenmac.py', '--schedule', 'invalid cron']):
            res = main()

            # Assert
            self.assertFalse(res)

    def test_main_spinner(self):
        """Verifies --spinner CLI flag launches demo Wave animations."""
        # Arrange
        with patch('freshenmac.main.util.Wave') as mock_wave_cls, \
            patch('freshenmac.main.sleep', side_effect=KeyboardInterrupt), \
            patch('freshenmac.plist.update_installed_script') as mock_update_script, \
            patch('freshenmac.computer.MacOS') as mock_mac_cls, \
            patch('sys.argv', ['freshenmac.py', '--spinner']):
            mock_wave = mock_wave_cls.return_value
            mock_wave.__enter__.return_value = mock_wave

            # Act
            res = main()

            # Assert: ran spinner test and bypassed standard update workflow
            self.assertTrue(res)
            self.assertEqual(mock_wave_cls.call_count, 2)
            self.assertEqual(
                mock_wave_cls.call_args_list,
                [
                    call('Testing spinner wave animation'),
                    call(
                        'Testing spinner wave animation with arguments',
                        direction='right',
                        force=True,
                        persist=True,
                    ),
                ],
            )
            mock_update_script.assert_not_called()
            mock_mac_cls.assert_not_called()

    def test_main_with_some_options_list(self):
        """Verifies main accepts explicit list of argument strings via some_options."""
        # Arrange & Act
        with patch('freshenmac.main.spinner_test', return_value=True) as mock_spinner:
            res = main(some_options=['--spinner'])

            # Assert
            self.assertTrue(res)
            mock_spinner.assert_called_once()

    def test_main_with_some_options_namespace(self):
        """Verifies main accepts pre-parsed argparse.Namespace via some_options."""
        # Arrange & Act
        with patch('freshenmac.main.spinner_test', return_value=True) as mock_spinner:
            res = main(some_options=argparse.Namespace(spinner=True))

            # Assert
            self.assertTrue(res)
            mock_spinner.assert_called_once()

    def test_main_without_update(self):
        """Verifies --no-update skips self-updating check but proceeds with system updates."""
        # Arrange
        with patch('freshenmac.plist.update_installed_script') as mock_update_script, \
            patch('freshenmac.computer.MacOS') as mock_mac_cls, \
            patch('freshenmac.plist.check_plist'), \
            patch('sys.argv', ['freshenmac.py', '--no-update']):
            mock_mac = mock_mac_cls.return_value

            # Act
            res = main()

            # Assert
            self.assertTrue(res)
            mock_update_script.assert_not_called()
            mock_mac.update.assert_called_once_with('all')

    def test_yield_arguments_action(self):
        """Verifies yield_arguments parses -a and --action flags and normalizes to title case."""
        with patch('sys.argv', ['freshenmac.py', '-a', 'install']):
            opts = yield_arguments()
            self.assertEqual(opts.action, 'Install')

        with patch('sys.argv', ['freshenmac.py', '--action', 'uninstall']):
            opts = yield_arguments()
            self.assertEqual(opts.action, 'Uninstall')

        with patch('sys.argv', ['freshenmac.py', '--action', 'UPDATE']):
            opts = yield_arguments()
            self.assertEqual(opts.action, 'Update')

    def test_yield_arguments_default(self):
        """Verifies yield_arguments parses default options in interactive and non-interactive sessions."""
        # Interactive session (TTY attached)
        with patch('sys.argv', ['freshenmac.py']), \
            patch('sys.stdin.isatty', return_value=True):
            opts = yield_arguments()
            self.assertIsNone(opts.action)
            self.assertFalse(opts.debug)
            self.assertFalse(opts.force_reboot)
            self.assertTrue(opts.reboot)
            self.assertFalse(opts.spinner)
            self.assertTrue(opts.update)

        # Non-interactive session (automated daemon execution defaults update=False)
        with patch('sys.argv', ['freshenmac.py']), \
            patch('sys.stdin.isatty', return_value=False):
            opts = yield_arguments()
            self.assertFalse(opts.update)

    def test_yield_arguments_flags(self):
        """Verifies yield_arguments parses short option flags (-f, -r, -v, -u)."""
        with patch('sys.argv', ['freshenmac.py', '-f', '-r', '-v', '-u']):
            opts = yield_arguments()
            self.assertTrue(opts.force_reboot)
            self.assertFalse(opts.reboot)
            self.assertTrue(opts.debug)
            self.assertTrue(opts.update)

        with patch('sys.argv', ['freshenmac.py', '-v']):
            opts = yield_arguments()
            self.assertTrue(opts.debug)

    def test_yield_arguments_ignore_mas(self):
        """Verifies yield_arguments parses --ignore-mas, --skip-mas, and -i aliases."""
        with patch('sys.argv', ['freshenmac.py', '--ignore-mas', 'Xcode', '497799835']):
            opts = yield_arguments()
            self.assertEqual(opts.ignore_mas, ['Xcode', '497799835'])

        with patch('sys.argv', ['freshenmac.py', '--skip-mas', '1Password']):
            opts = yield_arguments()
            self.assertEqual(opts.ignore_mas, ['1Password'])

        with patch('sys.argv', ['freshenmac.py', '-i', 'Slack']):
            opts = yield_arguments()
            self.assertEqual(opts.ignore_mas, ['Slack'])

        with patch('sys.argv', ['freshenmac.py', '--skip-app', 'Telegram']):
            opts = yield_arguments()
            self.assertEqual(opts.ignore_mas, ['Telegram'])

    def test_yield_arguments_no_update(self):
        """Verifies yield_arguments handles --no-update flag correctly."""
        with patch('sys.argv', ['freshenmac.py', '--no-update']), \
            patch('sys.stdin.isatty', return_value=True):
            opts = yield_arguments()
            self.assertFalse(opts.update)

    def test_yield_arguments_save_prefs(self):
        """Verifies yield_arguments parses -s and --save-prefs options."""
        with patch('sys.argv', ['freshenmac.py', '-s']):
            opts = yield_arguments()
            self.assertEqual(opts.save_prefs, '')

        with patch('sys.argv', ['freshenmac.py', '--save-prefs', 'Schedule=0 6 * * 1']):
            opts = yield_arguments()
            self.assertEqual(opts.save_prefs, 'Schedule=0 6 * * 1')

    def test_yield_arguments_schedule(self):
        """Verifies yield_arguments parses --schedule and --cron aliases."""
        with patch('sys.argv', ['freshenmac.py', '--schedule', '0 6 * * 1']):
            opts = yield_arguments()
            self.assertEqual(opts.schedule, '0 6 * * 1')

        with patch('sys.argv', ['freshenmac.py', '--cron', '30 7 * * 2']):
            opts = yield_arguments()
            self.assertEqual(opts.schedule, '30 7 * * 2')

    def test_yield_arguments_spinner(self):
        """Verifies yield_arguments parses --spinner flag."""
        with patch('sys.argv', ['freshenmac.py', '--spinner']):
            opts = yield_arguments()
            self.assertTrue(opts.spinner)

    def test_yield_arguments_trace(self):
        """Verifies yield_arguments parses --trace setting debug=9 and debug_limit=-1."""
        with patch('sys.argv', ['freshenmac.py', '--trace']):
            opts = yield_arguments()
            self.assertTrue(opts.trace)
            self.assertEqual(opts.debug, 9)
            self.assertEqual(opts.debug_limit, -1)

    def test_yield_arguments_version(self):
        """Verifies yield_arguments parses --version flag and prints version."""
        with patch('sys.argv', ['freshenmac.py', '--version']), \
            patch('sys.stdout', new_callable=io.StringIO) as mock_stdout:
            with self.assertRaises(SystemExit) as cm:
                yield_arguments()
            self.assertEqual(cm.exception.code, 0)
            self.assertIn(f"{APP_INFO['Name']} {APP_INFO['Version']}", mock_stdout.getvalue())


if __name__ == '__main__':
    unittest.main()
