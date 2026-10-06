from __future__ import annotations

import os
import shutil
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TYPE_CHECKING

from freshenmac import util
from freshenmac.config import APP_INFO, IDLE_THRESHOLD

if TYPE_CHECKING:
    from freshenmac.computer import MacOS

FILE_BUILD = 20261005


class Reboot:
    """
    Manages the reboot workflow and escalation sequence.

    Flow:
    - Runs within a caffeinate context to prevent system sleep while waiting.
    - Chimes audio alert and displays uptime with 'Restart Now' vs 'Snooze'.
    - If 'Restart Now':
        1. Graceful restart attempt (auto-saves open files, uses fdesetup authrestart if FileVault active);
           checks after 2 minutes.
        2. If still up, prompts to 'Reboot Now (Force)' or 'Try Again Gracefully' (loops up to 10 times).
        3. After 10 attempts, notifies user of forced reboot in 2 minutes and triggers force reboot.
    - If 'Snooze':
        1. Check all-user idle.  If idle for >= IDLE_THRESHOLD['Seconds'], triggers the 'Now' flow immediately.
        2. Wait an hour, then ask again.
        3. If deferred ``self.max_snooze`` times, triggers the 'Now' flow immediately.
    - Force reboot:
        1. Escalate: sudo shutdown -r +1
        2. guard active backups and kill all programs and daemons
        3. attempt to turn off the computer
        4. wait wait_time between each one
    """

    computer: MacOS

    def __init__(
        self,
        hardware: MacOS | None = None,
        *,
        debug: int = 0,
        debug_limit: int = 0,
        dry_run: bool = False,
        log_file: Path | None = None,
        logger: Any | None = None,
    ) -> None:
        self.debug_vars = {'debug': debug, 'debug_limit': debug_limit}
        self.max_now_attempts = 10
        self.max_snooze = 3
        self.snoozes_left = self.max_snooze
        if hardware is not None:
            self.computer = hardware
        else:
            from freshenmac.computer import MacOS
            self.computer = MacOS(dry_run=dry_run, **self.debug_vars)
        self.dry_run = dry_run or self.computer.dry_run
        self.log_file = log_file or util.get_log_file()
        self.logger = logger or util.Logger(dry_run=self.dry_run, log_file=self.log_file)
        self.logger.component = 'Reboot'

    def _sleep(self, seconds: int | float) -> None:
        """Helper to sleep, respecting dry_run for testing."""
        if self.dry_run:
            self.log(f"[Dry-run] Simulated wait: {seconds}s")
            time.sleep(0.1)
        else:
            time.sleep(seconds)

    def attempt_authrestart(self) -> bool:
        """
        Attempts "fdesetup authrestart" if FileVault is active and supported.
        Allows the Mac to reboot past the FileVault screen directly into macOS to apply updates.
        """
        if not (self.computer.filevault_enabled and self.computer.supports_authrestart):
            return False

        if not self.computer.user_password:
            self.log("FileVault is active, but no password is available for authrestart.")
            return False

        self.log("FileVault is active and supports authrestart.  Attempting fdesetup authrestart...")
        if self.dry_run:
            self.log("[Dry-run] Would execute: sudo fdesetup authrestart -inputplist")
            return True

        plist_data = f"""
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Username</key>
    <string>{self.computer.console_user}</string>
    <key>Password</key>
    <string>{self.computer.user_password}</string>
</dict>
</plist>"""[1:]

        a_cmd = ['fdesetup', 'authrestart', '-delayminutes', '1', '-inputplist']
        if self.debug_vars.get('debug', 0) > 2:
            a_cmd.append('-verbose')
        authrestart_cmd = util.RunCMD(
            a_cmd,
            timeout=300,
            to_input=plist_data,
            need_root=True,
            **self.debug_vars,
        )
        if authrestart_cmd:
            self.log("FileVault configuration tool's authenticated restart (reboot) initiated successfully.")
            return True

        else:
            self.log(
                f"FileVault configuration tool's authenticated restart (reboot) failed (code {authrestart_cmd.returncode}).",
            )
            return False

    def check_and_stop_backup(self) -> None:
        """Checks if Time Machine backup is running and cleanly stops it before force reboot."""

        def running(active: int, status: str) -> bool:
            return f"Running = {active}" in status or f'Running = "{active}"' in status

        if self.dry_run:
            self.log("[Dry-run] Checked Time Machine status (simulated)")
            return

        # Check Time Machine
        tm_cmd = ['tmutil', 'status']
        tm_status = util.RunCMD(tm_cmd, **self.debug_vars)
        if running(1, str(tm_status)):
            self.log("Time Machine backup is active.  Requesting clean stop before escalation...")
            stop_cmd = ['tmutil', 'stopbackup']
            util.RunCMD(stop_cmd, timeout=30, **self.debug_vars)
            for i in range(5):
                self._sleep(i * 10)
                poll_status = util.RunCMD(tm_cmd, **self.debug_vars)
                if running(0, str(poll_status)):
                    self.log("Time Machine backup stopped successfully.")
                    break
            else:
                self.log("Time Machine backup did not stop within 100s.  Proceeding with escalation.")

    def ensure_root(self) -> bool:
        """
        Verifies administrator privileges for force reboot actions.
        Prompts for sudo in interactive sessions if not already cached.
        """
        if self.dry_run or os.geteuid() == 0:
            return True

        if util.RunCMD(['true'], need_root=True, **self.debug_vars):
            return True

        if util.is_interactive():
            self.log("Administrator privileges are required to force restart.  Prompting for sudo password...")
            return bool(util.RunCMD(['-v'], interactive=True, need_root=True))

        return False

    def escalate_shutdown(self) -> None:
        """Schedules a mandatory restart via sudo shutdown or AppleScript."""
        reboot_secs = IDLE_THRESHOLD['Reboot Wait']
        reboot_mins = reboot_secs // 60
        self.log(f"Escalating: sudo shutdown -r +{reboot_mins}")
        if self.dry_run:
            return

        a_reboot_cmd = [
            'shutdown', '-r', f"+{reboot_mins}",
            f"[{APP_INFO['Name']}] Mandatory system restart in {reboot_mins} minutes.",
        ]
        if self.ensure_root():
            util.RunCMD(a_reboot_cmd, reboot_secs + 10, need_root=True, **self.debug_vars)
        else:
            applescript = (
                fr'do shell script "shutdown -r +{reboot_mins} \"Mandatory system restart in {reboot_mins} '
                r'minutes.\"" with administrator privileges'
            )
            util.RunAppleScript(applescript, timeout=reboot_secs + 45, **self.debug_vars)

    def force_reboot(self, wait_time: int = 60) -> None:
        """
        Executes the Force Reboot sequence:
        1. Escalate: sudo shutdown -r +2
        2. Guard active backups and kill all programs and daemons
        3. Attempt to turn off the computer
        4. Wait wait_time between each one
        :param wait_time: seconds to wait between each step
        """
        iteration = 1
        while True:
            self.log(f"════ Force Reboot Sequence (Iteration {iteration}) ════")
            for i in range(6):
                if i % 2:
                    self._sleep(wait_time)
                elif not i:
                    self.escalate_shutdown()
                elif i == 2:
                    self.check_and_stop_backup()
                    self.log("Attempting to kill all programs and daemons...")
                    self.kill_programs_and_daemons()
                elif i == 4:
                    self.escalate_shutdown()
                    iteration += 1
            if self.dry_run:
                break

    def graceful_restart(self) -> None:
        """Attempts a standard graceful restart via System Events, first auto-saving documents."""
        if self.dry_run:
            self.log('[Dry-run] Would auto-save open documents and attempt authrestart / System Events restart')
            return

        # 1. Tell scriptable apps to cleanly save existing open documents
        save_script = [
            'tell application "System Events"',
            '    set appList to (name of every process whose background only is false and name is not "Finder")',
            'end tell',
            'repeat with appName in appList',
            '    try',
            '        tell application appName to close every document saving yes',
            '    end try',
            'end repeat',
        ]
        util.RunAppleScript(save_script, timeout=30, **self.debug_vars)

        # 2. Check FileVault authrestart first
        if self.computer.filevault_enabled:
            if not self.attempt_authrestart():
                self.log(
                    'FileVault is active and authrestart credentials unavailable. '
                    'Leaving computer ON to avoid unauthenticated pre-boot timeout and shutdown.',
                )
                # Standard/forced restart commented out to prevent machine from stalling at
                # FileVault pre-boot and powering off:
                # util.RunCMD(['shutdown', '-r', 'now'], need_root=True, **self.debug_vars)
                # util.RunAppleScript('tell application "System Events" to restart', **self.debug_vars)
                return

        else:
            util.RunAppleScript('tell application "System Events" to restart', **self.debug_vars)

    @contextmanager
    def keep_awake(self):
        """Spawns caffeinate via RunCMD to prevent sleep while reboot workflow runs."""
        pid = os.getpid()
        started = False
        if not self.dry_run and shutil.which('caffeinate'):
            a_cmd = [f"caffeinate -dim -w {pid} >/dev/null 2>&1 &"]
            caffeinate_cmd = util.RunCMD(a_cmd, need_shell=True, **self.debug_vars)
            if caffeinate_cmd:
                self.log(f"Spawned caffeinate attached to PID {pid} via RunCMD.")
                started = True
            else:
                self.log(f"Failed to spawn caffeinate (code {caffeinate_cmd.returncode}): {caffeinate_cmd.error}")
        try:
            yield

        finally:
            if started:
                if util.RunCMD(['pkill', '-f', f"caffeinate.*-w {pid}"], **self.debug_vars):
                    self.log("Terminated caffeinate sleep inhibitor via RunCMD.")
                else:
                    self.log("Failed to terminate caffeinate sleep inhibitor.")

    def kill_programs_and_daemons(self) -> None:
        """Attempts to terminate all running GUI programs, user sessions, and WindowServer."""
        if self.dry_run:
            self.log('[Dry-run] Would attempt to kill all programs, sessions, and daemons')
            return

        # 1. Ask GUI apps to quit immediately
        applescript = (
            'tell application "System Events"\n'
            '    set appList to (name of every process whose background only is false)\n'
            'end tell\n'
            'repeat with appName in appList\n'
            '    try\n'
            '        tell application appName to quit with saving no\n'
            '    end try\n'
            'end repeat'
        )
        util.RunAppleScript(applescript, timeout=60, **self.debug_vars)

        # 2. Kill WindowServer to terminate GUI sessions immediately
        if util.RunCMD.can_sudo():
            util.RunCMD(['killall', '-9', 'WindowServer'], timeout=30, need_root=True, **self.debug_vars)
        else:
            applescript = 'do shell script "killall -9 WindowServer" with administrator privileges'
            util.RunAppleScript(applescript, timeout=60, **self.debug_vars)

    def log(self, message: str) -> None:
        """Logs message to console and persistent log file."""
        self.logger.print_store(message)

    def notify_final_warning(self) -> None:
        """Informs the user that the computer will force reboot in 2 minutes."""
        self.play_chime('Sosumi')
        if self.dry_run:
            self.log("[Dry-run] Displayed 2-minute final warning dialog")
            return

        dialog = (
            f"Graceful restart attempts failed.\n\n"
            f"The computer will force reboot in {IDLE_THRESHOLD['Reboot Wait'] // 60} minutes.\n"
            "Please save your work immediately."
        )
        util.RunAppleScript.dialog(
            dialog,
            buttons=('OK',),
            default_button='OK',
            giving_up_after=IDLE_THRESHOLD['Reboot Wait'],
            icon='stop',
            **self.debug_vars,
        )

    def play_chime(self, sound_name: str = 'Ping') -> None:
        """Plays a gentle system alert chime before presenting dialogs."""
        if not self.dry_run:
            util.PlaySound(f"/System/Library/Sounds/{sound_name}.aiff")

    def prompt_initial(self) -> str:
        """
        Displays initial prompt showing uptime with 'Restart Now' and 'Snooze'.
        If all users are already idle for >= IDLE_THRESHOLD['Seconds'], automatically returns 'now'.
        Otherwise, displays dialog with 'giving up after' set to remaining idle seconds.
        If the dialog times out and IDLE_THRESHOLD['Seconds'] of idle is reached, automatically returns 'now'.
        Returns 'now' or 'snooze'.
        """

        def run_snooze() -> str:
            self.snoozes_left -= 1
            if self.computer.os_summary['available'] or self.computer.os_summary['downloaded']:
                util.RunCMD(
                    ['open', 'x-apple.systempreferences:com.apple.Software-Update-Settings.extension'],
                    **self.debug_vars,
                )
            return 'snooze'

        if self.computer.is_idle:
            self.log(
                f"All users have been idle for {self.computer.all_user_idle_time // 60}m "
                f"(>= {IDLE_THRESHOLD['Minutes']}m).  Automatically selecting 'Restart Now'.",
            )
            return 'now'

        self.play_chime('Ping')
        if self.snoozes_left <= 0:
            self.log("No snoozes left.  Automatically selecting 'Restart Now'.")
            return 'now'

        remaining_until_idle = self.remaining_idle_time()
        uptime_days = self.computer.uptime_days
        dialog = (
            f"The computer has been up for {uptime_days:.3f} days.\n\n"
            "A system restart is required for updates.\n"
            f"{self.snoozes_left} Snooze{'s' if self.snoozes_left != 1 else ''} for {IDLE_THRESHOLD['Minutes']} "
            "minutes available."
        )
        if self.dry_run:
            self.log(
                f"[Dry-run] Prompting initial dialog (Uptime: {uptime_days:.4f} days, "
                f"giving up after {remaining_until_idle}s) -> default: 'now'",
            )
            return 'now'

        dialog_cmd = util.RunAppleScript.dialog(
            dialog,
            buttons=('Snooze', 'Restart Now'),
            default_button='Restart Now',
            giving_up_after=remaining_until_idle,
            icon='caution',
            **self.debug_vars,
        )
        if 'button returned:Restart Now' in dialog_cmd:
            self.log("User clicked 'Restart Now'.")
            return 'now'

        if 'button returned:Snooze' in dialog_cmd:
            self.log("User clicked 'Snooze'.")
            return run_snooze()

        # Check if dialog closed because user reached IDLE_THRESHOLD['Seconds'] idle threshold
        if 'gave up:true' in dialog_cmd or self.computer.is_idle:
            self.log(
                f"Prompt timed out and user has been idle for >= {IDLE_THRESHOLD['Minutes']} minutes.  "
                f"Automatically selecting 'Restart Now'.",
            )
            return 'now'

        if not dialog_cmd:
            self.log(f"Dialog failed to display (code {dialog_cmd.returncode}): {dialog_cmd.error or 'unknown error'}")
            return run_snooze()

        self.log("User dismissed dialog or selected 'Snooze'.")
        return run_snooze()

    def prompt_retry(self, attempt: int) -> str:
        """
        Asks user to 'Reboot Now (Force)' or 'Try Again Gracefully'.
        If all users are idle for >= IDLE_THRESHOLD['Seconds'], automatically returns 'force'.
        Returns 'force' or 'graceful'.
        """
        if self.computer.is_idle:
            self.log(
                f"All users idle for {self.computer.all_user_idle_time // 60}m "
                f"(>= {IDLE_THRESHOLD['Minutes']}m).  Automatically selecting 'Reboot Now (Force)'.",
            )
            return 'force'

        self.play_chime('Ping')
        remaining_until_idle = self.remaining_idle_time()
        dialog = [
            f"Restart attempt {attempt}/{self.max_now_attempts} was incomplete.\n",
            "An application may be blocking the restart.",
        ]
        if self.dry_run:
            self.log(
                f"[Dry-run] Retry prompt {attempt}/{self.max_now_attempts} displayed (giving up after "
                f"{remaining_until_idle}s) -> default: 'graceful'",
            )
            return 'graceful'

        dialog_cmd = util.RunAppleScript.dialog(
            dialog,
            buttons=('Try Again Gracefully', 'Reboot Now (Force)'),
            default_button='Try Again Gracefully',
            giving_up_after=remaining_until_idle,
            icon='caution',
            **self.debug_vars,
        )
        if 'button returned:Reboot Now (Force)' in dialog_cmd:
            self.log("User selected 'Reboot Now (Force)'.")
            return 'force'

        if 'button returned:Try Again Gracefully' in dialog_cmd:
            self.log("User selected 'Try Again Gracefully'.")
            return 'graceful'

        if 'gave up:true' in dialog_cmd or self.computer.is_idle:
            self.log(
                f"Prompt timed out and user has been idle for >= {IDLE_THRESHOLD['Minutes']} minutes.  "
                f"Automatically selecting 'Reboot Now (Force)'.",
            )
            return 'force'

        if not dialog_cmd:
            self.log(f"Dialog failed to display (code {dialog_cmd.returncode}): {dialog_cmd.error or 'unknown error'}")
            return 'graceful'

        self.log("User selected 'Try Again Gracefully'.")
        return 'graceful'

    def remaining_idle_time(self, min_seconds: int = 10) -> int:
        """Calculates remaining seconds until IDLE_THRESHOLD['Seconds'], capped at min_seconds."""
        return max(min_seconds, IDLE_THRESHOLD['Seconds'] - self.computer.all_user_idle_time)

    def run_now_flow(self) -> None:
        """
        Executes the 'Now' restart flow:
        1. Start graceful restart and check after IDLE_THRESHOLD['Reboot Wait'] seconds.
        2. If still up, ask to reboot now or try again gracefully (up to 10 times).
        3. After 10 attempts, notify user of forced reboot in IDLE_THRESHOLD['Reboot Wait'] seconds and force reboot.
        """
        for attempt in range(self.max_now_attempts):
            self.log(f"Starting graceful restart (attempt {attempt + 1}/{self.max_now_attempts})...")
            self.graceful_restart()
            # Wait IDLE_THRESHOLD['Reboot Wait'] seconds to check if computer is still up
            self._sleep(IDLE_THRESHOLD['Reboot Wait'])
            # If still running, prompt user
            choice = self.prompt_retry(attempt + 1)
            if choice == 'force':
                self.log("User chose 'Reboot Now (Force)'.")
                self.force_reboot()
                return

        self.log(
            f"{self.max_now_attempts} graceful restart attempts exhausted.  "
            f"Notifying user of forced reboot in {IDLE_THRESHOLD['Reboot Wait']} seconds.",
        )
        self.notify_final_warning()
        self._sleep(IDLE_THRESHOLD['Reboot Wait'])
        self.force_reboot()

    def run_snooze_flow(self) -> None:
        """
        Executes the 'Snooze' flow:
        1. Check all-user idle.  If idle for >= IDLE_THRESHOLD['Seconds'], start 'Now' flow immediately.
        2. Wait an hour, ask user again.
        3. If user has been asked ``self.max_snooze`` times, start 'Now' flow immediately.
        """
        check_idle_seconds = 5 * 60

        while True:
            if self.computer.is_idle:
                self.log(
                    f"All users idle for {self.computer.all_user_idle_time}s "
                    f"(>= {IDLE_THRESHOLD['Minutes']}m).  Starting 'Now' flow.",
                )
                self.run_now_flow()
                return

            if self.snoozes_left < 0:
                self.log(f"Reboot deferred {self.max_snooze} times.  Escalating to 'Now' flow.")
                self.run_now_flow()
                return

            self.log(f"User selected 'Snooze'.  Waiting {IDLE_THRESHOLD['Minutes']} min before asking again...")
            if not self.dry_run:
                for i in range(IDLE_THRESHOLD['Seconds'] // check_idle_seconds):
                    time.sleep(check_idle_seconds)
                    if self.computer.is_idle:
                        self.log(
                            f"All users became idle (>= {IDLE_THRESHOLD['Minutes']}m) after "
                            f"{(i + 1) * check_idle_seconds // 60}m.  Starting 'Now' flow.",
                        )
                        self.run_now_flow()
                        return

            else:
                self._sleep(IDLE_THRESHOLD['Seconds'])

            choice = self.prompt_initial()
            if choice == 'now':
                self.run_now_flow()
                return

    def start(self) -> None:
        """Entry point for the reboot workflow.  Wrapped with caffeinate to prevent sleep."""
        with self.keep_awake():
            choice = self.prompt_initial()
            if choice == 'now':
                self.run_now_flow()
            else:
                self.run_snooze_flow()

    def turn_off(self) -> None:
        """Attempts to shut down / power off the computer."""
        if self.dry_run:
            self.log('[Dry-run] Would execute: sudo shutdown -h now')
            return

        if not self.ensure_root():
            applescript = 'do shell script "shutdown -h now" with administrator privileges'
            util.RunAppleScript(applescript, timeout=IDLE_THRESHOLD['Reboot Wait'] + 10, **self.debug_vars)
            return

        a_cmd = ['shutdown', '-h', 'now']
        util.RunCMD(a_cmd, timeout=IDLE_THRESHOLD['Reboot Wait'] + 10, need_root=True, **self.debug_vars)


@dataclass
class RebootState:
    """
    Pure data container holding reboot state flags.
    Contains no execution logic or system side effects.
    """
    reason: str | None = None
    requested: int = 0
    required: int = 0
    suggested: int = 0

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item.lower())

    def __setitem__(self, item: str, value: Any) -> None:
        attr = item.lower()
        if hasattr(self, attr):
            setattr(self, attr, value)
        else:
            raise KeyError(f"Invalid RebootState key: {item}")
