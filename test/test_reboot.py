import os
import signal
import subprocess
import time
import unittest
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock, PropertyMock, patch

from freshenmac.boot import Reboot
from freshenmac.computer import MacOS
from freshenmac.config import APP_INFO
from freshenmac.util import RunCMD

# Unmocked subprocess references to ensure safety checks can always inspect and control real OS processes
_unmocked_popen = subprocess.Popen
_unmocked_run = subprocess.run


def ensure_no_active_shutdown(
    *,
    _get_pids: Callable[[], list[int]] | None = None,
    _kill_fn: Callable[[int, int], Any] | None = None,
    _notify_fn: Callable[[str], Any] | None = None,
    _popen_cmd: Callable[..., Any] | None = None,
    _run_cmd: Callable[..., Any] | None = None,
    _sleep_fn: Callable[[float], None] | None = None,
) -> bool:
    """
    Safety guard for unit tests.

    Guarantees no uncancelled macOS system shutdown timer remains on the host machine:
    1. Looks for any active 'shutdown' process via pgrep.
    2. If found, terminates it immediately (SIGTERM -> killall -9 -> sudo killall -> osascript).
    3. Confirms 'shutdown' is no longer running.
    4. If unable to terminate, presents an urgent GUI popup alert with instructions to cancel.

    Returns True if no shutdown is running or was successfully terminated; False if it persists.
    """
    run_cmd = _run_cmd or _unmocked_run
    get_pids = _get_pids or (lambda: get_active_shutdown_pids(_run_cmd=run_cmd))
    kill_fn = _kill_fn or os.kill
    popen_cmd = _popen_cmd or _unmocked_popen
    sleep_fn = _sleep_fn or time.sleep

    pids = get_pids()
    if not pids:
        return True

    # Attempt 1: Standard SIGTERM kill on each detected shutdown PID
    for pid in pids:
        try:
            kill_fn(pid, signal.SIGTERM)
        except OSError:
            pass

    # Attempt 2: Standard killall -9 shutdown
    try:
        run_cmd(['killall', '-9', 'shutdown'], capture_output=True)
    except Exception:
        pass

    # Attempt 3: Sudo killall -9 if the process remains active
    if get_pids():
        try:
            run_cmd(['sudo', '-n', 'killall', '-9', 'shutdown'], capture_output=True)
        except Exception:
            pass

    # Attempt 4: Elevated AppleScript prompt if un-elevated commands could not kill it
    if get_pids():
        try:
            ascript = 'do shell script "killall -9 shutdown" with administrator privileges'
            run_cmd(['osascript', '-e', ascript], capture_output=True)
        except Exception:
            pass

    # Verify shutdown is no longer running (poll briefly)
    for _ in range(5):
        if not get_pids():
            return True
        sleep_fn(0.1)

    # If STILL running after all termination attempts, trigger an urgent GUI alert
    cancel_cmd = 'sudo killall -9 shutdown'
    dialog_msg = (
        "CRITICAL ALERT: A unit test caused an active macOS system shutdown that could not be terminated!\\n\\n"
        f"To prevent your computer from shutting down, open Terminal immediately and run:\\n"
        f"    {cancel_cmd}"
    )
    if _notify_fn:
        _notify_fn(dialog_msg)
    else:
        popup_script = (
            'tell application "System Events"\n'
            '    activate\n'
            f'    display dialog "{dialog_msg}" buttons {{"OK"}} default button "OK" with title '
            '"CRITICAL: Test Caused System Shutdown" with icon stop\n'
            'end tell'
        )
        try:
            popen_cmd(['osascript', '-e', popup_script])
        except Exception:
            pass

    return False


def get_active_shutdown_pids(*, _run_cmd: Callable[..., Any] | None = None) -> list[int]:
    """
    Returns list of process IDs (PIDs) for any active /sbin/shutdown processes.

    Uses `pgrep -x shutdown` to detect scheduled or active shutdown processes.
    """
    run_cmd = _run_cmd or _unmocked_run
    try:
        res = run_cmd(['pgrep', '-x', 'shutdown'], capture_output=True, text=True)
        if all([res.returncode == 0, bool(res.stdout.strip())]):
            return [int(p) for p in res.stdout.split() if p.isdigit()]
    except Exception:
        pass
    return []


def make_ascript(stdout: str = "", stderr: str = "", errno: int = 0):
    """Creates a mock RunAppleScript instance with string membership and truthiness support."""
    res = MagicMock()
    res.stdout = stdout
    res.stderr = stderr
    res.returncode = errno
    res.__contains__.side_effect = lambda s: s in stdout
    res.__bool__.return_value = (errno == 0)
    return res


def make_cmd(stdout: str = "", stderr: str = "", errno: int = 0) -> RunCMD:
    """Creates a real RunCMD instance without executing a subprocess."""
    cmd = RunCMD.__new__(RunCMD)
    cmd.stdout = stdout
    cmd.stderr = stderr
    cmd.returncode = errno
    cmd.debug = False
    return cmd


class TestReboot(unittest.TestCase):
    """
    Unit tests for Reboot workflow, escalation logic, and shutdown safety guards.

    Tests user prompts, idle timers, Time Machine backups, FileVault authrestart,
    forced process termination (WindowServer), and graceful restarts.
    """

    def setUp(self):
        """Configure mock environment and install safety shields to block real system calls."""
        # Arrange computer and reboot instances
        self.mac = MacOS(dry_run=True)
        self.reboot = Reboot(self.mac, dry_run=True)

        # Baseline safety shield: Never allow unmocked execution of AppleScripts, chimes, or subprocesses
        patch_ascript = patch('freshenmac.boot.util.RunAppleScript')
        self.mock_run_applescript = patch_ascript.start()
        self.addCleanup(patch_ascript.stop)

        patch_playsound = patch('freshenmac.boot.util.PlaySound')
        self.mock_playsound = patch_playsound.start()
        self.addCleanup(patch_playsound.stop)

        patch_sub_run = patch('subprocess.run')
        self.mock_sub_run = patch_sub_run.start()
        self.mock_sub_run.side_effect = RuntimeError("Real subprocess.run is blocked in TestReboot")
        self.addCleanup(patch_sub_run.stop)

        patch_sub_popen = patch('subprocess.Popen')
        self.mock_sub_popen = patch_sub_popen.start()
        self.mock_sub_popen.side_effect = RuntimeError("Real subprocess.Popen is blocked in TestReboot")
        self.addCleanup(patch_sub_popen.stop)

    def tearDown(self):
        """Defense-in-depth safety guard: confirm no active shutdown timer remains on the OS."""
        ensure_no_active_shutdown()

    def test_attempt_authrestart_skips_when_no_password(self):
        """Verifies attempt_authrestart returns False when user password is not provided."""
        # Arrange: FileVault active and supported, but no credentials available
        self.reboot.dry_run = False
        self.reboot.computer.user_password = None

        with patch.object(MacOS, 'filevault_enabled', new_callable=PropertyMock, return_value=True), \
            patch.object(MacOS, 'supports_authrestart', new_callable=PropertyMock, return_value=True):
            # Act
            result = self.reboot.attempt_authrestart()

            # Assert
            self.assertFalse(result)

    def test_attempt_authrestart_skips_when_no_sudo(self):
        """Verifies attempt_authrestart returns False when fdesetup fails or lacks sudo."""
        # Arrange: Password provided, but fdesetup authrestart returns non-zero code
        self.reboot.dry_run = False
        self.reboot.computer.user_password = 'secret_password'

        with patch.object(MacOS, 'filevault_enabled', new_callable=PropertyMock, return_value=True), \
            patch.object(MacOS, 'supports_authrestart', new_callable=PropertyMock, return_value=True), \
            patch('freshenmac.boot.util.RunCMD', return_value=make_cmd(errno=1)):
            # Act
            result = self.reboot.attempt_authrestart()

            # Assert
            self.assertFalse(result)

    def test_attempt_authrestart_success(self):
        """Verifies attempt_authrestart returns True when fdesetup succeeds."""
        # Arrange: Password provided and fdesetup authrestart returns exit code 0
        self.reboot.dry_run = False
        self.reboot.computer.user_password = 'secret_password'

        with patch.object(MacOS, 'filevault_enabled', new_callable=PropertyMock, return_value=True), \
            patch.object(MacOS, 'supports_authrestart', new_callable=PropertyMock, return_value=True), \
            patch('freshenmac.boot.util.RunCMD', return_value=make_cmd(errno=0)):
            # Act
            result = self.reboot.attempt_authrestart()

            # Assert
            self.assertTrue(result)

    def test_check_and_stop_backup_active_stops_cleanly(self):
        """Verifies check_and_stop_backup requests tmutil stopbackup when a backup is active."""
        # Arrange: tmutil status returns running on first check, then stopped
        self.reboot.dry_run = False
        call_count = 0

        def fake_runcmd(cmd, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if 'status' in cmd:
                return make_cmd(stdout='Running = 1;' if call_count == 1 else 'Running = 0;')
            return make_cmd(stdout='')

        with patch('freshenmac.boot.util.RunCMD', side_effect=fake_runcmd) as mock_cmd, \
            patch.object(self.reboot, '_sleep'):
            # Act
            self.reboot.check_and_stop_backup()

            # Assert: stopbackup was issued to cleanly cancel the active backup
            stopbackup_calls = [c for c in mock_cmd.call_args_list if 'stopbackup' in c[0][0]]
            self.assertEqual(len(stopbackup_calls), 1)
            self.assertEqual(stopbackup_calls[0][0][0], ['tmutil', 'stopbackup'])

    def test_check_and_stop_backup_inactive(self):
        """Verifies check_and_stop_backup does nothing when no Time Machine backup is running."""
        # Arrange: tmutil status reports Running = 0
        self.reboot.dry_run = False
        with patch(
            'freshenmac.boot.util.RunCMD',
            return_value=make_cmd(stdout='Backup session status:\n{\n    Running = 0;\n}'),
        ) as mock_cmd:
            # Act
            self.reboot.check_and_stop_backup()

            # Assert: stopbackup was never called
            for call_args in mock_cmd.call_args_list:
                args, _ = call_args
                self.assertNotIn('stopbackup', args[0])

    def test_ensure_no_active_shutdown_alerts_fallback_popen(self):
        """Verifies fallback GUI dialog via osascript popen when notification function is omitted."""
        # Arrange: shutdown PID cannot be terminated, and _notify_fn is None
        popen_calls = []

        # Act
        result = ensure_no_active_shutdown(
            _get_pids=lambda: [9999],
            _kill_fn=lambda pid, sig: None,
            _notify_fn=None,
            _popen_cmd=lambda cmd, **kw: popen_calls.append(cmd),
            _run_cmd=lambda cmd, **kw: None,
            _sleep_fn=lambda s: None,
        )

        # Assert: returns False and invoked osascript display dialog with emergency message
        self.assertFalse(result)
        self.assertEqual(len(popen_calls), 1)
        self.assertEqual(popen_calls[0][0], 'osascript')
        self.assertIn('CRITICAL: Test Caused System Shutdown', popen_calls[0][2])

    def test_ensure_no_active_shutdown_alerts_when_kill_fails(self):
        """Verifies emergency callback receives instructions when shutdown cannot be terminated."""
        # Arrange: shutdown PID persists after all termination attempts
        notified = []
        run_calls = []

        # Act
        result = ensure_no_active_shutdown(
            _get_pids=lambda: [9999],
            _kill_fn=lambda pid, sig: None,
            _notify_fn=lambda msg: notified.append(msg),
            _run_cmd=lambda cmd, **kw: run_calls.append(cmd),
            _sleep_fn=lambda s: None,
        )

        # Assert: returns False and calls notification callback with remediation command
        self.assertFalse(result)
        self.assertEqual(len(notified), 1)
        self.assertIn('CRITICAL ALERT: A unit test caused an active macOS system shutdown', notified[0])
        self.assertIn('sudo killall -9 shutdown', notified[0])

    def test_ensure_no_active_shutdown_clean(self):
        """Verifies ensure_no_active_shutdown returns True immediately when no shutdown is running."""
        # Arrange: no shutdown processes running
        run_calls = []

        # Act
        result = ensure_no_active_shutdown(
            _get_pids=lambda: [],
            _run_cmd=lambda cmd, **kw: run_calls.append(cmd),
        )

        # Assert
        self.assertTrue(result)
        self.assertEqual(run_calls, [])

    def test_ensure_no_active_shutdown_kills_and_confirms(self):
        """Verifies ensure_no_active_shutdown sends SIGTERM and killall -9 when shutdown PID detected."""
        # Arrange: initial check returns PID 9999; subsequent checks return empty list
        pids = [[9999], []]

        def fake_get_pids():
            return pids.pop(0) if pids else []

        killed = []
        run_calls = []

        # Act
        result = ensure_no_active_shutdown(
            _get_pids=fake_get_pids,
            _kill_fn=lambda pid, sig: killed.append((pid, sig)),
            _run_cmd=lambda cmd, **kw: run_calls.append(cmd),
            _sleep_fn=lambda s: None,
        )

        # Assert: PID was signaled and confirmed terminated
        self.assertTrue(result)
        self.assertEqual(killed, [(9999, signal.SIGTERM)])
        self.assertIn(['killall', '-9', 'shutdown'], run_calls)

    def test_ensure_root_states(self):
        """Verifies ensure_root across dry-run, cached sudo, interactive prompt, and non-interactive."""
        # 1. Dry run: returns True without running commands
        self.reboot.dry_run = True
        self.assertTrue(self.reboot.ensure_root())

        # 2. Sudo cached: returns True when cached credentials exist
        self.reboot.dry_run = False
        with patch('os.geteuid', return_value=501):
            with patch('freshenmac.boot.util.RunCMD', return_value=make_cmd(errno=0)):
                self.assertTrue(self.reboot.ensure_root())

            # 3. Interactive TTY prompt: primes sudo credentials via interactive prompt
            with patch('freshenmac.boot.util.RunCMD') as mock_cmd:
                mock_cmd.side_effect = [make_cmd(errno=1), make_cmd(errno=0)]
                with patch('sys.stdin.isatty', return_value=True):
                    self.assertTrue(self.reboot.ensure_root())
                    self.assertEqual(mock_cmd.call_args[0][0], ['-v'])
                    self.assertTrue(mock_cmd.call_args[1].get('interactive'))
                    self.assertTrue(mock_cmd.call_args[1].get('need_root'))

            # 4. Non-interactive session without cached sudo: returns False
            with patch('freshenmac.boot.util.RunCMD', return_value=make_cmd(errno=1)):
                with patch('sys.stdin.isatty', return_value=False):
                    self.assertFalse(self.reboot.ensure_root())

    def test_escalate_shutdown(self):
        """Verifies escalate_shutdown schedules shutdown -r +2 via root or elevated AppleScript."""
        self.reboot.dry_run = False
        executed_cmds = []

        def fake_runcmd(cmd, *args, **kwargs):
            executed_cmds.append(cmd)
            return make_cmd()

        # Case 1: Root privileges available -> executes sudo shutdown -r +2
        with patch.object(self.reboot, 'ensure_root', return_value=True):
            with patch('freshenmac.boot.util.RunCMD', side_effect=fake_runcmd):
                self.reboot.escalate_shutdown()
                self.assertIn(
                    ['shutdown', '-r', '+2', f"[{APP_INFO['Name']}] Mandatory system restart in 2 minutes."],
                    executed_cmds,
                )

        # Case 2: Non-root fallback -> invokes elevated AppleScript
        executed_cmds.clear()
        with patch.object(self.reboot, 'ensure_root', return_value=False):
            with patch('freshenmac.boot.util.RunAppleScript') as mock_ascript:
                self.reboot.escalate_shutdown()
                mock_ascript.assert_called_once()
                call_arg = mock_ascript.call_args[0][0]
                self.assertIn('shutdown -r +2', call_arg)
                self.assertIn('with administrator privileges', call_arg)

        # Safety guard verification
        ensure_no_active_shutdown()

    def test_get_active_shutdown_pids(self):
        """Verifies get_active_shutdown_pids parses pgrep output and handles command errors."""
        # Case 1: PIDs successfully parsed from pgrep stdout
        mock_res = MagicMock(returncode=0, stdout=' 123 \n 456 \n')
        mock_run = MagicMock(return_value=mock_res)
        self.assertEqual(get_active_shutdown_pids(_run_cmd=mock_run), [123, 456])

        # Case 2: pgrep returns non-zero (no processes found)
        mock_fail_res = MagicMock(returncode=1, stdout='')
        mock_run = MagicMock(return_value=mock_fail_res)
        self.assertEqual(get_active_shutdown_pids(_run_cmd=mock_run), [])

        # Case 3: OSError / execution failure handled safely
        def failing_run(*args, **kwargs):
            raise OSError("command failed")

        self.assertEqual(get_active_shutdown_pids(_run_cmd=failing_run), [])

        # Safety guard verification
        ensure_no_active_shutdown()

    def test_graceful_restart_dry_run(self):
        """Verifies graceful_restart performs no actions when dry_run is True."""
        self.reboot.dry_run = True
        with patch('freshenmac.boot.util.RunAppleScript') as mock_ascript:
            self.reboot.graceful_restart()
            mock_ascript.assert_not_called()

    def test_graceful_restart_standard(self):
        """Verifies standard graceful restart auto-saves open documents and prompts System Events."""
        self.reboot.dry_run = False
        with patch.object(MacOS, 'filevault_enabled', new_callable=PropertyMock, return_value=False), \
            patch('freshenmac.boot.util.RunAppleScript') as mock_ascript:
            self.reboot.graceful_restart()

            # Two AppleScripts: auto-save documents, then tell System Events to restart
            self.assertEqual(mock_ascript.call_count, 2)
            self.assertIn('close every document saving yes', '\n'.join(mock_ascript.call_args_list[0][0][0]))
            self.assertEqual(mock_ascript.call_args_list[1][0][0], 'tell application "System Events" to restart')

    def test_kill_programs_and_daemons_dry_run(self):
        """Verifies kill_programs_and_daemons performs no actions when dry_run is True."""
        self.reboot.dry_run = True
        with patch('freshenmac.boot.util.RunAppleScript') as mock_ascript, \
            patch('freshenmac.boot.util.RunCMD') as mock_cmd:
            self.reboot.kill_programs_and_daemons()
            mock_ascript.assert_not_called()
            mock_cmd.assert_not_called()

    def test_kill_programs_and_daemons_no_sudo(self):
        """Verifies kill_programs_and_daemons uses elevated AppleScript when sudo is unavailable."""
        self.reboot.dry_run = False
        with patch('freshenmac.boot.util.RunAppleScript') as mock_ascript, \
            patch('freshenmac.boot.util.RunCMD') as mock_cls:
            mock_cls.can_sudo.return_value = False
            self.reboot.kill_programs_and_daemons()

            # Quits apps via AppleScript and kills WindowServer with admin privileges
            self.assertEqual(mock_ascript.call_count, 2)
            self.assertIn('quit with saving no', mock_ascript.call_args_list[0][0][0])
            self.assertIn('killall -9 WindowServer', mock_ascript.call_args_list[1][0][0])
            self.assertIn('with administrator privileges', mock_ascript.call_args_list[1][0][0])

    def test_kill_programs_and_daemons_sudo(self):
        """Verifies kill_programs_and_daemons kills WindowServer with need_root when sudo is available."""
        self.reboot.dry_run = False
        executed = []

        def fake_runcmd(cmd, **kwargs):
            executed.append((cmd, kwargs))
            m = MagicMock()
            m.returncode = 0
            return m

        with patch('freshenmac.boot.util.RunAppleScript') as mock_ascript, \
            patch('freshenmac.boot.util.RunCMD', side_effect=fake_runcmd) as mock_cls:
            mock_cls.can_sudo.return_value = True
            self.reboot.kill_programs_and_daemons()

            # First quits apps via AppleScript, then terminates WindowServer via root RunCMD
            mock_ascript.assert_called_once()
            self.assertIn('quit with saving no', mock_ascript.call_args[0][0])
            self.assertEqual(executed[0][0], ['killall', '-9', 'WindowServer'])
            self.assertTrue(executed[0][1].get('need_root'))

    def test_notify_final_warning(self):
        """Verifies notify_final_warning plays Sosumi chime and displays 120-second warning dialog."""
        self.reboot.dry_run = False
        with patch('freshenmac.boot.util.RunAppleScript.dialog') as mock_dialog, \
            patch.object(self.reboot, 'play_chime') as mock_chime:
            # Act
            self.reboot.notify_final_warning()

            # Assert: audio alert and dialog parameters
            mock_chime.assert_called_once_with('Sosumi')
            mock_dialog.assert_called_once()
            args, kwargs = mock_dialog.call_args
            self.assertIn('The computer will force reboot in 2 minutes', args[0])
            self.assertEqual(kwargs.get('giving_up_after'), 120)
            self.assertEqual(kwargs.get('icon'), 'stop')

    def test_prompt_initial_dry_run(self):
        """Verifies prompt_initial returns 'now' immediately in dry-run mode."""
        self.reboot.dry_run = True
        choice = self.reboot.prompt_initial()
        self.assertEqual(choice, 'now')

    def test_prompt_initial_exhausted_snoozes(self):
        """Verifies prompt_initial defaults to 'now' when all snoozes have been exhausted."""
        self.reboot.dry_run = False
        self.reboot.snoozes_left = 0
        with patch.object(self.reboot, 'play_chime') as mock_chime:
            choice = self.reboot.prompt_initial()
            self.assertEqual(choice, 'now')
            mock_chime.assert_called_once_with('Ping')

    def test_prompt_initial_idle(self):
        """Verifies prompt_initial returns 'now' automatically when computer is idle."""
        self.reboot.dry_run = False
        with patch.object(MacOS, 'is_idle', new_callable=PropertyMock, return_value=True):
            choice = self.reboot.prompt_initial()
            self.assertEqual(choice, 'now')

    def test_prompt_initial_restart_now(self):
        """Verifies prompt_initial returns 'now' when user clicks Restart Now."""
        self.reboot.dry_run = False
        with patch.object(self.reboot, 'play_chime') as mock_chime, \
            patch(
                'freshenmac.boot.util.RunAppleScript.dialog',
                return_value=make_ascript(stdout='button returned:Restart Now'),
            ):
            choice = self.reboot.prompt_initial()
            self.assertEqual(choice, 'now')
            mock_chime.assert_called_once_with('Ping')

    def test_prompt_initial_snooze(self):
        """Verifies prompt_initial returns 'snooze' and decrements snoozes_left on Snooze click."""
        self.reboot.dry_run = False
        self.reboot.snoozes_left = 3
        with patch.object(self.reboot, 'play_chime') as mock_chime, \
            patch(
                'freshenmac.boot.util.RunAppleScript.dialog',
                return_value=make_ascript(stdout='button returned:Snooze'),
            ):
            choice = self.reboot.prompt_initial()
            self.assertEqual(choice, 'snooze')
            self.assertEqual(self.reboot.snoozes_left, 2)
            mock_chime.assert_called_once_with('Ping')

    def test_prompt_retry_graceful_and_force(self):
        """Verifies prompt_retry maps dialog button clicks to 'graceful' or 'force'."""
        self.reboot.dry_run = False
        with patch.object(self.reboot, 'play_chime'), \
            patch('freshenmac.boot.util.RunAppleScript.dialog') as mock_dialog:
            # User chooses graceful retry
            mock_dialog.return_value = make_ascript(stdout='button returned:Try Again Gracefully')
            choice = self.reboot.prompt_retry(1)
            self.assertEqual(choice, 'graceful')

            # User chooses forced reboot
            mock_dialog.return_value = make_ascript(stdout='button returned:Reboot Now (Force)')
            choice = self.reboot.prompt_retry(2)
            self.assertEqual(choice, 'force')

    def test_prompt_retry_idle(self):
        """Verifies prompt_retry returns 'force' automatically when computer is idle."""
        self.reboot.dry_run = False
        with patch.object(MacOS, 'is_idle', new_callable=PropertyMock, return_value=True):
            choice = self.reboot.prompt_retry(1)
            self.assertEqual(choice, 'force')

    def test_remaining_idle_time(self):
        """Verifies remaining_idle_time calculates countdown based on user idle time and min_seconds."""
        # Zero idle time -> full timeout (3600 seconds)
        with patch.object(MacOS, 'all_user_idle_time', new_callable=PropertyMock, return_value=0):
            self.assertEqual(self.reboot.remaining_idle_time(), 3600)

        # Excess idle time -> clamped to min_seconds
        with patch.object(MacOS, 'all_user_idle_time', new_callable=PropertyMock, return_value=4000):
            self.assertEqual(self.reboot.remaining_idle_time(min_seconds=10), 10)

    def test_turn_off_escalation(self):
        """Verifies turn_off issues shutdown -h now via root RunCMD or elevated AppleScript."""
        self.reboot.dry_run = False
        executed_cmds = []

        def fake_runcmd(cmd, *args, **kwargs):
            executed_cmds.append(cmd)
            return make_cmd()

        # Case 1: Root available -> executes shutdown -h now directly
        with patch.object(self.reboot, 'ensure_root', return_value=True):
            with patch('freshenmac.boot.util.RunCMD', side_effect=fake_runcmd):
                self.reboot.turn_off()
                self.assertIn(['shutdown', '-h', 'now'], executed_cmds)

        # Case 2: Non-root fallback -> invokes elevated AppleScript
        executed_cmds.clear()
        with patch.object(self.reboot, 'ensure_root', return_value=False):
            with patch('freshenmac.boot.util.RunCMD', side_effect=fake_runcmd), \
                patch('freshenmac.boot.util.RunAppleScript') as mock_ascript:
                self.reboot.turn_off()
                cmd_match = any('shutdown -h now' in str(c) for c in executed_cmds)
                ascript_match = all(
                    [
                        mock_ascript.called,
                        any('shutdown -h now' in str(c) for c in mock_ascript.call_args_list),
                    ],
                )
                self.assertTrue(cmd_match or ascript_match)

        # Safety guard verification
        ensure_no_active_shutdown()


if __name__ == '__main__':
    unittest.main()
