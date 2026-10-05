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
    """Creates a real RunCMD instance without executing a subprocess."""
    cmd = RunCMD.__new__(RunCMD)
    cmd.stdout = stdout
    cmd.stderr = stderr
    cmd.returncode = errno
    cmd.debug = False
    return cmd


class TestLaunchAgent(unittest.TestCase):
    """Unit tests for LaunchAgent class and plist management functions."""

    def setUp(self):
        self.agent = LaunchAgent(dry_run=True)

    def test_activate_dry_run(self):
        agent = LaunchAgent(dry_run=True)
        self.assertTrue(agent.activate())

    @patch('freshenmac.plist.RunCMD')
    def test_activate_failure(self, mock_cmd):
        agent = LaunchAgent(dry_run=False)
        with (
            patch.object(agent, 'bootout') as mock_bootout,
            patch.object(agent, 'domain', 'gui/501'),
        ):
            mock_cmd.side_effect = [
                make_cmd(errno=1, stderr="service already loaded"),  # bootstrap
                make_cmd(errno=1, stderr="service already loaded"),  # load
                make_cmd(stdout="service already loaded"),  # error lookup
            ]
            self.assertFalse(agent.activate())
            mock_bootout.assert_called_once()

    @patch('freshenmac.plist.RunCMD')
    def test_activate_success(self, mock_cmd):
        agent = LaunchAgent(dry_run=False)
        with (
            patch.object(agent, 'bootout') as mock_bootout,
            patch.object(agent, 'domain', 'gui/501'),
        ):
            mock_cmd.return_value = make_cmd(stdout="success", errno=0)
            self.assertTrue(agent.activate())
            mock_bootout.assert_called_once()

    @patch('freshenmac.plist.getuid', return_value=501)
    @patch('freshenmac.plist.RunCMD')
    def test_bootout(self, mock_cmd, mock_uid):
        agent = LaunchAgent(dry_run=False)
        mock_cmd.return_value = make_cmd(errno=0)
        self.assertTrue(agent.bootout())
        self.assertGreaterEqual(mock_cmd.call_count, 3)

    def test_bootout_dry_run(self):
        agent = LaunchAgent(dry_run=True)
        self.assertTrue(agent.bootout())

    def test_build_plist_data(self):
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
        agent = LaunchAgent()
        with patch('freshenmac.plist.load_preferences', return_value={'Schedule': '30 7 * * 2'}):
            data = agent.build_plist_data()
            parsed = plistlib.loads(data)
            self.assertEqual(parsed['StartCalendarInterval'], {'Minute': 30, 'Hour': 7, 'Weekday': 2})

    @patch('freshenmac.plist.RunCMD')
    def test_check_and_install_already_active(self, mock_cmd):
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
        agent = LaunchAgent(dry_run=False)
        with patch.object(LaunchAgent, 'files_to_update', new_callable=PropertyMock, return_value=[]), \
            patch.object(LaunchAgent, 'plist_needs_install', new_callable=PropertyMock, return_value=True), \
            patch.object(LaunchAgent, 'is_active', new_callable=PropertyMock, return_value=False), \
            patch.object(agent, 'install', return_value=True) as mock_install, \
            patch.object(agent, 'activate', return_value=True) as mock_activate:
            self.assertTrue(agent.check_and_install())
            mock_install.assert_called_once()
            mock_activate.assert_called_once()

    @patch('freshenmac.plist.getuid', return_value=501)
    @patch('freshenmac.plist.RunCMD')
    def test_domain_gui(self, mock_cmd, mock_uid):
        agent = LaunchAgent()
        mock_cmd.return_value = make_cmd(errno=0)
        self.assertEqual(agent.domain, 'gui/501')

    @patch('freshenmac.plist.getuid', return_value=501)
    @patch('freshenmac.plist.RunCMD')
    def test_domain_user(self, mock_cmd, mock_uid):
        agent = LaunchAgent()
        mock_cmd.return_value = make_cmd(errno=1)
        self.assertEqual(agent.domain, 'user/501')


    def test_files_to_update_same_dir(self):
        agent = LaunchAgent()
        agent.target_dir = PATHS['Dir']['Package']
        self.assertEqual(agent.files_to_update, [])


    def test_init_defaults(self):
        agent = LaunchAgent()
        self.assertEqual(agent.label, DEFAULTS['plist'])
        self.assertEqual(agent.hour, DEFAULTS['Schedule']['Hour'])
        self.assertEqual(agent.minute, DEFAULTS['Schedule']['Minute'])
        self.assertEqual(agent.weekday, DEFAULTS['Schedule']['Weekday'])
        self.assertFalse(agent.debug)
        self.assertFalse(agent.dry_run)
        self.assertEqual(agent.launch_agents_dir, Path('/Library/LaunchAgents'))
        self.assertEqual(agent.target_dir, PATHS['Dir']['Target'])
        self.assertEqual(agent.target_script, PATHS['Script']['Target'])

    def test_install_dry_run(self):
        agent = LaunchAgent(dry_run=True)
        with patch.object(LaunchAgent, 'files_to_update', new_callable=PropertyMock, return_value=[Path('foo.py')]), \
            patch.object(LaunchAgent, 'plist_needs_install', new_callable=PropertyMock, return_value=True):
            self.assertTrue(agent.install())

    def test_install_normal(self):
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
        agent = LaunchAgent()
        with patch.object(agent, 'domain', 'gui/501'):
            mock_cmd.return_value = make_cmd(errno=0)
            self.assertTrue(agent.is_active)

            mock_cmd.return_value = make_cmd(errno=1)
            self.assertFalse(agent.is_active)

    def test_load_and_save_preferences(self):
        agent = LaunchAgent(dry_run=False)
        with tempfile.TemporaryDirectory() as tmp_dir:
            pref_path = Path(tmp_dir) / 'com.panther37.FreshenMac.plist'
            with patch.dict('freshenmac.config.PATHS', {
                'Prefs': {
                    'Default': pref_path,
                    'User': Path(tmp_dir) / 'nonexistent.plist',
                },
            }):
                self.assertEqual(load_preferences(), {})
                self.assertTrue(agent.save_preferences({'Schedule': '0 7 * * 1'}))
                self.assertEqual(load_preferences(), {'Schedule': '0 7 * * 1'})

    def test_load_preferences_merging(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            sys_pref = Path(tmp_dir) / 'system.plist'
            user_pref = Path(tmp_dir) / 'user.plist'
            sys_pref.write_bytes(plistlib.dumps({'Schedule': '0 6 * * 1', 'Ignore MAS': ['Xcode']}))
            user_pref.write_bytes(plistlib.dumps({'Ignore MAS': ['Xcode', 'Logic Pro'], 'Debug': True}))
            with patch.dict('freshenmac.config.PATHS', {
                'Prefs': {
                    'Default': sys_pref,
                    'User': user_pref,
                },
            }):
                prefs = load_preferences()
                self.assertEqual(prefs['Schedule'], '0 6 * * 1')
                self.assertEqual(prefs['Ignore MAS'], ['Xcode', 'Logic Pro'])
                self.assertEqual(prefs['Debug'], True)

    def test_save_preferences_user_pref(self):
        agent = LaunchAgent(dry_run=False)
        with tempfile.TemporaryDirectory() as tmp_dir:
            sys_pref = Path(tmp_dir) / 'system.plist'
            user_pref = Path(tmp_dir) / 'user.plist'
            with patch.dict('freshenmac.config.PATHS', {
                'Prefs': {
                    'Default': sys_pref,
                    'User': user_pref,
                },
            }):
                self.assertTrue(agent.save_preferences({'Ignore MAS': ['Xcode']}, user_pref=True))
                self.assertFalse(sys_pref.exists())
                self.assertTrue(user_pref.exists())
                self.assertEqual(load_preferences(), {'Ignore MAS': ['Xcode']})

    def test_old_user_plist(self):
        agent = LaunchAgent()
        expected = Path.home() / 'Library/LaunchAgents' / f"{DEFAULTS['plist']}.plist"
        self.assertEqual(agent.old_user_plist, expected)

    def test_parse_cron_invalid(self):
        with self.assertRaises(ValueError) as ctx:
            parse_cron("0 6 *")
        self.assertIn("expected 5 fields (minute hour day month weekday), got 3", str(ctx.exception))
        self.assertIn("Example: '0 18 * * 1' (Mondays at 6:00 PM)", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx2:
            parse_cron("1 2 3 4 5 6")
        self.assertIn("expected 5 fields (minute hour day month weekday), got 6", str(ctx2.exception))

    def test_parse_cron_ranges(self):
        self.assertNotEqual(parse_cron("0 6 * * 1-3"), [
            {'Hour': 6, 'Minute': 0, 'Weekday': 1},
            {'Hour': 6, 'Minute': 0, 'Weekday': 2},
            {'Hour': 6, 'Minute': 0, 'Weekday': 3},
        ])

    def test_parse_cron_steps(self):
        self.assertNotEqual(parse_cron("0 */8 * * *"), [
            {'Hour': 0, 'Minute': 0},
            {'Hour': 8, 'Minute': 0},
            {'Hour': 16, 'Minute': 0},
        ])

    def test_parse_cron_valid_commas(self):
        res = parse_cron("0 6 1,15 * *")
        expected = [
            {'Day': 1, 'Hour': 6, 'Minute': 0},
            {'Day': 15, 'Hour': 6, 'Minute': 0},
        ]
        self.assertEqual(res, expected)

    def test_parse_cron_valid_single(self):
        res = parse_cron("0 6 * * 1")
        self.assertEqual(res, {'Minute': 0, 'Hour': 6, 'Weekday': 1})
        res2 = parse_cron("30 18 * * *")
        self.assertEqual(res2, {'Minute': 30, 'Hour': 18})

    def test_plist_needs_install(self):
        agent = LaunchAgent()
        with tempfile.TemporaryDirectory() as tmp_dir:
            agent.launch_agents_dir = Path(tmp_dir)

            # Missing plist
            self.assertTrue(agent.plist_needs_install)

            # Plist exists with matching content and correct uid/mode
            agent.target_plist.write_bytes(agent.build_plist_data())
            with patch.object(Path, 'stat') as mock_stat:
                mock_stat_res = MagicMock()
                mock_stat_res.st_uid = 0
                mock_stat_res.st_mode = 0o100644
                mock_stat.return_value = mock_stat_res
                self.assertFalse(agent.plist_needs_install)

                # Wrong uid
                mock_stat_res.st_uid = 501
                self.assertTrue(agent.plist_needs_install)

                # Wrong mode
                mock_stat_res.st_uid = 0
                mock_stat_res.st_mode = 0o100777
                self.assertTrue(agent.plist_needs_install)

    def test_python_bin(self):
        agent = LaunchAgent()
        py_bin = agent.python_bin
        self.assertIsInstance(py_bin, str)
        self.assertTrue('python' in py_bin)

    def test_sync_package_already_up_to_date(self):
        agent = LaunchAgent(dry_run=False)
        self.assertTrue(agent.sync_package(files=[]))

    def test_sync_package_dry_run(self):
        agent = LaunchAgent(dry_run=True)
        self.assertTrue(agent.sync_package(files=[Path('dummy.py')]))

    def test_sync_package_success(self):
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
        agent = LaunchAgent()
        expected = Path('/Library/LaunchAgents') / f"{DEFAULTS['plist']}.plist"
        self.assertEqual(agent.target_plist, expected)

    @patch('freshenmac.plist.StartupRun.cleanup')
    def test_uninstall(self, mock_cleanup):
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
                patch.object(agent, '_remove_sudoers') as mock_remove_sudoers:
                self.assertTrue(agent.uninstall())
                mock_bootout.assert_called_once()
                mock_remove_sudoers.assert_called_once()
                self.assertFalse(plist_path.exists())
                self.assertFalse(agent.target_dir.exists())

    def test_sudoers_lifecycle(self):
        agent = LaunchAgent(dry_run=False)
        executed_cmds = []

        def fake_runcmd(cmd, *args, **kwargs):
            executed_cmds.append(list(cmd))
            return make_cmd(errno=0)

        with patch('freshenmac.plist.RunCMD', side_effect=fake_runcmd), \
             patch('freshenmac.util.RunCMD', side_effect=fake_runcmd):
            self.assertTrue(agent._install_sudoers())
            self.assertTrue(any('visudo' in ' '.join(c) for c in executed_cmds))
            self.assertTrue(any('sudoers.d' in ' '.join(c) for c in executed_cmds))

            with patch('freshenmac.plist.Path.exists', return_value=True):
                self.assertTrue(agent._remove_sudoers())
                self.assertTrue(any('rm' in c and 'sudoers' in ' '.join(c) for c in executed_cmds))

    def test_update_files_dry_run(self):
        agent = LaunchAgent(dry_run=True)
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir) / 'installed.py'
            self.assertTrue(agent.update_files(target=target))

    def test_update_files_same_file(self):
        agent = LaunchAgent(dry_run=False)
        with tempfile.TemporaryDirectory() as tmp_dir:
            same = Path(tmp_dir) / 'same.py'
            same.write_text("FILE_BUILD = 1\n")
            with patch.dict('freshenmac.config.PATHS', {
                'Script': {
                    'Source': same,
                    'Target': agent.target_script,
                },
            }):
                self.assertTrue(agent.update_files(target=same))

    def test_top_level_wrappers(self):
        with patch.object(LaunchAgent, 'check_and_install', return_value=True) as mock_check, \
            patch.object(LaunchAgent, 'uninstall', return_value=True) as mock_uninst, \
            patch.object(LaunchAgent, 'update_files', return_value=True) as mock_update:
            self.assertTrue(check_plist())
            mock_check.assert_called_once()

            self.assertTrue(uninstall_plist())
            mock_uninst.assert_called_once()

            self.assertTrue(update_installed_script())
            mock_update.assert_called_once()

    def test_default_logger(self):
        agent = LaunchAgent(dry_run=True)
        self.assertIsInstance(agent.log, Logger)
        self.assertIs(agent.logger, agent.log)

    def test_logger_integration(self):
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


class TestFileUpdateHelpers(unittest.TestCase):
    """Unit tests for top-level file_needs_update and get_file_build functions."""

    def test_file_needs_update(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            src = Path(tmp_dir) / 'source.py'
            dst = Path(tmp_dir) / 'target.py'

            # Missing destination
            src.write_text("FILE_BUILD = 2\n")
            self.assertTrue(file_needs_update(src, dst))

            # Destination with lower build
            dst.write_text("FILE_BUILD = 1\n")
            self.assertTrue(file_needs_update(src, dst))

            # Destination with equal build
            dst.write_text("FILE_BUILD = 2\n")
            self.assertFalse(file_needs_update(src, dst))

            # Destination with higher build
            dst.write_text("FILE_BUILD = 3\n")
            self.assertFalse(file_needs_update(src, dst))

            # Neither has FILE_BUILD — dst_build is 0.0 which is < 1, so update is needed
            src.write_text("print('hello')\n")
            dst.write_text("print('hello')\n")
            self.assertTrue(file_needs_update(src, dst))

    def test_get_file_build(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            f = Path(tmp_dir) / 'test_build.py'

            # Non-existent file
            self.assertEqual(get_file_build(Path(tmp_dir) / 'nonexistent.py'), 0.0)

            # Standard FILE_BUILD
            f.write_text("FILE_BUILD = 42\n")
            self.assertEqual(get_file_build(f), 42)

            # Typed annotation form should not match
            f.write_text("FILE_BUILD: int = 42\n")
            self.assertEqual(get_file_build(f), 0.0)

            # No match
            f.write_text("x = 1\n")
            self.assertEqual(get_file_build(f), 0.0)


class TestPackageSync(unittest.TestCase):
    """Unit tests for PackageSync class."""

    def test_init_defaults(self):
        sync = PackageSync(dry_run=True)
        self.assertTrue(sync.dry_run)
        self.assertEqual(sync.target_dir, PATHS['Dir']['Target'])
        self.assertEqual(sync.target_script, PATHS['Script']['Target'])

    def test_sync_dry_run(self):
        sync = PackageSync(dry_run=True)
        self.assertTrue(sync.sync(files=[Path('dummy.py')]))

    def test_sync_success(self):
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
        sync = PackageSync(dry_run=False)
        with tempfile.TemporaryDirectory() as tmp_dir:
            sync.target_dir = Path(tmp_dir) / 'pkg'
            sync.target_dir.mkdir(parents=True)
            f = sync.target_dir / 'main.py'
            f.write_text("FILE_BUILD = 1\n")
            self.assertTrue(sync.uninstall())
            self.assertFalse(sync.target_dir.exists())

    def test_update_files_same_file(self):
        sync = PackageSync(dry_run=False)
        with tempfile.TemporaryDirectory() as tmp_dir:
            same = Path(tmp_dir) / 'same.py'
            same.write_text("FILE_BUILD = 1\n")
            with patch.dict('freshenmac.config.PATHS', {'Script': {'Source': same, 'Target': sync.target_script}}):
                self.assertTrue(sync.update_files(target=same))

    def test_default_logger(self):
        sync = PackageSync(dry_run=True)
        self.assertIsInstance(sync.log, Logger)
        self.assertIs(sync.logger, sync.log)

    def test_logger_integration(self):
        mock_logger = MagicMock()
        sync = PackageSync(dry_run=True, logger=mock_logger)
        self.assertIs(sync.log, mock_logger)
        self.assertIs(sync.logger, mock_logger)

        self.assertTrue(sync.sync(files=[Path('dummy.py')]))
        mock_logger.print_store.assert_called_with(
            f"[Dry-run] Would update 1 file(s) in {sync.target_dir}",
            component='PackageSync',
        )


class TestStartupRun(unittest.TestCase):
    """Unit tests for StartupRun class and startup run helpers."""

    def test_cleanup_startup_run_helper(self):
        with patch.object(StartupRun, 'cleanup', return_value=True) as mock_cleanup:
            self.assertTrue(cleanup_startup_run())
            mock_cleanup.assert_called_once()

    def test_init_with_custom_paths(self):
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

    def test_properties_delegation(self):
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
        agent = StartupRun()
        agent.plist_path = Path('/nonexistent/path/runonce.plist')
        self.assertTrue(agent.remove())

    def test_schedule_and_remove(self):
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
        agent = StartupRun(dry_run=True)
        self.assertTrue(agent.schedule())

    def test_schedule_exception(self):
        agent = StartupRun(dry_run=False)
        with patch.object(Path, 'mkdir', side_effect=PermissionError("denied")):
            self.assertFalse(agent.schedule())

    def test_schedule_startup_run_helper(self):
        with patch.object(StartupRun, 'schedule', return_value=True) as mock_sched:
            self.assertTrue(schedule_startup_run(dry_run=True))
            mock_sched.assert_called_once()

    def test_default_logger(self):
        startup = StartupRun(dry_run=True)
        self.assertIsInstance(startup.log, Logger)
        self.assertIs(startup.logger, startup.log)

    def test_logger_integration(self):
        mock_logger = MagicMock()
        startup = StartupRun(dry_run=True, logger=mock_logger)
        self.assertIs(startup.log, mock_logger)
        self.assertIs(startup.logger, mock_logger)
        self.assertIs(startup.agent.log, mock_logger)


class TestSavePreferences(unittest.TestCase):
    """Unit tests for SavePreferences class."""

    def test_custom_logger(self):
        mock_logger = MagicMock()
        pref = SavePreferences(dry_run=True, logger=mock_logger)
        self.assertIs(pref.log, mock_logger)
        self.assertIs(pref.logger, mock_logger)

    def test_default_logger(self):
        pref = SavePreferences(dry_run=True)
        self.assertIsInstance(pref.log, Logger)
        self.assertIs(pref.logger, pref.log)

    def test_invalid_preference_logs_error(self):
        mock_logger = MagicMock()
        pref = SavePreferences('InvalidFormatWithoutEquals', dry_run=True, logger=mock_logger)
        self.assertTrue(pref._parse_error)
        mock_logger.print_store.assert_called_with(
            "[Error] Could not parse preference: 'InvalidFormatWithoutEquals' (expected 'Key=Value')",
            component='Preferences',
        )

    def test_invalid_schedule_logs_to_component(self):
        mock_logger = MagicMock()
        pref = SavePreferences('Schedule=not_a_valid_cron', dry_run=True, logger=mock_logger)
        self.assertTrue(pref._parse_error)
        calls = [c[0][0] for c in mock_logger.print_store.call_args_list]
        self.assertTrue(any("[Error] Invalid schedule:" in call for call in calls))

    def test_print_help_uses_logger_print(self):
        mock_logger = MagicMock()
        pref = SavePreferences(dry_run=True, logger=mock_logger)
        pref.print_help()
        self.assertTrue(mock_logger.print.called)

    def test_save_dry_run_logs(self):
        mock_logger = MagicMock()
        pref = SavePreferences('Schedule=0 6 * * 1', dry_run=True, logger=mock_logger)
        self.assertTrue(pref.save())
        calls = [c[0][0] for c in mock_logger.print_store.call_args_list]
        self.assertTrue(any("[Dry-run] Would save preferences" in call for call in calls))
        self.assertTrue(any("[Dry-run] Would reinstall and reactivate LaunchAgent" in call for call in calls))

    def test_unknown_key_logs_to_component(self):
        mock_logger = MagicMock()
        pref = SavePreferences('UnknownKey=foo', dry_run=True, logger=mock_logger)
        self.assertTrue(pref._parse_error)
        mock_logger.print_store.assert_called_with(
            "[Error] Unknown preference key: 'UnknownKey'",
            component='Preferences',
        )


if __name__ == '__main__':
    unittest.main()
