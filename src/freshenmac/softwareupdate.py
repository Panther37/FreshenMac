from __future__ import annotations

import plistlib
import re
import time
from pathlib import Path
from typing import Any

from freshenmac import config, plist, util
from freshenmac.boot import RebootState

FILE_BUILD = 20261103

PATTERN: dict[str, re.Pattern[str]] = {
    'Downloaded Ver':   re.compile(r"Download\w*:? +(?:macOS (?:[A-Za-z]+ ){0,3})?(?P<ver>\d+(?:[.-]\w+)+)(?![%\w.-])"),
    'Restart Required': re.compile(r"Action:\s*restart|\[restart\]|restart required|must restart", re.IGNORECASE),
    'SWU Label':        re.compile(r"^\* Label: (?P<label>.+)$", re.MULTILINE),
    'SWU Size':         re.compile(r"Size:\s*(?P<size>[0-9,]+(?:\.[0-9]+)?)\s*(?P<unit>[KkMGTP]i?B?)", re.IGNORECASE),
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
        debug_vars: dict[str, int] | None = None,
        download_speed_mbps: float = 0.0,
        dry_run: bool = False,
        logger: Any | None = None,
        reboot: RebootState | None = None,
        timeout: int = config.TIMEOUT['Software Update']['Default'],
        user_password: str | None = None,
    ) -> None:
        self.arm_installed = arm_installed
        self.console_user = console_user
        self.debug_vars = debug_vars or {'debug': 0, 'debug_limit': 0}
        self.download_speed_mbps = float(download_speed_mbps)
        self.dry_run = dry_run
        self.log = logger or util.Logger(dry_run=self.dry_run)
        self.os_ver: dict[str, util.Version | str] = {
            'Installed': util.parse_version(util.RunCMD(['sw_vers', '--productVersion']).strip()),
        }
        self.reboot = reboot if reboot is not None else RebootState()  # Checking existence
        self.softwareupdate_list: util.RunCMD | None = None
        self.summary: dict[str, list[str]] = {'available': [], 'downloaded': []}
        self.timeout = timeout
        self.updates_performed = False
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
        timeout = self.calculate_timeout(a_label)
        os_dl = util.RunCMD(
            dl_cmd_args,
            **self.cmd_options(update_options={'spinner': False, 'taskpolicy': None, 'timeout': timeout}),
        )
        match = PATTERN['Downloaded Ver'].search(os_dl.std_all)
        ver_str = match.group('ver') if match else ''
        if not ver_str and (os_dl or '100.00%' in os_dl.std_all or 'Downloaded' in os_dl.std_all):
            if 'Available' in self.os_ver:
                ver_str = str(self.os_ver['Available'])
            else:
                ver_match = PATTERN['Version'].search(a_label)
                if ver_match:
                    ver_str = ver_match.group('ver')

        if ver_str:
            self.os_ver['Downloaded'] = util.parse_version(ver_str)
            self.summary['downloaded'].append(a_label)
            self.os_ver['Label'] = a_label

        if 'Failed to authenticate' in os_dl.std_all:
            self.summary['available'].append(a_label)
            self.notify_manual(a_label)
        else:
            self.updates_performed = True

        dl_ver = self.os_ver.get('Downloaded')
        if (
            (dl_ver and dl_ver > self.os_ver['Installed'])
            or 'Downloaded' in os_dl.std_all
            or '100.00%' in os_dl.std_all
        ):
            target_ver = dl_ver or self.os_ver.get('Available', '')
            self.reboot.required.append(f"macOS update: {self.os_ver['Installed']} ─▷ {target_ver}")

    def _update_software(self, a_label: str) -> None:
        comp = 'macOS'
        self.log.print_store(f"Downloading update: {a_label}...", comp)
        timeout = self.calculate_timeout(a_label)
        util.RunCMD(
            ['softwareupdate', '-d', a_label],
            **self.cmd_options(update_options={'taskpolicy': None, 'timeout': timeout}),
        )
        self.log.print_store(f"Installing / staging update: {a_label}...", comp)
        install_cmd = self.install_label(a_label)
        output = install_cmd.std_all
        if PATTERN['Restart Required'].search(output) or (
            self.softwareupdate_list and 'Action: restart' in self.softwareupdate_list.std_all
        ):
            self.reboot.required.append('Apple Software Updates')
        if 'Failed to authenticate' in output:
            self.summary['available'].append(a_label)
            self.notify_manual(a_label)
        else:
            self.updates_performed = True
            self.summary['downloaded'].append(a_label)

    def calculate_timeout(self, a_label: str) -> int:
        """
        Calculates dynamic timeout in seconds scaled by update size and configured download speed.
        Base overhead (300s) + (size_mb / (speed_mbps / 8.0)) * (1.0 + buffer).
        """
        speed_mbps = self.download_speed_mbps
        if not speed_mbps:
            prefs = plist.load_preferences()
            speed_mbps = float(prefs.get('download-speed') or config.DEFAULTS['Download Speed Mbps'])

        buffer = config.DEFAULTS['Download Speed Buffer']
        rate_mb_per_sec = max(0.1, speed_mbps / 8.0)

        size_mb = self.get_update_size_mb(a_label)
        if size_mb == 0.0:
            return self.timeout

        buffered_download_sec = (size_mb / rate_mb_per_sec) * (1.0 + buffer)
        base_overhead = config.TIMEOUT['Software Update']['Base Overhead']
        return int(base_overhead + buffered_download_sec)

    @staticmethod
    def check_staged_reboot() -> int:
        """Check /Library/Updates/index.plist for staged updates that require a reboot."""
        index_path = Path('/Library/Updates/index.plist')
        if not index_path.is_file():
            return 0

        try:
            with open(index_path, 'rb') as a_file:
                data = plistlib.load(a_file)
        except (OSError, plistlib.InvalidFileException):
            return 0

        verified_keys: set[str] = set()

        if product_paths := data.get('ProductPaths'):
            for key, path_str in product_paths.items():
                if path_str and Path(path_str).exists():
                    verified_keys.add(str(key))

        for key in ('InstallLater', 'InstallAtLogout'):
            if items := data.get(key):
                for k in items:
                    if isinstance(k, str) and (index_path.parent / k).is_dir():
                        verified_keys.add(k)

        return len(verified_keys)

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

    def get_update_size_mb(self, a_label: str) -> float:
        """Retrieves package download size in megabytes from softwareupdate --list. Returns 0.0 if unavailable."""
        if not self.softwareupdate_list or not self.softwareupdate_list.std_all:
            return 0.0

        in_label = False
        for line in self.softwareupdate_list.std_all.splitlines():
            label_match = PATTERN['SWU Label'].match(line)
            if label_match:
                if in_label:
                    break

                if label_match.group('label').strip() == a_label.strip():
                    in_label = True

                continue

            if in_label:
                size_match = PATTERN['SWU Size'].search(line)
                if size_match:
                    raw_size = size_match.group('size').replace(',', '')
                    unit = size_match.group('unit').upper()
                    try:
                        val = float(raw_size)
                        if unit.startswith('K'):
                            return val / 1024.0

                        if unit.startswith('G'):
                            return val * 1024.0

                        if unit.startswith('T'):
                            return val * 1024.0 * 1024.0

                        if unit.startswith('B'):
                            return val / (1024.0 * 1024.0)

                        return val  # MB or MiB

                    except ValueError:
                        return 0.0

        return 0.0

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

    def update(self) -> bool:
        """Stages macOS software updates and OS upgrades."""
        comp = 'macOS'
        self.log.print_store('════ Staging macOS Software Updates ════', comp)
        if self.dry_run:
            return False

        try:
            self.softwareupdate_list = util.RunCMD(['softwareupdate', '--list'], **self.cmd_options())
            if not self.softwareupdate_list:
                self.log.print_store(
                    f"Failed to fetch software update list. ({self.softwareupdate_list.returncode})",
                    comp,
                )
                return False

            labels = PATTERN['SWU Label'].findall(self.softwareupdate_list.std_all)
            if not labels:
                return False

            for a_label in labels:
                self._parse_a_label(a_label)
        except KeyboardInterrupt:
            self.log.print_store("\n[Skipped] User canceled macOS software update.", comp)

        return self.updates_performed
