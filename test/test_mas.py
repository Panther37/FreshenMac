import unittest
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

from freshenmac.boot import RebootState
from freshenmac.mas import AppStore
from freshenmac.util import RunCMD


def make_cmd(stdout: str = "", stderr: str = "", errno: int = 0) -> RunCMD:
    """Creates a mock RunCMD result without executing a subprocess."""
    cmd = RunCMD.__new__(RunCMD)
    cmd.stdout = stdout
    cmd.stderr = stderr
    cmd.returncode = errno
    cmd.debug = False
    return cmd


class TestAppStore(unittest.TestCase):
    """
    Unit tests for the AppStore class in freshenmac.mas.

    Validates mas CLI availability and installation via Homebrew, resolution of the mas binary path,
    filtering and normalization of ignored apps (no-mas preference), detection of running GUI apps
    to avoid terminating user sessions, and triggering Xcode license fixes upon Xcode upgrades.
    """

    def setUp(self):
        """Initializes a dry-run AppStore instance with a clean RebootState."""
        self.reboot = RebootState()
        self.app_store = AppStore(dry_run=True, reboot=self.reboot)

    def test_has_mas_dry_run_missing(self):
        """Verifies has_mas returns False in dry-run mode when binary is missing, without attempting brew install."""
        self.app_store.__dict__['mas_path'] = Path('')
        with patch('freshenmac.mas.RunCMD') as mock_cmd:
            self.assertFalse(self.app_store.has_mas)
            mock_cmd.assert_not_called()

    def test_has_mas_installs_via_brew(self):
        """Verifies missing mas binary triggers 'brew install mas' in active run mode."""
        app_store = AppStore(brew_path=Path('/opt/homebrew/bin/brew'), dry_run=False)
        app_store.__dict__['mas_path'] = Path('')
        with patch('freshenmac.mas.RunCMD') as mock_cmd:
            mock_cmd.return_value = make_cmd(errno=0)
            mock_cmd.return_value.__bool__ = lambda self: True
            with patch('freshenmac.mas.shutil.which', return_value='/opt/homebrew/bin/mas'):
                self.assertTrue(app_store.has_mas)
                mock_cmd.assert_called_once_with(
                    ['/opt/homebrew/bin/brew', 'install', 'mas'],
                    cancelable=True,
                    debug=0,
                    debug_limit=0,
                    taskpolicy='utility',
                    timeout=1800,
                )

    def test_ignored_apps_merging_and_normalization(self):
        """Verifies merging of CLI ignored apps with no-mas preference values and case normalization."""
        # 1. Merging CLI arguments with list of preferences
        app_store = AppStore(ignore_mas=['Slack', ' 497799835 ', ''])
        with patch('freshenmac.plist.load_preferences', return_value={'no-mas': ['iPhoto', '408981381', ' ']}):
            ignored = app_store.ignored_apps
            self.assertEqual(ignored, {'slack', '497799835', 'iphoto', '408981381'})

        # 2. Single integer preference value normalization
        app_store_single = AppStore()
        with patch('freshenmac.plist.load_preferences', return_value={'no-mas': 12345}):
            self.assertEqual(app_store_single.ignored_apps, {'12345'})

    def test_init_defaults(self):
        """Verifies default values upon initialization."""
        self.assertTrue(self.app_store.dry_run)
        self.assertEqual(self.app_store.timeout, 1800)
        self.assertFalse(self.app_store.updates_performed)
        self.assertEqual(self.app_store.summary, {'installed': {}, 'unchanged': {}, 'updated': {}})
        self.assertEqual(self.app_store.reboot.required, 0)

    def test_mas_path_candidates(self):
        """Verifies resolution of mas binary relative to brew_path."""
        app_store = AppStore(brew_path=Path('/custom/bin/brew'))
        with patch.object(Path, 'is_file', return_value=True):
            self.assertEqual(app_store.mas_path, Path('/custom/bin/mas'))

    def test_update_all_up_to_date(self):
        """Verifies empty outdated list logs 'All App Store apps are up to date'."""
        app_store = AppStore(dry_run=False)
        with patch.object(AppStore, 'has_mas', new_callable=PropertyMock, return_value=True), \
            patch.object(AppStore, 'mas_path', new_callable=PropertyMock, return_value=Path('/usr/local/bin/mas')), \
            patch('freshenmac.mas.RunCMD', return_value=make_cmd(stdout='')):
            with patch.object(app_store.log, 'print_store') as mock_log:
                app_store.update()
                mock_log.assert_any_call("All App Store apps are up to date.", 'App Store')

    def test_update_dry_run(self):
        """Verifies update in dry-run mode logs intent without executing upgrade commands."""
        with patch.object(AppStore, 'has_mas', new_callable=PropertyMock, return_value=True), \
            patch.object(self.app_store.log, 'print_store') as mock_log, \
            patch('freshenmac.mas.RunCMD') as mock_cmd:
            self.app_store.update()
            mock_log.assert_any_call("[Dry-run] Would check and update App Store apps.", 'App Store')
            mock_cmd.assert_not_called()

    def test_update_skips_ignored_app(self):
        """Verifies apps specified in ignored list are skipped and recorded in unchanged summary."""
        app_store = AppStore(
            dry_run=False,
            ignore_mas=['497799835'],  # Ignore Xcode ID
            reboot=self.reboot,
        )
        outdated_out = (
            "497799835 Xcode (15.0 -> 15.1)\n"
            "634148309 Logic Pro (12.3 -> 12.3.1)"
        )
        upgraded_ids = []

        def fake_runcmd(cmd, *args, **kwargs):
            if 'outdated' in cmd:
                return make_cmd(stdout=outdated_out)
            if 'upgrade' in cmd:
                upgraded_ids.append(cmd[2])
                return make_cmd(stdout="Upgraded 634148309 Logic Pro")
            return make_cmd(errno=0)

        with patch.object(AppStore, 'has_mas', new_callable=PropertyMock, return_value=True), \
            patch.object(AppStore, 'mas_path', new_callable=PropertyMock, return_value=Path('/usr/local/bin/mas')), \
            patch('freshenmac.mas.RunCMD', side_effect=fake_runcmd):
            app_store.update()

            # Verify ignored app was skipped, non-ignored was upgraded
            self.assertNotIn('497799835', upgraded_ids)
            self.assertIn('634148309', upgraded_ids)
            self.assertEqual(app_store.summary['unchanged']['Xcode'], '15.0 ═▷ 15.1')
            self.assertEqual(app_store.summary['updated']['Logic Pro'], '12.3 ═▷ 12.3.1')
            self.assertTrue(app_store.updates_performed)

    def test_update_skips_running_gui_app_and_schedules(self):
        """Verifies actively running GUI app update is deferred to next startup to avoid killing active work."""
        scheduled = False

        def fake_schedule():
            nonlocal scheduled
            scheduled = True
            return True

        app_store = AppStore(
            dry_run=False,
            is_app_running_fn=lambda name: name == 'Logic Pro',
            reboot=self.reboot,
            schedule_fn=fake_schedule,
        )
        outdated_out = "634148309 Logic Pro (12.3 -> 12.3.1)"

        def fake_runcmd(cmd, *args, **kwargs):
            if 'outdated' in cmd:
                return make_cmd(stdout=outdated_out)
            return make_cmd(errno=0)

        with patch.object(AppStore, 'has_mas', new_callable=PropertyMock, return_value=True), \
            patch.object(AppStore, 'mas_path', new_callable=PropertyMock, return_value=Path('/usr/local/bin/mas')), \
            patch('freshenmac.mas.RunCMD', side_effect=fake_runcmd) as mock_cmd:
            app_store.update()

            # Must mark reboot required, schedule one-shot startup agent, and not invoke upgrade
            self.assertEqual(self.reboot.required, 1)
            self.assertTrue(scheduled)
            self.assertEqual(app_store.summary['unchanged']['Logic Pro'], '12.3 ═▷ 12.3.1')
            for call_args in mock_cmd.call_args_list:
                args = call_args[0][0]
                self.assertNotIn('upgrade', args)

    def test_update_triggers_xcode_license_fix(self):
        """Verifies upgrading Xcode triggers an automated license agreement acceptance check."""
        mock_xcode = MagicMock()
        app_store = AppStore(dry_run=False, xcode=mock_xcode)
        outdated_out = "497799835 Xcode (15.0 -> 15.1)"

        def fake_runcmd(cmd, *args, **kwargs):
            if 'outdated' in cmd:
                return make_cmd(stdout=outdated_out)
            if 'upgrade' in cmd:
                return make_cmd(stdout="Upgraded Xcode")
            return make_cmd(errno=0)

        with patch.object(AppStore, 'has_mas', new_callable=PropertyMock, return_value=True), \
            patch.object(AppStore, 'mas_path', new_callable=PropertyMock, return_value=Path('/usr/local/bin/mas')), \
            patch('freshenmac.mas.RunCMD', side_effect=fake_runcmd):
            app_store.update()
            mock_xcode.check_and_fix_license.assert_called_once()


if __name__ == '__main__':
    unittest.main()
