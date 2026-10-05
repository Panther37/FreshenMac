import argparse
import tempfile
import unittest
from pathlib import Path
from unittest.mock import ANY, call, patch

from freshenmac.main import (
    FILE_BUILD,
    main,
    yield_arguments,
)
from freshenmac.plist import (
    check_plist,
    uninstall_plist,
    update_installed_script,
)


class TestTopLevelFunctions(unittest.TestCase):
    """Unit tests for top-level helper and setup functions in freshenmac.py."""

    def setUp(self):
        self._can_sudo_patcher = patch('freshenmac.util.RunCMD.can_sudo', return_value=True)
        self._can_sudo_patcher.start()
        self._getpass_patcher = patch('getpass.getpass', return_value='')
        self._getpass_patcher.start()

    def tearDown(self):
        self._getpass_patcher.stop()
        self._can_sudo_patcher.stop()

    def test_main_primes_sudo_when_interactive(self):
        with patch('freshenmac.main.util.is_interactive', return_value=True), \
             patch('freshenmac.main.util.RunCMD') as mock_cmd, \
             patch('freshenmac.main.perform_updates', return_value=True), \
             patch('freshenmac.main.util.SudoKeepAlive'):
            mock_cmd.can_sudo.return_value = False
            main([])
            mock_cmd.assert_called_with(['sudo', '-v'], interactive=True, timeout=120)

    def test_yield_arguments_default(self):
        with patch('sys.argv', ['freshenmac.py']):
            with patch('sys.stdin.isatty', return_value=True):
                opts = yield_arguments()
                self.assertFalse(opts.debug)
                self.assertFalse(opts.force_reboot)
                self.assertTrue(opts.reboot)
                self.assertFalse(opts.spinner)
                self.assertFalse(opts.uninstall)
                self.assertTrue(opts.update)

            with patch('sys.stdin.isatty', return_value=False):
                opts = yield_arguments()
                self.assertFalse(opts.update)

    def test_yield_arguments_flags(self):
        with patch('sys.argv', ['freshenmac.py', '-f', '-r', '-v', '-u']):
            opts = yield_arguments()
            self.assertTrue(opts.force_reboot)
            self.assertFalse(opts.reboot)
            self.assertTrue(opts.debug)
            self.assertTrue(opts.update)

        with patch('sys.argv', ['freshenmac.py', '-v']):
            opts = yield_arguments()
            self.assertTrue(opts.debug)

    def test_yield_arguments_spinner(self):
        with patch('sys.argv', ['freshenmac.py', '--spinner']):
            opts = yield_arguments()
            self.assertTrue(opts.spinner)

    def test_yield_arguments_save_prefs(self):
        with patch('sys.argv', ['freshenmac.py', '-s']):
            opts = yield_arguments()
            self.assertEqual(opts.save_prefs, '')

        with patch('sys.argv', ['freshenmac.py', '--save-prefs', 'Schedule=0 6 * * 1']):
            opts = yield_arguments()
            self.assertEqual(opts.save_prefs, 'Schedule=0 6 * * 1')

    def test_yield_arguments_no_update(self):
        with patch('sys.argv', ['freshenmac.py', '--no-update']):
            with patch('sys.stdin.isatty', return_value=True):
                opts = yield_arguments()
                self.assertFalse(opts.update)

    def test_yield_arguments_schedule(self):
        with patch('sys.argv', ['freshenmac.py', '--schedule', '0 6 * * 1']):
            opts = yield_arguments()
            self.assertEqual(opts.schedule, '0 6 * * 1')

        with patch('sys.argv', ['freshenmac.py', '--cron', '30 7 * * 2']):
            opts = yield_arguments()
            self.assertEqual(opts.schedule, '30 7 * * 2')

    def test_yield_arguments_ignore_mas(self):
        with patch('sys.argv', ['freshenmac.py', '--ignore-mas', 'Xcode', '497799835']):
            opts = yield_arguments()
            self.assertEqual(opts.ignore_mas, ['Xcode', '497799835'])

        with patch('sys.argv', ['freshenmac.py', '--skip-mas', '1Password']):
            opts = yield_arguments()
            self.assertEqual(opts.ignore_mas, ['1Password'])

        with patch('sys.argv', ['freshenmac.py', '-i', 'Slack']):
            opts = yield_arguments()
            self.assertEqual(opts.ignore_mas, ['Slack'])

    def test_check_plist_dry_run(self):
        with patch('freshenmac.plist.LaunchAgent.check_and_install', return_value=True) as mock_install:
            result = check_plist(dry_run=True)
            self.assertTrue(result)
            mock_install.assert_called_once()

    def test_update_installed_script_lower_build(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fake_target = Path(temp_dir) / 'installed_freshenmac.py'
            fake_target.write_text("FILE_BUILD = 0\n", encoding='utf-8')

            result = update_installed_script(a_script=str(fake_target))
            self.assertTrue(result)
            self.assertIn(f"FILE_BUILD = {FILE_BUILD}", fake_target.read_text(encoding='utf-8'))

    def test_update_installed_script_already_current(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fake_target = Path(temp_dir) / 'installed_freshenmac.py'
            fake_target.write_text(f"FILE_BUILD = {FILE_BUILD + 1}\n", encoding='utf-8')

            result = update_installed_script(a_script=str(fake_target))
            self.assertTrue(result)
            self.assertIn(f"FILE_BUILD = {FILE_BUILD + 1}", fake_target.read_text(encoding='utf-8'))

    def test_uninstall_plist(self):
        with patch('freshenmac.util.RunCMD') as mock_util_cmd, \
             patch('freshenmac.plist.RunCMD') as mock_cmd:
            mock_cmd.return_value.returncode = 0
            mock_util_cmd.return_value.returncode = 0
            with patch('freshenmac.plist.Path.is_file', return_value=False):
                with patch('freshenmac.plist.Path.is_dir', return_value=False):
                    with patch('freshenmac.plist.StartupRun.cleanup'):
                        result = uninstall_plist()
                        self.assertTrue(result)

    def test_main_forwards_cli_flags_to_macos(self):
        with patch('freshenmac.computer.MacOS') as mock_mac_cls:
            mock_mac = mock_mac_cls.return_value
            with patch('freshenmac.plist.check_plist'):
                with patch('freshenmac.plist.update_installed_script'):
                    with patch('sys.argv', ['freshenmac.py', '-f', '-l', '500']):
                        res = main()
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
        with patch('freshenmac.computer.MacOS') as mock_mac_cls:
            mock_mac = mock_mac_cls.return_value
            with patch('freshenmac.plist.check_plist'):
                with patch('freshenmac.plist.update_installed_script'):
                    with patch('sys.argv', ['freshenmac.py', '--ignore-mas', 'Xcode', '497799835']):
                        res = main()
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

    def test_main_runs_update_and_continues(self):
        with patch('freshenmac.plist.update_installed_script') as mock_update_script:
            with patch('freshenmac.computer.MacOS') as mock_mac_cls:
                mock_mac = mock_mac_cls.return_value
                with patch('freshenmac.plist.check_plist'):
                    with patch('sys.argv', ['freshenmac.py']):
                        with patch('sys.stdin.isatty', return_value=True):
                            res = main()
                            self.assertTrue(res)
                            mock_update_script.assert_once_with(debug=False, debug_limit=0) if hasattr(mock_update_script, 'assert_once_with') else mock_update_script.assert_called_once_with(debug=False, debug_limit=0)
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

    def test_main_without_update(self):
        with patch('freshenmac.plist.update_installed_script') as mock_update_script:
            with patch('freshenmac.computer.MacOS') as mock_mac_cls:
                mock_mac = mock_mac_cls.return_value
                with patch('freshenmac.plist.check_plist'):
                    with patch('sys.argv', ['freshenmac.py', '--no-update']):
                        res = main()
                        self.assertTrue(res)
                        mock_update_script.assert_not_called()
                        mock_mac.update.assert_called_once_with('all')

    def test_main_spinner(self):
        with patch('freshenmac.main.util.Wave') as mock_wave_cls:
            mock_wave = mock_wave_cls.return_value
            mock_wave.__enter__.return_value = mock_wave
            with patch('freshenmac.main.sleep', side_effect=KeyboardInterrupt):
                with patch('freshenmac.plist.update_installed_script') as mock_update_script:
                    with patch('freshenmac.computer.MacOS') as mock_mac_cls:
                        with patch('sys.argv', ['freshenmac.py', '--spinner']):
                            res = main()
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

    def test_main_schedule(self):
        with patch('freshenmac.plist.SavePreferences.save', return_value=True) as mock_save:
            with patch('sys.argv', ['freshenmac.py', '--schedule', '0 7 * * 1']):
                self.assertTrue(main())
                mock_save.assert_called_once()

    def test_main_schedule_invalid(self):
        with patch('sys.argv', ['freshenmac.py', '--schedule', 'invalid cron']):
            self.assertFalse(main())

    def test_main_with_some_options_namespace(self):
        with patch('freshenmac.main.spinner_test', return_value=True) as mock_spinner:
            res = main(some_options=argparse.Namespace(spinner=True))
            self.assertTrue(res)
            mock_spinner.assert_called_once()

    def test_main_with_some_options_list(self):
        with patch('freshenmac.main.spinner_test', return_value=True) as mock_spinner:
            res = main(some_options=['--spinner'])
            self.assertTrue(res)
            mock_spinner.assert_called_once()


if __name__ == '__main__':
    unittest.main()
