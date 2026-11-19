from __future__ import annotations

import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any

from freshenmac import fix, plist
from freshenmac.boot import RebootState
from freshenmac.config import APP_INFO, DEFAULTS, TIMEOUT
from freshenmac.util import Logger, RunCMD, Version, parse_version
from freshenmac.xcode import Xcode

FILE_BUILD = 20261118
NEW_INSTALL = 'New Install'
PATTERN: dict[str, re.Pattern[str]] = {
    'Content Length': re.compile(r"content-length:\s*(?P<length>\d+)", re.IGNORECASE),
    'Intel Warning':  re.compile(
        r"(Warning: You are using macOS on Intel x86_64\..*?"
        r"https://(?:www\.)?macports\.org/?\s*)|"
        r"(This is a Tier 3 configuration:.*?"
        r"Read the above document before opening any issues or PRs\.\s*)",
        re.DOTALL,
    ),
    'Rebase':         re.compile(r"rebase-merge|rebase-apply|in the middle of another rebase", re.IGNORECASE),
    'Reboot':         re.compile(
        r"(?:system|computer|macOS)\s+(?:reboot|restart)|"
        r"(?:reboot|restart)\s+your\s+(?:mac|system|computer)|"
        r"restart\s+is\s+required",
        re.IGNORECASE,
    ),
}


@dataclass
class BrewBottles:
    """
    Manages installed Homebrew packages, version lookups, and upgrade lifecycle snapshots.
    """
    installed: dict[str, list[Version]] = field(default_factory=dict)
    loaded: bool = False

    def __contains__(self, pkg: str) -> bool:
        return pkg in self.installed

    def __getitem__(self, pkg: str) -> list[Version]:
        return self.installed[pkg]

    @staticmethod
    def compute_summary(
        snapshot_start: dict[str, str],
        snapshot_end: dict[str, str],
        attempted_updates: list[str],
        attempted_installs: list[str],
        arrow: str = DEFAULTS['Arrow'],
    ) -> dict[str, dict[str, str]]:
        """
        Computes updated, unchanged, and newly installed version mappings between snapshots.
        :return: Dictionary mapping 'Installed', 'Unchanged', and 'Updated' to package version dicts.
        """
        results: dict[str, dict[str, str]] = {
            'Installed': {},
            'Unchanged': {},
            'Updated':   {},
        }

        for a_pkg in attempted_updates:
            start_ver = snapshot_start.get(a_pkg, '')
            end_ver = snapshot_end.get(a_pkg, '')
            if start_ver and end_ver and parse_version(end_ver) > parse_version(start_ver):
                results['Updated'][a_pkg] = f"{start_ver} {arrow} {end_ver}"
            elif start_ver and a_pkg not in snapshot_end:
                results['Unchanged'][a_pkg] = f"{start_ver} (missing)"
            elif start_ver:
                results['Unchanged'][a_pkg] = start_ver
            elif end_ver:
                results['Installed'][a_pkg] = end_ver

        for a_pkg in attempted_installs:
            end_ver = snapshot_end.get(a_pkg, 'installed')
            if a_pkg not in snapshot_start:
                results['Installed'][a_pkg] = end_ver
            else:
                start_ver = snapshot_start.get(a_pkg, '')
                if start_ver and end_ver and parse_version(end_ver) > parse_version(start_ver):
                    results['Updated'][a_pkg] = f"{start_ver} {arrow} {end_ver}"
                else:
                    results['Unchanged'][a_pkg] = end_ver

        return results

    @classmethod
    def from_cmd(cls, cmd_output: str | RunCMD) -> BrewBottles:
        """Parses `brew list --versions` output into a BrewBottles inventory."""
        installed: dict[str, list[Version]] = {}
        for line in cmd_output.splitlines():
            parts = line.strip().split()
            if not parts:
                continue

            if len(parts) > 1:
                installed[parts[0]] = [parse_version(v) for v in parts[1:]]
            else:
                installed[parts[0]] = [parse_version('installed')]

        return cls(installed=installed, loaded=True)

    def get(self, pkg: str, default: Any = None) -> Any:
        return self.installed.get(pkg, default)

    def latest_version(self, pkg: str) -> Version | None:
        """Returns the highest installed Version for package, or None if uninstalled."""
        versions = self.installed.get(pkg)
        if not versions:
            return None

        return max(versions)

    def mark_installed(self, pkg: str, ver: str | Version = 'installed') -> None:
        """Registers or updates a package as installed with the given version."""
        v = ver if isinstance(ver, Version) else parse_version(str(ver))
        self.installed[pkg] = [v]

    def snapshot(self) -> dict[str, str]:
        """Returns mapping of package names to their primary installed version string."""
        res: dict[str, str] = {}
        for k, versions in self.installed.items():
            if versions:
                res[k] = str(max(versions))
            else:
                res[k] = 'installed'

        return res


@dataclass
class BrewOutdated:
    """
    Encapsulates outdated Homebrew packages, command telemetry, and query status.
    """
    success: bool

    casks: dict[str, str] = field(default_factory=dict)
    formulae: dict[str, str] = field(default_factory=dict)
    error: str = ''
    issue: str = ''
    returncode: int = -1
    stderr: str = ''
    stdout: str = ''

    def __contains__(self, key: object) -> bool:
        return key in ('casks', 'formulae')

    def __getitem__(self, key: str) -> dict[str, str]:
        if key == 'casks':
            return self.casks

        if key == 'formulae':
            return self.formulae

        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        if key == 'casks':
            return self.casks

        if key == 'formulae':
            return self.formulae

        return default

    @property
    def has_updates(self) -> bool:
        return bool(self.casks or self.formulae)

    @property
    def is_empty(self) -> bool:
        return not self.has_updates

    def to_dict(self) -> dict[str, dict[str, str]]:
        return {'casks': dict(self.casks), 'formulae': dict(self.formulae)}


class HomeBrew:
    """
    Manages Homebrew updates, bottle vs source build detection, and package cleanup.
    Takes an input specifying whether the computer architecture is ARM (Apple Silicon) or x86 (Intel).
    """

    def __init__(
        self,
        is_arm: bool,
        *,
        arrow: str = DEFAULTS['Arrow'],
        cask_timeout: int = TIMEOUT['Homebrew']['Cask'],
        cmake_timeout: int = TIMEOUT['Homebrew']['CMake Build'],
        debug_vars: dict[str, int] | None = None,
        download_speed_mbps: float = 0.0,
        dry_run: bool = False,
        logger: Any | None = None,
        reboot: RebootState | None = None,
        timeout: int = TIMEOUT['Homebrew']['Formula'],
    ) -> None:
        self.arrow = arrow
        self.attempted_installs: set[str] = set()
        self.attempted_updates: set[str] = set()
        self.summary_results: dict[str, dict[str, str]] = {'updated': {}, 'unchanged': {}, 'installed': {}}
        self.cask_timeout = cask_timeout
        self.cmake_timeout = cmake_timeout
        self.debug_vars = debug_vars or {'debug': 0, 'debug_limit': 0}
        self.download_speed_mbps = download_speed_mbps
        self.dry_run = dry_run
        self.is_arm = bool(is_arm)
        self.logger = logger or Logger(dry_run=self.dry_run)
        self.reboot = reboot if reboot is not None else RebootState()  # Checking existence
        self.timeout = timeout
        self.updates_performed = False
        self.xcode = Xcode(debug_vars=self.debug_vars, dry_run=dry_run, logger=self.logger)
        self._bottles: BrewBottles = BrewBottles()
        self._fix: fix.RecoveryEngine | None = None
        self.environ: dict[str, str] = self._configure_environment()

    def _apply_dependency_changes(
        self,
        a_formula: str,
        dep_actions: dict[str, str],
        outdated: dict[str, str],
    ) -> bool:
        """Upgrades outdated dependencies and installs missing dependencies for a formula."""
        success = True
        for dep, ver_str in dep_actions.items():
            is_install = ver_str == NEW_INSTALL
            action = 'install' if is_install else 'upgrade'
            if is_install:
                self.log(f"════ Installing missing dependency {dep} (for {a_formula}) ════")
            else:
                self.log(f"════ Upgrading outdated dependency {dep} ({ver_str}) (for {a_formula}) ════")

            cmd = self._make_brew_cmd(dep, action)
            if not cmd:
                self._warning(action, dep, cmd.returncode, cmd.stderr)
                success = False
                continue

            if '==> Installing' in cmd or '==> Upgrading' in cmd:
                self.updates_performed = True
            if is_install:
                self.attempted_installs.add(dep)
                self.bottles.mark_installed(dep, 'installed')
                self.logger.store(f"Successfully installed dependency {dep}", 'Homebrew')
            else:
                self.attempted_updates.add(dep)
                outdated.pop(dep, None)
                new_ver = ver_str.split(self.arrow)[-1].strip() if self.arrow in ver_str else ver_str
                self.bottles.mark_installed(dep, new_ver)
                self.logger.store(f"Successfully upgraded dependency {dep} ({ver_str})", 'Homebrew')

        return success

    def _cleanup_homebrew(self) -> None:
        """Runs `brew cleanup --prune=all` and logs freed space summary or warnings."""
        self.log("════ Cleaning up Homebrew ════")
        cleanup_cmd = RunCMD([str(self.brew_path), 'cleanup', '--prune=all'], **self.cmd_options())
        if cleanup_cmd:
            freed_lines = [
                line for line in cleanup_cmd.splitlines(clean=True)
                if 'freed' in line.lower() or 'pruned' in line.lower()
            ]
            if freed_lines:
                self.log(f"Homebrew cleanup: {freed_lines[-1]}")
            else:
                self.log("Homebrew cleanup completed successfully.")
        else:
            self.log("[Warning] Homebrew cleanup encountered an issue.")

    def _configure_environment(self) -> dict[str, str]:
        """Build subprocess environment with architecture-appropriate PATH and non-interactive Homebrew flags."""
        brew_bin = '/opt/homebrew/bin' if self.is_arm else '/usr/local/bin'
        existing = [p for p in os.environ.get('PATH', '').split(os.pathsep) if p]
        return {
            'HOMEBREW_NO_ASK':       '1',
            'HOMEBREW_NO_ENV_HINTS': '1',
            'PATH':                  os.pathsep.join(dict.fromkeys([brew_bin, *existing])),
        }

    def _filter_dependencies(
        self,
        a_formula: str,
        deps: list[str],
        outdated_formulae: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Partitions dependencies into missing installs ('New Install') and outdated upgrades."""
        installed_info = self.brew_list
        outdated = outdated_formulae if outdated_formulae is not None else {}

        already_ok: list[str] = []
        actions: dict[str, str] = {}

        for dep in deps:
            if dep not in installed_info or not installed_info[dep]:
                actions[dep] = NEW_INSTALL
            elif dep in outdated:
                actions[dep] = outdated[dep]
            else:
                already_ok.append(dep)

        for dep in already_ok:
            self.logger.printf(f"Dependency '{dep}' for '{a_formula}' is already installed and up to date.")
        if actions:
            self.log(f"Dependencies for '{a_formula}' needing action ({len(actions)}):")
            for dep, ver_str in actions.items():
                self.logger.printf(f"  • {dep} ({ver_str or 'outdated'})")

        return actions

    def _finalize_summary(self, expected_updates: dict[str, str], start_snapshot: dict[str, str]) -> None:
        """Captures end snapshot, computes/validates update summary, and prints package results."""
        end_snapshot = self.snapshot_installed()
        summary = BrewBottles.compute_summary(
            start_snapshot,
            end_snapshot,
            list(self.attempted_updates),
            list(self.attempted_installs),
            arrow=self.arrow,
        )
        for category, items in summary.items():
            self.summary_results[category.lower()].update(items)

        for pkg in summary['Unchanged']:
            self.summary_results['updated'].pop(pkg, None)

        self._validate_updates(expected_updates, start_snapshot, end_snapshot)

        display_categories = (
            ('updated', 'Successfully updated', '✔︎'),
            ('installed', 'Newly installed', '✚'),
            ('unchanged', 'Attempted but unchanged', '•'),
        )
        for cat_key, label, symbol in display_categories:
            items = self.summary_results[cat_key]
            if items:
                self.log(f"\n{label} {len(items)} Homebrew package(s):")
                for pkg, ver in items.items():
                    self.logger.printf(f"  {symbol} {pkg} ({ver})")

    def _get_cmake_formulae(self, formula_names: list[str] | dict[str, str]) -> list[str]:
        """Identifies which outdated formulae require cmake to build."""
        names = list(formula_names.keys()) if isinstance(formula_names, dict) else list(formula_names)
        if not names:
            return []

        a_cmd = [str(self.brew_path), 'deps', '--direct', '--include-build', '--for-each', *names]
        debug_args = dict(self.debug_vars)
        if debug_args.get('debug', 0) > 1 and debug_args.get('debug_limit', 0) < 0:
            debug_args['debug'] = 1
        cmake_list: list[str] = []
        if deps_cmd := RunCMD(a_cmd, environ=self.environ, timeout=min(self.timeout, 120), **debug_args):
            deps_dict = deps_cmd.parse_dict()
            cmake_list = [pkg for pkg, deps in deps_dict.items() if pkg in names and 'cmake' in deps.split()]

        if 'cmake' in cmake_list:
            cmake_list.remove('cmake')
            cmake_list.insert(0, 'cmake')
        if self.debug_vars['debug'] > 1:
            self.logger.printf(f"HomeBrew._get_cmake_formulae() found: {cmake_list}")
        return cmake_list

    def _get_dependencies(self, a_formula: str) -> list[str] | None:
        """Queries build and runtime dependencies for a formula, recovering once on command failure."""
        a_cmd = [str(self.brew_path), 'deps', '-n', '--include-build', a_formula]
        deps_cmd = RunCMD(a_cmd, environ=self.environ, timeout=60, **self.debug_vars)
        if not deps_cmd:
            deps_cmd = self.fixer.recover(
                fix.FailureInfo.from_runcmd(deps_cmd, package=a_formula, subsystem='Homebrew'),
                lambda: RunCMD(a_cmd, environ=self.environ, timeout=60, **self.debug_vars),
            ) or deps_cmd
            if not deps_cmd:
                self._warning('deps', a_formula, deps_cmd.returncode, deps_cmd.stderr)
                return None

        return deps_cmd.splitlines(clean=True)

    def _get_outdated(self, json_data: str | dict[str, Any] | None = None) -> BrewOutdated:
        """Returns formatted outdated packages mapping. Coordinates read and parse if no data passed."""
        if json_data is None:
            return self._read_outdated()

        return self._parse_outdated(json_data)

    def _inspect_outdated(self) -> BrewOutdated:
        """Queries and normalizes outdated Homebrew packages, logging warnings on failure."""
        outdated = self._get_outdated()
        if not isinstance(outdated, BrewOutdated):
            outdated = BrewOutdated(
                'Error' not in outdated,
                casks=outdated.get('casks', {}),
                formulae=outdated.get('formulae', {}),
            )

        if not outdated.success:
            self.log(f"[Warning] Failed to inspect outdated Homebrew packages ({outdated.issue}): {outdated.error}")
            self.log("[Warning] Skipping formula and cask upgrades due to inspection failure.")

        return outdated

    def _load_bottles(self) -> BrewBottles:
        """Executes `brew list --versions` and returns a BrewBottles inventory."""
        cmd = RunCMD([str(self.brew_path), 'list', '--versions'], environ=self.environ, **self.debug_vars)
        return BrewBottles.from_cmd(cmd)

    def _log_dry_run(self) -> None:
        """Logs dry-run placeholder messages for an update cycle."""
        self.log("[Dry-run] Would run Homebrew update.")
        self.log("[Dry-run] Would inspect outdated formulae and install missing dependencies.")
        self.log("[Dry-run] Would upgrade outdated formulae and casks individually.")
        self.log("[Dry-run] Would run Homebrew cleanup.")

    def _log_pending_upgrades(self, items: dict[str, str], singular: str, plural: str) -> None:
        """Logs queued package upgrades with optional version transitions."""
        label = singular if len(items) == 1 else plural
        self.log(f"Found {len(items)} {label} (upgrading individually):")
        for name, ver in items.items():
            ver_str = f" ({ver})" if ver else ""
            self.logger.printf(f"  • {name}{ver_str}")

    def _make_brew_cmd(
        self,
        bottle: str,
        action: str = 'upgrade',
        *,
        cask: bool = False,
        nice: bool = True,
        timeout: int = 0,
    ) -> RunCMD:
        """
        :param cask: Is it a cask?
        :param nice: True: ``taskpolicy -c utility`` + command
        """
        a_cmd = [str(self.brew_path), action, '--yes']
        if cask:
            a_cmd.append('--cask')
        a_cmd.append(bottle)
        opts_dict: dict[str, Any] = {}
        if not nice:
            opts_dict['taskpolicy'] = None
        if timeout:
            opts_dict['timeout'] = timeout
        cmd = RunCMD(a_cmd, **self.cmd_options(opts_dict))
        if not cmd:
            cmd = self.fixer.recover(
                fix.FailureInfo.from_runcmd(cmd, package=bottle, subsystem='Homebrew'),
                lambda: RunCMD(a_cmd, **self.cmd_options(opts_dict)),
            ) or cmd
        return cmd

    def _parse_bottle_entries(self, items: Any) -> dict[str, str]:
        """Parses list of bottle metadata dictionaries into package-to-version mapping."""
        if not isinstance(items, list):
            return {}

        res: dict[str, str] = {}
        for an_item in items:
            if not isinstance(an_item, dict):
                continue

            name = an_item.get('name', '') or an_item.get('token', '')
            if an_item.get('pinned') or not name:
                continue

            installed_list = an_item.get('installed_versions', [])
            installed = ', '.join(installed_list)
            current = an_item.get('current_version', '')
            if installed_list and current:
                latest_installed = max(parse_version(v) for v in installed_list)
                if latest_installed >= parse_version(current):
                    if self.debug_vars['debug'] > 0:
                        self.logger.printf(
                            f"[Skipping] '{name}' installed ({installed}) "
                            f"is already at or above current version ({current}).",
                        )
                    continue

            res[name] = self._ver_to_ver(installed, current)
        return res

    def _parse_outdated(
        self,
        json_data: str | dict[str, Any],
        *,
        returncode: int = 0,
        stderr: str = '',
        stdout: str = '',
    ) -> BrewOutdated:
        """Parses brew outdated JSON data into BrewOutdated value object."""
        data: Any = json_data
        if isinstance(json_data, str):
            try:
                data = json.loads(json_data)
            except json.decoder.JSONDecodeError as err_json:
                error_msg = f"Failed to parse `brew outdated` JSON: {err_json}"
                self.log(f"[Warning] {error_msg}")
                return BrewOutdated(
                    False,
                    error=error_msg,
                    issue='JSONDecodeError',
                    returncode=returncode,
                    stderr=stderr,
                    stdout=stdout or json_data,
                )

        if not isinstance(data, dict):
            error_msg = f"Unexpected outdated data root type: {type(data).__name__}"
            self.log(f"[Warning] {error_msg}")
            return BrewOutdated(
                False,
                error=error_msg,
                issue='InvalidData',
                returncode=returncode,
                stderr=stderr,
                stdout=stdout,
            )

        if 'Error' in data:
            error_msg = f"HomeBrew._parse_outdated: Error payload received:\n{json.dumps(data, indent=2)}"
            self.log(f"[Warning] {error_msg}")
            return BrewOutdated(
                False,
                error=error_msg,
                issue='ErrorPayload',
                returncode=returncode,
                stderr=stderr,
                stdout=stdout,
            )

        expected_keys = ('casks', 'formulae')
        if expected_keys[0] not in data and expected_keys[1] not in data:
            error_msg = f"Brew outdated did not contain either key: {list(expected_keys)}. Got: {list(data.keys())}"
            self.log(f"[Warning] {error_msg}")
            return BrewOutdated(
                False,
                error=error_msg,
                issue='MissingKeys',
                returncode=returncode,
                stderr=stderr,
                stdout=stdout,
            )

        if self.debug_vars['debug'] > 0:
            if self.debug_vars['debug'] == 1:
                self.logger.printf(f"HomeBrew._parse_outdated read as:\n{json.dumps(data, indent=2)}")
            else:
                self.logger.printf(f"HomeBrew._parse_outdated with {len(data)} keys: {list(data.keys())}")

        casks = self._parse_bottle_entries(data.get('casks', []))
        formulae = self._parse_bottle_entries(data.get('formulae', []))
        return BrewOutdated(
            True,
            casks=casks,
            formulae=formulae,
            returncode=returncode,
            stderr=stderr,
            stdout=stdout,
        )

    def _read_outdated(self) -> BrewOutdated:
        """Returns BrewOutdated with formulae and casks from brew outdated --json=v2."""
        a_cmd = [str(self.brew_path), 'outdated', '--json=v2']
        if self.debug_vars['debug_limit'] < 0 and self.debug_vars['debug'] > 1:
            cmd = RunCMD(a_cmd, debug=1, environ=self.environ)
            if cmd:
                self.logger.printf(f"{json.loads(cmd.stdout)}\n")
            else:
                self.logger.printf(f"RunCMD({a_cmd}) failed:{cmd.std_all}\n")
        else:
            cmd = RunCMD(a_cmd, environ=self.environ, **self.debug_vars)

        if not cmd or not cmd.stdout.strip():
            issue = 'NoOutput' if cmd.stdout == '' and cmd.returncode == 0 else 'CommandFailed'
            error_msg = f"brew outdated exited with code {cmd.returncode}: {cmd.error or 'No output'}"
            self.log(f"[Warning] {error_msg}")
            return BrewOutdated(
                error=error_msg,
                issue=issue,
                returncode=cmd.returncode,
                stderr=cmd.error,
                stdout=cmd.stdout,
                success=False,
            )

        return self._parse_outdated(
            cmd.stdout,
            returncode=cmd.returncode,
            stderr=cmd.error,
            stdout=cmd.stdout,
        )

    def _resolve_and_update_dependencies(self, a_formula: str, outdated_formulae: dict[str, str] | None = None) -> bool:
        """
        Inspects dependencies for formula:
        1. Dependency NOT installed: installs dependency.
        2. Dependency installed and outdated: upgrades dependency first.
        3. Dependency installed and up to date: leaves it be.
        :return: True if all dependencies were successfully resolved/updated, False otherwise.
        """
        deps = self._get_dependencies(a_formula)
        if deps is None:  # Error getting dependencies
            return False

        if not deps:  # No dependencies returned
            return True

        dep_actions = self._filter_dependencies(a_formula, deps, outdated_formulae)
        if not dep_actions:
            return True  # No dependencies need to change

        outdated = outdated_formulae if outdated_formulae is not None else {}
        return self._apply_dependency_changes(a_formula, dep_actions, outdated)

    @staticmethod
    def _strip_intel_warning(text: str) -> str:
        """Strip Homebrew Intel x86_64 deprecation notice from output."""
        if not text:
            return ''

        return PATTERN['Intel Warning'].sub('', text).strip()

    @staticmethod
    def _truncate_lines(text: str, max_lines: int = 7, head: int = 3, tail: int = 3) -> str:
        """Truncates multi-line text to head + omitted notice + tail if line count > max_lines."""
        if not text:
            return ''

        lines = text.strip().splitlines()
        if len(lines) <= max_lines:
            return text.strip()

        omitted = len(lines) - (head + tail)
        truncated_lines = lines[:head] + [f"... ({omitted} lines omitted) ..."] + lines[-tail:]
        return '\n'.join(truncated_lines)

    def _update_metadata(self) -> None:
        """Runs `brew update --force`, recovering from stranded git rebases on failure."""
        u_cmd = [str(self.brew_path), 'update', '--force']
        update_cmd = RunCMD(u_cmd, **self.cmd_options())
        if (update_cmd.std_all and PATTERN['Rebase'].search(update_cmd.std_all)) or not update_cmd:
            self.log(
                "[Warning] Homebrew repository is stuck in an interrupted git rebase or dirty tap.  Self-healing...",
            )
            if self.recover_rebase():
                self.log("Retrying Homebrew update after clearing stranded rebase...")
                RunCMD(u_cmd, **self.cmd_options())
            else:
                self.log("[Warning] Rebase self-healing did not find any stranded locks.")

    def _upgrade_casks(self, outdated_casks: dict[str, str]) -> None:
        """Upgrades outdated casks one at a time."""
        casks_to_upgrade = {k: v for k, v in outdated_casks.items() if k != 'freshenmac'}
        for c_name, ver in casks_to_upgrade.items():
            ver_str = f"{c_name} ({ver})" if ver else c_name
            self.log(f"════ Upgrading cask {ver_str} ════")
            self.attempted_updates.add(c_name)
            try:
                cask_upgrade = self._make_brew_cmd(c_name, cask=True, timeout=self.calculate_cask_timeout(c_name))
                if not cask_upgrade:
                    self._warning('upgrade --cask', ver_str, cask_upgrade.returncode, cask_upgrade.stderr)
                elif cask_upgrade:
                    if '==> Upgrading' in cask_upgrade or '==> Installing' in cask_upgrade:
                        self.updates_performed = True
                    self.logger.store(f"Successfully upgraded cask {ver_str}", 'Homebrew')
                    if PATTERN['Reboot'].search(str(cask_upgrade)):
                        self.reboot.suggested.append(f"Homebrew: Upgraded cask suggested a reboot: {ver_str}")
            except KeyboardInterrupt:
                self.log(f"\n[Skipped] User canceled upgrade of cask '{c_name}'.")
                continue

    def _upgrade_cmake_formulae(self, cmake_formulae: list[str], outdated_formulae: dict[str, str]) -> None:
        """Upgrades formulae that depend on cmake individually, handling Xcode license acceptance."""
        self.log(
            f"Found {len(cmake_formulae)} formula{'e' if len(cmake_formulae) != 1 else ''} "
            "requiring cmake (building/upgrading individually):",
        )

        cmake_dict: dict[str, str] = {}
        for f_name in cmake_formulae:
            ver = outdated_formulae.get(f_name, '')
            ver_str = f"{f_name} ({ver})" if ver else f_name
            cmake_dict[f_name] = ver_str
            self.logger.printf(f"  • {ver_str}")

        if not self.xcode.check_and_install_clt():
            self.log("[Warning] Skipping CMake builds because Xcode Command Line Tools are missing.")
            for f_name in cmake_formulae:
                self.summary_results['unchanged'].setdefault(
                    f_name, outdated_formulae.get(f_name, '') or 'skipped (missing Xcode CLT)',
                )
            return

        self.xcode.check_and_fix_license()

        for key, value in cmake_dict.items():
            if outdated_formulae is not None and key not in outdated_formulae and key in self.attempted_updates:
                if self.debug_vars['debug'] > 0:
                    self.logger.printf(f"[Skipping] '{key}' was already upgraded as a dependency.")
                continue

            self.attempted_updates.add(key)
            try:
                if not self._resolve_and_update_dependencies(key, outdated_formulae):
                    self.log(f"[Skipped] Skipping upgrade of '{key}' because a required dependency failed to update.")
                    continue

                self.log(f"════ Upgrading {value} (requires cmake) ════")
                brew_upgrade = self._make_brew_cmd(key, timeout=self.cmake_timeout)
                if not brew_upgrade:
                    self._warning('upgrade', value, brew_upgrade.returncode, brew_upgrade.stderr)
                elif brew_upgrade:
                    if '==> Upgrading' in brew_upgrade or '==> Installing' in brew_upgrade:
                        self.updates_performed = True
                    self.logger.store(f"Successfully upgraded {value} (requires cmake)", 'Homebrew')
                    if outdated_formulae is not None:
                        outdated_formulae.pop(key, None)
                    if PATTERN['Reboot'].search(brew_upgrade.stdout):
                        self.reboot.suggested.append(f"Homebrew: Upgraded formula suggested a reboot: {value}")
            except KeyboardInterrupt:
                self.log(f"\n[Skipped] User canceled upgrade of '{key}'.")
                continue

    def _upgrade_formula(
        self,
        formula: str,
        ver_str: str = '',
        outdated_formulae: dict[str, str] | None = None,
    ) -> bool:
        """Upgrades a single formula, handling missing dependencies and Xcode license acceptance."""
        if outdated_formulae is not None and formula not in outdated_formulae and formula in self.attempted_updates:
            if self.debug_vars['debug'] > 0:
                self.logger.printf(f"[Skipping] '{formula}' was already upgraded as a dependency.")
            return True

        self.attempted_updates.add(formula)
        try:
            if not self._resolve_and_update_dependencies(formula, outdated_formulae):
                self.log(f"[Skipped] Skipping upgrade of '{formula}' because a required dependency failed to update.")
                return False

            formatted_ver = f"{formula} ({ver_str})" if ver_str else formula
            self.log(f"════ Upgrading {formatted_ver} ════")
            brew_upgrade = self._make_brew_cmd(formula)
            if not brew_upgrade:
                self._warning('upgrade', formatted_ver, brew_upgrade.returncode, brew_upgrade.stderr)
                return False

            elif brew_upgrade:
                if '==> Upgrading' in brew_upgrade or '==> Installing' in brew_upgrade:
                    self.updates_performed = True
                self.logger.store(f"Successfully upgraded {formatted_ver}", 'Homebrew')
                if outdated_formulae is not None:
                    outdated_formulae.pop(formula, None)
                if PATTERN['Reboot'].search(brew_upgrade.stdout):
                    self.reboot.suggested.append(f"Homebrew: Upgraded formula suggested a reboot: {formatted_ver}")
                return True

        except KeyboardInterrupt:
            self.log(f"\n[Skipped] User canceled upgrade of '{formula}'.")

        return False

    def _upgrade_formulae(self, outdated_formulae: dict[str, str]) -> None:
        """Upgrades all outdated formulae one at a time."""
        for f_name, ver in list(outdated_formulae.items()):
            if f_name not in outdated_formulae:
                continue

            self._upgrade_formula(f_name, ver, outdated_formulae)

    def _upgrade_outdated(self, outdated: BrewOutdated) -> dict[str, str]:
        """Upgrades outdated FreshenMac cask, CMake formulae, remaining formulae, and casks."""
        expected_updates = {**outdated.formulae, **outdated.casks}
        if 'freshenmac' in outdated.casks:
            self.self_update_freshenmac(outdated.casks.pop('freshenmac'))

        cmake_formulae = self._get_cmake_formulae(outdated.formulae)
        # # TODO: Only update Major and Minor versions of formulae that use cmake (unless something needs the Micro)
        if cmake_formulae:
            self._upgrade_cmake_formulae(cmake_formulae, outdated.formulae)

        remaining_formulae = {k: v for k, v in outdated.formulae.items() if k not in cmake_formulae}
        if remaining_formulae:
            self._log_pending_upgrades(remaining_formulae, 'remaining formula', 'remaining formulae')
            self._upgrade_formulae(remaining_formulae)
        elif not cmake_formulae:
            self.log("All Homebrew formulae are up to date.")

        if outdated.casks:
            self._log_pending_upgrades(outdated.casks, 'outdated Homebrew cask', 'outdated Homebrew casks')
            self._upgrade_casks(outdated.casks)
        else:
            self.log("All Homebrew casks are up to date.")

        return expected_updates

    def _validate_updates(
        self,
        expected_updates: dict[str, str],
        start_snapshot: dict[str, str],
        end_snapshot: dict[str, str],
    ) -> list[str]:
        """Validates expected updates reached their target versions in the end snapshot."""
        if not expected_updates or not (start_snapshot or end_snapshot):
            return []

        unverified: list[str] = []
        for pkg, ver_transition in expected_updates.items():
            target_ver = ver_transition.split(self.arrow)[-1].strip() if self.arrow in ver_transition else ''
            start_ver = start_snapshot.get(pkg, '')
            end_ver = end_snapshot.get(pkg, '')
            did_not_advance = bool(start_ver and end_ver and parse_version(end_ver) <= parse_version(start_ver))

            if not end_ver:
                unverified.append(f"{pkg} (missing)")
                self.summary_results['updated'].pop(pkg, None)
                self.summary_results['unchanged'].setdefault(pkg, f"{start_ver} (missing)" if start_ver else 'missing')
            elif (target_ver and parse_version(end_ver) < parse_version(target_ver)) or did_not_advance:
                below_target = target_ver and parse_version(end_ver) < parse_version(target_ver)
                reason = f"{end_ver} < {target_ver}" if below_target else end_ver
                unverified.append(f"{pkg} ({reason})")
                if did_not_advance:
                    self.summary_results['updated'].pop(pkg, None)
                if pkg not in self.summary_results['updated']:
                    self.summary_results['unchanged'].setdefault(pkg, end_ver)

        if unverified:
            self.log(
                f"[Warning] {len(unverified)} expected Homebrew update(s) not verified updated: "
                f"{', '.join(unverified)}",
            )
        else:
            self.log(f"✔︎ Verified all {len(expected_updates)} expected Homebrew update(s) updated.")

        return unverified

    def _ver_to_ver(self, start_ver: str, end_ver: str) -> str:
        return f"{start_ver} {self.arrow} {end_ver}" if (start_ver and end_ver) else (end_ver or start_ver)

    def _warning(self, action: str, package: str, err_no: int = 0, err_msg: str = '') -> None:
        message = 'failed because the Xcode license agreement has not been accepted'
        if err_no:
            cleaned = self._strip_intel_warning(err_msg)
            truncated = self._truncate_lines(cleaned)
            message = f"exited with code {err_no}: {truncated}".strip(': ')
        self.log(f"[Warning] 'brew {action} {package}' {message}.")

    @property
    def bottles(self) -> BrewBottles:
        """Returns the current BrewBottles inventory, refreshing from brew list if uninitialized."""
        if not self._bottles.loaded:
            self._bottles = self._load_bottles()

        return self._bottles

    @property
    def brew_list(self) -> dict[str, list[Version]]:
        """Returns mapping of package names to their installed Version list."""
        return self.bottles.installed

    @brew_list.setter
    def brew_list(self, val: dict[str, list[Version]]) -> None:
        self._bottles = BrewBottles(installed=val, loaded=True)

    @brew_list.deleter
    def brew_list(self) -> None:
        self._bottles = BrewBottles()

    @cached_property
    def brew_path(self) -> Path:
        """Locate the brew binary based on architecture and filesystem presence."""
        some_paths = ['/opt/homebrew/bin/brew', '/usr/local/bin/brew']
        if not self.is_arm:
            some_paths.reverse()

        for candidate in some_paths:
            brew_candidate = Path(candidate)
            if brew_candidate.exists():
                return brew_candidate

        which_brew = shutil.which('brew', path=self.environ['PATH'])
        if which_brew:
            return Path(which_brew)

        which_brew_cmd = RunCMD(['/bin/zsh', '-l', '-c', 'which brew'])
        zsh_brew = which_brew_cmd.strip()
        if which_brew_cmd and zsh_brew and Path(zsh_brew).is_file():
            return Path(zsh_brew)

        raise FileNotFoundError(
            '\nHomebrew is not installed.\n'
            '\tHomebrew homepage: https://brew.sh/\n'
            '\tDirect install:   `/bin/bash -c "$(curl -fsSL '
            'https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"`',
        )

    def calculate_cask_timeout(self, cask_token: str) -> int:
        """Calculates dynamic timeout in seconds scaled by cask size and configured download speed."""
        speed_mbps = self.download_speed_mbps
        if not speed_mbps:
            prefs = plist.load_preferences()
            speed_mbps = float(prefs.get('download-speed') or DEFAULTS['Download Speed Mbps'])

        buffer = DEFAULTS['Download Speed Buffer']
        rate_mb_per_sec = max(0.1, speed_mbps / 8.0)

        size_mb = self.get_cask_size_mb(cask_token)
        if size_mb == 0.0:
            return self.cask_timeout

        buffered_download_sec = (size_mb / rate_mb_per_sec) * (1.0 + buffer)
        base_overhead = TIMEOUT['Homebrew']['Base Overhead']
        return int(base_overhead + buffered_download_sec)

    def cmd_options(self, update_options: dict | None = None, **kwargs: Any) -> dict[str, Any]:
        """Common options dictionary for long-running system/app updates under utility QoS."""
        options = {
            'cancelable': True,
            'cwd':        Path('/'),
            'environ':    self.environ,
            'taskpolicy': 'utility',
            'timeout':    self.timeout,
            **self.debug_vars,
        }
        if update_options:
            options.update(update_options)
        if kwargs:
            options.update(kwargs)
        return options

    @property
    def fixer(self) -> fix.RecoveryEngine:
        """Lazily initializes the self-healing recovery engine and registers diagnostic rules."""
        if self._fix is None:
            self._fix = fix.RecoveryEngine(
                debug_vars=self.debug_vars,
                dry_run=self.dry_run,
                logger=self.logger,
                target=self,
            )
        assert self._fix is not None
        return self._fix

    def get_cask_size_mb(self, cask_token: str) -> float:
        """Retrieves download size in MB for a cask via brew info URL HEAD request.  Returns 0.0 if unavailable."""
        cmd = RunCMD([str(self.brew_path), 'info', '--json=v2', '--cask', cask_token], environ=self.environ, timeout=30)
        if not cmd:
            return 0.0

        try:
            data = json.loads(cmd.stdout)
            casks = data.get('casks', [])
            if not casks:
                return 0.0

            url = casks[0].get('url')
            if not url:
                return 0.0

            head_cmd = RunCMD(['curl', '-sI', '-L', '--max-time', '5', url], timeout=10)
            if not head_cmd:
                return 0.0

            match = PATTERN['Content Length'].search(head_cmd.stdout)
            if match:
                return int(match.group('length')) / (1024 * 1024)

        except Exception:
            return 0.0

        return 0.0

    def log(self, message: str) -> None:
        """Logs message to console and persistent log file."""
        self.logger.print_store(message, component='Homebrew')

    def recover_rebase(self) -> bool:
        """
        Recovers Homebrew and all taps from interrupted/stranded git rebases and conflicts.
        """
        try:
            repo_cmd = RunCMD([str(self.brew_path), '--repository'], environ=self.environ, **self.debug_vars)
            if repo_cmd and repo_cmd.stdout.strip():
                repo_path = Path(repo_cmd.stdout.strip())
            else:
                repo_path = Path('/opt/homebrew' if self.is_arm else '/usr/local/Homebrew')
        except Exception as err_clean:
            self.log(str(err_clean))
            return False

        recovered = False
        repos_to_check = [repo_path]
        taps_dir = repo_path / 'Library' / 'Taps'
        if taps_dir.is_dir():
            for tap in taps_dir.glob('*/*'):
                if tap.is_dir() and (tap / '.git').is_dir():
                    repos_to_check.append(tap)

        for repo in repos_to_check:
            is_dirty = False
            lock_dir = repo / '.git' / 'rebase-merge'
            apply_dir = repo / '.git' / 'rebase-apply'
            if lock_dir.exists() or apply_dir.exists():
                is_dirty = True
                self.log(f"[Self-Healing] Found stranded rebase lock in {repo}.  Aborting rebase...")
                RunCMD(['git', '-C', str(repo), 'rebase', '--abort'], **self.debug_vars)
                shutil.rmtree(lock_dir, ignore_errors=True)
                shutil.rmtree(apply_dir, ignore_errors=True)
            status_cmd = RunCMD(['git', '-C', str(repo), 'status', '--porcelain'], **self.debug_vars)
            if status_cmd and ('UU ' in status_cmd.stdout or 'both modified' in status_cmd.stdout or is_dirty):
                self.log(f"[Self-Healing] Cleaning conflicted/dirty tap repository {repo.name}...")
                git_cmds = ['rebase --abort', 'checkout -f .', 'clean -fd', 'reset --hard origin/HEAD']
                for a_git_cmd in git_cmds:
                    RunCMD(['git', '-C', str(repo)] + a_git_cmd.split(), **self.debug_vars)
                is_dirty = True
            if is_dirty:
                recovered = True
                self.log(f"[Self-Healing] Rebase recovery in {repo.name} succeeded.")

        if recovered:
            RunCMD([str(self.brew_path), 'update-reset'], **self.cmd_options())
        return recovered

    def reinstall_dependency(self, needs: str) -> bool:
        """Reinstalls a formula to heal broken Python module links or build environments."""
        if not needs:
            return False

        self.log(f"════ Reinstalling build dependency '{needs}' to repair environment ════")
        cmd = self._make_brew_cmd(needs, 'reinstall')
        if cmd:
            self.logger.store(f"Successfully reinstalled dependency '{needs}'", 'Homebrew')
            return True

        self._warning('reinstall', needs, cmd.returncode, cmd.stderr)
        return False

    def self_update_freshenmac(self, ver_str: str | None = None) -> bool:
        """
        Checks if freshenmac cask is installed and outdated via Homebrew.
        If an update is available, upgrades freshenmac before proceeding with any other updates.
        """
        if not self.brew_path:
            return False

        if ver_str is None:
            outdated = self._get_outdated()
            ver_str = outdated.get('casks', {}).pop(APP_INFO['Name'].lower(), None)
            if not ver_str:
                return False

        curr_ver = ver_str.split(self.arrow)[-1].strip() if self.arrow in ver_str else ver_str
        formatted_ver = f"{APP_INFO['Name']} ({ver_str})" if ver_str else APP_INFO['Name']
        self.log(f"════ Updating {APP_INFO['Name']} First: {formatted_ver} ════")
        try:
            upgrade_cmd = self._make_brew_cmd(APP_INFO['Name'].lower(), cask=True, nice=False, timeout=300)
            if upgrade_cmd:
                self.updates_performed = True
                self.summary_results['updated'][APP_INFO['Name'].lower()] = ver_str
                self.logger.print_store(
                    f"✔︎ [Self-Update] {APP_INFO['Name']} upgraded to {curr_ver} (takes effect on next run).",
                    'SelfUpdate',
                )
                if not self.dry_run and RunCMD.can_sudo(validate=True):
                    target_script = plist.LaunchAgent(debug_vars=self.debug_vars, logger=self.logger).target_script
                    if target_script.is_file():
                        RunCMD(
                            [sys.executable, str(target_script), '--action', 'Install'],
                            **self.cmd_options({'taskpolicy': None, 'timeout': 60}),
                        )
                return True

            self._warning('upgrade --cask', APP_INFO['Name'].lower(), upgrade_cmd.returncode, upgrade_cmd.stderr)
            return False

        except Exception as err_upgrade:
            self.logger.store(f"Self-update check error: {err_upgrade}", 'SelfUpdate')
            return False

    def snapshot_installed(self) -> dict[str, str]:
        """Snapshots all installed formulae and casks with their primary version."""
        self._bottles = self._load_bottles()
        return self._bottles.snapshot()

    def update(self) -> bool:
        self.log(f"════ Updating Homebrew ({'Apple Silicon' if self.is_arm else 'Intel'}) ════")
        if self.dry_run:
            self._log_dry_run()
            return False

        start_snapshot = self.snapshot_installed()
        self._update_metadata()

        outdated = self._inspect_outdated()
        expected_updates = self._upgrade_outdated(outdated) if outdated.success else {}

        self._cleanup_homebrew()
        self._finalize_summary(expected_updates, start_snapshot)

        return bool(self.summary_results['updated'] or self.summary_results['installed'] or self.updates_performed)
