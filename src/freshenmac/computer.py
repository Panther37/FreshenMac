from __future__ import annotations

import getpass
import platform
import re
import time
from functools import cached_property
from pathlib import Path
from typing import Any

from freshenmac import config, plist, util
from freshenmac.boot import Reboot, RebootState
from freshenmac.homebrew import HomeBrew
from freshenmac.mas import AppStore
from freshenmac.softwareupdate import SoftwareUpdate
from freshenmac.util import Logger, RunCMD
from freshenmac.xcode import Xcode

FILE_BUILD = 20261005

PATTERN: dict[str, re.Pattern[str]] = {
    'Boot Time': re.compile(r"sec = (?P<epoch>\d+)"),
    'Idle Time': re.compile(r"['\"]?HIDIdleTime['\"]?\s*=\s*(?P<ns>\d+)"),
}


class MacOS:
    """
    Host computer orchestrator. Manages system facts and coordinates
    HomeBrew, AppStore, SoftwareUpdate, Xcode, and Reboot workflows.
    """

    def __init__(
        self,
        *,
        console_user: str | None = None,
        debug: int = 0,
        debug_limit: int = 0,
        dry_run: bool = False,
        force_reboot: bool = False,
        ignore_mas: list[str] | set[str] | None = None,
        logger: Any | None = None,
        mas_timeout: int = config.TIMEOUT['App Store'],
        timeout: int = 1800,
        upgrade_timeout: int = config.TIMEOUT['Upgrade'],
        user_password: str | None = None,
    ) -> None:
        self._updates_performed = False
        self.console_user = console_user or getpass.getuser()
        self.debug_vars = {'debug': debug, 'debug_limit': debug_limit}
        self.dry_run = dry_run
        self.log = logger or Logger(dry_run=self.dry_run)
        self.mas_timeout = mas_timeout
        self.reboot = RebootState(required=int(force_reboot))
        self.timeout = timeout
        self.upgrade_timeout = upgrade_timeout
        self.user_password = user_password

        # Instantiate workers
        self.app_store = AppStore(
            brew_path=self.brew.brew_path,
            debug=debug,
            debug_limit=debug_limit,
            dry_run=self.dry_run,
            ignore_mas=ignore_mas,
            is_app_running_fn=lambda app: self.is_app_running(app),
            logger=self.log,
            reboot=self.reboot,
            schedule_fn=lambda: self.schedule_startup_run(),
            timeout=self.mas_timeout,
            xcode=self.xcode,
        )
        self.softwareupdate = SoftwareUpdate(
            arm_installed=self.arm_installed,
            console_user=self.console_user,
            debug=debug,
            debug_limit=debug_limit,
            dry_run=self.dry_run,
            logger=self.log,
            notify_manual_fn=lambda lbl: self.notify_manual_os_update(lbl),
            reboot=self.reboot,
            timeout=self.timeout,
            upgrade_timeout=self.upgrade_timeout,
            user_password=self.user_password,
        )

    def _update_all(self) -> None:
        self._update_brew()
        self._update_mas()
        self._update_os()

    def _update_brew(self) -> None:
        self.brew.update()
        if self.brew.updates_performed:
            self._updates_performed = True

    def _update_mas(self) -> None:
        if 'mas_path' in self.__dict__:
            self.app_store.__dict__['mas_path'] = self.__dict__['mas_path']
        self.app_store.dry_run = self.dry_run
        self.app_store.timeout = self.mas_timeout
        self.app_store.update()
        if self.app_store.updates_performed:
            self._updates_performed = True

    def _update_os(self) -> None:
        self.softwareupdate.arm_installed = self.arm_installed
        self.softwareupdate.dry_run = self.dry_run
        self.softwareupdate.timeout = self.timeout
        self.softwareupdate.upgrade_timeout = self.upgrade_timeout
        self.softwareupdate.update()
        if self.softwareupdate.updates_performed:
            self._updates_performed = True

    @property
    def all_user_idle_time(self) -> int:
        """
        Returns the minimum idle time across all users and interfaces (seconds).
        Considers:
        1. Physical hardware input (keyboard/mouse) via IOHIDSystem.
        2. All active terminal and SSH sessions via `who -u`.
        The system is only considered idle if ALL users and sessions are idle.
        """
        idles = [self.idle_time]
        who_cmd = RunCMD(['who', '-u'])
        if who_cmd:
            for line in who_cmd:
                parts = line.split()
                if len(parts) >= 6:
                    idle_field = parts[5]
                    if idle_field == '.':
                        continue

                    if idle_field == 'old':
                        idles.append(86400)
                    elif ':' in idle_field:
                        hr, minute = idle_field.split(':')
                        idles.append(int(hr) * 3600 + int(minute) * 60)
                    elif idle_field.isdigit():
                        idles.append(int(idle_field) * 60)
        return min(idles) if idles else self.idle_time

    @cached_property
    def arm_installed(self) -> bool:
        """Query sysctl directly to avoid Rosetta 2 translation traps."""
        if not self.is_mac:
            return False

        arm_cmd = RunCMD(['sysctl', '-in', 'hw.optional.arm64'])
        return arm_cmd.strip() == '1'

    @cached_property
    def brew(self) -> HomeBrew:
        """HomeBrew management instance configured for this machine's architecture."""
        return HomeBrew(
            is_arm=self.arm_installed,
            dry_run=self.dry_run,
            logger=self.log,
            reboot=self.reboot,
            timeout=self.timeout,
            upgrade_timeout=self.upgrade_timeout,
            xcode=self.xcode,
            **self.debug_vars,
        )

    def check_and_reboot(self) -> None:
        """Checks reboot status and launches Reboot workflow if required or suggested."""
        self.check_reboot_status()
        if self.reboot.required or self.reboot.suggested:
            rebooter = Reboot(self, dry_run=self.dry_run, logger=self.log, **self.debug_vars)
            rebooter.start()
        else:
            self.log.print_store("No reboot required or suggested.", 'Reboot')

    def check_reboot_status(self) -> RebootState:
        """Refreshes and returns the current reboot status dataclass."""
        self.check_staged_reboot()
        if self._updates_performed and self.uptime_days >= config.IDLE_THRESHOLD['Uptime in Days']:
            if not self.reboot.suggested:
                self.reboot.suggested += 1
        return self.reboot

    def check_staged_reboot(self) -> bool:
        """Check /Library/Updates/index.plist for staged updates that require a reboot."""
        return self.softwareupdate.check_staged_reboot()

    @staticmethod
    def cleanup_startup_run() -> bool:
        """Unloads and removes the temporary one-shot startup LaunchAgent if present."""
        return plist.cleanup_startup_run()

    def cmd_options(self, update_options: dict | None = None) -> dict[str, Any]:
        """Common options dictionary for long-running system/app updates under utility QoS."""
        options = {
            'cancelable': True,
            'taskpolicy': 'utility',
            'timeout':    self.timeout,
            **self.debug_vars,
        }
        if update_options:
            options.update(update_options)
        return options

    @cached_property
    def filevault_enabled(self) -> bool:
        """Check if FileVault disk encryption is enabled."""
        if not self.is_mac:
            return False

        status_cmd = RunCMD(['fdesetup', 'status'])
        return 'FileVault is On.' in status_cmd

    @property
    def has_mas(self) -> bool:
        """Check if App Store CLI (mas) is installed."""
        if 'mas_path' in self.__dict__:
            self.app_store.__dict__['mas_path'] = self.__dict__['mas_path']
        return self.app_store.has_mas

    @property
    def idle_time(self) -> int:
        """Get current user idle time in seconds from IOHIDSystem."""
        # I don't like this method, but I don't know another way
        hid_cmd = RunCMD(['ioreg', '-c', 'IOHIDSystem'])
        output = hid_cmd.stdout if hasattr(hid_cmd, 'stdout') else str(hid_cmd)
        match = PATTERN['Idle Time'].search(output)
        return 0 if not match else int(match.group('ns')) // int(1e9)

    def is_app_running(self, app_name: str) -> bool:
        """Checks if an application is currently running on the system."""
        if not self.is_mac:
            return False

        if RunCMD(['pgrep', '-f', f"{app_name}.app"]):
            return True

        if RunCMD(['pgrep', '-x', app_name]):
            return True

        gui_cmd = util.RunAppleScript(
            'tell application "System Events" to get name of every process whose background only is false',
            **self.debug_vars,
        )
        if gui_cmd:
            running_names = [name.strip().lower() for name in gui_cmd.stdout.split(',')]
            if app_name.lower() in running_names:
                return True

        return False

    @property
    def is_idle(self) -> bool:
        """Returns True if all users/sessions have been idle for >= IDLE_THRESHOLD['Seconds']."""
        return self.all_user_idle_time >= config.IDLE_THRESHOLD['Seconds']

    @cached_property
    def is_mac(self) -> bool:
        return platform.system() == 'Darwin'

    @property
    def mas(self) -> AppStore:
        return self.app_store

    @property
    def mas_path(self) -> Path:
        """Locate the mas binary based on brew_path, PATH, or filesystem presence."""
        return self.app_store.mas_path

    def notify_manual_os_update(self, label: str) -> None:
        """
        Handles Apple Silicon manual Volume Owner authentication requirement for OS updates.
        Executes 3 actions simultaneously:
        1. Prints a status message.
        2. Displays a persistent GUI popup dialog (no timeout).
        3. Opens System Settings > General > Software Update.
        """
        msg = (
            f"[Software Update] macOS update '{label}' has been downloaded to disk.\n"
            "Apple Silicon requires manual authentication by a Volume Owner to apply this update.\n"
            "Opening System Settings > General > Software Update..."
        )
        self.log.print_store(f"\n{msg}\n", 'SoftwareUpdate')
        if self.dry_run:
            self.log.print_store(
                "[Dry-run] Would display alert dialog and open System Settings > Software Update.",
                'SoftwareUpdate',
            )
            return

        RunCMD(
            ['open', 'x-apple.systempreferences:com.apple.Software-Update-Settings.extension'],
            **self.debug_vars,
        )
        dialog = (
            f"macOS update '{label}' is downloaded and ready to install.\n\n"
            "Volume Owner authentication is required in System Settings to complete the update."
        )
        util.RunAppleScript.dialog(
            dialog,
            background=True,
            buttons=('OK',),
            default_button='OK',
            icon='caution',
            title=f"{config.APP_INFO['Name']} - Software Update",
            **self.debug_vars,
        )

    @property
    def os_summary(self) -> dict[str, list[str]]:
        return self.softwareupdate.summary

    def print_summary(self) -> None:
        """Prints a comprehensive update summary across Homebrew, App Store, and macOS."""

        def not_empty(some_dict: dict) -> bool:
            return any(any(v) for v in some_dict.values())

        updated = False
        summary_lines = [f"════════════ {config.APP_INFO['Name']} Summary ════════════"]
        if not_empty(self.brew.summary_results):
            updated = True
            summary_lines.append('Homebrew:')
            for pkg, ver in self.brew.summary_results['updated'].items():
                summary_lines.append(f"  ✔︎ [Updated] {pkg} ({ver})")
            for pkg, ver in self.brew.summary_results['installed'].items():
                summary_lines.append(f"  ✚ [Installed] {pkg} ({ver})")
            for pkg, ver in self.brew.summary_results['unchanged'].items():
                summary_lines.append(f"  • [Unchanged] {pkg} ({ver})")

        if not_empty(self.app_store.summary):
            updated = True
            summary_lines.append('App Store:')
            for pkg, ver in self.app_store.summary['updated'].items():
                summary_lines.append(f"  ✔︎ [Updated] {pkg} ({ver})")
            for pkg, ver in self.app_store.summary['installed'].items():
                summary_lines.append(f"  ✚ [Installed] {pkg} ({ver})")
            for pkg, ver in self.app_store.summary['unchanged'].items():
                summary_lines.append(f"  • [Unchanged] {pkg} ({ver})")

        if not_empty(self.softwareupdate.summary):
            updated = True
            summary_lines.append('macOS:')
            for dl in self.softwareupdate.summary['downloaded']:
                summary_lines.append(f"  ✔︎ [Staged] {dl}")
            for label in self.softwareupdate.summary['available']:
                summary_lines.append(f"  ⬆ [Available] {label} — install via System Settings")

        if 'macos' not in str(self.softwareupdate.summary).lower():
            installed = self.softwareupdate.os_ver.get('Installed', '')
            summary_lines.append(f"  • [Checked] macOS {installed} (up to date)")

        if not updated:
            summary_lines.append('  • No updates performed.')
        summary_lines.append('════════════════════════════════════════════')

        summary_text = '\n'.join(summary_lines)
        self.log.print_store(summary_text, 'Summary')
        if all(
            [
                self.softwareupdate.os_ver.get('Downloaded') > self.softwareupdate.os_ver['Installed'],
                self.softwareupdate.os_ver.get('Label'),
            ],
        ):
            install_cmd = self.softwareupdate.install_label(
                str(self.softwareupdate.os_ver['Label']),
                # force_restart_now=True,
                timeout=self.upgrade_timeout
            )
            self.log.print_store(
                f"[Software Update] To install macOS {self.softwareupdate.os_ver['Label']}:\n  {install_cmd.std_all}",
            )

    def schedule_startup_run(self) -> bool:
        """Creates a one-shot LaunchAgent to rerun FreshenMac on next user login."""
        return plist.schedule_startup_run(dry_run=self.dry_run, logger=self.log)

    @cached_property
    def supports_authrestart(self) -> bool:
        """Check if machine supports fdesetup authrestart to reboot past FileVault."""
        if not self.is_mac:
            return False

        authrestart_cmd = RunCMD(['fdesetup', 'supportsauthrestart'])
        return authrestart_cmd.strip().lower() == 'true'

    def update(self, target: str = 'all') -> None:
        """
        Dispatches updates for the specified target: 'brew', 'mas', 'os', or 'all'.
        """
        if not self.is_mac:
            raise NotImplementedError('This computer is not running macOS.')

        target_clean = target.lower().strip()
        update_target = f"_update_{target_clean}"
        target_func = getattr(self, update_target, None)
        if not callable(target_func):
            raise ValueError(f"Unknown update target: '{target}'.  Choose from 'brew', 'mas', 'os', or 'all'.")

        self.log.store(f"Starting update run (target: {target_clean})", config.APP_INFO['Name'])
        target_func()
        self.print_summary()

    @property
    def uptime_days(self) -> float:
        """Get system uptime in days."""
        return self.uptime_seconds / 86400.0

    @property
    def uptime_seconds(self) -> int:
        """Get system uptime in seconds from kern.boottime."""
        boottime_cmd = RunCMD(['sysctl', '-n', 'kern.boottime'])
        match = PATTERN['Boot Time'].search(boottime_cmd.stdout)
        return int(time.time()) - int(match.group('epoch')) if match else 0

    @cached_property
    def xcode(self) -> Any:
        """Xcode management instance for checking and fixing Xcode tools and license."""
        return Xcode(dry_run=self.dry_run, **self.debug_vars)
