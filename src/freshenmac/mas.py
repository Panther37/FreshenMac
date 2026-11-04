from __future__ import annotations

import re
import shutil
from functools import cached_property
from pathlib import Path
from typing import Any

from freshenmac import boot, config, plist, util
from freshenmac.xcode import Xcode

FILE_BUILD = 20261103
PATTERN: dict[str, re.Pattern[str]] = {
    'Mas Size':   re.compile(r"Size\s+.*?([0-9,]+(?:\.[0-9]+)?)\s*(GB|MB|KB)", re.IGNORECASE),
    'Mas Update': re.compile(r"^\s*(?P<id>\d+)\s+(?P<name>.+?)\s+(?P<vers>\([^)]+\))"),
}


class AppStore:
    """Manages updates for App Store applications via the 'mas' CLI."""

    def __init__(
        self,
        *,
        brew_path: Path | None = None,
        debug_vars: dict[str, int] | None = None,
        download_speed_mbps: float = 0.0,
        dry_run: bool = False,
        ignore_mas: set[str] | None = None,
        logger: Any | None = None,
        reboot: boot.RebootState | None = None,
        timeout: int = config.TIMEOUT['App Store']['Unknown Size'],
    ) -> None:

        self.brew_path = brew_path
        self.debug_vars = debug_vars or {'debug': 0, 'debug_limit': 0}
        self.download_speed_mbps = download_speed_mbps
        self.dry_run = dry_run
        self.ignore_mas: set[str] = ignore_mas or set()
        self.log = logger or util.Logger(dry_run=self.dry_run)
        self.reboot = reboot if reboot is not None else boot.RebootState()  # Checking existence
        self.summary: dict[str, dict[str, str]] = {'installed': {}, 'unchanged': {}, 'updated': {}}
        self.timeout = timeout
        self.updates_performed = False

    def calculate_timeout(self, app_id: str) -> int:
        """
        Calculates dynamic timeout in seconds scaled by app size and configured download speed.
        Base overhead (300s) + (size_mb / (speed_mbps / 8.0)) * (1.0 + buffer).
        """
        speed_mbps = self.download_speed_mbps
        if not speed_mbps:
            prefs = plist.load_preferences()
            speed_mbps = float(prefs.get('download-speed') or config.DEFAULTS['Download Speed Mbps'])

        buffer = config.DEFAULTS['Download Speed Buffer']
        rate_mb_per_sec = max(0.1, speed_mbps / 8.0)

        size_mb = self.get_app_size_mb(app_id)
        if size_mb == 0.0:
            return self.timeout

        buffered_download_sec = (size_mb / rate_mb_per_sec) * (1.0 + buffer)
        base_overhead = config.TIMEOUT['App Store']['Base Overhead']
        return int(base_overhead + buffered_download_sec)

    def cmd_options(self, update_options: dict | None = None) -> dict[str, Any]:
        """Common options dictionary for long-running App Store updates under utility QoS."""
        options = {
            'cancelable': True,
            'taskpolicy': 'utility',
            'timeout':    self.timeout,
            **self.debug_vars,
        }
        if update_options:
            options.update(update_options)
        return options

    def get_app_size_mb(self, app_id: str) -> float:
        """Queries app size in megabytes using 'mas info <app_id>'. Returns 0.0 if unavailable."""
        if not self.has_mas or not self.mas_path:
            return 0.0

        info_cmd = util.RunCMD([str(self.mas_path), 'info', str(app_id)], timeout=30, **self.debug_vars)
        if not info_cmd:
            return 0.0

        for line in str(info_cmd).splitlines():
            match = PATTERN['Mas Size'].search(line)
            if match:
                raw_num = match.group(1).replace(',', '')
                unit = match.group(2).upper()
                try:
                    val = float(raw_num)
                    if unit == 'GB':
                        return val * 1024.0

                    if unit == 'KB':
                        return val / 1024.0

                    return val  # MB

                except ValueError:
                    pass  # Ignore unparseable numeric size

        return 0.0

    @property
    def has_mas(self) -> bool:
        """Check if App Store CLI (mas) is installed."""
        if self.mas_path == Path(''):
            if self.dry_run or not self.brew_path:
                return False

            if util.RunCMD([str(self.brew_path), 'install', 'mas'], **self.cmd_options()):
                self.__dict__.pop('mas_path', None)
        return self.mas_path != Path('')

    @cached_property
    def ignored_apps(self) -> set[str]:
        """
        Returns a normalized set of App Store app names and numeric IDs to skip during updates.

        Normalizes ignored app identifiers from multiple sources:
        1. Unpacks both persistent plist configuration (*raw) and runtime CLI flags (*self.ignore_mas).
        2. Converts each item to string and strips leading/trailing whitespace.
        3. Discards empty strings / whitespace-only entries via 'if str(x).strip()'.
        4. Lowercases all identifiers so matching is case-insensitive for both app names and numeric IDs.
        """
        prefs = plist.load_preferences()
        raw = prefs.get('no-mas', [])
        if isinstance(raw, (int, str)):
            raw = [raw]
        ignored = set()
        for item in (*raw, *self.ignore_mas):
            cleaned = str(item).strip().lower()
            if cleaned:
                ignored.add(cleaned)
        return ignored

    @cached_property
    def mas_path(self) -> Path:
        """Locate the mas binary based on brew_path, PATH, or filesystem presence."""
        if self.brew_path:
            candidate = Path(self.brew_path).parent / 'mas'
            if candidate.is_file():
                return candidate

        which_mas = shutil.which('mas')
        if which_mas:
            return Path(which_mas)

        for candidate_str in ['/opt/homebrew/bin/mas', '/usr/local/bin/mas']:
            if Path(candidate_str).is_file():
                return Path(candidate_str)

        return Path('')

    def update(self) -> bool:
        """Checks and updates App Store applications using mas."""
        comp = 'App Store'
        if not self.has_mas or self.mas_path == Path(''):
            self.log.print_store("App Store CLI ('mas') is not installed.  Skipping 'mas' update.", comp)
            return False

        self.log.print_store('════ Updating App Store Apps ════', comp)
        if self.dry_run:
            self.log.print_store("[Dry-run] Would check and update App Store apps.", comp)
            return False

        outdated_cmd = util.RunCMD([str(self.mas_path), 'outdated'], timeout=60)
        if not outdated_cmd or not outdated_cmd.stdout.strip():
            self.log.print_store("All App Store apps are up to date.", comp)
            return False

        found_updates = []
        for line in outdated_cmd.stdout.splitlines():
            line = line.strip()
            if not line:
                continue

            match = PATTERN['Mas Update'].match(line)
            if match:
                vers = match.group('vers').strip('()').replace('->', '═▷').strip()
                found_updates.append((match.group('id'), match.group('name').strip(), vers))

        self.log.print_store(f"Found {len(found_updates)} {comp} update{'s' if len(found_updates) != 1 else ''}:", comp)
        for app_id, app_name, version_info in found_updates:
            ver_display = f" ({version_info})" if version_info else ""
            self.log.printf(f"  • {app_id}  {app_name} {ver_display}".rstrip())

        has_running_app = False
        updatable_apps = []
        running_gui_apps = util.get_running_gui_apps(self.debug_vars)
        for app_id, app_name, version_info in found_updates:
            if app_id in self.ignored_apps or app_name.lower() in self.ignored_apps:
                self.log.print_store(f"  [Skipping] '{app_name}' ({app_id}) is in Ignore MAS preferences.", comp)
                self.summary['unchanged'][app_name] = version_info or 'ignored'
            elif util.is_app_running(app_name, self.debug_vars, running_gui_apps=running_gui_apps):
                has_running_app = True
                ver_display = f" ({version_info})" if version_info else ""
                self.reboot.suggested.append(f"App Store: {app_name}{ver_display} deferred (app currently running)")
                self.log.print_store(f"  [Skipping] '{app_name}' is currently running on the GUI.", comp)
                self.summary['unchanged'][app_name] = version_info or 'deferred (running)'
            else:
                updatable_apps.append((app_id, app_name, version_info))

        if has_running_app:
            # # TODO: App is running and cannot update, schedule to try again after next reboot
            if not plist.StartupRun(dry_run=self.dry_run, logger=self.log).schedule():
                self.log.print_store("Failed to schedule deferred App Store updates.", comp)

        if not updatable_apps:
            if has_running_app:
                self.log.print_store("All pending App Store updates are currently running.  Skipping for now.", comp)
            else:
                self.log.print_store("No remaining App Store updates to install.", comp)
            return False

        if not util.RunCMD.can_sudo():
            interactive_shell = util.is_interactive()
            if interactive_shell:
                self.log.printf(
                    f"\n[Permission Required] Administrator privileges needed to update "
                    f"{len(updatable_apps)} App Store app{'s' if len(updatable_apps) != 1 else ''}:\n"
                    "Prompting for sudo password...\n",
                )
                util.RunCMD(['sudo', '-v'], interactive=True, timeout=120)
            else:
                fail_str = (
                    "[Warning] Sudo credentials unavailable for unattended App Store update.  "
                    "Skipping 'mas' upgrade to avoid stalling."
                )
                self.log.print_store(fail_str, comp)
                return False

        for app_id, app_name, version_info in updatable_apps:
            self.log.print_store(f"════ Updating {app_name} ({app_id}) ════", comp)
            app_timeout = self.calculate_timeout(app_id)
            if self.debug_vars.get('debug', 0) > 0:
                self.log.printf(f"[MAS] Dynamic timeout for '{app_name}' ({app_id}): {app_timeout}s")
            try:
                mas_upgrade = util.RunCMD(
                    [str(self.mas_path), 'upgrade', app_id],
                    **self.cmd_options({'timeout': app_timeout}),
                )
                if not mas_upgrade:
                    fail_str = (
                        f"[Warning] 'mas upgrade {app_name}' ({app_id}) exited with code {mas_upgrade.returncode}: "
                        f"{mas_upgrade.stderr}"
                    )
                    self.log.print_store(fail_str, comp)
                    self.summary['unchanged'][app_name] = version_info or 'failed'
                elif mas_upgrade.stdout and 'Everything' not in mas_upgrade:
                    self.updates_performed = True
                    self.summary['updated'][app_name] = version_info or 'updated'
                    self.log.store(f"Successfully updated {app_name} ({app_id})", comp)
                    if 'reboot' in mas_upgrade.stdout.lower() or 'restart' in mas_upgrade.stdout.lower():
                        ver_display = f" ({version_info})" if version_info else ""
                        self.reboot.suggested.append(f"App Store: Upgraded {app_name}{ver_display} suggested a reboot")
                    if 'xcode' in app_name.lower() or app_id == '497799835':
                        Xcode(debug_vars=self.debug_vars, dry_run=self.dry_run, logger=self.log).check_and_fix_license()
                else:
                    self.summary['unchanged'][app_name] = version_info or 'already up to date'
            except KeyboardInterrupt:
                self.log.print_store(f"[Skipped] User canceled update of '{app_name}'.", comp)
                self.summary['unchanged'][app_name] = version_info or 'canceled'
                continue

        return self.updates_performed
