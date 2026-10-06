from __future__ import annotations

import json
import os
import re
import shutil
from functools import cached_property
from pathlib import Path
from typing import Any

from freshenmac.boot import RebootState
from freshenmac.config import TIMEOUT
from freshenmac.util import Logger, RunCMD, Version, parse_version
from freshenmac.xcode import Xcode

FILE_BUILD = 20261005

PATTERN: dict[str, re.Pattern[str]] = {
    'Intel Warning': re.compile(
        r"Warning: You are using macOS on Intel x86_64\..*?"
        r"https://(?:www\.)?macports\.org/?\s*"
        r"(?:This is a Tier 3 configuration:.*?"
        r"Read the above document before opening any issues or PRs\.\s*)?",
        re.DOTALL,
    ),
    'Rebase':        re.compile(r"rebase-merge|rebase-apply|in the middle of another rebase", re.IGNORECASE),
    'Reboot':        re.compile(r"restart|reboot", re.IGNORECASE),
    'Xcode':         re.compile(
        r"xcodebuild\s+-license|Xcode license agreements|agreed to the.*Xcode",
        re.IGNORECASE,
    ),
}


class HomeBrew:
    """
    Manages Homebrew updates, bottle vs source build detection, and package cleanup.
    Takes an input specifying whether the computer architecture is ARM (Apple Silicon) or x86 (Intel).
    """

    def __init__(
        self,
        is_arm: bool,
        *,
        debug: int = 0,
        debug_limit: int = 0,
        dry_run: bool = False,
        logger: Any | None = None,
        reboot: RebootState | None = None,
        timeout: int = 1800,
        upgrade_timeout: int = TIMEOUT['Upgrade'],
        xcode: Any | None = None,
    ) -> None:
        self.attempted_installs: set[str] = set()
        self.attempted_updates: set[str] = set()
        self.summary_results: dict[str, dict[str, str]] = {'updated': {}, 'unchanged': {}, 'installed': {}}
        self.debug = debug
        self.debug_limit = debug_limit
        self.debug_vars = {'debug': debug, 'debug_limit': debug_limit}
        self.dry_run = dry_run
        self.is_arm = bool(is_arm)
        self.logger = logger or Logger(dry_run=self.dry_run)
        self.reboot = reboot or RebootState()
        self.timeout = timeout
        self.upgrade_timeout = upgrade_timeout
        self.updates_performed = False
        self.xcode = xcode or Xcode(dry_run=self.dry_run, **self.debug_vars)
        self._configure_environment()

    @staticmethod
    def _configure_environment() -> None:
        """Prepend Homebrew paths to PATH and enforce non-interactive execution."""
        path_list = os.environ.get('PATH', '').split(os.pathsep)
        for brew_dir in ['/opt/homebrew/bin', '/usr/local/bin']:
            if brew_dir not in path_list:
                path_list.insert(0, brew_dir)
        os.environ['PATH'] = os.pathsep.join(path_list)
        os.environ['HOMEBREW_NO_ASK'] = '1'
        os.environ['HOMEBREW_NO_ENV_HINTS'] = '1'

    def _get_cmake_formulae(self, formula_names: list[str] | dict[str, str]) -> list[str]:
        """Identifies which outdated formulae require cmake to build."""
        names = list(formula_names.keys()) if isinstance(formula_names, dict) else list(formula_names)
        if not names:
            return []

        cmake_list: list[str] = []
        chunk_size = 50
        for i in range(0, len(names), chunk_size):
            chunk = names[i:i + chunk_size]
            a_cmd = [str(self.brew_path), 'info', '--json=v2'] + chunk
            info_cmd = RunCMD(a_cmd, timeout=min(self.timeout, 120), **self.debug_vars)
            if not info_cmd or not info_cmd.stdout.strip():
                continue
            try:
                info_data = json.loads(info_cmd.stdout)
                for formula in info_data.get('formulae', []):
                    name = formula.get('name', '')
                    build_deps = [d.lower() for d in formula.get('build_dependencies', [])]
                    deps = [d.lower() for d in formula.get('dependencies', [])]
                    if 'cmake' in build_deps or 'cmake' in deps:
                        if name and name not in cmake_list:
                            cmake_list.append(name)
            except Exception as err:
                if self.debug > 0:
                    self.logger.print(f"[Warning] Failed to parse brew info JSON: {err}")

        if 'cmake' in cmake_list:
            cmake_list.remove('cmake')
            cmake_list.insert(0, 'cmake')
        if self.debug > 1:
            self.logger.print(f"HomeBrew._get_cmake_formulae() found: {cmake_list}")
        return cmake_list

    def _get_outdated_casks(self) -> dict[str, str]:
        """Returns outdated casks."""
        cmd = RunCMD([str(self.brew_path), 'outdated', '--cask', '--json=v2'], **self.debug_vars)
        if not cmd or not cmd.stdout.strip():
            return {}

        try:
            return self._read_outdated_casks(cmd.stdout)

        except Exception as err:
            if self.debug > 0:
                self.logger.print(f"[Warning] Failed to parse brew outdated casks JSON: {err}")
            return {}

    def _get_outdated_formulae(self) -> dict[str, str]:
        """Returns outdated formulae that are not pinned."""
        cmd = RunCMD([str(self.brew_path), 'outdated', '--formula', '--json=v2'], **self.debug_vars)
        if not cmd or not cmd.stdout.strip():
            return {}

        try:
            return self._read_outdated(cmd.stdout)

        except Exception as err:
            if self.debug > 0:
                self.logger.print(f"[Warning] Failed to parse brew outdated JSON: {err}")
            return {}

    def _read_outdated(self, json_str: str) -> dict[str, str]:
        data = json.loads(json_str)
        if self.debug > 2:
            if self.debug == 1:
                self.logger.print(f"HomeBrew._read_outdated.json_str read as:\n{json.dumps(data, indent=2)}")
            else:
                self.logger.print(f"HomeBrew._read_outdated.json_str with {len(data)} keys: {[data.keys()]}")
        outdated: dict[str, str] = {}
        for i in data.get('formulae', []):
            if i.get('pinned') or 'name' not in i:
                continue

            name = i['name']
            installed_list = i.get('installed_versions', [])
            installed = ', '.join(installed_list)
            current = i.get('current_version', '')
            if installed_list and current:
                latest_installed = max(parse_version(v) for v in installed_list)
                current_ver = parse_version(current)
                if latest_installed >= current_ver:
                    if self.debug > 0:
                        self.logger.print(
                            f"[Skipping] '{name}' installed ({installed}) "
                            f"is already at or above current version ({current}).",
                        )
                    continue

            value = f"{installed} ═▷ {current}" if (installed and current) else (current or installed)
            outdated[name] = value
        return outdated

    @staticmethod
    def _read_outdated_casks(json_str: str) -> dict[str, str]:
        data = json.loads(json_str)
        outdated: dict[str, str] = {}
        for i in data.get('casks', []):
            name = i.get('name', '') or i.get('token', '')
            if not name:
                continue

            installed_list = i.get('installed_versions', [])
            installed = ', '.join(installed_list)
            current = i.get('current_version', '')
            if installed_list and current:
                latest_installed = max(parse_version(v) for v in installed_list)
                current_ver = parse_version(current)
                if latest_installed >= current_ver:
                    continue

            value = f"{installed} ═▷ {current}" if (installed and current) else (current or installed)
            outdated[name] = value
        return outdated

    def _recover_rebase(self) -> bool:
        """
        Recovers Homebrew and all taps from interrupted/stranded git rebases and conflicts.
        """
        try:
            repo_cmd = RunCMD([str(self.brew_path), '--repository'], **self.debug_vars)
            if repo_cmd and repo_cmd.stdout.strip():
                repo_path = Path(repo_cmd.stdout.strip())
            else:
                repo_path = Path('/usr/local/Homebrew')
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
                self.log(f"[Self-Healing] Found stranded rebase lock in {repo}. Aborting rebase...")
                RunCMD(['git', '-C', str(repo), 'rebase', '--abort'], **self.debug_vars)
                shutil.rmtree(lock_dir, ignore_errors=True)
                shutil.rmtree(apply_dir, ignore_errors=True)

            status_cmd = RunCMD(['git', '-C', str(repo), 'status', '--porcelain'], **self.debug_vars)
            if status_cmd and ('UU ' in status_cmd.stdout or 'both modified' in status_cmd.stdout or is_dirty):
                self.log(f"[Self-Healing] Cleaning conflicted/dirty tap repository {repo.name}...")
                RunCMD(['git', '-C', str(repo), 'rebase', '--abort'], **self.debug_vars)
                RunCMD(['git', '-C', str(repo), 'checkout', '-f', '.'], **self.debug_vars)
                RunCMD(['git', '-C', str(repo), 'clean', '-fd'], **self.debug_vars)
                RunCMD(['git', '-C', str(repo), 'reset', '--hard', 'origin/HEAD'], **self.debug_vars)
                is_dirty = True

            if is_dirty:
                recovered = True
                self.log(f"[Self-Healing] Rebase recovery in {repo.name} succeeded.")
        if recovered:
            RunCMD([str(self.brew_path), 'update-reset'], **self.cmd_options())

        return recovered

    def _resolve_and_update_dependencies(self, formula: str, outdated_formulae: dict[str, str] | None = None) -> None:
        """
        Inspects dependencies for formula:
        1. Dependency NOT installed: installs dependency.
        2. Dependency installed and outdated: upgrades dependency first.
        3. Dependency installed and up to date: leaves it be.
        """
        a_cmd = [str(self.brew_path), 'deps', '-n', '--include-build', formula]
        deps_cmd = RunCMD(a_cmd, timeout=60, **self.debug_vars)
        if not deps_cmd or not deps_cmd.stdout.strip():
            return

        deps = [
            line.strip() for line in deps_cmd.stdout.splitlines()
            if line.strip() and not line.startswith(('Warning:', 'Hide these hints', 'This means'))
        ]
        if not deps:
            return

        installed_info = self.brew_list
        outdated = outdated_formulae if outdated_formulae is not None else {}

        already_ok: list[str] = []
        to_update: list[tuple[str, str]] = []
        to_install: list[str] = []

        for dep in deps:
            if dep not in installed_info or not installed_info[dep]:
                to_install.append(dep)
            elif dep in outdated:
                to_update.append((dep, outdated[dep]))
            else:
                already_ok.append(dep)

        for dep in already_ok:
            self.logger.print(f"Dependency '{dep}' for '{formula}' is already installed and up to date.")
        if to_update:
            self.log(f"Dependencies for '{formula}' needing upgrade ({len(to_update)}):")
            for dep, ver_str in to_update:
                self.logger.print(f"  • {dep} ({ver_str})")
        if to_install:
            self.log(f"Dependencies for '{formula}' needing installation ({len(to_install)}):")
            for dep in to_install:
                self.logger.print(f"  • {dep}")

        for dep, ver_str in to_update:
            self.log(f"════ Upgrading outdated dependency {dep} ({ver_str}) (for {formula}) ════")
            cmd = RunCMD(
                [str(self.brew_path), 'upgrade', '--yes', dep],
                **self.cmd_options({'timeout': self.upgrade_timeout}),
            )
            if cmd:
                if '==> Installing' in cmd or '==> Upgrading' in cmd:
                    self.updates_performed = True
                self.attempted_updates.add(dep)
                outdated.pop(dep, None)
                self.__dict__.pop('brew_list', None)
                self.logger.store(f"Successfully upgraded dependency {dep} ({ver_str})", 'Homebrew')
            else:
                self._warning('upgrade', dep, cmd.returncode, cmd.stderr)

        for dep in to_install:
            self.log(f"════ Installing missing dependency {dep} (for {formula}) ════")
            cmd = RunCMD(
                [str(self.brew_path), 'install', '--yes', dep],
                **self.cmd_options({'timeout': self.upgrade_timeout}),
            )
            if not cmd:
                output = cmd.std_all
                if PATTERN['Xcode'].search(output):
                    self._warning('install', dep)
                    if self.xcode.check_and_fix_license():
                        self.log(f"Retrying 'brew install {dep}' after accepting Xcode license...")
                        cmd = RunCMD(
                            [str(self.brew_path), 'install', '--yes', dep],
                            **self.cmd_options({'timeout': self.upgrade_timeout}),
                        )
            if cmd:
                if '==> Installing' in cmd or '==> Upgrading' in cmd:
                    self.updates_performed = True
                self.attempted_installs.add(dep)
                self.__dict__.pop('brew_list', None)
                self.logger.store(f"Successfully installed dependency {dep}", 'Homebrew')
            else:
                self._warning('install', dep, cmd.returncode, cmd.stderr)

    @staticmethod
    def _strip_intel_warning(text: str) -> str:
        """Strip Homebrew Intel x86_64 deprecation notice from output."""
        if not text:
            return ''

        return PATTERN['Intel Warning'].sub('', text).strip()

    def _upgrade_casks(self, outdated_casks: dict[str, str]) -> None:
        """Upgrades outdated casks one at a time."""
        for c_name, ver in outdated_casks.items():
            ver_str = f" ({ver})" if ver else ""
            self.log(f"════ Upgrading cask {c_name}{ver_str} ════")
            self.attempted_updates.add(c_name)
            try:
                a_cmd = [str(self.brew_path), 'upgrade', '--cask', '--yes', c_name]
                cask_upgrade = RunCMD(a_cmd, **self.cmd_options({'timeout': self.upgrade_timeout}))
                if not cask_upgrade:
                    self._warning('upgrade --cask', ver_str, cask_upgrade.returncode, cask_upgrade.stderr)
                elif cask_upgrade:
                    if '==> Upgrading' in cask_upgrade or '==> Installing' in cask_upgrade:
                        self.updates_performed = True
                    self.logger.store(f"Successfully upgraded cask {c_name}{ver_str}", 'Homebrew')
                    if PATTERN['Reboot'].search(str(cask_upgrade)):
                        self.reboot.suggested += 1
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
            self.logger.print(f"  • {ver_str}")

        if not self.xcode.check_and_install_clt():
            self.log("[Warning] Skipping CMake builds because Xcode Command Line Tools are missing.")
            return

        self.xcode.check_and_fix_license()

        for key, value in cmake_dict.items():
            self.attempted_updates.add(key)
            try:
                self._resolve_and_update_dependencies(key, outdated_formulae)
                self.log(f"════ Upgrading {value} (requires cmake) ════")
                a_cmd = [str(self.brew_path), 'upgrade', '--yes', key]
                brew_upgrade = RunCMD(a_cmd, **self.cmd_options({'timeout': self.upgrade_timeout}))
                if not brew_upgrade:
                    output = brew_upgrade.std_all
                    if PATTERN['Xcode'].search(output):
                        self._warning('upgrade', value)
                        if self.xcode.check_and_fix_license():
                            self.log(f"Retrying 'brew upgrade {value}' after accepting Xcode license...")
                            brew_upgrade = RunCMD(a_cmd, **self.cmd_options({'timeout': self.upgrade_timeout}))
                if not brew_upgrade:
                    self._warning('upgrade', value, brew_upgrade.returncode, brew_upgrade.stderr)
                elif brew_upgrade:
                    if '==> Upgrading' in brew_upgrade or '==> Installing' in brew_upgrade:
                        self.updates_performed = True
                    self.logger.store(f"Successfully upgraded {value} (requires cmake)", 'Homebrew')
                    if PATTERN['Reboot'].search(brew_upgrade.stdout):
                        self.reboot.suggested += 1
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
        self.attempted_updates.add(formula)
        try:
            self._resolve_and_update_dependencies(formula, outdated_formulae)
            formatted_ver = f"{formula} ({ver_str})" if ver_str else formula
            self.log(f"════ Upgrading {formatted_ver} ════")
            a_cmd = [str(self.brew_path), 'upgrade', '--yes', formula]
            brew_upgrade = RunCMD(a_cmd, **self.cmd_options({'timeout': self.upgrade_timeout}))
            if not brew_upgrade:
                output = brew_upgrade.std_all
                if PATTERN['Xcode'].search(output):
                    self._warning('upgrade', formatted_ver)
                    if self.xcode.check_and_fix_license():
                        self.log(f"Retrying 'brew upgrade {formatted_ver}' after accepting Xcode license...")
                        brew_upgrade = RunCMD(a_cmd, **self.cmd_options({'timeout': self.upgrade_timeout}))
            if not brew_upgrade:
                self._warning('upgrade', formatted_ver, brew_upgrade.returncode, brew_upgrade.stderr)
                return False

            elif brew_upgrade:
                if '==> Upgrading' in brew_upgrade or '==> Installing' in brew_upgrade:
                    self.updates_performed = True
                self.logger.store(f"Successfully upgraded {formatted_ver}", 'Homebrew')
                if PATTERN['Reboot'].search(brew_upgrade.stdout):
                    self.reboot.suggested += 1
                return True

        except KeyboardInterrupt:
            self.log(f"\n[Skipped] User canceled upgrade of '{formula}'.")
            return False

        return False

    def _upgrade_formulae(self, outdated_formulae: dict[str, str]) -> None:
        """Upgrades all outdated formulae one at a time."""
        for f_name, ver in list(outdated_formulae.items()):
            self._upgrade_formula(f_name, ver, outdated_formulae)

    def _warning(self, action: str, package: str, err_no: int = 0, err_msg: str = '') -> None:
        message = 'failed because the Xcode license agreement has not been accepted'
        if err_no:
            message = f"exited with code {err_no}: {self._strip_intel_warning(err_msg)}".strip(': ')
        self.log(f"[Warning] 'brew {action} {package}' {message}.")

    @cached_property
    def brew_list(self) -> dict[str, list[Version]]:
        brew_list_dict: dict[str, list[Version]] = {}
        a_cmd = [str(self.brew_path), 'list', '--versions']
        list_cmd = RunCMD(a_cmd, **self.debug_vars)
        for line in list_cmd.splitlines():
            parts = line.strip().split()
            if parts:
                brew_list_dict[parts[0]] = [parse_version(v) for v in parts[1:]]
        return brew_list_dict

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

        which_brew = shutil.which('brew')
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

    def cmd_options(self, update_options: dict | None = None) -> dict[str, Any]:
        """Common options dictionary for long-running system/app updates under utility QoS."""
        options = {
            'cancelable':  True,
            'debug':       self.debug,
            'debug_limit': self.debug_limit,
            'taskpolicy':  'utility',
            'timeout':     self.timeout,
        }
        if update_options:
            options.update(update_options)
        return options

    def log(self, message: str) -> None:
        """Logs message to console and persistent log file."""
        self.logger.print_store(message, component='Homebrew')

    def snapshot_installed(self) -> dict[str, str]:
        """Snapshots all installed formulae and casks with their primary version."""
        snapshots: dict[str, str] = {}
        cmd_formulae = RunCMD([str(self.brew_path), 'list', '--versions'], **self.debug_vars)
        for line in cmd_formulae.splitlines():
            parts = line.strip().split()
            if parts:
                if len(parts) > 1:
                    latest = max((parse_version(v) for v in parts[1:]), default=parse_version(parts[1]))
                    snapshots[parts[0]] = str(latest)
                else:
                    snapshots[parts[0]] = 'installed'
        cmd_casks = RunCMD([str(self.brew_path), 'list', '--cask', '--versions'], **self.debug_vars)
        for line in cmd_casks.splitlines():
            parts = line.strip().split()
            if parts:
                if len(parts) > 1:
                    latest = max((parse_version(v) for v in parts[1:]), default=parse_version(parts[1]))
                    snapshots[parts[0]] = str(latest)
                else:
                    snapshots[parts[0]] = 'installed'
        return snapshots

    def update(self) -> None:
        self.log(f"════ Updating Homebrew ({'Apple Silicon' if self.is_arm else 'Intel'}) ════")

        if self.dry_run:
            self.log("[Dry-run] Would run Homebrew update.")
            self.log("[Dry-run] Would inspect outdated formulae and install missing dependencies.", )
            self.log("[Dry-run] Would upgrade outdated formulae and casks individually.")
            self.log("[Dry-run] Would run Homebrew cleanup.")
            return

        start_snapshot = self.snapshot_installed()

        # 1. Update Homebrew metadata
        u_cmd = [str(self.brew_path), 'update', '--force']
        update_cmd = RunCMD(u_cmd, **self.cmd_options())
        update_output = update_cmd.std_all if isinstance(getattr(update_cmd, 'std_all', None), str) else ''
        if (update_output and PATTERN['Rebase'].search(update_output)) or not update_cmd:
            self.log(
                "[Warning] Homebrew repository is stuck in an interrupted git rebase or dirty tap. Self-healing...",
            )
            if self._recover_rebase():
                self.log("Retrying Homebrew update after clearing stranded rebase...")
                RunCMD(u_cmd, **self.cmd_options())
            else:
                self.log("[Warning] Rebase self-healing did not find any stranded locks.")

        # 2. Check for outdated formulae and casks
        outdated_formulae = self._get_outdated_formulae()
        outdated_casks = self._get_outdated_casks()
        cmake_formulae = self._get_cmake_formulae(outdated_formulae) if outdated_formulae else []
        if cmake_formulae:
            self._upgrade_cmake_formulae(cmake_formulae, outdated_formulae)

        remaining_formulae = {k: v for k, v in outdated_formulae.items() if k not in cmake_formulae}
        if remaining_formulae:
            self.log(
                f"Found {len(remaining_formulae)} remaining formula{'e' if len(remaining_formulae) != 1 else ''} "
                "(upgrading individually):",
            )
            for f_name, ver in remaining_formulae.items():
                ver_str = f" ({ver})" if ver else ""
                self.logger.print(f"  • {f_name}{ver_str}")
            self._upgrade_formulae(remaining_formulae)
        elif not cmake_formulae:
            self.log("All Homebrew formulae are up to date.")

        if outdated_casks:
            self.log(
                f"Found {len(outdated_casks)} outdated Homebrew cask{'s' if len(outdated_casks) != 1 else ''} "
                "(upgrading individually):",
            )
            for c_name, ver in outdated_casks.items():
                ver_str = f" ({ver})" if ver else ""
                self.logger.print(f"  • {c_name}{ver_str}")
            self._upgrade_casks(outdated_casks)
        else:
            self.log("All Homebrew casks are up to date.")

        # 3. Clean up old downloads and kegs
        self.log("════ Cleaning up Homebrew ════")
        cleanup_cmd = RunCMD([str(self.brew_path), 'cleanup', '--prune=all'], **self.cmd_options())
        if cleanup_cmd:
            freed_lines = [
                line.strip() for line in cleanup_cmd.stdout.splitlines()
                if 'freed' in line.lower() or 'pruned' in line.lower()
            ]
            if freed_lines:
                self.log(f"Homebrew cleanup: {freed_lines[-1]}")
            else:
                self.log("Homebrew cleanup completed successfully.")
        else:
            self.log("[Warning] Homebrew cleanup encountered an issue.")

        end_snapshot = self.snapshot_installed()
        for pkg in self.attempted_updates:
            start_ver = start_snapshot.get(pkg, '')
            end_ver = end_snapshot.get(pkg, start_ver)
            if start_ver and end_ver and parse_version(end_ver) > parse_version(start_ver):
                self.summary_results['updated'][pkg] = f"{start_ver} ═▷ {end_ver}"
            elif start_ver:
                self.summary_results['unchanged'][pkg] = start_ver

        for pkg in self.attempted_installs:
            end_ver = end_snapshot.get(pkg, 'installed')
            if pkg not in start_snapshot:
                self.summary_results['installed'][pkg] = end_ver
            else:
                start_ver = start_snapshot.get(pkg, '')
                if start_ver and end_ver and parse_version(end_ver) > parse_version(start_ver):
                    self.summary_results['updated'][pkg] = f"{start_ver} ═▷ {end_ver}"
                else:
                    self.summary_results['unchanged'][pkg] = end_ver

        if self.summary_results['updated']:
            self.log(f"\nSuccessfully updated {len(self.summary_results['updated'])} Homebrew package(s):")
            for pkg, ver in self.summary_results['updated'].items():
                self.logger.print(f"  ✔︎ {pkg} ({ver})")
        if self.summary_results['installed']:
            self.log(f"\nNewly installed {len(self.summary_results['installed'])} Homebrew package(s):")
            for pkg, ver in self.summary_results['installed'].items():
                self.logger.print(f"  ✚ {pkg} ({ver})")
        if self.summary_results['unchanged']:
            self.log(f"\nAttempted but unchanged {len(self.summary_results['unchanged'])} Homebrew package(s):")
            for pkg, ver in self.summary_results['unchanged'].items():
                self.logger.print(f"  • {pkg} ({ver})")
