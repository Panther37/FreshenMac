from __future__ import annotations

import platform
import re
import time
from functools import cached_property
from typing import Any

from freshenmac import config, plist, util
from freshenmac.boot import Reboot, RebootState
from freshenmac.homebrew import HomeBrew
from freshenmac.mas import AppStore
from freshenmac.softwareupdate import SoftwareUpdate

FILE_BUILD = 20261103

PATTERN: dict[str, re.Pattern[str]] = {
    'Boot Time': re.compile(r"sec = (?P<epoch>\d+)"),
    'Idle Time': re.compile(r"['\"]?HIDIdleTime['\"]?\s*=\s*(?P<ns>\d+)"),
}


class MacOS:
    """
    Host computer orchestrator.  Manages system facts and coordinates
    HomeBrew, AppStore, SoftwareUpdate, Xcode, and Reboot workflows.
    """

    SUG_REBOOT_STR = 'Suggested updates and over uptime threshold'

    def __init__(
        self,
        *,
        console_user: str | None = None,
        debug_vars: dict[str, int] | None = None,
        download_speed: float = 0.0,
        dry_run: bool = False,
        force_reboot: bool = False,
        ignore_mas: set[str] | None = None,
        logger: Any | None = None,
        mas_timeout: int = config.TIMEOUT['App Store']['Unknown Size'],
        timeout: int = config.TIMEOUT['Homebrew']['Formula'],
        upgrade_timeout: int = config.TIMEOUT['Homebrew']['CMake Build'],
        user_password: str | None = None,
    ) -> None:
        self._updates_performed = False
        self.console_user = console_user or util.TortimerCreds.get_console_user()
        self.debug_vars = debug_vars or {'debug': 0, 'debug_limit': 0}
        self.dry_run = dry_run
        self.ignore_mas: set[str] = ignore_mas or set()
        self.log = logger or util.Logger(dry_run=self.dry_run)
        self.mas_timeout = mas_timeout
        self.prefs = plist.load_preferences()
        self.reboot = RebootState()
        self.timeout = timeout
        self.upgrade_timeout = upgrade_timeout
        self.user_password = user_password
        if not download_speed:
            download_speed = float(self.prefs.get('download-speed') or config.DEFAULTS['Download Speed Mbps'])
        self.download_speed = download_speed
        if force_reboot:
            self.reboot.required.append('Command-Line Override')

    def _update_all(self) -> bool:
        brew_updated = self._update_brew()
        mas_updated = self._update_mas()
        os_updated = self._update_os()
        return brew_updated or mas_updated or os_updated

    def _update_brew(self) -> bool:
        if self.brew.update():
            self._updates_performed = True
            return True

        return False

    def _update_mas(self) -> bool:
        if 'mas_path' in self.__dict__:
            self.app_store.__dict__['mas_path'] = self.__dict__['mas_path']
        if self.prefs.get('no-mas'):
            self.ignore_mas.update(self.prefs['no-mas'])
        self.app_store.dry_run = self.dry_run
        self.app_store.timeout = self.mas_timeout
        if self.app_store.update():
            self._updates_performed = True
            return True

        return False

    def _update_os(self) -> bool:
        self.softwareupdate.arm_installed = self.arm_installed
        self.softwareupdate.dry_run = self.dry_run
        self.softwareupdate.timeout = self.timeout
        if self.softwareupdate.update():
            self._updates_performed = True
            return True

        return False

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
        who_cmd = util.RunCMD(['who', '-u'])
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
    def app_store(self) -> AppStore:
        """Mac App Store management instance."""
        return AppStore(
            brew_path=self.brew.brew_path,
            debug_vars=self.debug_vars,
            download_speed_mbps=self.download_speed,
            dry_run=self.dry_run,
            ignore_mas=self.ignore_mas,
            logger=self.log,
            reboot=self.reboot,
            timeout=self.mas_timeout,
        )

    @cached_property
    def arm_installed(self) -> bool:
        """Query sysctl directly to avoid Rosetta 2 translation traps."""
        if not self.is_mac:
            return False

        arm_cmd = util.RunCMD(['sysctl', '-in', 'hw.optional.arm64'])
        return arm_cmd.strip() == '1'

    @cached_property
    def brew(self) -> HomeBrew:
        """HomeBrew management instance configured for this machine's architecture."""
        return HomeBrew(
            is_arm=self.arm_installed,
            cmake_timeout=self.upgrade_timeout,
            debug_vars=self.debug_vars,
            download_speed_mbps=self.download_speed,
            dry_run=self.dry_run,
            logger=self.log,
            reboot=self.reboot,
            timeout=self.timeout,
        )

    def check_and_reboot(self) -> None:
        """Checks reboot status and launches Reboot workflow if required or suggested."""
        self.check_reboot_status()
        if self.reboot.required or self.SUG_REBOOT_STR in self.reboot.suggested:
            self.log.print_store(f"Reboot {'required' if self.reboot.required else 'suggested'}", 'Reboot')
            rebooter = Reboot(self, debug_vars=self.debug_vars, dry_run=self.dry_run, logger=self.log)
            rebooter.start_reboot(self.reboot)
        else:
            self.log.print_store("No reboot required or suggested.", 'Reboot')

    def check_reboot_status(self) -> RebootState:
        """Refreshes and returns the current reboot status dataclass."""
        if num_staged := self.softwareupdate.check_staged_reboot():
            self.reboot.required.append(
                f"Staged Apple Software Update{'s' if num_staged != 1 else ''} ({num_staged})",
            )
        if self._updates_performed and self.uptime_days > config.IDLE_THRESHOLD['Uptime in Days']:
            if self.SUG_REBOOT_STR not in self.reboot.suggested:
                self.reboot.suggested.append(
                    f"Uptime: {self.uptime_days} > {config.IDLE_THRESHOLD['Uptime in Days']}",
                )
                self.reboot.suggested.append(self.SUG_REBOOT_STR)
        return self.reboot

    @cached_property
    def filevault_enabled(self) -> bool:
        """Check if FileVault disk encryption is enabled."""
        if not self.is_mac:
            return False

        status_cmd = util.RunCMD(['fdesetup', 'status'])
        return 'FileVault is On.' in status_cmd

    @property
    def idle_time(self) -> int:
        """Get current user idle time in seconds from IOHIDSystem."""
        # I don't like this method, but I don't know another way
        hid_cmd = util.RunCMD(['ioreg', '-c', 'IOHIDSystem'])
        output = hid_cmd.stdout if hasattr(hid_cmd, 'stdout') else str(hid_cmd)
        match = PATTERN['Idle Time'].search(output)
        return 0 if not match else int(match.group('ns')) // int(1e9)

    @property
    def is_idle(self) -> bool:
        """Returns True if all users/sessions have been idle for >= IDLE_THRESHOLD['Seconds']."""
        return self.all_user_idle_time >= config.IDLE_THRESHOLD['Seconds']

    @cached_property
    def is_mac(self) -> bool:
        return platform.system() == 'Darwin'

    @property
    def pending_os_update(self) -> str:
        """
        Returns the pending/staged macOS update label, if one was downloaded and needs install. Returns '' if none.
        """
        dl_ver = self.softwareupdate.os_ver.get('Downloaded')
        inst_ver = self.softwareupdate.os_ver['Installed']
        label = self.softwareupdate.os_ver.get('Label')
        if dl_ver and inst_ver and dl_ver > inst_ver and label:
            return str(label)

        for dl in self.softwareupdate.summary.get('downloaded', []):
            if 'macos' in dl.lower():
                return dl

        return ''

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

        has_os_update = (
            any('macos' in x.lower() for x in self.softwareupdate.summary.get('downloaded', []))
            or any('macos' in x.lower() for x in self.softwareupdate.summary.get('available', []))
        )
        if not has_os_update:
            has_os_update = bool(
                    self.softwareupdate.os_ver.get('Downloaded') and
                    self.softwareupdate.os_ver['Downloaded'] > self.softwareupdate.os_ver.get('Installed'),
            )
        if not has_os_update:
            installed = self.softwareupdate.os_ver.get('Installed', '')
            summary_lines.append(f"  • [Checked] macOS {installed} (up to date)")

        if not updated:
            summary_lines.append('  • No updates performed.')
        summary_lines.append('════════════════════════════════════════════')

        summary_text = '\n'.join(summary_lines)
        self.log.print_store(summary_text, 'Summary')
        if label := self.pending_os_update:
            self.log.print_store(
                f"[Software Update] macOS update '{label}' is staged and will be applied during system restart.",
                'Summary',
            )

    @cached_property
    def softwareupdate(self) -> SoftwareUpdate:
        """Apple Software Update management instance."""
        return SoftwareUpdate(
            arm_installed=self.arm_installed,
            console_user=self.console_user,
            debug_vars=self.debug_vars,
            download_speed_mbps=self.download_speed,
            dry_run=self.dry_run,
            logger=self.log,
            reboot=self.reboot,
            timeout=self.timeout,
            user_password=self.user_password,
        )

    @cached_property
    def supports_authrestart(self) -> bool:
        """Check if machine supports fdesetup authrestart to reboot past FileVault."""
        if not self.is_mac:
            return False

        authrestart_cmd = util.RunCMD(['fdesetup', 'supportsauthrestart'])
        return authrestart_cmd.strip().lower() == 'true'

    def update(self, target: str = 'all') -> bool:
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
        result = bool(target_func())  # noqa  # we can't get here if target_func == None
        self.print_summary()

        return result

    @property
    def uptime_days(self) -> float:
        """Get system uptime in days."""
        return self.uptime_seconds / 86400.0

    @property
    def uptime_seconds(self) -> int:
        """Get system uptime in seconds from kern.boottime."""
        boottime_cmd = util.RunCMD(['sysctl', '-n', 'kern.boottime'])
        match = PATTERN['Boot Time'].search(boottime_cmd.stdout)
        return int(time.time()) - int(match.group('epoch')) if match else 0
