import plistlib
import unittest
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

from freshenmac.config import DEFAULTS, PATHS
from freshenmac.plist import (
    CronSchedule,
    LaunchAgent,
    SavePreferences,
    StartupRun,
    load_preferences,
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


class TestCronSchedule(unittest.TestCase):
    """
    Unit tests for CronSchedule dataclass.

    Verifies parsing from cron syntax, launchd dictionary formats,
    Cartesian interval generation, resolution precedence, and helper methods.
    """

    def test_bool(self):
        """Verifies __bool__ reflects whether any fields are populated."""
        self.assertFalse(bool(CronSchedule()))
        self.assertTrue(bool(CronSchedule.parse("0 6 * * 1")))

    def test_from_cron_help(self):
        """Verifies from_cron handles 'help' keyword by outputting guidance and returning empty schedule."""
        with patch('builtins.print') as mock_print:
            sched = CronSchedule.from_cron("help")
            self.assertFalse(bool(sched))
            mock_print.assert_called_once()

    def test_from_cron_invalid(self):
        """Verifies from_cron raises ValueError when token count is not 5 or tokens are invalid."""
        with self.assertRaises(ValueError) as ctx:
            CronSchedule.from_cron("0 6 *")
        self.assertIn("Expected 5 fields, got 3", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx2:
            CronSchedule.from_cron("1 2 3 4 5 6")
        self.assertIn("Expected 5 fields, got 6", str(ctx2.exception))

        with self.assertRaises(ValueError) as ctx3:
            CronSchedule.from_cron("0 6 abc * 1")
        self.assertIn("Invalid integer 'abc' for field 'Day'", str(ctx3.exception))

    def test_from_cron_sunday_normalization(self):
        """Verifies from_cron normalizes Sunday value 7 to 0 for launchd."""
        sched = CronSchedule.from_cron("0 6 * * 7")
        self.assertEqual(sched.to_launchd(), {'Minute': 0, 'Hour': 6, 'Weekday': 0})

    def test_from_cron_valid_commas(self):
        """Verifies from_cron parses comma-separated field values into Cartesian intervals."""
        sched = CronSchedule.from_cron("0 6 1,15 * *")
        expected = [
            {'Minute': 0, 'Hour': 6, 'Day': 1},
            {'Minute': 0, 'Hour': 6, 'Day': 15},
        ]
        self.assertEqual(sched.to_launchd(), expected)
        self.assertEqual(sched.to_cron(), "0 6 1,15 * *")
        self.assertEqual(len(sched), 2)

    def test_from_cron_valid_single(self):
        """Verifies from_cron parses standard 5-part cron expressions."""
        sched = CronSchedule.from_cron("0 6 * * 1")
        self.assertEqual(sched.to_launchd(), {'Minute': 0, 'Hour': 6, 'Weekday': 1})
        self.assertEqual(sched.to_cron(), "0 6 * * 1")
        self.assertEqual(len(sched), 1)

    def test_from_dicts(self):
        """Verifies from_dicts parses single dictionaries and lists of dictionaries."""
        d = {'Minute': 0, 'Hour': 6, 'Weekday': 1}
        sched = CronSchedule.from_dicts(d)
        self.assertEqual(sched.to_launchd(), d)

        lst = [{'Minute': 0, 'Hour': 6, 'Day': 1}, {'Minute': 0, 'Hour': 6, 'Day': 15}]
        sched_list = CronSchedule.from_dicts(lst)
        self.assertEqual(sched_list.to_launchd(), lst)

    def test_parse_and_iter(self):
        """Verifies parse accepts CronSchedule instances, strings, dicts, and lists, and supports iteration."""
        sched = CronSchedule.parse("0 6 1,15 * *")
        intervals = list(sched)
        self.assertEqual(len(intervals), 2)
        self.assertEqual(CronSchedule.parse(sched), sched)

        with self.assertRaises(ValueError):
            CronSchedule.parse("")
        with self.assertRaises(ValueError):
            CronSchedule.parse(123)  # type: ignore[arg-type]

    def test_resolve_precedence(self):
        """Verifies resolve respects saved preferences and falls back to defaults."""
        # 1. Saved preferences
        with patch('freshenmac.plist.load_preferences', return_value={'schedule': '30 7 * * 2'}):
            s1 = CronSchedule.resolve()
            self.assertEqual(s1.to_launchd(), {'Minute': 30, 'Hour': 7, 'Weekday': 2})

        # 2. Invalid saved preference logs warning and falls back to DEFAULTS
        mock_log = MagicMock()
        with patch('freshenmac.plist.load_preferences', return_value={'schedule': 'invalid cron'}):
            s2 = CronSchedule.resolve(logger=mock_log)
            self.assertEqual(s2.to_launchd(), DEFAULTS['schedule'])
            self.assertTrue(mock_log.print_store.called)

        # 3. Empty preferences falls back to DEFAULTS
        with patch('freshenmac.plist.load_preferences', return_value={}):
            s3 = CronSchedule.resolve()
            self.assertEqual(s3.to_launchd(), DEFAULTS['schedule'])


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

    @patch('freshenmac.plist.util.RunCMD')
    def test_activate_failure(self, mock_cmd):
        """Verifies activate returns False when launchctl bootstrap and fallback load fail."""
        agent = LaunchAgent(dry_run=False)
        with patch.object(agent, 'bootout') as mock_bootout, patch.object(agent, 'domain', 'gui/501'):
            mock_cmd.side_effect = [
                make_cmd(errno=1, stderr="service already loaded"),  # bootstrap
                make_cmd(errno=1, stderr="service already loaded"),  # fallback load
                make_cmd(stdout="service already loaded"),  # .        error lookup
            ]
            self.assertFalse(agent.activate())
            mock_bootout.assert_called_once()

    @patch('freshenmac.plist.util.RunCMD')
    def test_activate_success(self, mock_cmd):
        """Verifies activate boots out old agent and bootstraps new agent successfully."""
        agent = LaunchAgent(dry_run=False)
        with patch.object(agent, 'bootout') as mock_bootout, patch.object(agent, 'domain', 'gui/501'):
            mock_cmd.return_value = make_cmd(stdout="success", errno=0)
            self.assertTrue(agent.activate())
            mock_bootout.assert_called_once()

    @patch('freshenmac.plist.getuid', return_value=501)
    @patch('freshenmac.plist.util.RunCMD')
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
        """Verifies build_plist_data generates valid XML plist bytes matching default schedule parameters."""
        with patch('freshenmac.plist.load_preferences', return_value={}):
            agent = LaunchAgent(dry_run=True)
            data = agent.build_plist_data()
            self.assertIsInstance(data, bytes)
            parsed = plistlib.loads(data)
            self.assertEqual(parsed['Label'], DEFAULTS['plist'])
            self.assertEqual(parsed['StartCalendarInterval'], DEFAULTS['schedule'])
            self.assertEqual(parsed['ProgramArguments'][1], str(agent.target_script))

    def test_build_plist_data_with_preferences(self):
        """Verifies build_plist_data incorporates cron schedule from stored preferences."""
        with patch('freshenmac.plist.load_preferences', return_value={'schedule': '30 7 * * 2'}):
            agent = LaunchAgent(dry_run=True)
            data = agent.build_plist_data()
            parsed = plistlib.loads(data)
            self.assertEqual(parsed['StartCalendarInterval'], {'Minute': 30, 'Hour': 7, 'Weekday': 2})

    def test_build_plist_data_with_dict_preferences(self):
        """Verifies build_plist_data incorporates pre-parsed dict schedule from preferences."""
        with patch(
            'freshenmac.plist.load_preferences',
            return_value={'schedule': {'Minute': 0, 'Hour': 6, 'Weekday': 1}},
        ):
            agent = LaunchAgent(dry_run=True)
            data = agent.build_plist_data()
            parsed = plistlib.loads(data)
            self.assertEqual(parsed['StartCalendarInterval'], {'Minute': 0, 'Hour': 6, 'Weekday': 1})

    @patch('freshenmac.plist.util.RunCMD')
    def test_check_and_install_already_active(self, mock_cmd):
        """Verifies check_and_install skips installation if plist and launchd are current."""
        agent = LaunchAgent(dry_run=False)
        with patch.object(
            LaunchAgent, 'plist_needs_install', new_callable=PropertyMock, return_value=False,
        ), patch.object(
            LaunchAgent, 'is_active', new_callable=PropertyMock, return_value=True,
        ), patch.object(
            agent, 'install',
        ) as mock_install, patch.object(
            agent, 'activate',
        ) as mock_activate:
            self.assertTrue(agent.check_and_install())
            mock_install.assert_not_called()
            mock_activate.assert_not_called()

    @patch('freshenmac.plist.util.RunCMD')
    def test_check_and_install_needs_install(self, mock_cmd):
        """Verifies check_and_install triggers install and activate when plist requires update."""
        agent = LaunchAgent(dry_run=False)
        with patch.object(
            LaunchAgent, 'plist_needs_install', new_callable=PropertyMock, return_value=True,
        ), patch.object(
            LaunchAgent, 'is_active', new_callable=PropertyMock, return_value=False,
        ), patch.object(
            agent, 'install', return_value=True,
        ) as mock_install, patch.object(
            agent, 'activate', return_value=True,
        ) as mock_activate:
            self.assertTrue(agent.check_and_install())
            mock_install.assert_called_once()
            mock_activate.assert_called_once()

    @patch('freshenmac.plist.pwd.getpwnam')
    @patch('freshenmac.plist.util.TortimerCreds.get_console_user', return_value='matt')
    @patch('freshenmac.plist.getuid', return_value=0)
    def test_console_uid_as_root(self, mock_uid, mock_console_user, mock_getpwnam):
        """Verifies console_uid resolves to the console user's UID when running as root."""
        mock_pw = MagicMock()
        mock_pw.pw_uid = 501
        mock_getpwnam.return_value = mock_pw
        agent = LaunchAgent(dry_run=True)
        self.assertEqual(agent.console_uid, 501)
        mock_getpwnam.assert_called_once_with('matt')

    def test_default_logger(self):
        """Verifies default logger initialization on LaunchAgent."""
        agent = LaunchAgent(dry_run=True)
        self.assertIsInstance(agent.log, Logger)

    @patch('freshenmac.plist.getuid', return_value=501)
    @patch('freshenmac.plist.util.RunCMD')
    def test_domain_gui(self, mock_cmd, mock_uid):
        """Verifies domain resolves to gui/UID when GUI domain is available."""
        agent = LaunchAgent(dry_run=True)
        mock_cmd.return_value = make_cmd(errno=0)
        self.assertEqual(agent.domain, 'gui/501')

    @patch('freshenmac.plist.getuid', return_value=501)
    @patch('freshenmac.plist.util.RunCMD')
    def test_domain_user(self, mock_cmd, mock_uid):
        """Verifies domain falls back to user/UID when GUI domain is unavailable."""
        agent = LaunchAgent(dry_run=True)
        mock_cmd.return_value = make_cmd(errno=1)
        self.assertEqual(agent.domain, 'user/501')

    def test_init_defaults(self):
        """Verifies LaunchAgent default property values and directory destinations."""
        agent = LaunchAgent()
        self.assertEqual(agent.label, DEFAULTS['plist'])
        self.assertEqual(agent.schedule.fields['Hour'], [DEFAULTS['schedule']['Hour']])
        self.assertEqual(agent.schedule.fields['Minute'], [DEFAULTS['schedule']['Minute']])
        self.assertEqual(agent.schedule.fields['Weekday'], [DEFAULTS['schedule']['Weekday']])
        self.assertEqual(agent.debug_vars, {'debug': 0, 'debug_limit': 0})
        self.assertFalse(agent.dry_run)
        self.assertEqual(agent.launch_agents_dir, Path('/Library/LaunchAgents'))
        self.assertIsInstance(agent.target_script, Path)

    @patch('freshenmac.plist.getuid', return_value=0)
    def test_install_as_root_calls_install_privileged(self, mock_uid):
        """Verifies install delegates to _install_privileged when running as root with privileged=True."""
        agent = LaunchAgent(dry_run=False)
        with patch.object(
            agent, '_install_privileged', return_value=True,
        ) as mock_priv:
            self.assertTrue(agent.install(privileged=True))
            mock_priv.assert_called_once()

    def test_install_dry_run(self):
        """Verifies install returns True without writing files in dry-run mode."""
        agent = LaunchAgent(dry_run=True)
        with patch.object(
            LaunchAgent, 'plist_needs_install', new_callable=PropertyMock, return_value=True,
        ):
            self.assertTrue(agent.install())

    def test_install_normal(self):
        """Verifies install writes target plist in normal mode."""
        agent = LaunchAgent(dry_run=False)
        agent.launch_agents_dir = Path('/fake/LaunchAgents')
        agent.log_dir = Path('/fake/Logs')
        agent.target_script = Path('/fake/freshenmac')

        with patch(
            'freshenmac.plist.write_file',
        ) as mock_write, patch.object(
            agent, '_init_log_files',
        ) as mock_init_logs:
            self.assertTrue(agent.install())
            mock_write.assert_called_once_with(agent.target_plist, agent.build_plist_data())
            mock_init_logs.assert_called_once()

    @patch('sys.stdin.isatty', return_value=True)
    @patch('freshenmac.util.RunCMD')
    @patch('freshenmac.plist.util.RunCMD')
    def test_install_permission_error_interactive(self, mock_cmd, mock_util_cmd, mock_isatty):
        """Verifies install falls back to privileged RunCMD when standard file write gets PermissionError."""
        agent = LaunchAgent(dry_run=False)
        mock_cmd.return_value = make_cmd(errno=0)
        mock_util_cmd.return_value = make_cmd(errno=0)
        agent.launch_agents_dir = Path('/fake/LaunchAgents')
        agent.log_dir = Path('/fake/Logs')

        with patch(
            'freshenmac.plist.write_file', side_effect=PermissionError("read-only"),
        ), patch.object(
            agent, '_prepare_sudoers_commands', return_value=([], Path('')),
        ), patch.object(
            Path, 'write_bytes',
        ), patch.object(
            Path, 'unlink',
        ):
            self.assertTrue(agent.install())
            self.assertTrue(mock_util_cmd.called)
            self.assertTrue(mock_util_cmd.call_args[1].get('need_root'))

    def test_install_privileged_log_permissions(self):
        """Verifies _install_privileged configures system log file with root:admin and 0664 permissions."""
        agent = LaunchAgent(dry_run=False)
        with patch.object(agent, 'run_privileged', return_value=True) as mock_run:
            agent._install_privileged(b'<plist/>')
            called_cmd = mock_run.call_args[0][0]
            joined_cmd = ' '.join(called_cmd)
            self.assertIn("chown root:admin '/Library/Logs/FreshenMac.log'", joined_cmd)
            self.assertIn("chmod 664 '/Library/Logs/FreshenMac.log'", joined_cmd)

    @patch('freshenmac.plist.util.RunCMD')
    def test_is_active(self, mock_cmd):
        """Verifies is_active inspects launchctl print status for current agent label."""
        agent = LaunchAgent(dry_run=True)
        with patch.object(agent, 'domain', 'gui/501'):
            mock_cmd.return_value = make_cmd(errno=0)
            self.assertTrue(agent.is_active)

            mock_cmd.return_value = make_cmd(errno=1)
            self.assertFalse(agent.is_active)

    def test_load_and_save_preferences(self):
        """Verifies saving preferences to plist and reading them back via load_preferences."""
        pref_path = Path('/fake/com.panther37.FreshenMac.plist')
        user_path = Path('/fake/nonexistent.plist')
        stored: dict[Path, bytes] = {}

        def fake_write(a_path: Path, data: bytes, mode: int = 0o644) -> None:
            stored[a_path] = data

        with patch.dict(
            'freshenmac.config.PATHS', {
                'Prefs': {
                    'Default': pref_path,
                    'User':    user_path,
                },
            },
        ), patch(
            'freshenmac.plist.write_file', side_effect=fake_write,
        ), patch.object(
            Path, 'is_file', autospec=True, side_effect=lambda p: p in stored,
        ), patch.object(
            Path, 'read_bytes', autospec=True, side_effect=lambda p: stored[p],
        ):
            self.assertEqual(load_preferences(), {})
            self.assertTrue(SavePreferences({'no-mas': ['Xcode']}).save())
            self.assertEqual(load_preferences(), {'no-mas': ['xcode']})

    def test_load_preferences_merging(self):
        """Verifies load_preferences merges system and user preference plists with user precedence."""
        sys_pref = Path('/fake/system.plist')
        user_pref = Path('/fake/user.plist')
        stored = {
            sys_pref:  plistlib.dumps({'schedule': '0 6 * * 1', 'no-mas': ['Xcode']}),
            user_pref: plistlib.dumps({'no-mas': ['Xcode', 'Logic Pro'], 'Debug': True}),
        }
        with patch.dict(
            'freshenmac.config.PATHS', {
                'Prefs': {
                    'Default': sys_pref,
                    'User':    user_pref,
                },
            },
        ), patch.object(
            Path, 'is_file', autospec=True, side_effect=lambda p: p in stored,
        ), patch.object(
            Path, 'read_bytes', autospec=True, side_effect=lambda p: stored[p],
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

        self.assertTrue(agent.activate())
        mock_logger.print_store.assert_called_with(
            f"[Dry-run] Would activate launchd service: {agent.label}",
            component='LaunchAgent',
        )

    def test_plist_needs_install(self):
        """Verifies plist_needs_install checks file existence, content matching, ownership, and mode."""
        agent = LaunchAgent(dry_run=True)
        agent.launch_agents_dir = Path('/fake/LaunchAgents')
        agent.target_script = Path('/fake/freshenmac')

        # Missing plist requires install
        with patch.object(Path, 'is_file', return_value=False):
            self.assertTrue(agent.plist_needs_install)

        # Plist exists with matching content and correct uid/mode -> does not need install
        plist_bytes = agent.build_plist_data()
        with patch.object(
            Path, 'is_file', return_value=True,
        ), patch.object(
            Path, 'read_bytes', return_value=plist_bytes,
        ), patch.object(
            Path, 'stat',
        ) as mock_stat:
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
        agent = LaunchAgent(dry_run=True)
        py_bin = agent.python_bin
        self.assertIsInstance(py_bin, str)
        self.assertTrue('python' in py_bin)

    def test_remove_script(self):
        """Verifies remove_script unloads active service and deletes target plist."""
        agent = LaunchAgent(dry_run=False)
        agent.launch_agents_dir = Path('/fake/LaunchAgents')
        with patch.object(
            LaunchAgent, 'is_active', new_callable=PropertyMock, return_value=True,
        ), patch.object(
            agent, 'bootout',
        ) as mock_bootout, patch.object(
            Path, 'is_file', return_value=True,
        ), patch.object(
            Path, 'unlink',
        ) as mock_unlink:
            self.assertTrue(agent.remove_script())
            mock_bootout.assert_called_once()
            mock_unlink.assert_called_once()

    def test_sudoers_lifecycle(self):
        """Verifies _remove_sudoers cleans up /etc/sudoers.d configuration."""
        agent = LaunchAgent(dry_run=False)
        executed_cmds = []

        def fake_runcmd(cmd, *args, **kwargs):
            executed_cmds.append(list(cmd))
            return make_cmd(errno=0)

        with patch(
            'freshenmac.plist.util.RunCMD', side_effect=fake_runcmd,
        ), patch(
            'freshenmac.util.RunCMD', side_effect=fake_runcmd,
        ):
            # Remove sudoers
            with patch('freshenmac.plist.Path.exists', return_value=True):
                self.assertTrue(agent._remove_sudoers())
                self.assertTrue(any(all(['rm' in c, 'sudoers' in ' '.join(c)]) for c in executed_cmds))

    def test_target_plist(self):
        """Verifies target_plist resolves to system LaunchAgents destination path."""
        agent = LaunchAgent(dry_run=True)
        expected = Path('/Library/LaunchAgents') / f"{DEFAULTS['plist']}.plist"
        self.assertEqual(agent.target_plist, expected)

    def test_target_script_resolution(self):
        """Verifies target_script resolves from PATHS['Script']['Target'] list and fallbacks."""
        cand1 = Path('/fake/missing_dir/freshenmac')
        cand2 = Path('/fake/existing_dir/freshenmac')
        src = Path('/fake/FreshenMac.py')

        # 1. Resolves to candidate whose parent directory exists
        with patch.dict(
            'freshenmac.config.PATHS', {'Script': {'Source': src, 'Target': [cand1, cand2]}},
        ), patch.object(
            Path, 'is_file', autospec=True, return_value=False,
        ), patch.object(
            Path, 'is_dir', autospec=True, side_effect=lambda p: p == cand2.parent,
        ):
            self.assertEqual(LaunchAgent(dry_run=True).target_script, cand2)

        # 2. Resolves to candidate file that exists
        with patch.dict(
            'freshenmac.config.PATHS', {'Script': {'Source': src, 'Target': [cand1, cand2]}},
        ), patch.object(
            Path, 'is_file', autospec=True, side_effect=lambda p: p == cand1,
        ):
            self.assertEqual(LaunchAgent(dry_run=True).target_script, cand1)

        # 3. Falls back to brew --prefix or Source when no target dir exists
        missing = Path('/fake/none/freshenmac')
        with patch.dict(
            'freshenmac.config.PATHS', {'Script': {'Source': src, 'Target': [missing]}},
        ), patch.object(
            Path, 'is_file', autospec=True, return_value=False,
        ), patch.object(
            Path, 'is_dir', autospec=True, return_value=False,
        ), patch(
            'freshenmac.plist.shutil.which', return_value=None,
        ), patch(
            'freshenmac.plist.util.RunCMD', return_value=make_cmd(stdout=''),
        ):
            self.assertEqual(LaunchAgent(dry_run=True).target_script, src)

    def test_uninstall(self):
        """Verifies uninstall unloads LaunchAgent, removes sudoers, and cleans up plist."""
        agent = LaunchAgent(dry_run=False)
        agent.launch_agents_dir = Path('/fake/LaunchAgents')

        with patch.object(
            LaunchAgent, 'is_active', new_callable=PropertyMock, return_value=True,
        ), patch.object(
            agent, 'bootout',
        ) as mock_bootout, patch.object(
            agent, '_remove_sudoers',
        ) as mock_remove_sudoers, patch(
            'freshenmac.plist.StartupRun',
        ), patch.object(
            Path, 'is_file', return_value=True,
        ), patch.object(
            Path, 'unlink',
        ) as mock_unlink:
            self.assertTrue(agent.uninstall())
            mock_bootout.assert_called_once()
            mock_remove_sudoers.assert_called_once()
            mock_unlink.assert_called_once()


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

    def test_default_logger(self):
        """Verifies default logger initialization on SavePreferences."""
        pref = SavePreferences(dry_run=True)
        self.assertIsInstance(pref.log, Logger)

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

    def test_print_help_uses_logger_printf(self):
        """Verifies print_help outputs documentation via configured logger."""
        mock_logger = MagicMock()
        pref = SavePreferences(dry_run=True, logger=mock_logger)
        pref.print_help()
        self.assertTrue(mock_logger.printf.called)

    def test_save_download_speed(self):
        """Verifies download-speed preference parses and normalizes correctly."""
        pref_speed = SavePreferences('download-speed=15.5', dry_run=True)
        self.assertEqual(pref_speed.updates, {'download-speed': 15.5})

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

    def test_default_logger(self):
        """Verifies default logger initialization on StartupRun."""
        startup = StartupRun(dry_run=True)
        self.assertIsInstance(startup.log, Logger)

    def test_init_with_custom_paths(self):
        """Verifies StartupRun accepts custom directories and plist paths."""
        custom_agents = Path('/tmp/custom_agents')
        custom_logs = Path('/tmp/custom_logs')
        custom_plist = Path('/tmp/custom.plist')
        agent = StartupRun(
            dry_run=True,
            launch_agents_dir=custom_agents,
            log_dir=custom_logs,
            target_plist=custom_plist,
        )
        self.assertEqual(agent.launch_agents_dir, custom_agents)
        self.assertEqual(agent.log_dir, custom_logs)
        self.assertEqual(agent.target_plist, custom_plist)

    def test_logger_integration(self):
        """Verifies custom logger propagation to underlying LaunchAgent in StartupRun."""
        mock_logger = MagicMock()
        startup = StartupRun(dry_run=True, logger=mock_logger)
        self.assertIs(startup.log, mock_logger)
        self.assertIs(startup.agent.log, mock_logger)

    def test_properties_delegation(self):
        """Verifies setting property wrappers updates underlying LaunchAgent instance."""
        agent = StartupRun(dry_run=True)
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
        self.assertEqual(agent.agent.target_plist, new_plist)

    def test_remove_missing_file_returns_true(self):
        """Verifies remove_script returns True when target plist does not exist."""
        agent = StartupRun(dry_run=True)
        agent.target_plist = Path('/nonexistent/path/runonce.plist')
        self.assertTrue(agent.agent.remove_script())

    def test_schedule_and_remove(self):
        """Verifies schedule creates RunAtLoad plist and remove_script deletes it."""
        agent = StartupRun(dry_run=False)
        agent.launch_agents_dir = Path('/fake/LaunchAgents')
        agent.log_dir = Path('/fake/Logs')
        agent.target_plist = agent.launch_agents_dir / f"{agent.label}.plist"
        stored: dict[Path, bytes] = {}

        def fake_write(a_path: Path, data: bytes, mode: int = 0o644) -> None:
            stored[a_path] = data

        with patch(
            'freshenmac.plist.util.RunCMD',
        ) as mock_cmd, patch(
            'freshenmac.plist.write_file', side_effect=fake_write,
        ), patch.object(
            agent.agent, '_init_log_files',
        ), patch.object(
            Path, 'is_file', autospec=True, side_effect=lambda p: p in stored,
        ), patch.object(
            Path, 'unlink', autospec=True, side_effect=lambda p, *args, **kwargs: stored.pop(p, None),
        ):
            mock_cmd.return_value.returncode = 0
            self.assertTrue(agent.schedule())
            self.assertIn(agent.target_plist, stored)

            plist_data = plistlib.loads(stored[agent.target_plist])
            self.assertEqual(plist_data['Label'], agent.label)
            self.assertTrue(plist_data['RunAtLoad'])

            # Test remove
            self.assertTrue(agent.agent.remove_script())
            self.assertNotIn(agent.target_plist, stored)

    def test_schedule_dry_run(self):
        """Verifies schedule returns True without creating files in dry run."""
        agent = StartupRun(dry_run=True)
        self.assertTrue(agent.schedule())

    def test_schedule_exception(self):
        """Verifies schedule handles filesystem exceptions gracefully by returning False."""
        agent = StartupRun(dry_run=False)
        with patch('freshenmac.plist.write_file', side_effect=PermissionError("denied")):
            self.assertFalse(agent.schedule())


if __name__ == '__main__':
    unittest.main()
