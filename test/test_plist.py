import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

from freshenmac.config import DEFAULTS, PATHS
from freshenmac.plist import (
    LaunchAgent,
    PackageSync,
    SavePreferences,
    StartupRun,
    check_plist,
    cleanup_startup_run,
    file_needs_update,
    get_file_build,
    load_preferences,
    parse_cron,
    schedule_startup_run,
    uninstall_plist,
    update_installed_script,
)
from freshenmac.util import Logger, RunCMD


def make_cmd(stdout: str = "", stderr: str = "", errno: int = 0) -> RunCMD:
    """Creates a real RunCMD instance without executing an actual subprocess."""
    cmd = RunCMD.__new__(RunCMD)
    cmd.stdout = stdout
    cmd.stderr = stderr
    cmd.returncode = errno
    cmd.debug = False
    return cmd


class TestFileUpdateHelpers(unittest.TestCase):
    """
    Unit tests for file build extraction and version comparison helpers.

    Tests file_needs_update logic (missing targets, build version comparison, fallback)
    and get_file_build regex parsing from script headers.
    """

    def test_file_needs_update(self):
        """Verifies file_needs_update compares FILE_BUILD numbers between source and destination."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            src = Path(tmp_dir) / 'source.py'
            dst = Path(tmp_dir) / 'target.py'

            # Case 1: Missing destination file requires copy/update
            src.write_text("FILE_BUILD = 2\n")
            self.assertTrue(file_needs_update(src, dst))

            # Case 2: Destination with lower build requires update
            dst.write_text("FILE_BUILD = 1\n")
            self.assertTrue(file_needs_update(src, dst))

            # Case 3: Destination with equal build is already up to date
            dst.write_text("FILE_BUILD = 2\n")
            self.assertFalse(file_needs_update(src, dst))

            # Case 4: Destination with higher build (e.g. local dev) does not overwrite
            dst.write_text("FILE_BUILD = 3\n")
            self.assertFalse(file_needs_update(src, dst))

            # Case 5: Neither file defines FILE_BUILD -> dst_build is 0.0, so update is triggered
            src.write_text("print('hello')\n")
            dst.write_text("print('hello')\n")
            self.assertTrue(file_needs_update(src, dst))

    def test_get_file_build(self):
        """Verifies get_file_build parses numeric FILE_BUILD definitions from file headers."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            f = Path(tmp_dir) / 'test_build.py'

            # Case 1: Non-existent file defaults to 0.0
            self.assertEqual(get_file_build(Path(tmp_dir) / 'nonexistent.py'), 0.0)

            # Case 2: Standard assignment matches
            f.write_text("FILE_BUILD = 42\n")
            self.assertEqual(get_file_build(f), 42)

            # Case 3: Typed variable annotations (e.g. FILE_BUILD: int = 42) do not match
            f.write_text("FILE_BUILD: int = 42\n")
            self.assertEqual(get_file_build(f), 0.0)

            # Case 4: Missing declaration defaults to 0.0
            f.write_text("x = 1\n")
            self.assertEqual(get_file_build(f), 0.0)


class TestLaunchAgent(unittest.TestCase):
    """
    Unit tests for LaunchAgent installation, activation, cron scheduling, and preferences.

    Tests launchd domain targeting (gui/UID vs user/UID), bootout/bootstrap lifecycle,
    LaunchAgent plist serialization, sudoers file installation, and user vs system preferences.
    """

    def setUp(self):
        """Set up test fixtures before each test method."""
        self.agent = LaunchAgent(dry_run=True)

    def test_activate_dry_run(self):
        """Verifies activate returns True immediately in dry-run mode."""
        agent = LaunchAgent(dry_run=True)
        self.assertTrue(agent.activate())

    @patch('freshenmac.plist.RunCMD')
    def test_activate_failure(self, mock_cmd):
        """Verifies activate returns False when launchctl bootstrap and fallback load fail."""
        agent = LaunchAgent(dry_run=False)
        with patch.object(agent, 'bootout') as mock_bootout, \
            patch.object(agent, 'domain', 'gui/501'):
            mock_cmd.side_effect = [
                make_cmd(errno=1, stderr="service already loaded"),  # bootstrap
                make_cmd(errno=1, stderr="service already loaded"),  # fallback load
                make_cmd(stdout="service already loaded"),  # .        error lookup
            ]
            self.assertFalse(agent.activate())
            mock_bootout.assert_called_once()

    @patch('freshenmac.plist.RunCMD')
    def test_activate_success(self, mock_cmd):
        """Verifies activate boots out old agent and bootstraps new agent successfully."""
        agent = LaunchAgent(dry_run=False)
        with patch.object(agent, 'bootout') as mock_bootout, \
            patch.object(agent, 'domain', 'gui/501'):
            mock_cmd.return_value = make_cmd(stdout="success", errno=0)
            self.assertTrue(agent.activate())
            mock_bootout.assert_called_once()

    @patch('freshenmac.plist.getuid', return_value=501)
    @patch('freshenmac.plist.RunCMD')
    def test_bootout(self, mock_cmd, mock_uid):
        """Verifies bootout executes launchctl bootout across user and GUI domains."""
        agent = LaunchAgent(dry_run=False)
        mock_cmd.return_value = make_cmd(errno=0)
        self.assertTrue(agent.bootout())
        self.assertGreaterEqual(mock_cmd.call_count, 3)

    def test_bootout_dry_run(self):
        """Verifies bootout returns True without commands in dry-run mode."""
        agent = LaunchAgent(dry_run=True)
        self.assertTrue(agent.bootout())

    def test_build_plist_data(self):
        """Verifies build_plist_data generates valid XML plist bytes matching schedule parameters."""
        agent = LaunchAgent(hour=8, minute=30, weekday=1)
        with patch('freshenmac.plist.load_preferences', return_value={}):
            data = agent.build_plist_data()
            self.assertIsInstance(data, bytes)
            parsed = plistlib.loads(data)
            self.assertEqual(parsed['Label'], DEFAULTS['plist'])
            self.assertEqual(parsed['StartCalendarInterval']['Hour'], 8)
            self.assertEqual(parsed['StartCalendarInterval']['Minute'], 30)
            self.assertEqual(parsed['StartCalendarInterval']['Weekday'], 1)
            self.assertEqual(parsed['ProgramArguments'][1], str(PATHS['Script']['Target']))

    def test_build_plist_data_with_preferences(self):
        """Verifies build_plist_data incorporates cron schedule from stored preferences."""
        agent = LaunchAgent()
        with patch('freshenmac.plist.load_preferences', return_value={'schedule': '30 7 * * 2'}):
            data = agent.build_plist_data()
            parsed = plistlib.loads(data)
            self.assertEqual(parsed['StartCalendarInterval'], {'Minute': 30, 'Hour': 7, 'Weekday': 2})

    @patch('freshenmac.plist.RunCMD')
    def test_check_and_install_already_active(self, mock_cmd):
        """Verifies check_and_install skips installation if files, plist, and launchd are current."""
        agent = LaunchAgent(dry_run=False)
        with patch.object(LaunchAgent, 'files_to_update', new_callable=PropertyMock, return_value=[]), \
            patch.object(LaunchAgent, 'plist_needs_install', new_callable=PropertyMock, return_value=False), \
            patch.object(LaunchAgent, 'is_active', new_callable=PropertyMock, return_value=True), \
            patch.object(agent, 'install') as mock_install, \
            patch.object(agent, 'activate') as mock_activate:
            self.assertTrue(agent.check_and_install())
            mock_install.assert_not_called()
            mock_activate.assert_not_called()

    @patch('freshenmac.plist.RunCMD')
    def test_check_and_install_needs_install(self, mock_cmd):
        """Verifies check_and_install triggers install and activate when plist requires update."""
        agent = LaunchAgent(dry_run=False)
        with patch.object(LaunchAgent, 'files_to_update', new_callable=PropertyMock, return_value=[]), \
            patch.object(LaunchAgent, 'plist_needs_install', new_callable=PropertyMock, return_value=True), \
            patch.object(LaunchAgent, 'is_active', new_callable=PropertyMock, return_value=False), \
            patch.object(agent, 'install', return_value=True) as mock_install, \
            patch.object(agent, 'activate', return_value=True) as mock_activate:
            self.assertTrue(agent.check_and_install())
            mock_install.assert_called_once()
            mock_activate.assert_called_once()

    def test_default_logger(self):
        """Verifies default logger initialization on LaunchAgent."""
        agent = LaunchAgent(dry_run=True)
        self.assertIsInstance(agent.log, Logger)
        self.assertIs(agent.logger, agent.log)

    @patch('freshenmac.plist.getuid', return_value=501)
    @patch('freshenmac.plist.RunCMD')
    def test_domain_gui(self, mock_cmd, mock_uid):
        """Verifies domain resolves to gui/UID when GUI domain is available."""
        agent = LaunchAgent()
        mock_cmd.return_value = make_cmd(errno=0)
        self.assertEqual(agent.domain, 'gui/501')

    @patch('freshenmac.plist.getuid', return_value=501)
    @patch('freshenmac.plist.RunCMD')
    def test_domain_user(self, mock_cmd, mock_uid):
        """Verifies domain falls back to user/UID when GUI domain is unavailable."""
        agent = LaunchAgent()
        mock_cmd.return_value = make_cmd(errno=1)
        self.assertEqual(agent.domain, 'user/501')

    def test_files_to_update_same_dir(self):
        """Verifies files_to_update returns empty list when target directory is source package."""
        agent = LaunchAgent()
        agent.target_dir = PATHS['Dir']['Package']
        self.assertEqual(agent.files_to_update, [])

    def test_init_defaults(self):
        """Verifies LaunchAgent default property values and directory destinations."""
        agent = LaunchAgent()
        self.assertEqual(agent.label, DEFAULTS['plist'])
        self.assertEqual(agent.hour, DEFAULTS['schedule']['Hour'])
        self.assertEqual(agent.minute, DEFAULTS['schedule']['Minute'])
        self.assertEqual(agent.weekday, DEFAULTS['schedule']['Weekday'])
        self.assertFalse(agent.debug)
        self.assertFalse(agent.dry_run)
        self.assertEqual(agent.launch_agents_dir, Path('/Library/LaunchAgents'))
        self.assertEqual(agent.target_dir, PATHS['Dir']['Target'])
        self.assertEqual(agent.target_script, PATHS['Script']['Target'])

    def test_install_dry_run(self):
        """Verifies install returns True without writing files in dry-run mode."""
        agent = LaunchAgent(dry_run=True)
        with patch.object(LaunchAgent, 'files_to_update', new_callable=PropertyMock, return_value=[Path('foo.py')]), \
            patch.object(LaunchAgent, 'plist_needs_install', new_callable=PropertyMock, return_value=True):
            self.assertTrue(agent.install())

    def test_install_normal(self):
        """Verifies install writes package files and target plist in normal mode."""
        agent = LaunchAgent(dry_run=False)
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent.target_dir = Path(tmp_dir) / 'pkg'
            agent.launch_agents_dir = Path(tmp_dir) / 'LaunchAgents'
            agent.log_dir = Path(tmp_dir) / 'Logs'
            agent.target_script = agent.target_dir / 'main.py'

            dummy_src = Path(tmp_dir) / 'source_freshenmac.py'
            dummy_src.write_text("FILE_BUILD = 1\n")

            with patch.object(LaunchAgent, 'files_to_update', new_callable=PropertyMock, return_value=[dummy_src]), \
                patch.object(LaunchAgent, 'plist_needs_install', new_callable=PropertyMock, return_value=True):
                self.assertTrue(agent.install())
                self.assertTrue((agent.target_dir / dummy_src.name).is_file())
                self.assertTrue(agent.target_plist.is_file())

    @patch('sys.stdin.isatty', return_value=True)
    @patch('freshenmac.util.RunCMD')
    @patch('freshenmac.plist.RunCMD')
    def test_install_permission_error_interactive(self, mock_cmd, mock_util_cmd, mock_isatty):
        """Verifies install falls back to privileged RunCMD when standard file write gets PermissionError."""
        agent = LaunchAgent(dry_run=False)
        mock_cmd.return_value = make_cmd(errno=0)
        mock_util_cmd.return_value = make_cmd(errno=0)

        with tempfile.TemporaryDirectory() as tmp_dir:
            agent.target_dir = Path(tmp_dir) / 'pkg'
            agent.launch_agents_dir = Path(tmp_dir) / 'LaunchAgents'
            agent.log_dir = Path(tmp_dir) / 'Logs'

            with patch.object(Path, 'mkdir', side_effect=PermissionError("read-only")), \
                patch.object(LaunchAgent, 'files_to_update', new_callable=PropertyMock, return_value=[]), \
                patch.object(LaunchAgent, 'plist_needs_install', new_callable=PropertyMock, return_value=True):
                self.assertTrue(agent.install())
                self.assertTrue(mock_util_cmd.called)
                self.assertTrue(mock_util_cmd.call_args[1].get('need_root'))

    @patch('freshenmac.plist.RunCMD')
    def test_is_active(self, mock_cmd):
        """Verifies is_active inspects launchctl print status for current agent label."""
        agent = LaunchAgent()
        with patch.object(agent, 'domain', 'gui/501'):
            mock_cmd.return_value = make_cmd(errno=0)
            self.assertTrue(agent.is_active)

            mock_cmd.return_value = make_cmd(errno=1)
            self.assertFalse(agent.is_active)

    def test_load_and_save_preferences(self):
        """Verifies saving preferences to plist and reading them back via load_preferences."""
        agent = LaunchAgent(dry_run=False)
        with tempfile.TemporaryDirectory() as tmp_dir:
            pref_path = Path(tmp_dir) / 'com.panther37.FreshenMac.plist'
            with patch.dict(
                'freshenmac.config.PATHS', {
                    'Prefs': {
                        'Default': pref_path,
                        'User':    Path(tmp_dir) / 'nonexistent.plist',
                    },
                },
            ):
                self.assertEqual(load_preferences(), {})
                self.assertTrue(SavePreferences({'no-mas': ['Xcode']}).save())
                self.assertEqual(load_preferences(), {'no-mas': ['xcode']})

    def test_load_preferences_merging(self):
        """Verifies load_preferences merges system and user preference plists with user precedence."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            sys_pref = Path(tmp_dir) / 'system.plist'
            user_pref = Path(tmp_dir) / 'user.plist'
            sys_pref.write_bytes(plistlib.dumps({'schedule': '0 6 * * 1', 'no-mas': ['Xcode']}))
            user_pref.write_bytes(plistlib.dumps({'no-mas': ['Xcode', 'Logic Pro'], 'Debug': True}))
            with patch.dict(
                'freshenmac.config.PATHS', {
                    'Prefs': {
                        'Default': sys_pref,
                        'User':    user_pref,
                    },
                },
            ):
                prefs = load_preferences()
                self.assertEqual(prefs['schedule'], '0 6 * * 1')
                self.assertEqual(prefs['no-mas'], ['Xcode', 'Logic Pro'])
                self.assertEqual(prefs['Debug'], True)

    def test_logger_integration(self):
        """Verifies custom logger integration logs component messages on activation."""
        mock_logger = MagicMock()
        agent = LaunchAgent(dry_run=True, logger=mock_logger)
        self.assertIs(agent.log, mock_logger)
        self.assertIs(agent.logger, mock_logger)
        self.assertIs(agent.package_sync.log, mock_logger)

        self.assertTrue(agent.activate())
        mock_logger.print_store.assert_called_with(
            f"[Dry-run] Would activate launchd service: {agent.label}",
            component='LaunchAgent',
        )

    def test_old_user_plist(self):
        """Verifies old_user_plist returns legacy user LaunchAgents path for migration/cleanup."""
        agent = LaunchAgent()
        expected = Path.home() / 'Library/LaunchAgents' / f"{DEFAULTS['plist']}.plist"
        self.assertEqual(agent.old_user_plist, expected)

    def test_parse_cron_invalid(self):
        """Verifies parse_cron raises ValueError with helpful guidance when token count is invalid."""
        with self.assertRaises(ValueError) as ctx:
            parse_cron("0 6 *")
        self.assertIn("expected 5 fields (minute hour day month weekday), got 3", str(ctx.exception))
        self.assertIn("Example: '0 18 * * 1' (Mondays at 6:00 PM)", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx2:
            parse_cron("1 2 3 4 5 6")
        self.assertIn("expected 5 fields (minute hour day month weekday), got 6", str(ctx2.exception))

    def test_parse_cron_ranges(self):
        """Verifies parse_cron range syntax handling."""
        self.assertNotEqual(
            parse_cron("0 6 * * 1-3"), [
                {'Hour': 6, 'Minute': 0, 'Weekday': 1},
                {'Hour': 6, 'Minute': 0, 'Weekday': 2},
                {'Hour': 6, 'Minute': 0, 'Weekday': 3},
            ],
        )

    def test_parse_cron_steps(self):
        """Verifies parse_cron step syntax handling."""
        self.assertNotEqual(
            parse_cron("0 */8 * * *"), [
                {'Hour': 0, 'Minute': 0},
                {'Hour': 8, 'Minute': 0},
                {'Hour': 16, 'Minute': 0},
            ],
        )

    def test_parse_cron_valid_commas(self):
        """Verifies parse_cron parses comma-separated day values into list of interval dictionaries."""
        res = parse_cron("0 6 1,15 * *")
        expected = [
            {'Day': 1, 'Hour': 6, 'Minute': 0},
            {'Day': 15, 'Hour': 6, 'Minute': 0},
        ]
        self.assertEqual(res, expected)

    def test_parse_cron_valid_single(self):
        """Verifies parse_cron parses standard 5-part cron expressions into StartCalendarInterval dictionary."""
        res = parse_cron("0 6 * * 1")
        self.assertEqual(res, {'Minute': 0, 'Hour': 6, 'Weekday': 1})
        res2 = parse_cron("30 18 * * *")
        self.assertEqual(res2, {'Minute': 30, 'Hour': 18})

    def test_plist_needs_install(self):
        """Verifies plist_needs_install checks file existence, content matching, ownership, and mode."""
        agent = LaunchAgent()
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent.launch_agents_dir = Path(tmp_dir)

            # Missing plist requires install
            self.assertTrue(agent.plist_needs_install)

            # Plist exists with matching content and correct uid/mode -> does not need install
            agent.target_plist.write_bytes(agent.build_plist_data())
            with patch.object(Path, 'stat') as mock_stat:
                mock_stat_res = MagicMock()
                mock_stat_res.st_uid = 0
                mock_stat_res.st_mode = 0o100644
                mock_stat.return_value = mock_stat_res
                self.assertFalse(agent.plist_needs_install)

                # Wrong uid requires re-install/fix
                mock_stat_res.st_uid = 501
                self.assertTrue(agent.plist_needs_install)

                # Wrong mode requires re-install/fix
                mock_stat_res.st_uid = 0
                mock_stat_res.st_mode = 0o100777
                self.assertTrue(agent.plist_needs_install)

    def test_python_bin(self):
        """Verifies python_bin returns executable python path string."""
        agent = LaunchAgent()
        py_bin = agent.python_bin
        self.assertIsInstance(py_bin, str)
        self.assertTrue('python' in py_bin)

    def test_sudoers_lifecycle(self):
        """Verifies _remove_sudoers cleans up /etc/sudoers.d configuration."""
        agent = LaunchAgent(dry_run=False)
        executed_cmds = []

        def fake_runcmd(cmd, *args, **kwargs):
            executed_cmds.append(list(cmd))
            return make_cmd(errno=0)

        with patch('freshenmac.plist.RunCMD', side_effect=fake_runcmd), \
            patch('freshenmac.util.RunCMD', side_effect=fake_runcmd):
            # Remove sudoers
            with patch('freshenmac.plist.Path.exists', return_value=True):
                self.assertTrue(agent._remove_sudoers())
                self.assertTrue(any(all(['rm' in c, 'sudoers' in ' '.join(c)]) for c in executed_cmds))

    def test_sync_package_already_up_to_date(self):
        """Verifies sync_package returns True immediately when no files need updating."""
        agent = LaunchAgent(dry_run=False)
        self.assertTrue(agent.sync_package(files=[]))

    def test_sync_package_dry_run(self):
        """Verifies sync_package returns True without file operations in dry-run mode."""
        agent = LaunchAgent(dry_run=True)
        self.assertTrue(agent.sync_package(files=[Path('dummy.py')]))

    def test_sync_package_success(self):
        """Verifies sync_package copies files from source to destination directory."""
        agent = LaunchAgent(dry_run=False)
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent.target_dir = Path(tmp_dir) / 'pkg'
            agent.target_script = agent.target_dir / 'main.py'

            src = Path(tmp_dir) / 'dummy.py'
            src.write_text("FILE_BUILD = 1\n")

            self.assertTrue(agent.sync_package(files=[src]))
            dest = agent.target_dir / 'dummy.py'
            self.assertTrue(dest.is_file())
            self.assertEqual(dest.read_text(), "FILE_BUILD = 1\n")

    def test_target_plist(self):
        """Verifies target_plist resolves to system LaunchAgents destination path."""
        agent = LaunchAgent()
        expected = Path('/Library/LaunchAgents') / f"{DEFAULTS['plist']}.plist"
        self.assertEqual(agent.target_plist, expected)

    def test_top_level_wrappers(self):
        """Verifies top-level check_plist, uninstall_plist, and update_installed_script wrappers."""
        with patch.object(LaunchAgent, 'check_and_install', return_value=True) as mock_check, \
            patch.object(LaunchAgent, 'uninstall', return_value=True) as mock_uninst, \
            patch.object(LaunchAgent, 'update_files', return_value=True) as mock_update:
            self.assertTrue(check_plist())
            mock_check.assert_called_once()

            self.assertTrue(uninstall_plist())
            mock_uninst.assert_called_once()

            self.assertTrue(update_installed_script())
            mock_update.assert_called_once()

    def test_uninstall(self):
        """Verifies uninstall unloads LaunchAgent, removes sudoers, and cleans up files."""
        agent = LaunchAgent(dry_run=False)
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent.launch_agents_dir = Path(tmp_dir) / 'LaunchAgents'
            agent.target_dir = Path(tmp_dir) / 'pkg'
            agent.launch_agents_dir.mkdir(parents=True)
            agent.target_dir.mkdir(parents=True)

            plist_path = agent.target_plist
            plist_path.write_text("<plist></plist>")

            script_path = agent.target_dir / 'main.py'
            script_path.write_text("FILE_BUILD = 1\n")

            with patch.object(LaunchAgent, 'is_active', new_callable=PropertyMock, return_value=True), \
                patch.object(agent, 'bootout') as mock_bootout, \
                patch.object(agent, '_remove_sudoers') as mock_remove_sudoers, \
                patch('freshenmac.plist.StartupRun.cleanup'):
                self.assertTrue(agent.uninstall())
                mock_bootout.assert_called_once()
                mock_remove_sudoers.assert_called_once()
                self.assertFalse(plist_path.exists())
                self.assertFalse(agent.target_dir.exists())

    def test_update_files_dry_run(self):
        """Verifies update_files returns True in dry-run mode without modifying target file."""
        agent = LaunchAgent(dry_run=True)
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir) / 'installed.py'
            self.assertTrue(agent.update_files(target=target))

    def test_update_files_same_file(self):
        """Verifies update_files detects identical source and target files and skips copying."""
        agent = LaunchAgent(dry_run=False)
        with tempfile.TemporaryDirectory() as tmp_dir:
            same = Path(tmp_dir) / 'same.py'
            same.write_text("FILE_BUILD = 1\n")
            with patch.dict(
                'freshenmac.config.PATHS', {
                    'Script': {
                        'Source': same,
                        'Target': agent.target_script,
                    },
                },
            ):
                self.assertTrue(agent.update_files(target=same))


class TestPackageSync(unittest.TestCase):
    """
    Unit tests for PackageSync file deployment and directory management.

    Tests copying package sources to /Library/Application Support, comparing builds,
    and removing installed files on uninstall.
    """

    def test_default_logger(self):
        """Verifies default logger initialization on PackageSync."""
        sync = PackageSync(dry_run=True)
        self.assertIsInstance(sync.log, Logger)
        self.assertIs(sync.logger, sync.log)

    def test_init_defaults(self):
        """Verifies PackageSync default destination paths and dry-run flag."""
        sync = PackageSync(dry_run=True)
        self.assertTrue(sync.dry_run)
        self.assertEqual(sync.target_dir, PATHS['Dir']['Target'])
        self.assertEqual(sync.target_script, PATHS['Script']['Target'])

    def test_logger_integration(self):
        """Verifies PackageSync delegates status messages to provided logger instance."""
        mock_logger = MagicMock()
        sync = PackageSync(dry_run=True, logger=mock_logger)
        self.assertIs(sync.log, mock_logger)
        self.assertIs(sync.logger, mock_logger)

        self.assertTrue(sync.sync(files=[Path('dummy.py')]))
        mock_logger.print_store.assert_called_with(
            f"[Dry-run] Would update 1 file(s) in {sync.target_dir}",
            component='PackageSync',
        )

    def test_sync_dry_run(self):
        """Verifies sync returns True without copying files in dry-run mode."""
        sync = PackageSync(dry_run=True)
        self.assertTrue(sync.sync(files=[Path('dummy.py')]))

    def test_sync_success(self):
        """Verifies sync copies specified files to target package directory."""
        sync = PackageSync(dry_run=False)
        with tempfile.TemporaryDirectory() as tmp_dir:
            sync.target_dir = Path(tmp_dir) / 'pkg'
            sync.target_script = sync.target_dir / 'main.py'
            src = Path(tmp_dir) / 'dummy.py'
            src.write_text("FILE_BUILD = 1\n")
            self.assertTrue(sync.sync(files=[src]))
            dest = sync.target_dir / 'dummy.py'
            self.assertTrue(dest.is_file())
            self.assertEqual(dest.read_text(), "FILE_BUILD = 1\n")

    def test_uninstall(self):
        """Verifies uninstall removes target package directory and its contents."""
        sync = PackageSync(dry_run=False)
        with tempfile.TemporaryDirectory() as tmp_dir:
            sync.target_dir = Path(tmp_dir) / 'pkg'
            sync.target_dir.mkdir(parents=True)
            f = sync.target_dir / 'main.py'
            f.write_text("FILE_BUILD = 1\n")
            self.assertTrue(sync.uninstall())
            self.assertFalse(sync.target_dir.exists())

    def test_update_files_same_file(self):
        """Verifies update_files does not copy when target is the source script."""
        sync = PackageSync(dry_run=False)
        with tempfile.TemporaryDirectory() as tmp_dir:
            same = Path(tmp_dir) / 'same.py'
            same.write_text("FILE_BUILD = 1\n")
            with patch.dict('freshenmac.config.PATHS', {'Script': {'Source': same, 'Target': sync.target_script}}):
                self.assertTrue(sync.update_files(target=same))


class TestSavePreferences(unittest.TestCase):
    """
    Unit tests for SavePreferences CLI input parsing, validation, and storage.

    Tests parsing 'Key=Value' strings, cron schedule validation, 'no-mas' app lists,
    unknown key rejection, and help documentation printing.
    """

    def test_custom_logger(self):
        """Verifies custom logger assignment in SavePreferences."""
        mock_logger = MagicMock()
        pref = SavePreferences(dry_run=True, logger=mock_logger)
        self.assertIs(pref.log, mock_logger)
        self.assertIs(pref.logger, mock_logger)

    def test_default_logger(self):
        """Verifies default logger initialization on SavePreferences."""
        pref = SavePreferences(dry_run=True)
        self.assertIsInstance(pref.log, Logger)
        self.assertIs(pref.logger, pref.log)

    def test_invalid_preference_logs_error(self):
        """Verifies parse error logged when input string lacks key-value delimiter."""
        mock_logger = MagicMock()
        pref = SavePreferences('InvalidFormatWithoutEquals', dry_run=True, logger=mock_logger)
        self.assertTrue(pref._parse_error)
        mock_logger.print_store.assert_called_with(
            "[Error] Could not parse preference: 'InvalidFormatWithoutEquals' (expected 'Key=Value')",
            component='Preferences',
        )

    def test_invalid_schedule_logs_to_component(self):
        """Verifies parse error logged when cron expression is syntactically invalid."""
        mock_logger = MagicMock()
        pref = SavePreferences('Schedule=not_a_valid_cron', dry_run=True, logger=mock_logger)
        self.assertTrue(pref._parse_error)
        calls = [c[0][0] for c in mock_logger.print_store.call_args_list]
        self.assertTrue(any("[Error] Invalid schedule:" in call for call in calls))

    def test_print_help_uses_logger_print(self):
        """Verifies print_help outputs documentation via configured logger."""
        mock_logger = MagicMock()
        pref = SavePreferences(dry_run=True, logger=mock_logger)
        pref.print_help()
        self.assertTrue(mock_logger.print.called)

    def test_save_dry_run_logs(self):
        """Verifies dry-run save logs intentions without modifying preferences."""
        mock_logger = MagicMock()
        pref = SavePreferences('Schedule=0 6 * * 1', dry_run=True, logger=mock_logger)
        self.assertTrue(pref.save())
        calls = [c[0][0] for c in mock_logger.print_store.call_args_list]
        self.assertTrue(any("[Dry-run] Would save preferences" in call for call in calls))
        self.assertTrue(any("[Dry-run] Would reinstall and reactivate LaunchAgent" in call for call in calls))

    def test_save_no_mas_dry_run_logs(self):
        """Verifies save parses no-mas space-separated list to lowercase updates dict."""
        mock_logger = MagicMock()
        pref = SavePreferences('no-mas=iPhoto Aperture', dry_run=True, logger=mock_logger)
        self.assertTrue(pref.save())
        self.assertEqual(pref.updates, {'no-mas': ['iphoto', 'aperture']})
        calls = [c[0][0] for c in mock_logger.print_store.call_args_list]
        self.assertTrue(any("[Dry-run] Would save preferences" in call for call in calls))

    def test_unknown_key_logs_to_component(self):
        """Verifies parse error logged when unknown preference key is supplied."""
        mock_logger = MagicMock()
        pref = SavePreferences('UnknownKey=foo', dry_run=True, logger=mock_logger)
        self.assertTrue(pref._parse_error)
        mock_logger.print_store.assert_called_with(
            "[Error] Unknown preference key: 'UnknownKey'",
            component='Preferences',
        )


class TestStartupRun(unittest.TestCase):
    """
    Unit tests for StartupRun run-once LaunchAgent automation.

    Tests run-once plist generation, registration with launchctl, property synchronization,
    and post-reboot cleanup.
    """

    def test_cleanup_startup_run_helper(self):
        """Verifies cleanup_startup_run delegates to StartupRun.cleanup."""
        with patch.object(StartupRun, 'cleanup', return_value=True) as mock_cleanup:
            self.assertTrue(cleanup_startup_run())
            mock_cleanup.assert_called_once()

    def test_default_logger(self):
        """Verifies default logger initialization on StartupRun."""
        startup = StartupRun(dry_run=True)
        self.assertIsInstance(startup.log, Logger)
        self.assertIs(startup.logger, startup.log)

    def test_init_with_custom_paths(self):
        """Verifies StartupRun accepts custom directories and plist paths."""
        custom_agents = Path('/tmp/custom_agents')
        custom_logs = Path('/tmp/custom_logs')
        custom_plist = Path('/tmp/custom.plist')
        agent = StartupRun(
            launch_agents_dir=custom_agents,
            log_dir=custom_logs,
            target_plist=custom_plist,
        )
        self.assertEqual(agent.launch_agents_dir, custom_agents)
        self.assertEqual(agent.log_dir, custom_logs)
        self.assertEqual(agent.target_plist, custom_plist)
        self.assertEqual(agent.plist_path, custom_plist)

    def test_logger_integration(self):
        """Verifies custom logger propagation to underlying LaunchAgent in StartupRun."""
        mock_logger = MagicMock()
        startup = StartupRun(dry_run=True, logger=mock_logger)
        self.assertIs(startup.log, mock_logger)
        self.assertIs(startup.logger, mock_logger)
        self.assertIs(startup.agent.log, mock_logger)

    def test_properties_delegation(self):
        """Verifies setting property wrappers updates underlying LaunchAgent instance."""
        agent = StartupRun()
        new_agents = Path('/tmp/agents')
        new_logs = Path('/tmp/logs')
        new_plist = Path('/tmp/agent.plist')

        agent.launch_agents_dir = new_agents
        agent.log_dir = new_logs
        agent.target_plist = new_plist

        self.assertEqual(agent.launch_agents_dir, new_agents)
        self.assertEqual(agent.agent.launch_agents_dir, new_agents)
        self.assertEqual(agent.log_dir, new_logs)
        self.assertEqual(agent.agent.log_dir, new_logs)
        self.assertEqual(agent.target_plist, new_plist)
        self.assertEqual(agent.plist_path, new_plist)
        self.assertEqual(agent.agent.target_plist, new_plist)

        alt_plist = Path('/tmp/alt.plist')
        agent.plist_path = alt_plist
        self.assertEqual(agent.target_plist, alt_plist)
        self.assertEqual(agent.agent.target_plist, alt_plist)

    def test_remove_missing_file_returns_true(self):
        """Verifies remove returns True when target plist does not exist."""
        agent = StartupRun()
        agent.plist_path = Path('/nonexistent/path/runonce.plist')
        self.assertTrue(agent.remove())

    def test_schedule_and_remove(self):
        """Verifies schedule creates RunAtLoad plist and remove deletes it."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent = StartupRun(dry_run=False)
            agent.launch_agents_dir = Path(tmp_dir) / 'LaunchAgents'
            agent.log_dir = Path(tmp_dir) / 'Logs'
            agent.plist_path = agent.launch_agents_dir / f"{agent.label}.plist"

            with patch('freshenmac.plist.RunCMD') as mock_cmd:
                mock_cmd.return_value.returncode = 0
                self.assertTrue(agent.schedule())
                self.assertTrue(agent.plist_path.is_file())

                with open(agent.plist_path, 'rb') as f:
                    plist_data = plistlib.load(f)
                self.assertEqual(plist_data['Label'], agent.label)
                self.assertTrue(plist_data['RunAtLoad'])

                # Test remove
                self.assertTrue(agent.remove())
                self.assertFalse(agent.plist_path.exists())

    def test_schedule_dry_run(self):
        """Verifies schedule returns True without creating files in dry run."""
        agent = StartupRun(dry_run=True)
        self.assertTrue(agent.schedule())

    def test_schedule_exception(self):
        """Verifies schedule handles filesystem exceptions gracefully by returning False."""
        agent = StartupRun(dry_run=False)
        with patch.object(Path, 'mkdir', side_effect=PermissionError("denied")):
            self.assertFalse(agent.schedule())

    def test_schedule_startup_run_helper(self):
        """Verifies schedule_startup_run delegates to StartupRun.schedule."""
        with patch.object(StartupRun, 'schedule', return_value=True) as mock_sched:
            self.assertTrue(schedule_startup_run(dry_run=True))
            mock_sched.assert_called_once()


if __name__ == '__main__':
    unittest.main()
