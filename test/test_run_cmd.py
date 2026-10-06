import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from freshenmac.config import PATHS
from freshenmac.util import (
    Logger,
    PlaySound,
    RunAppleScript,
    RunCMD,
    Runner,
    SudoKeepAlive,
    Version,
    get_log_file,
    parse_version,
)


class TestLogging(unittest.TestCase):
    """
    Unit tests for get_log_file, log_message, and Logger multi-stage fallback engine.

    Tests five-stage permission escalation and recovery:
    Stage 1: direct write to system log (/Library/Logs/FreshenMac/freshenmac.log)
    Stage 2a: sudo touch & chmod 666 recovery
    Stage 2b: sudo tee -a append recovery
    Stage 3: user directory fallback (~/Library/Logs/FreshenMac/freshenmac.log)
    Stage 4: emergency /var/tmp fallback
    Stage 5: graceful disable with warning
    """

    def test_disable_logger_prints_and_sets_enabled_false(self):
        """Verifies _disable_logger sets enabled=False and prints human-readable warning."""
        # Arrange
        logger = Logger()
        logger.enabled = True

        # Act & Assert
        with patch('builtins.print') as mock_print:
            logger._disable_logger(PermissionError("Denied"))
            self.assertFalse(logger.enabled)
            mock_print.assert_called_once()
            self.assertIn("Log write failed: Denied", mock_print.call_args[0][0])

    def test_get_log_file_system_and_user_fallbacks(self):
        """Verifies get_log_file checks write permissions to system log before falling back to user log."""
        # System log directory writable -> returns system log path
        with patch('pathlib.Path.exists', return_value=False), \
            patch('os.access', return_value=True):
            self.assertEqual(get_log_file(), PATHS['Log']['System'])

        # System log directory not writable -> falls back to user log path
        with patch('pathlib.Path.exists', return_value=False), \
            patch('os.access', return_value=False):
            self.assertEqual(get_log_file(), PATHS['Log']['User'])

    def test_line_append_returns_exception(self):
        """Verifies _line_append returns the caught Exception when write fails."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_log = Path(tmp_dir) / 'test.log'
            logger = Logger(log_file=test_log)
            with patch('builtins.open', side_effect=PermissionError("Denied")):
                res = logger._line_append("Failing line")
                self.assertIsInstance(res, PermissionError)

    def test_line_append_success(self):
        """Verifies _line_append appends formatted line and returns None on success."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_log = Path(tmp_dir) / 'test.log'
            logger = Logger(log_file=test_log)
            res = logger._line_append("Success line")
            self.assertIsNone(res)
            self.assertEqual(test_log.read_text(encoding='utf-8'), "Success line\n")

    def test_logger_dry_run_skips_file_write(self):
        """Verifies Logger instance ignores store calls when dry_run is True."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_log = Path(tmp_dir) / 'test.log'
            logger = Logger(dry_run=True, log_file=test_log)
            self.assertFalse(logger.enabled)
            logger.store("Dry run test", component='TestComp')
            self.assertFalse(test_log.exists())

    def test_logger_is_disabled_for_default_log_under_unittest(self):
        """Verifies Logger disables writing to the default system log during unit test runs."""
        logger = Logger()
        self.assertFalse(logger.enabled)

    def test_logger_permission_fallback(self):
        """Verifies Logger falls back to user log directory when system log gets PermissionError."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            sys_log = Path(tmp_dir) / 'sys' / 'test.log'
            user_log = Path(tmp_dir) / 'user' / 'test.log'
            with patch.dict(PATHS['Log'], {'User': user_log}):
                logger = Logger(log_file=sys_log)
                real_open = open

                def mock_open(path, mode='r', **kwargs):
                    if Path(path) == sys_log:
                        raise PermissionError("Permission denied")
                    return real_open(path, mode, **kwargs)

                with patch('builtins.open', side_effect=mock_open):
                    logger.store("Fallback message", component='PermTest')

                # Log redirected to user log
                self.assertEqual(logger.target, user_log)
                self.assertTrue(user_log.exists())
                self.assertIn('[PermTest] Fallback message', user_log.read_text(encoding='utf-8'))

    def test_logger_print_outputs_to_stdout(self):
        """Verifies logger.print outputs to standard stdout via builtins.print."""
        logger = Logger()
        with patch('builtins.print') as mock_print:
            logger.print("Screen output message")
            mock_print.assert_called_once_with("Screen output message")

    def test_logger_print_store_calls_print_and_store(self):
        """Verifies logger.print_store outputs to stdout and persists to log file."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_log = Path(tmp_dir) / 'test.log'
            logger = Logger(log_file=test_log)
            with patch('builtins.print') as mock_print:
                logger.print_store("Combined message", component='Combo')
                mock_print.assert_called_once_with("Combined message")
            self.assertTrue(test_log.exists())
            self.assertIn('[Combo] Combined message', test_log.read_text(encoding='utf-8'))

    def test_logger_stage2a_sudo_chmod_recovery(self):
        """Verifies stage 2a recovery attempts sudo touch && chmod 666 when write permission denied."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            sys_log = Path(tmp_dir) / 'sys.log'
            logger = Logger(log_file=sys_log)
            call_count = 0

            def fake_append(line):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return PermissionError("Permission denied")
                return None

            with patch.object(RunCMD, 'can_sudo', return_value=True), \
                patch('freshenmac.util.RunCMD') as mock_run_cmd, \
                patch.object(logger, '_line_append', side_effect=fake_append):
                # Act
                logger._write_line("Privileged line")

                # Assert: executed touch && chmod with root privileges
                mock_run_cmd.assert_called_with(
                    ['sh', '-c', f"touch '{sys_log}' && chmod 666 '{sys_log}'"],
                    need_root=True,
                )
                self.assertEqual(call_count, 2)
                self.assertEqual(logger.target, sys_log)
                self.assertTrue(logger.enabled)

    def test_logger_stage2b_sudo_tee_append(self):
        """Verifies stage 2b recovery pipes input to sudo tee -a if chmod 666 fails."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            sys_log = Path(tmp_dir) / 'sys.log'
            logger = Logger(log_file=sys_log)
            mock_cmd = MagicMock(returncode=0)

            with patch.object(RunCMD, 'can_sudo', return_value=True), \
                patch('freshenmac.util.RunCMD', return_value=mock_cmd) as mock_run_cmd, \
                patch.object(logger, '_line_append', return_value=PermissionError("Permission denied")):
                # Act
                logger._write_line("Tee line")

                # Assert: piped line to sudo tee -a
                mock_run_cmd.assert_any_call(
                    ['tee', '-a', str(sys_log)],
                    to_input="Tee line\n",
                    need_root=True,
                )
                self.assertEqual(logger.target, sys_log)
                self.assertTrue(logger.enabled)

    def test_logger_stage4_var_tmp_fallback(self):
        """Verifies stage 4 emergency fallback redirects to /var/tmp when user directory is inaccessible."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            sys_log = Path(tmp_dir) / 'sys.log'
            user_log = Path(tmp_dir) / 'user.log'
            var_tmp_log = Path('/var/tmp') / user_log.name
            logger = Logger(log_file=sys_log)

            def fake_append(line):
                if logger.target != var_tmp_log:
                    return PermissionError("Permission denied")
                return None

            with patch.object(RunCMD, 'can_sudo', return_value=False), \
                patch.dict(PATHS['Log'], {'User': user_log}), \
                patch.object(logger, '_line_append', side_effect=fake_append):
                # Act
                logger._write_line("Emergency message")

                # Assert: redirected to /var/tmp
                self.assertEqual(logger.target, var_tmp_log)
                self.assertTrue(logger.enabled)

    def test_logger_stage5_disable_logging(self):
        """Verifies stage 5 disables logger when all fallback directories fail."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_log = Path(tmp_dir) / 'test.log'
            logger = Logger(log_file=test_log)
            with patch.object(RunCMD, 'can_sudo', return_value=False), \
                patch.object(logger, '_line_append', return_value=PermissionError("Permission denied")), \
                patch.object(logger, '_disable_logger') as mock_disable:
                # Act
                logger._write_line("Failing all stages")

                # Assert: disabled cleanly
                mock_disable.assert_called_once()

    def test_logger_store_writes_formatted_lines(self):
        """Verifies logger.store writes timestamped and component-tagged lines."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_log = Path(tmp_dir) / 'test.log'
            logger = Logger(log_file=test_log)
            logger.store("Line A\nLine B", component='StoreComp')
            self.assertTrue(test_log.exists())
            content = test_log.read_text(encoding='utf-8')
            self.assertIn('[StoreComp] Line A', content)
            self.assertIn('[StoreComp] Line B', content)


class TestPlaySound(unittest.TestCase):
    """
    Unit tests for PlaySound audio alert wrapper.

    Tests system sound normalization, afplay execution, and context manager support.
    """

    def test_context_manager(self):
        """Verifies PlaySound context manager plays chime on block exit."""
        with patch('freshenmac.util.RunCMD') as mock_cmd:
            mock_cmd.return_value.__bool__.return_value = True
            with PlaySound('Pop.aiff') as ps:
                self.assertIsInstance(ps, PlaySound)
            mock_cmd.assert_called_with(['afplay', ps.sound], interactive=True)

    def test_normalize_file_adds_sound_dir(self):
        """Verifies PlaySound resolves standard macOS sound names to /System/Library/Sounds/."""
        with patch('freshenmac.util.RunCMD') as mock_cmd:
            mock_cmd.return_value.__bool__.return_value = True
            with patch('os.path.isfile', return_value=True):
                ps = PlaySound('Ping.aiff')
                self.assertEqual(ps.sound, '/System/Library/Sounds/Ping.aiff')

    def test_pick_sound_fallback(self):
        """Verifies PlaySound falls back to DEFAULT_SOUND when given file does not exist."""
        ps = PlaySound.__new__(PlaySound)
        ps.sound = '/nonexistent/sound.aiff'
        ps.sound_list = []
        with patch('os.path.isfile', return_value=False), \
            patch('freshenmac.util.RunCMD') as mock_cmd:
            mock_cmd.return_value.__bool__.return_value = False
            self.assertEqual(ps._normalize_file(), PlaySound.DEFAULT_SOUND)

        with patch('os.path.isfile', return_value=True), \
            patch('freshenmac.util.RunCMD') as mock_cmd:
            mock_cmd.return_value.__bool__.return_value = True
            ps.sound = '/custom/sound.aiff'
            self.assertEqual(ps._normalize_file(), '/custom/sound.aiff')

    def test_play_delegates_to_run_cmd(self):
        """Verifies PlaySound.play executes afplay via RunCMD with interactive=True."""
        with patch('freshenmac.util.RunCMD') as mock_cmd:
            mock_cmd.return_value.__bool__.return_value = True
            with patch('os.path.isfile', return_value=True):
                ps = PlaySound('Pop.aiff')
                ps.play()
                mock_cmd.assert_called_with(['afplay', ps.sound], interactive=True)


class TestRunAppleScript(unittest.TestCase):
    """
    Unit tests for RunAppleScript executor.

    Tests background osascript dispatch, string membership testing (__contains__),
    debug output limits, script dedenting, and helper classmethods (.dialog and .notify).
    """

    def test_background_run(self):
        """Verifies background=True invokes subprocess.Popen without blocking."""
        with patch('subprocess.Popen') as mock_popen, patch('subprocess.run') as mock_run:
            ascript = RunAppleScript('display notification "test"', background=True)
            mock_popen.assert_called_once_with(['osascript', '-e', 'display notification "test"'])
            mock_run.assert_not_called()
            self.assertTrue(bool(ascript))
            self.assertEqual(ascript.returncode, 0)

    def test_contains_operator(self):
        """Verifies string membership testing directly on RunAppleScript instance."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=['osascript'], returncode=0, stdout='button returned:Restart Now\n', stderr='',
            )
            ascript = RunAppleScript('prompt')
            self.assertIn('button returned:Restart Now', ascript)
            self.assertNotIn('button returned:Snooze', ascript)

    def test_debug_limit_default_and_custom(self):
        """Verifies debug_limit limits output truncation when debug logging is active."""
        with patch('subprocess.run') as mock_run, patch('builtins.print'):
            mock_run.return_value = subprocess.CompletedProcess(
                args=['osascript'], returncode=0, stdout='a' * 15000, stderr='',
            )
            # Default debug_limit=0 means unlimited (None)
            ascript_default = RunAppleScript('test', debug=2)
            self.assertIsNone(ascript_default.debug_limit)

            # Custom positive debug_limit
            ascript_custom = RunAppleScript('test', debug=2, debug_limit=50)
            self.assertEqual(ascript_custom.debug_limit, 50)

            # Zero or negative debug_limit sets self.debug_limit to None (unlimited)
            ascript_unlimited = RunAppleScript('test', debug=2, debug_limit=-1)
            self.assertIsNone(ascript_unlimited.debug_limit)

    def test_debug_output(self):
        """Verifies debug=2 prints stdout and stderr to console."""
        with patch('subprocess.run') as mock_run, patch('builtins.print') as mock_print:
            mock_run.return_value = subprocess.CompletedProcess(
                args=['osascript'], returncode=0, stdout='debug_val', stderr='',
            )
            RunAppleScript('return "debug_val"', debug=2)
            self.assertGreaterEqual(mock_print.call_count, 2)

    def test_dedent_and_strip(self):
        """Verifies multi-line scripts are dedented and stripped of leading/trailing whitespace."""
        raw = """
            tell application "System Events"
                return "hello"
            end tell
        """
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=['osascript'], returncode=0, stdout='hello\n', stderr='',
            )
            ascript = RunAppleScript(raw)
            expected = 'tell application "System Events"\n    return "hello"\nend tell'
            self.assertEqual(ascript.script, expected)

    def test_dialog_classmethod(self):
        """Verifies RunAppleScript.dialog constructs System Events display dialog script."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=['osascript'], returncode=0, stdout='button returned:OK, gave up:false\n', stderr='',
            )
            dlg = RunAppleScript.dialog(
                "System update ready",
                buttons=['Cancel', 'OK'],
                default_button='OK',
                giving_up_after=30,
                icon='stop',
                title='FreshenMac',
            )
            expected = (
                'tell application "System Events"\n'
                '    activate\n'
                '    display dialog "System update ready" buttons {"Cancel", "OK"} '
                'default button "OK" giving up after 30 with icon stop with title "FreshenMac"\n'
                'end tell'
            )
            self.assertEqual(dlg.script, expected)
            self.assertEqual(dlg.timeout, 40)
            self.assertTrue(bool(dlg))

    def test_list_script_input(self):
        """Verifies list of script lines is joined with newlines."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=['osascript'], returncode=0, stdout='ok\n', stderr='',
            )
            ascript = RunAppleScript(['tell application "Finder"', 'activate', 'end tell'])
            self.assertEqual(ascript.script, 'tell application "Finder"\nactivate\nend tell')

    def test_notify_classmethod(self):
        """Verifies RunAppleScript.notify constructs display notification script."""
        with patch('subprocess.Popen') as mock_popen:
            res = RunAppleScript.notify(
                "Maintenance complete",
                subtitle="All tasks passed",
                title="FreshenMac",
            )
            expected = 'display notification "Maintenance complete" with title "FreshenMac" subtitle "All tasks passed"'
            self.assertEqual(res.script, expected)
            mock_popen.assert_called_once_with(['osascript', '-e', expected])
            self.assertTrue(res.background)

    def test_sync_run_exception(self):
        """Verifies OSError during execution returns returncode -3 and sets error attribute."""
        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = OSError("Exec format error")
            ascript = RunAppleScript('broken')
            self.assertFalse(bool(ascript))
            self.assertEqual(ascript.returncode, -3)
            self.assertIsInstance(ascript.error, OSError)
            self.assertIn("Exec format error", ascript.stderr)

    def test_sync_run_script_error(self):
        """Verifies non-zero exit code returns truthiness False and captures stderr."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=['osascript'], returncode=1, stdout='', stderr='syntax error',
            )
            ascript = RunAppleScript('bad script')
            self.assertFalse(bool(ascript))
            self.assertEqual(ascript.returncode, 1)
            self.assertEqual(ascript.stderr, 'syntax error')

    def test_sync_run_success(self):
        """Verifies synchronous execution captures stdout, sets returncode 0, and returns truthiness True."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=['osascript'], returncode=0, stdout='42\n', stderr='',
            )
            ascript = RunAppleScript('return 42', timeout=10)
            self.assertEqual(ascript.stdout, '42')
            self.assertEqual(ascript.stderr, '')
            self.assertEqual(ascript.returncode, 0)
            self.assertTrue(bool(ascript))
            self.assertIsNone(ascript.error)
            self.assertEqual(str(ascript), '42')
            self.assertEqual(repr(ascript), '42')
            mock_run.assert_called_once_with(
                ['osascript', '-e', 'return 42'],
                capture_output=True,
                text=True,
                timeout=10,
            )

    def test_sync_run_timeout(self):
        """Verifies TimeoutExpired returns returncode -2 and records timeout in stderr."""
        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd=['osascript'], timeout=5)
            ascript = RunAppleScript('delay 10', timeout=5)
            self.assertFalse(bool(ascript))
            self.assertEqual(ascript.returncode, -2)
            self.assertIsInstance(ascript.error, subprocess.TimeoutExpired)
            self.assertIn('timed out', ascript.stderr)


class TestRunCMD(unittest.TestCase):
    """
    Unit tests for RunCMD subprocess wrapper.

    Tests command execution, sequence protocols (len, indexing, slicing),
    key-value dictionary parsing, cancelable task handling (SIGINT / errno 130),
    taskpolicy QoS clamping, interactive mode, and root elevation (need_root / can_sudo).
    """

    def test_basic_command_execution(self):
        """Verifies basic command execution, exit status, strip(), and string membership."""
        cmd = RunCMD(['echo', 'hello world'])
        self.assertTrue(bool(cmd))
        self.assertEqual(cmd.returncode, 0)
        self.assertEqual(cmd.strip(), 'hello world')
        self.assertIn('hello', cmd)

    def test_can_sudo(self):
        """Verifies can_sudo checks euid == 0 or tests sudo -n true."""
        # When running as root (euid == 0), can_sudo returns True immediately
        with patch('os.geteuid', return_value=0):
            self.assertTrue(RunCMD.can_sudo())

        # When running as non-root (euid == 501), delegates to RunCMD(['true'], need_root=True)
        with patch('os.geteuid', return_value=501):
            with patch('subprocess.run') as mock_sub:
                mock_sub.return_value.returncode = 0
                mock_sub.return_value.stdout = ''
                mock_sub.return_value.stderr = ''
                self.assertTrue(RunCMD.can_sudo())
                mock_sub.assert_called_once()
                args, _ = mock_sub.call_args
                self.assertEqual(args[0], ['sudo', '-n', 'true'])

            with patch('subprocess.run') as mock_sub:
                mock_sub.return_value.returncode = 1
                mock_sub.return_value.stdout = ''
                mock_sub.return_value.stderr = 'password required'
                self.assertFalse(RunCMD.can_sudo())
                mock_sub.assert_called_once()
                args, _ = mock_sub.call_args
                self.assertEqual(args[0], ['sudo', '-n', 'true'])

    def test_cancelable_keyboard_interrupt_sets_errno_130(self):
        """Verifies cancelable=True intercepts KeyboardInterrupt and sets returncode 130."""
        with patch('subprocess.run', side_effect=KeyboardInterrupt):
            cmd = RunCMD(['long_running_task'], timeout=1800, cancelable=True)
            self.assertFalse(bool(cmd))
            self.assertEqual(cmd.returncode, 130)
            self.assertTrue(cmd.cancelled)
            self.assertIn('cancelled by user', cmd.stderr)

    def test_debug_limit_default_and_custom(self):
        """Verifies debug_limit clamps verbose command output printing."""
        with patch('subprocess.run') as mock_sub, patch('builtins.print'):
            mock_sub.return_value.returncode = 0
            mock_sub.return_value.stdout = "a" * 15000
            mock_sub.return_value.stderr = "b" * 15000

            # Default debug_limit=0 means unlimited (None)
            cmd_default = RunCMD(['test'], debug=True)
            self.assertIsNone(cmd_default.debug_limit)

            # Custom positive debug_limit
            cmd_custom = RunCMD(['test'], debug=True, debug_limit=50)
            self.assertEqual(cmd_custom.debug_limit, 50)

            # Zero or negative debug_limit sets self.debug_limit to None (unlimited)
            cmd_unlimited = RunCMD(['test'], debug=True, debug_limit=-1)
            self.assertIsNone(cmd_unlimited.debug_limit)

    def test_default_cancelable_logic(self):
        """Verifies commands with timeout >= 1800 default to cancelable=True."""
        with patch('subprocess.run') as mock_sub:
            mock_sub.return_value.returncode = 0
            mock_sub.return_value.stdout = ''
            mock_sub.return_value.stderr = ''
            c1 = RunCMD(['echo', 'hi'], timeout=1800)
            self.assertTrue(c1.cancelable)
            c2 = RunCMD(['echo', 'hi'], timeout=10)
            self.assertFalse(c2.cancelable)

    def test_failed_command_bool(self):
        """Verifies failed command returns truthiness False."""
        cmd = RunCMD(['false'])
        self.assertFalse(bool(cmd))
        self.assertNotEqual(cmd.returncode, 0)

    def test_interactive_mode(self):
        """Verifies interactive=True disables capture_output to allow direct user I/O."""
        with patch('subprocess.run') as mock_sub:
            mock_sub.return_value.returncode = 0
            mock_sub.return_value.stdout = None
            mock_sub.return_value.stderr = None
            cmd = RunCMD(['echo', 'test'], interactive=True)
            self.assertTrue(cmd.interactive)
            mock_sub.assert_called_once()
            _, kwargs = mock_sub.call_args
            self.assertFalse(kwargs.get('capture_output'))
            self.assertEqual(cmd.stdout, '')
            self.assertEqual(cmd.stderr, '')

    def test_need_root_interactive(self):
        """Verifies need_root=True with interactive=True prepends sudo without -n flag."""
        with patch('os.geteuid', return_value=501):
            with patch('subprocess.run') as mock_sub:
                mock_sub.return_value.returncode = 0
                mock_sub.return_value.stdout = None
                mock_sub.return_value.stderr = None
                cmd = RunCMD(['echo', 'test'], need_root=True, interactive=True)
                mock_sub.assert_called_once()
                args, _ = mock_sub.call_args
                self.assertEqual(args[0], ['sudo', 'echo', 'test'])

    def test_need_root_non_interactive(self):
        """Verifies need_root=True in non-interactive mode prepends sudo -n."""
        with patch('os.geteuid', return_value=501):
            with patch('subprocess.run') as mock_sub:
                mock_sub.return_value.returncode = 0
                mock_sub.return_value.stdout = 'output'
                mock_sub.return_value.stderr = ''
                cmd = RunCMD(['echo', 'test'], need_root=True, interactive=False)
                mock_sub.assert_called_once()
                args, _ = mock_sub.call_args
                self.assertEqual(args[0], ['sudo', '-n', 'echo', 'test'])

    def test_need_root_when_already_root(self):
        """Verifies need_root=True does not prepend sudo when already running as root."""
        with patch('os.geteuid', return_value=0):
            with patch('subprocess.run') as mock_sub:
                mock_sub.return_value.returncode = 0
                mock_sub.return_value.stdout = 'output'
                mock_sub.return_value.stderr = ''
                cmd = RunCMD(['echo', 'test'], need_root=True)
                mock_sub.assert_called_once()
                args, _ = mock_sub.call_args
                self.assertEqual(args[0], ['echo', 'test'])

    def test_non_cancelable_keyboard_interrupt_raises(self):
        """Verifies cancelable=False re-raises KeyboardInterrupt."""
        with patch('subprocess.run', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                RunCMD(['critical_task'], timeout=10, cancelable=False)

    def test_parse_dict(self):
        """Verifies parse_dict parses key-value delimiter lines with duplicate disambiguation."""
        sample_output = "Name: MacBook Pro\nModel: Mac14,2\nStatus: OK\nStatus: Second"
        cmd = RunCMD.__new__(RunCMD)
        cmd.stdout = sample_output
        cmd.stderr = ''
        cmd.returncode = 0
        cmd.debug = False

        parsed = cmd.parse_dict(delimiter=':')
        self.assertEqual(parsed.get('Name'), 'MacBook Pro')
        self.assertEqual(parsed.get('Model'), 'Mac14,2')
        self.assertEqual(parsed.get('Status'), 'OK')
        self.assertEqual(parsed.get('Status 0002'), 'Second')

    def test_prepend(self):
        """Verifies _prepend prepends prefix tokens to list and string command representations."""
        cmd = RunCMD(['echo', 'hello'])
        self.assertEqual(cmd._prepend('sudo'), ['sudo', 'echo', 'hello'])
        self.assertEqual(
            cmd._prepend(['taskpolicy', '-c', 'utility']),
            ['taskpolicy', '-c', 'utility', 'echo', 'hello'],
        )

        cmd_shell = RunCMD(['echo hello'], need_shell=True)
        self.assertEqual(cmd_shell._prepend('sudo'), ['sudo echo hello'])

    def test_splitlines_and_len(self):
        """Verifies RunCMD sequence indexing and splitlines length operations."""
        cmd = RunCMD(['printf', 'line1\nline2\nline3\n'])
        self.assertEqual(len(cmd), 3)
        self.assertEqual(cmd[0], 'line1')
        self.assertEqual(cmd[1], 'line2')
        self.assertEqual(cmd[2], 'line3')
        self.assertEqual(list(cmd), ['line1', 'line2', 'line3'])

    def test_std_all(self):
        """Verifies std_all joins stdout and stderr with double newlines."""
        cmd = RunCMD.__new__(RunCMD)
        cmd.stdout = 'line 1\n'
        cmd.stderr = 'error 1\n'
        self.assertEqual(cmd.std_all, 'line 1\n\nerror 1')

        cmd.stdout = 'line 1\n'
        cmd.stderr = ''
        self.assertEqual(cmd.std_all, 'line 1')

        cmd.stdout = ''
        cmd.stderr = 'error 1\n'
        self.assertEqual(cmd.std_all, 'error 1')

        cmd.stdout = ''
        cmd.stderr = ''
        self.assertEqual(cmd.std_all, '')

    def test_taskpolicy_prepends_clamp(self):
        """Verifies taskpolicy prepends taskpolicy -c <policy> to command arguments."""
        with patch('subprocess.run') as mock_sub:
            mock_sub.return_value.returncode = 0
            mock_sub.return_value.stdout = 'ok'
            mock_sub.return_value.stderr = ''

            # Valid QoS policies
            for clamp in ('utility', 'background', 'maintenance'):
                cmd = RunCMD(['brew', 'upgrade'], taskpolicy=clamp)
                self.assertEqual(cmd.cmd_args[:3], ['taskpolicy', '-c', clamp])

            # None or unsupported policy does not prepend
            cmd_none = RunCMD(['brew', 'upgrade'], taskpolicy=None)
            self.assertEqual(cmd_none.cmd_args, ['brew', 'upgrade'])

    def test_taskpolicy_real_execution(self):
        """Verifies real execution with taskpolicy='utility' succeeds."""
        cmd = RunCMD(['echo', 'qos test'], taskpolicy='utility')
        self.assertTrue(bool(cmd))
        self.assertEqual(cmd.returncode, 0)
        self.assertEqual(cmd.strip(), 'qos test')

    def test_timeout_handling(self):
        """Verifies TimeoutExpired sets returncode -2 and records timeout message."""
        with patch('subprocess.run', side_effect=subprocess.TimeoutExpired(cmd=['sleep', '5'], timeout=1)):
            cmd = RunCMD(['sleep', '5'], timeout=1)
            self.assertFalse(bool(cmd))
            self.assertEqual(cmd.returncode, -2)
            self.assertIn('timed out', cmd.stderr)


class TestRunner(unittest.TestCase):
    """
    Unit tests for Runner concurrency lifecycle manager.

    Tests background thread execution, conditional start flags, graceful join,
    and context manager integration.
    """

    def test_context_manager(self):
        """Verifies Runner context manager starts background thread on enter and stops on exit."""
        with patch.object(Runner, 'start') as mock_start, \
            patch.object(Runner, 'stop') as mock_stop:
            with Runner(target=lambda: None) as runner:
                self.assertIsInstance(runner, Runner)
                mock_start.assert_called_once()
                mock_stop.assert_not_called()
            mock_stop.assert_called_once()

    def test_custom_args_and_kwargs(self):
        """Verifies Runner passes positional and keyword arguments to target function."""
        result = []

        def worker(val, *, multiplier=1):
            result.append(val * multiplier)

        runner = Runner(target=worker, args=(5,), kwargs={'multiplier': 3})
        runner.start()
        runner.stop()
        self.assertEqual(result, [15])

    def test_is_alive_property(self):
        """Verifies is_alive reflects the state of the active worker thread."""
        runner = Runner(target=lambda: None)
        self.assertFalse(runner.is_alive)
        with patch('threading.Thread') as mock_thread_cls:
            mock_thread = mock_thread_cls.return_value
            mock_thread.is_alive.return_value = True
            runner.start()
            self.assertTrue(runner.is_alive)
            runner.stop()
            self.assertFalse(runner.is_alive)

    def test_start_skipped_when_bool_condition_false(self):
        """Verifies thread start is skipped when condition is False."""
        executed = []
        runner = Runner(target=lambda: executed.append(True), condition=False)
        runner.start()
        self.assertIsNone(runner._thread)
        self.assertFalse(runner.is_alive)
        runner.stop()
        self.assertEqual(executed, [])

    def test_start_skipped_when_callable_condition_false(self):
        """Verifies thread start is skipped when callable condition returns False."""
        executed = []
        runner = Runner(target=lambda: executed.append(True), condition=lambda: False)
        runner.start()
        self.assertIsNone(runner._thread)
        self.assertFalse(runner.is_alive)
        runner.stop()
        self.assertEqual(executed, [])

    def test_start_when_bool_condition_true(self):
        """Verifies thread starts when condition is True."""
        executed = []
        runner = Runner(target=lambda: executed.append(True), condition=True)
        runner.start()
        runner.stop()
        self.assertEqual(executed, [True])

    def test_start_when_callable_condition_true(self):
        """Verifies thread starts when callable condition returns True."""
        executed = []
        runner = Runner(target=lambda: executed.append(True), condition=lambda: True)
        runner.start()
        runner.stop()
        self.assertEqual(executed, [True])

    def test_stop_joins_thread_gracefully(self):
        """Verifies stop joins active thread within join_timeout."""
        runner = Runner(join_timeout=0.5)
        with patch('threading.Thread') as mock_thread_cls:
            mock_thread = mock_thread_cls.return_value
            mock_thread.is_alive.return_value = True
            runner.start()
            runner.stop()
            mock_thread.join.assert_called_once_with(timeout=0.5)
            self.assertIsNone(runner._thread)

    def test_subclass_default_run(self):
        """Verifies custom subclasses can override _run() without specifying a target."""
        executed = []

        class CustomRunner(Runner):
            def _run(self):
                executed.append('custom')

        runner = CustomRunner()
        runner.start()
        runner.stop()
        self.assertEqual(executed, ['custom'])


class TestSudoKeepAlive(unittest.TestCase):
    """
    Unit tests for SudoKeepAlive background elevation refresher.

    Tests repeating sudo -n -v refresh loop and thread lifecycle.
    """

    def test_context_manager(self):
        """Verifies SudoKeepAlive starts background thread on enter and terminates on exit."""
        with patch.object(SudoKeepAlive, 'start') as mock_start, \
            patch.object(SudoKeepAlive, 'stop') as mock_stop:
            with SudoKeepAlive(interval=10) as keepalive:
                self.assertIsInstance(keepalive, SudoKeepAlive)
                mock_start.assert_called_once()
                mock_stop.assert_not_called()
            mock_stop.assert_called_once()

    def test_keepalive_refreshes_sudo(self):
        """Verifies _keepalive executes sudo -n -v when can_sudo is True."""
        keepalive = SudoKeepAlive(interval=1)
        with patch.object(keepalive._stop_event, 'wait', side_effect=[False, True]), \
            patch('freshenmac.util.RunCMD') as mock_cmd:
            mock_cmd.can_sudo.return_value = True
            keepalive._keepalive()
            mock_cmd.assert_called_once_with(['sudo', '-n', '-v'])

    def test_keepalive_skips_refresh_when_cannot_sudo(self):
        """Verifies _keepalive skips command execution when can_sudo is False."""
        keepalive = SudoKeepAlive(interval=1)
        with patch.object(keepalive._stop_event, 'wait', side_effect=[False, True]), \
            patch('freshenmac.util.RunCMD') as mock_cmd:
            mock_cmd.can_sudo.return_value = False
            keepalive._keepalive()
            mock_cmd.assert_not_called()

    def test_start_and_stop(self):
        """Verifies start spawns background worker thread and stop terminates it cleanly."""
        keepalive = SudoKeepAlive(interval=1)
        keepalive.start()
        self.assertIsNotNone(keepalive._thread)
        self.assertTrue(keepalive._thread.is_alive())
        keepalive.stop()
        self.assertIsNone(keepalive._thread)


class TestVersion(unittest.TestCase):
    """
    Unit tests for Version container, semantic parsing, and natural sorting.

    Tests prefix handling ('v1.2.3'), pre-releases (alpha, beta, rc), post-releases,
    patches, cross-string comparisons, and set deduplication.
    """

    def test_comparisons_and_equality(self):
        """Verifies equality and ordering across release stages (rc, alpha, beta, patch)."""
        self.assertEqual(Version.parse('1.0'), Version.parse('1.0.0'))
        self.assertEqual(Version.parse('1.0.0'), Version.parse('1.0.0.0'))
        self.assertEqual(Version.parse('1.0.0-final'), Version.parse('1.0.0'))
        self.assertLess(Version.parse('1.0.0-rc1'), Version.parse('1.0.0'))
        self.assertLess(Version.parse('1.0.0-rc1'), Version.parse('1.0'))
        self.assertLess(Version.parse('1.0.0-rc1'), Version.parse('1.0.0-rc2'))
        self.assertLess(Version.parse('1.0.0-alpha'), Version.parse('1.0.0-beta'))
        self.assertLess(Version.parse('1.0.0-beta'), Version.parse('1.0.0-rc1'))
        self.assertLess(Version.parse('1.0.0'), Version.parse('1.0.0-patch1'))
        self.assertLess(Version.parse('1.0.0-patch1'), Version.parse('1.0.1'))
        self.assertGreater(Version.parse('1.1.0-rc1'), Version.parse('1.0.0'))
        self.assertGreater(Version.parse('2.0.0'), Version.parse('1.9.9'))

    def test_parse_leading_v(self):
        """Verifies Version.parse strips leading 'v' or 'V' prefixes."""
        self.assertEqual(Version.parse('v1.2.3').parts, (1, 2, 3))
        self.assertEqual(Version.parse('V2.0').parts, (2, 0))
        self.assertEqual(Version.parse('v1.2.3'), Version.parse('1.2.3'))

    def test_parse_numeric(self):
        """Verifies parsing standard dot-separated integer strings."""
        self.assertEqual(Version.parse('1.0.0').parts, (1, 0, 0))
        self.assertEqual(Version.parse('15.2.1').parts, (15, 2, 1))

    def test_parse_post_releases_and_patches(self):
        """Verifies parsing post-release suffixes (-final, -post, -patch, -sp)."""
        self.assertEqual(Version.parse('1.0.0-final').parts, (1, 0, 0, 1510))
        self.assertEqual(Version.parse('1.0.0-post1').parts, (1, 0, 0, 1600, 1))
        self.assertEqual(Version.parse('1.0.0-patch1').parts, (1, 0, 0, 1700, 1))
        self.assertEqual(Version.parse('1.0.0-sp1').parts, (1, 0, 0, 1800, 1))

    def test_parse_pre_releases(self):
        """Verifies parsing pre-release suffixes (-rc, -alpha, -beta, -dev, -a)."""
        self.assertEqual(Version.parse('1.0.0-rc1').parts, (1, 0, 0, -4, 1))
        self.assertEqual(Version.parse('1.0.0-rc').parts, (1, 0, 0, -4))
        self.assertEqual(Version.parse('1.0.0-alpha').parts, (1, 0, 0, -8))
        self.assertEqual(Version.parse('1.0.0-beta').parts, (1, 0, 0, -7))
        self.assertEqual(Version.parse('1.0.0-dev1').parts, (1, 0, 0, -9, 1))
        self.assertEqual(Version.parse('1.0.0-a').parts, (1, 0, 0, -97))
        self.assertEqual(Version.parse('1.0.0-a1').parts, (1, 0, 0, -8, 1))

    def test_parse_version_helper(self):
        """Verifies parse_version helper delegates to Version.parse."""
        self.assertEqual(parse_version('1.0.0'), Version.parse('1.0.0'))

    def test_str_and_bool_and_hash(self):
        """Verifies str representation, boolean truthiness, and hash compatibility with sets."""
        self.assertTrue(bool(Version.parse('1.0')))
        self.assertFalse(bool(Version.parse('')))
        self.assertTrue(bool(Version.parse(None)))
        self.assertEqual(str(Version.parse('1.0.0-rc1')), '1.0.0-rc1')
        self.assertEqual(hash(Version.parse('1.0')), hash(Version.parse('1.0.0')))
        self.assertEqual(len({Version.parse('1.0'), Version.parse('1.0.0')}), 1)

    def test_string_cross_comparison(self):
        """Verifies Version objects compare directly against version string literals."""
        self.assertEqual(Version.parse('1.0.0'), '1.0.0')
        self.assertEqual(Version.parse('1.0'), '1.0.0')
        self.assertLess(Version.parse('1.0.0-rc1'), '1.0.0')
        self.assertLess(Version.parse('1.0.0'), '1.0.0-patch1')


if __name__ == '__main__':
    unittest.main()
