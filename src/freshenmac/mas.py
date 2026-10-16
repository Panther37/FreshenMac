from __future__ import annotations

import re
import shutil
from functools import cached_property
from pathlib import Path
from typing import Any

from freshenmac import boot, config, plist, util
from freshenmac.xcode import Xcode

FILE_BUILD = 20261014
PATTERN_MAS_UPDATE = re.compile(r"^\s*(?P<id>\d+)\s+(?P<name>.+?)\s+(?P<vers>\([^)]+\))")


class AppStore:
    """Manages updates for App Store applications via the 'mas' CLI."""

    def __init__(
        self,
        *,
        brew_path: Path | None = None,
        debug_vars: dict[str, int] | None = None,
        defer: boot.DeferState | None = None,
        dry_run: bool = False,
        ignore_mas: list[str] | set[str] | None = None,
        logger: Any | None = None,
        reboot: boot.RebootState | None = None,
        timeout: int = config.TIMEOUT['App Store'],
    ) -> None:

        self._cli_ignore = set(ignore_mas) if ignore_mas else set()
        self.brew_path = brew_path
        self.debug_vars = debug_vars or {'debug': 0, 'debug_limit': 0}
        self.defer = defer if defer is not None else boot.DeferState()  # Checking existence
        self.dry_run = dry_run
        self.log = logger or util.Logger(dry_run=self.dry_run)
        self.reboot = reboot if reboot is not None else boot.RebootState()  # Checking existence
        self.summary: dict[str, dict[str, str]] = {'installed': {}, 'unchanged': {}, 'updated': {}}
        self.timeout = timeout
        self.updates_performed = False

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
        1. Unpacks both persistent plist configuration (*raw) and runtime CLI flags (*self._cli_ignore).
        2. Converts each item to string and strips leading/trailing whitespace.
        3. Discards empty strings / whitespace-only entries via 'if str(x).strip()'.
        4. Lowercases all identifiers so matching is case-insensitive for both app names and numeric IDs.
        """
        prefs = plist.load_preferences()
        raw = prefs.get('no-mas', [])
        if isinstance(raw, (int, str)):
            raw = [raw]
        ignored = set()
        for item in (*raw, *self._cli_ignore):
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

    def update(self) -> None:
        """Checks and updates App Store applications using mas."""
        comp = 'App Store'
        if not self.has_mas or self.mas_path == Path(''):
            self.log.print_store("App Store CLI ('mas') is not installed.  Skipping 'mas' update.", comp)
            return

        self.log.print_store('════ Updating App Store Apps ════', comp)
        if self.dry_run:
            self.log.print_store("[Dry-run] Would check and update App Store apps.", comp)
            return

        outdated_cmd = util.RunCMD([str(self.mas_path), 'outdated'], timeout=60)
        if not outdated_cmd or not outdated_cmd.stdout.strip():
            self.log.print_store("All App Store apps are up to date.", comp)
            return

        found_updates = []
        for line in outdated_cmd.stdout.splitlines():
            line = line.strip()
            if not line:
                continue

            match = PATTERN_MAS_UPDATE.match(line)
            if match:
                vers = match.group('vers').strip('()').replace('->', '═▷').strip()
                found_updates.append((match.group('id'), match.group('name').strip(), vers))

        self.log.print_store(f"Found {len(found_updates)} {comp} update{'s' if len(found_updates) != 1 else ''}:", comp)
        for app_id, app_name, version_info in found_updates:
            ver_display = f" ({version_info})" if version_info else ""
            self.log.printf(f"  • {app_id}  {app_name} {ver_display}".rstrip())

        has_running_app = False
        updatable_apps = []
        for app_id, app_name, version_info in found_updates:
            if app_id in self.ignored_apps or app_name.lower() in self.ignored_apps:
                self.log.print_store(f"  [Skipping] '{app_name}' ({app_id}) is in Ignore MAS preferences.", comp)
                self.summary['unchanged'][app_name] = version_info or 'ignored'
            elif util.is_app_running(app_name, self.debug_vars):
                has_running_app = True
                self.defer.running.append(f"App Store Update: {app_name} is running, cannot update.")
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
            return

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
                return

        for app_id, app_name, version_info in updatable_apps:
            self.log.print_store(f"════ Updating {app_name} ({app_id}) ════", comp)
            try:
                mas_upgrade = util.RunCMD([str(self.mas_path), 'upgrade', app_id], **self.cmd_options())
                if not mas_upgrade:
                    fail_str = (
                        f"[Warning] 'mas upgrade {app_id}' exited with code {mas_upgrade.returncode}: "
                        f"{mas_upgrade.stderr}"
                    )
                    self.log.print_store(fail_str, comp)
                    self.summary['unchanged'][app_name] = version_info or 'failed'
                elif mas_upgrade.stdout and 'Everything' not in mas_upgrade:
                    self.updates_performed = True
                    self.summary['updated'][app_name] = version_info or 'updated'
                    self.log.store(f"Successfully updated {app_name} ({app_id})", comp)
                    if 'xcode' in app_name.lower() or app_id == '497799835':
                        Xcode(debug_vars=self.debug_vars, dry_run=self.dry_run).check_and_fix_license()
                else:
                    self.summary['unchanged'][app_name] = version_info or 'already up to date'
            except KeyboardInterrupt:
                self.log.print_store(f"[Skipped] User canceled update of '{app_name}'.", comp)
                self.summary['unchanged'][app_name] = version_info or 'canceled'
                continue
