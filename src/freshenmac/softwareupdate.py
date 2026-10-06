from __future__ import annotations

import plistlib
import re
import time
from pathlib import Path
from typing import Any, Callable

from freshenmac import config, util
from freshenmac.boot import RebootState

FILE_BUILD = 20261005

PATTERN: dict[str, re.Pattern[str]] = {
    'Downloaded Ver':   re.compile(r"Downloaded:\s*(?:macOS\s+[A-Za-z]+\s+)?(?P<ver>\d+(?:\.\d+)+)"),
    'Restart Required': re.compile(r"Action:\s*restart|\[restart\]|restart required|must restart", re.IGNORECASE),
    'SWU Label':        re.compile(r"^\* Label: (?P<label>.+)$", re.MULTILINE),
    'SWU Title':        re.compile(r"Title:\s*(?P<title>[^,\n]+)(?:,.*?Version:\s*(?P<ver>[^,\n]+))?", re.IGNORECASE),
    'Version':          re.compile(r"(?P<ver>\d+(?:\.\d+)+)"),
}


class SoftwareUpdate:
    """Manages Apple Software Updates and macOS upgrades."""

    def __init__(
        self,
        *,
        arm_installed: bool = False,
        console_user: str = '',
        debug: int = 0,
        debug_limit: int = 0,
        dry_run: bool = False,
        logger: Any | None = None,
        notify_manual_fn: Callable[[str], None] | None = None,
        reboot: RebootState | None = None,
        timeout: int = 1800,
        upgrade_timeout: int = config.TIMEOUT['Upgrade'],
        user_password: str | None = None,
    ) -> None:
        self.arm_installed = arm_installed
        self.console_user = console_user
        self.debug_vars = {'debug': debug, 'debug_limit': debug_limit}
        self.dry_run = dry_run
        self.log = logger or util.Logger(dry_run=self.dry_run)
        self.notify_manual_fn = notify_manual_fn
        self.os_ver: dict[str, util.Version | str] = {
            'Installed': util.parse_version(util.RunCMD(['sw_vers', '--productVersion']).strip()),
        }
        self.reboot = reboot or RebootState()
        self.softwareupdate_list: util.RunCMD | None = None
        self.summary: dict[str, list[str]] = {'available': [], 'downloaded': []}
        self.timeout = timeout
        self.updates_performed = False
        self.upgrade_timeout = upgrade_timeout
        self.user_password = user_password

    def _parse_a_label(self, a_label: str) -> None:
        if a_label.lower().startswith('macos'):
            self._update_macos(a_label)
        else:
            self._update_software(a_label)

    def _update_macos(self, a_label: str) -> None:
        comp = 'macOS'
        dl_cmd_args = ['softwareupdate', '-d', a_label]
        if self.softwareupdate_list:
            for a_title in PATTERN['SWU Title'].finditer(self.softwareupdate_list.std_all):
                if a_title.group('title').lower().startswith('macos'):
                    title = a_title.group('title').strip()
                    ver = a_title.group('ver')
                    if not ver:
                        ver_match = PATTERN['Version'].search(title)
                        ver = ver_match.group('ver') if ver_match else ''
                    else:
                        ver = ver.strip()
                    if ver:
                        self.os_ver['Available'] = util.parse_version(ver)
                    break

        if 'Available' not in self.os_ver:
            ver_match = PATTERN['Version'].search(a_label)
            if ver_match:
                self.os_ver['Available'] = util.parse_version(ver_match.group('ver'))

        self.log.print_store(f"Downloading update: {a_label}...", comp)
        if not self.dry_run:
            with util.Wave(f"─▷ Downloading {a_label} (no animation - may prompt for password) ◁─", banner=True):
                time.sleep(0.01)
        os_dl = util.RunCMD(dl_cmd_args, **self.cmd_options(update_options={'taskpolicy': None, 'spinner': False}))
        match = PATTERN['Downloaded Ver'].search(os_dl.std_all)
        ver_str = match.group('ver') if match else ''
        if ver_str:
            self.os_ver['Downloaded'] = util.parse_version(ver_str)
            self.summary['downloaded'].append(a_label)
            self.os_ver['Label'] = a_label
        if 'Failed to authenticate' in os_dl.std_all:
            self.summary['available'].append(a_label)
            if self.notify_manual_fn:
                self.notify_manual_fn(a_label)
            else:
                self.notify_manual(a_label)
        else:
            self.updates_performed = True
        if self.os_ver.get('Downloaded') > self.os_ver['Installed'] or 'Downloaded' in os_dl.std_all:
            self.reboot.required += 1

    def _update_software(self, a_label: str) -> None:
        comp = 'macOS'
        self.log.print_store(f"Downloading update: {a_label}...", comp)
        util.RunCMD(
            ['softwareupdate', '-d', a_label],
            **self.cmd_options(update_options={'taskpolicy': None}),
        )
        self.log.print_store(f"Installing / staging update: {a_label}...", comp)
        install_cmd = self.install_label(a_label)
        output = install_cmd.std_all
        if PATTERN['Restart Required'].search(output) or (
            self.softwareupdate_list and 'Action: restart' in self.softwareupdate_list.std_all
        ):
            self.reboot.required += 1
        if 'Failed to authenticate' in output:
            self.summary['available'].append(a_label)
            if self.notify_manual_fn:
                self.notify_manual_fn(a_label)
            else:
                self.notify_manual(a_label)
        else:
            self.updates_performed = True
            self.summary['downloaded'].append(a_label)

    def check_staged_reboot(self) -> bool:
        """Check /Library/Updates/index.plist for staged updates that require a reboot."""
        index_path = Path('/Library/Updates/index.plist')
        if index_path.is_file():
            try:
                with open(index_path, 'rb') as a_file:
                    data = plistlib.load(a_file)
                    if data.get('InstallLater') or data.get('InstallAtLogout'):
                        self.reboot.required += 1
            except Exception:
                pass
        return bool(self.reboot.required)

    def cmd_options(self, update_options: dict | None = None) -> dict[str, Any]:
        """Common options dictionary for long-running system updates under utility QoS."""
        options = {
            'cancelable': True,
            'taskpolicy': 'utility',
            'timeout':    self.timeout,
            **self.debug_vars,
        }
        if update_options:
            options.update(update_options)
        return options

    def install_label(self, a_label: str, force_restart_now: bool = False, timeout: int = 0) -> util.RunCMD:
        timeout = timeout or self.timeout
        install_cmd_args = ['softwareupdate', '--install', a_label]
        if force_restart_now:
            # No dialog, no option to cancel, unless you cancel a closing application
            install_cmd_args.extend(['--restart'])

        input_pass = None
        if self.arm_installed:
            install_cmd_args.extend(['--stdinpass', '--user', self.console_user])
            if self.user_password:
                input_pass = f"{self.user_password}\n"

        return util.RunCMD(
            install_cmd_args,
            to_input=input_pass,
            need_root=True,
            **self.cmd_options(update_options={'taskpolicy': None, 'timeout': timeout}),
        )

    def notify_manual(self, label: str) -> None:
        """
        Handles Apple Silicon manual Volume Owner authentication requirement for OS updates.
        Executes 3 actions simultaneously:
        1. Prints a status message.
        2. Displays a persistent GUI popup dialog (no timeout).
        3. Opens System Settings > General > Software Update.
        """
        comp = 'SoftwareUpdate'
        msg = (
            f"[Software Update] macOS update '{label}' has been downloaded to disk.\n"
            "Apple Silicon requires manual authentication by a Volume Owner to apply this update.\n"
            "Opening System Settings > General > Software Update..."
        )
        self.log.print_store(f"\n{msg}\n", comp)
        if self.dry_run:
            self.log.print_store(
                "[Dry-run] Would display alert dialog and open System Settings > Software Update.",
                comp,
            )
            return

        util.RunCMD(
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

    def update(self) -> None:
        """Stages macOS software updates and OS upgrades."""
        comp = 'macOS'
        self.log.print_store('════ Staging macOS Software Updates ════', comp)
        if self.dry_run:
            return

        try:
            self.softwareupdate_list = util.RunCMD(['softwareupdate', '--list'], **self.cmd_options())
            if not self.softwareupdate_list:
                self.log.print_store(
                    f"Failed to fetch software update list. ({self.softwareupdate_list.returncode})",
                    comp,
                )
                return

            labels = PATTERN['SWU Label'].findall(self.softwareupdate_list.std_all)
            if not labels:
                self.check_staged_reboot()
                return

            for a_label in labels:
                self._parse_a_label(a_label)
            self.check_staged_reboot()
        except KeyboardInterrupt:
            self.log.print_store("\n[Skipped] User canceled macOS software update.", comp)
