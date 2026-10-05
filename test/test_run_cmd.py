import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from freshenmac.config import PATHS
from freshenmac.util import (
    Logger,
    PlaySound,
    PrivilegedCMD,
    RunAppleScript,
    RunCMD,
    Runner,
    SudoKeepAlive,
    Version,
    get_log_file,
    log_message,
    parse_version,
)


class TestPlaySound(unittest.TestCase):
    """Unit tests for PlaySound audio notification wrapper."""

    def test_context_manager(self):
        with patch('freshenmac.util.RunCMD') as mock_cmd:
            mock_cmd.return_value.__bool__.return_value = True
            with PlaySound('Pop.aiff') as ps:
                self.assertIsInstance(ps, PlaySound)
            mock_cmd.assert_called_with(['afplay', ps.sound], interactive=True)

    def test_normalize_file_adds_sound_dir(self):
        with patch('freshenmac.util.RunCMD') as mock_cmd:
            mock_cmd.return_value.__bool__.return_value = True
            with patch('os.path.isfile', return_value=True):
                ps = PlaySound('Ping.aiff')
                self.assertEqual(ps.sound, '/System/Library/Sounds/Ping.aiff')

    def test_pick_sound_fallback(self):
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
        with patch('freshenmac.util.RunCMD') as mock_cmd:
            mock_cmd.return_value.__bool__.return_value = True
            with patch('os.path.isfile', return_value=True):
                ps = PlaySound('Pop.aiff')
                ps.play()
                mock_cmd.assert_called_with(['afplay', ps.sound], interactive=True)


class TestRunAppleScript(unittest.TestCase):
    """Unit tests for RunAppleScript executor."""

    def test_background_run(self):
        with patch('subprocess.Popen') as mock_popen, patch('subprocess.run') as mock_run:
            ascript = RunAppleScript('display notification "test"', background=True)
            mock_popen.assert_called_once_with(['osascript', '-e', 'display notification "test"'])
            mock_run.assert_not_called()
            self.assertTrue(bool(ascript))
            self.assertEqual(ascript.returncode, 0)

    def test_contains_operator(self):
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=['osascript'], returncode=0, stdout='button returned:Restart Now\n', stderr='',
            )
            ascript = RunAppleScript('prompt')
            self.assertIn('button returned:Restart Now', ascript)
            self.assertNotIn('button returned:Snooze', ascript)

    def test_debug_limit_default_and_custom(self):
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
        with patch('subprocess.run') as mock_run, patch('builtins.print') as mock_print:
            mock_run.return_value = subprocess.CompletedProcess(
                args=['osascript'], returncode=0, stdout='debug_val', stderr='',
            )
            RunAppleScript('return "debug_val"', debug=2)
            self.assertGreaterEqual(mock_print.call_count, 2)

    def test_dedent_and_strip(self):
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
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=['osascript'], returncode=0, stdout='ok\n', stderr='',
            )
            ascript = RunAppleScript(['tell application "Finder"', 'activate', 'end tell'])
            self.assertEqual(ascript.script, 'tell application "Finder"\nactivate\nend tell')

    def test_notify_classmethod(self):
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
        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = OSError("Exec format error")
            ascript = RunAppleScript('broken')
            self.assertFalse(bool(ascript))
            self.assertEqual(ascript.returncode, -3)
            self.assertIsInstance(ascript.error, OSError)
            self.assertIn("Exec format error", ascript.stderr)

    def test_sync_run_script_error(self):
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=['osascript'], returncode=1, stdout='', stderr='syntax error',
            )
            ascript = RunAppleScript('bad script')
            self.assertFalse(bool(ascript))
            self.assertEqual(ascript.returncode, 1)
            self.assertEqual(ascript.stderr, 'syntax error')

    def test_sync_run_success(self):
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
        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd=['osascript'], timeout=5)
            ascript = RunAppleScript('delay 10', timeout=5)
            self.assertFalse(bool(ascript))
            self.assertEqual(ascript.returncode, -2)
            self.assertIsInstance(ascript.error, subprocess.TimeoutExpired)
            self.assertIn('timed out', ascript.stderr)

class TestRunCMD(unittest.TestCase):
    """Unit tests for RunCMD wrapper class."""

    def test_basic_command_execution(self):
        cmd = RunCMD(['echo', 'hello world'])
        self.assertTrue(bool(cmd))
        self.assertEqual(cmd.returncode, 0)
        self.assertEqual(cmd.strip(), 'hello world')
        self.assertIn('hello', cmd)

    def test_splitlines_and_len(self):
        cmd = RunCMD(['printf', 'line1\nline2\nline3\n'])
        self.assertEqual(len(cmd), 3)
        self.assertEqual(cmd[0], 'line1')
        self.assertEqual(cmd[1], 'line2')
        self.assertEqual(cmd[2], 'line3')
        self.assertEqual(list(cmd), ['line1', 'line2', 'line3'])

    def test_debug_limit_default_and_custom(self):
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

    def test_failed_command_bool(self):
        cmd = RunCMD(['false'])
        self.assertFalse(bool(cmd))
        self.assertNotEqual(cmd.returncode, 0)

    def test_parse_dict(self):
        sample_output = "Name: MacBook Pro\nModel: Mac14,2\nStatus: OK\nStatus: Second"
        with patch.object(RunCMD, 'run') as mock_run:
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

    def test_timeout_handling(self):
        with patch('subprocess.run', side_effect=subprocess.TimeoutExpired(cmd=['sleep', '5'], timeout=1)):
            cmd = RunCMD(['sleep', '5'], timeout=1)
            self.assertFalse(bool(cmd))
            self.assertEqual(cmd.returncode, -2)
            self.assertIn('timed out', cmd.stderr)

    def test_cancelable_keyboard_interrupt_sets_errno_130(self):
        with patch('subprocess.run', side_effect=KeyboardInterrupt):
            cmd = RunCMD(['long_running_task'], timeout=1800, cancelable=True)
            self.assertFalse(bool(cmd))
            self.assertEqual(cmd.returncode, 130)
            self.assertTrue(cmd.cancelled)
            self.assertIn('cancelled by user', cmd.stderr)

    def test_default_cancelable_logic(self):
        with patch('subprocess.run') as mock_sub:
            mock_sub.return_value.returncode = 0
            mock_sub.return_value.stdout = ''
            mock_sub.return_value.stderr = ''
            c1 = RunCMD(['echo', 'hi'], timeout=1800)
            self.assertTrue(c1.cancelable)
            c2 = RunCMD(['echo', 'hi'], timeout=10)
            self.assertFalse(c2.cancelable)

    def test_non_cancelable_keyboard_interrupt_raises(self):
        with patch('subprocess.run', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                RunCMD(['critical_task'], timeout=10, cancelable=False)

    def test_interactive_mode(self):
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

    def test_prepend(self):
        cmd = RunCMD(['echo', 'hello'])
        self.assertEqual(cmd._prepend('sudo'), ['sudo', 'echo', 'hello'])
        self.assertEqual(
            cmd._prepend(['taskpolicy', '-c', 'utility']),
            ['taskpolicy', '-c', 'utility', 'echo', 'hello'],
        )

        cmd_shell = RunCMD(['echo hello'], need_shell=True)
        self.assertEqual(cmd_shell._prepend('sudo'), ['sudo echo hello'])

    def test_taskpolicy_prepends_clamp(self):
        with patch('subprocess.run') as mock_sub:
            mock_sub.return_value.returncode = 0
            mock_sub.return_value.stdout = 'ok'
            mock_sub.return_value.stderr = ''

            # Valid clamps
            for clamp in ('utility', 'background', 'maintenance'):
                cmd = RunCMD(['brew', 'upgrade'], taskpolicy=clamp)
                self.assertEqual(cmd.cmd_args[:3], ['taskpolicy', '-c', clamp])

            # None or unsupported clamp should not prepend
            cmd_none = RunCMD(['brew', 'upgrade'], taskpolicy=None)
            self.assertEqual(cmd_none.cmd_args, ['brew', 'upgrade'])

    def test_taskpolicy_real_execution(self):
        cmd = RunCMD(['echo', 'qos test'], taskpolicy='utility')
        self.assertTrue(bool(cmd))
        self.assertEqual(cmd.returncode, 0)
        self.assertEqual(cmd.strip(), 'qos test')

    def test_need_root_interactive(self):
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
        with patch('os.geteuid', return_value=0):
            with patch('subprocess.run') as mock_sub:
                mock_sub.return_value.returncode = 0
                mock_sub.return_value.stdout = 'output'
                mock_sub.return_value.stderr = ''
                cmd = RunCMD(['echo', 'test'], need_root=True)
                mock_sub.assert_called_once()
                args, _ = mock_sub.call_args
                self.assertEqual(args[0], ['echo', 'test'])

    def test_can_sudo(self):
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


    def test_std_all(self):
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


class TestRunner(unittest.TestCase):
    """Unit tests for Runner concurrency lifecycle manager."""

    def test_context_manager(self):
        with patch.object(Runner, 'start') as mock_start, \
             patch.object(Runner, 'stop') as mock_stop:
            with Runner(target=lambda: None) as runner:
                self.assertIsInstance(runner, Runner)
                mock_start.assert_called_once()
                mock_stop.assert_not_called()
            mock_stop.assert_called_once()

    def test_custom_args_and_kwargs(self):
        result = []

        def worker(val, *, multiplier=1):
            result.append(val * multiplier)

        runner = Runner(target=worker, args=(5,), kwargs={'multiplier': 3})
        runner.start()
        runner.stop()
        self.assertEqual(result, [15])

    def test_is_alive_property(self):
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
        executed = []
        runner = Runner(target=lambda: executed.append(True), condition=False)
        runner.start()
        self.assertIsNone(runner._thread)
        self.assertFalse(runner.is_alive)
        runner.stop()
        self.assertEqual(executed, [])

    def test_start_skipped_when_callable_condition_false(self):
        executed = []
        runner = Runner(target=lambda: executed.append(True), condition=lambda: False)
        runner.start()
        self.assertIsNone(runner._thread)
        self.assertFalse(runner.is_alive)
        runner.stop()
        self.assertEqual(executed, [])

    def test_start_when_bool_condition_true(self):
        executed = []
        runner = Runner(target=lambda: executed.append(True), condition=True)
        runner.start()
        runner.stop()
        self.assertEqual(executed, [True])

    def test_start_when_callable_condition_true(self):
        executed = []
        runner = Runner(target=lambda: executed.append(True), condition=lambda: True)
        runner.start()
        runner.stop()
        self.assertEqual(executed, [True])

    def test_stop_joins_thread_gracefully(self):
        runner = Runner(join_timeout=0.5)
        with patch('threading.Thread') as mock_thread_cls:
            mock_thread = mock_thread_cls.return_value
            mock_thread.is_alive.return_value = True
            runner.start()
            runner.stop()
            mock_thread.join.assert_called_once_with(timeout=0.5)
            self.assertIsNone(runner._thread)

    def test_subclass_default_run(self):
        executed = []

        class CustomRunner(Runner):
            def _run(self):
                executed.append('custom')

        runner = CustomRunner()
        runner.start()
        runner.stop()
        self.assertEqual(executed, ['custom'])


class TestSudoKeepAlive(unittest.TestCase):
    """Unit tests for SudoKeepAlive background elevation refresher."""

    def test_context_manager(self):
        with patch.object(SudoKeepAlive, 'start') as mock_start, \
             patch.object(SudoKeepAlive, 'stop') as mock_stop:
            with SudoKeepAlive(interval=10) as keepalive:
                self.assertIsInstance(keepalive, SudoKeepAlive)
                mock_start.assert_called_once()
                mock_stop.assert_not_called()
            mock_stop.assert_called_once()

    def test_keepalive_skips_refresh_when_cannot_sudo(self):
        keepalive = SudoKeepAlive(interval=1)
        with patch.object(keepalive._stop_event, 'wait', side_effect=[False, True]), \
             patch('freshenmac.util.RunCMD') as mock_cmd:
            mock_cmd.can_sudo.return_value = False
            keepalive._keepalive()
            mock_cmd.assert_not_called()

    def test_keepalive_refreshes_sudo(self):
        keepalive = SudoKeepAlive(interval=1)
        with patch.object(keepalive._stop_event, 'wait', side_effect=[False, True]), \
             patch('freshenmac.util.RunCMD') as mock_cmd:
            mock_cmd.can_sudo.return_value = True
            keepalive._keepalive()
            mock_cmd.assert_called_once_with(['sudo', '-n', '-v'])

    def test_start_and_stop(self):
        keepalive = SudoKeepAlive(interval=1)
        keepalive.start()
        self.assertIsNotNone(keepalive._thread)
        self.assertTrue(keepalive._thread.is_alive())
        keepalive.stop()
        self.assertIsNone(keepalive._thread)


class TestVersion(unittest.TestCase):
    """Unit tests for Version data container and natural ordering."""

    def test_comparisons_and_equality(self):
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
        self.assertEqual(Version.parse('v1.2.3').parts, (1, 2, 3))
        self.assertEqual(Version.parse('V2.0').parts, (2, 0))
        self.assertEqual(Version.parse('v1.2.3'), Version.parse('1.2.3'))

    def test_parse_numeric(self):
        self.assertEqual(Version.parse('1.0.0').parts, (1, 0, 0))
        self.assertEqual(Version.parse('15.2.1').parts, (15, 2, 1))

    def test_parse_post_releases_and_patches(self):
        self.assertEqual(Version.parse('1.0.0-final').parts, (1, 0, 0, 1510))
        self.assertEqual(Version.parse('1.0.0-post1').parts, (1, 0, 0, 1600, 1))
        self.assertEqual(Version.parse('1.0.0-patch1').parts, (1, 0, 0, 1700, 1))
        self.assertEqual(Version.parse('1.0.0-sp1').parts, (1, 0, 0, 1800, 1))

    def test_parse_pre_releases(self):
        self.assertEqual(Version.parse('1.0.0-rc1').parts, (1, 0, 0, -4, 1))
        self.assertEqual(Version.parse('1.0.0-rc').parts, (1, 0, 0, -4))
        self.assertEqual(Version.parse('1.0.0-alpha').parts, (1, 0, 0, -8))
        self.assertEqual(Version.parse('1.0.0-beta').parts, (1, 0, 0, -7))
        self.assertEqual(Version.parse('1.0.0-dev1').parts, (1, 0, 0, -9, 1))
        self.assertEqual(Version.parse('1.0.0-a').parts, (1, 0, 0, -97))
        self.assertEqual(Version.parse('1.0.0-a1').parts, (1, 0, 0, -8, 1))

    def test_parse_version_helper(self):
        self.assertEqual(parse_version('1.0.0'), Version.parse('1.0.0'))

    def test_str_and_bool_and_hash(self):
        self.assertTrue(bool(Version.parse('1.0')))
        self.assertFalse(bool(Version.parse('')))
        self.assertFalse(bool(Version.parse(None)))
        self.assertEqual(str(Version.parse('1.0.0-rc1')), '1.0.0-rc1')
        self.assertEqual(hash(Version.parse('1.0')), hash(Version.parse('1.0.0')))
        self.assertEqual(len({Version.parse('1.0'), Version.parse('1.0.0')}), 1)

    def test_string_cross_comparison(self):
        self.assertEqual(Version.parse('1.0.0'), '1.0.0')
        self.assertEqual(Version.parse('1.0'), '1.0.0')
        self.assertLess(Version.parse('1.0.0-rc1'), '1.0.0')
        self.assertLess(Version.parse('1.0.0'), '1.0.0-patch1')


class TestLogging(unittest.TestCase):
    """Unit tests for get_log_file, log_message, and Logger class."""

    def test_get_log_file_system_and_user_fallbacks(self):
        with patch('pathlib.Path.exists', return_value=False), \
             patch('os.access', return_value=True):
            self.assertEqual(get_log_file(), PATHS['Log']['System'])

        with patch('pathlib.Path.exists', return_value=False), \
             patch('os.access', return_value=False):
            self.assertEqual(get_log_file(), PATHS['Log']['User'])

    def test_log_message_dry_run_skips_file_write(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_log = Path(tmp_dir) / 'test.log'
            log_message("Dry run message", component='TestComp', dry_run=True, log_file=test_log)
            self.assertFalse(test_log.exists())

    def test_log_message_writes_formatted_lines(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_log = Path(tmp_dir) / 'test.log'
            log_message("Line 1\nLine 2", component='TestComp', dry_run=False, log_file=test_log)
            self.assertTrue(test_log.exists())
            content = test_log.read_text(encoding='utf-8')
            self.assertIn('[TestComp] Line 1', content)
            self.assertIn('[TestComp] Line 2', content)

    def test_logger_dry_run_skips_file_write(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_log = Path(tmp_dir) / 'test.log'
            logger = Logger(dry_run=True, log_file=test_log)
            self.assertFalse(logger.enabled)
            logger.store("Dry run test", component='TestComp')
            self.assertFalse(test_log.exists())

    def test_logger_is_disabled_for_default_log_under_unittest(self):
        logger = Logger()
        self.assertFalse(logger.enabled)

    def test_logger_permission_fallback(self):
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

                self.assertEqual(logger.target, user_log)
                self.assertTrue(user_log.exists())
                self.assertIn('[PermTest] Fallback message', user_log.read_text(encoding='utf-8'))

    def test_logger_print_outputs_to_stdout(self):
        logger = Logger()
        with patch('builtins.print') as mock_print:
            logger.print("Screen output message")
            mock_print.assert_called_once_with("Screen output message")

    def test_logger_print_store_calls_print_and_store(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_log = Path(tmp_dir) / 'test.log'
            logger = Logger(log_file=test_log)
            with patch('builtins.print') as mock_print:
                logger.print_store("Combined message", component='Combo')
                mock_print.assert_called_once_with("Combined message")
            self.assertTrue(test_log.exists())
            self.assertIn('[Combo] Combined message', test_log.read_text(encoding='utf-8'))

    def test_logger_store_writes_formatted_lines(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_log = Path(tmp_dir) / 'test.log'
            logger = Logger(log_file=test_log)
            logger.store("Line A\nLine B", component='StoreComp')
            self.assertTrue(test_log.exists())
            content = test_log.read_text(encoding='utf-8')
            self.assertIn('[StoreComp] Line A', content)
            self.assertIn('[StoreComp] Line B', content)

    def test_line_append_success(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_log = Path(tmp_dir) / 'test.log'
            logger = Logger(log_file=test_log)
            res = logger._line_append("Success line")
            self.assertIsNone(res)
            self.assertEqual(test_log.read_text(encoding='utf-8'), "Success line\n")

    def test_line_append_returns_exception(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_log = Path(tmp_dir) / 'test.log'
            logger = Logger(log_file=test_log)
            with patch('builtins.open', side_effect=PermissionError("Denied")):
                res = logger._line_append("Failing line")
                self.assertIsInstance(res, PermissionError)

    def test_logger_stage2a_sudo_chmod_recovery(self):
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
                logger._write_line("Privileged line")
                mock_run_cmd.assert_called_with(
                    ['sh', '-c', f"touch '{sys_log}' && chmod 666 '{sys_log}'"],
                    need_root=True,
                )
                self.assertEqual(call_count, 2)
                self.assertEqual(logger.target, sys_log)
                self.assertTrue(logger.enabled)

    def test_logger_stage2b_sudo_tee_append(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            sys_log = Path(tmp_dir) / 'sys.log'
            logger = Logger(log_file=sys_log)
            mock_cmd = MagicMock(returncode=0)

            with patch.object(RunCMD, 'can_sudo', return_value=True), \
                 patch('freshenmac.util.RunCMD', return_value=mock_cmd) as mock_run_cmd, \
                 patch.object(logger, '_line_append', return_value=PermissionError("Permission denied")):
                logger._write_line("Tee line")
                mock_run_cmd.assert_any_call(
                    ['tee', '-a', str(sys_log)],
                    to_input="Tee line\n",
                    need_root=True,
                )
                self.assertEqual(logger.target, sys_log)
                self.assertTrue(logger.enabled)

    def test_logger_stage4_var_tmp_fallback(self):
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
                logger._write_line("Emergency message")
                self.assertEqual(logger.target, var_tmp_log)
                self.assertTrue(logger.enabled)

    def test_logger_stage5_disable_logging(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_log = Path(tmp_dir) / 'test.log'
            logger = Logger(log_file=test_log)
            with patch.object(RunCMD, 'can_sudo', return_value=False), \
                 patch.object(logger, '_line_append', return_value=PermissionError("Permission denied")), \
                 patch.object(logger, '_disable_logger') as mock_disable:
                logger._write_line("Failing all stages")
                mock_disable.assert_called_once()

    def test_disable_logger_prints_and_sets_enabled_false(self):
        logger = Logger()
        logger.enabled = True
        with patch('builtins.print') as mock_print:
            logger._disable_logger(PermissionError("Denied"))
            self.assertFalse(logger.enabled)
            mock_print.assert_called_once()
            self.assertIn("Log write failed: Denied", mock_print.call_args[0][0])


if __name__ == '__main__':
    unittest.main()

