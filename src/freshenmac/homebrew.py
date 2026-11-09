from __future__ import annotations

import json
import os
import re
import shutil
from functools import cached_property
from pathlib import Path
from typing import Any

from freshenmac import fix, plist
from freshenmac.boot import RebootState
from freshenmac.config import DEFAULTS, TIMEOUT
from freshenmac.util import Logger, RunCMD, Version, parse_version
from freshenmac.xcode import Xcode

FILE_BUILD = 20261109

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


class HomeBrew:
    """
    Manages Homebrew updates, bottle vs source build detection, and package cleanup.
    Takes an input specifying whether the computer architecture is ARM (Apple Silicon) or x86 (Intel).
    """

    def __init__(
        self,
        is_arm: bool,
        *,
        cask_timeout: int = TIMEOUT['Homebrew']['Cask'],
        cmake_timeout: int = TIMEOUT['Homebrew']['CMake Build'],
        debug_vars: dict[str, int] | None = None,
        download_speed_mbps: float = 0.0,
        dry_run: bool = False,
        logger: Any | None = None,
        reboot: RebootState | None = None,
        timeout: int = TIMEOUT['Homebrew']['Formula'],
    ) -> None:
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
        self._fix: fix.RecoveryEngine | None = None
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

    def _failure_context(self, cmd: RunCMD, package: str) -> fix.FailureInfo:
        """Constructs a FailureInfo from a failed RunCMD command and target package."""
        return fix.FailureInfo.from_runcmd(
            cmd,
            log_file=self._get_log_path(package),
            package=package,
            subsystem='Homebrew',
        )

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
        if deps_cmd := RunCMD(a_cmd, timeout=min(self.timeout, 120), **debug_args):
            deps_dict = deps_cmd.parse_dict()
            cmake_list = [pkg for pkg, deps in deps_dict.items() if pkg in names and 'cmake' in deps.split()]

        if 'cmake' in cmake_list:
            cmake_list.remove('cmake')
            cmake_list.insert(0, 'cmake')
        if self.debug_vars['debug'] > 1:
            self.logger.printf(f"HomeBrew._get_cmake_formulae() found: {cmake_list}")
        return cmake_list

    @staticmethod
    def _get_log_path(package: str) -> Path:
        """Discovers the most recent Homebrew build log path for the specified package."""
        if not package:
            return Path()

        log_dir = Path.home() / 'Library' / 'Logs' / 'Homebrew' / package
        if not log_dir.is_dir():
            return Path()

        # .        `ls ${log_dir}/*.log`, key=(Use time modified as the value)
        return max(log_dir.glob('*.log'), key=lambda p: p.stat().st_mtime, default=Path())

    def _get_outdated(
        self,
        list_data: list[dict[str, Any]] | str | dict[str, Any] | None = None,
    ) -> dict[str, dict[str, str]]:
        """Returns formatted outdated packages mapping. Coordinates read and parse if no data passed."""
        if list_data is None:
            list_data = self._parse_outdated(self._read_outdated())
        elif isinstance(list_data, (str, dict)):
            list_data = self._parse_outdated(list_data)

        outdated: dict[str, dict[str, str]] = {'casks': {}, 'formulae': {}}
        for i in list_data:
            name = i.get('name', '') or i.get('token', '')
            if i.get('pinned') or not name:
                continue

            installed_list = i.get('installed_versions', [])
            installed = ', '.join(installed_list)
            current = i.get('current_version', '')
            if installed_list and current:
                latest_installed = max(parse_version(v) for v in installed_list)
                current_ver = parse_version(current)
                if latest_installed >= current_ver:
                    if self.debug_vars['debug'] > 0:
                        self.logger.printf(
                            f"[Skipping] '{name}' installed ({installed}) "
                            f"is already at or above current version ({current}).",
                        )
                    continue

            value = f"{installed} ═▷ {current}" if (installed and current) else (current or installed)
            bottle_type = i.get('bottle_type', 'formulae')
            if bottle_type in outdated:
                outdated[bottle_type][name] = value

        return outdated

    def _make_brew_cmd(self, bottle: str, action: str = 'upgrade', cask: bool = False, timeout: int = 0) -> RunCMD:
        a_cmd = [str(self.brew_path), action, '--yes']
        if cask:
            a_cmd.append('--cask')
        a_cmd.append(bottle)
        if not timeout:
            timeout = self.timeout
        cmd = RunCMD(a_cmd, **self.cmd_options({'timeout': timeout}))
        if not cmd:
            context = self._failure_context(cmd, bottle)
            cmd = self.fixer.recover(context, lambda: RunCMD(a_cmd, **self.cmd_options({'timeout': timeout}))) or cmd
        return cmd

    def _parse_outdated(self, json_data: str | dict[str, Any]) -> list[dict[str, str]]:
        """Parses brew outdated JSON data into mappings of outdated formulae and casks."""
        expected_keys = ['casks', 'formulae']
        data = json.loads(json_data) if isinstance(json_data, str) else json_data
        if self.debug_vars['debug'] > 0:
            if self.debug_vars['debug'] == 1:
                self.logger.printf(f"HomeBrew._read_outdated.json_str read as:\n{json.dumps(data, indent=2)}")
            else:
                self.logger.printf(f"HomeBrew._read_outdated.json_str with {len(data)} keys: {[data.keys()]}")

        if expected_keys[0] not in data and expected_keys[1] not in data:
            self.log(f"Brew outdated did not contain either key: {expected_keys}.  Got: {data.keys()}")
            return []

        useful_list = []
        for bottle_type, bottle_info in data.items():
            if bottle_type in expected_keys and isinstance(bottle_info, list):
                for a_bottle in bottle_info:
                    a_bottle['bottle_type'] = bottle_type
                    useful_list.append(a_bottle)
        return useful_list

    def _read_outdated(self) -> dict[str, list[dict[str, Any]]]:
        """Returns raw JSON dictionary with 'formulae' and 'casks' from brew outdated."""
        a_cmd = [str(self.brew_path), 'outdated', '--json=v2']
        if self.debug_vars['debug_limit'] < 0 and self.debug_vars['debug'] > 1:
            cmd = RunCMD(a_cmd, debug=1)
            self.logger.printf(f"{json.loads(cmd.stdout)}\n")
        else:
            cmd = RunCMD(a_cmd, **self.debug_vars)
        if not cmd or not cmd.stdout.strip():
            return {'casks': [], 'formulae': []}

        try:
            return json.loads(cmd.stdout)

        except Exception as err_json:
            message = f"[Warning] Failed to parse brew outdated JSON: {err_json}"
            if self.debug_vars['debug'] > 0:
                self.log(message)
            else:
                self.logger.store(message, 'Homebrew')
            return {'casks': [], 'formulae': []}

    def _resolve_and_update_dependencies(self, a_formula: str, outdated_formulae: dict[str, str] | None = None) -> bool:
        """
        Inspects dependencies for formula:
        1. Dependency NOT installed: installs dependency.
        2. Dependency installed and outdated: upgrades dependency first.
        3. Dependency installed and up to date: leaves it be.
        :return: True if all dependencies were successfully resolved/updated, False otherwise.
        """
        a_cmd = [str(self.brew_path), 'deps', '-n', '--include-build', a_formula]
        deps_cmd = RunCMD(a_cmd, timeout=60, **self.debug_vars)
        if not deps_cmd or not deps_cmd.stdout.strip():
            return True

        deps = [
            line.strip() for line in deps_cmd.stdout.splitlines()
            if line.strip() and not line.startswith(('Warning:', 'Hide these hints', 'This means'))
        ]
        if not deps:
            return True

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
            self.logger.printf(f"Dependency '{dep}' for '{a_formula}' is already installed and up to date.")
        if to_update:
            self.log(f"Dependencies for '{a_formula}' needing upgrade ({len(to_update)}):")
            for dep, ver_str in to_update:
                self.logger.printf(f"  • {dep} ({ver_str})")
        if to_install:
            self.log(f"Dependencies for '{a_formula}' needing installation ({len(to_install)}):")
            for dep in to_install:
                self.logger.printf(f"  • {dep}")

        success = True
        for dep, ver_str in to_update:
            self.log(f"════ Upgrading outdated dependency {dep} ({ver_str}) (for {a_formula}) ════")
            cmd = self._make_brew_cmd(dep)
            if cmd:
                if '==> Installing' in cmd or '==> Upgrading' in cmd:
                    self.updates_performed = True
                self.attempted_updates.add(dep)
                outdated.pop(dep, None)
                new_ver = ver_str.split('═▷')[-1].strip() if '═▷' in ver_str else ver_str
                self.brew_list[dep] = [parse_version(new_ver)]
                self.logger.store(f"Successfully upgraded dependency {dep} ({ver_str})", 'Homebrew')
            else:
                self._warning('upgrade', dep, cmd.returncode, cmd.stderr)
                success = False

        for dep in to_install:
            self.log(f"════ Installing missing dependency {dep} (for {a_formula}) ════")
            cmd = self._make_brew_cmd(dep, 'install')
            if cmd:
                if '==> Installing' in cmd or '==> Upgrading' in cmd:
                    self.updates_performed = True
                self.attempted_installs.add(dep)
                self.brew_list[dep] = [parse_version('installed')]
                self.logger.store(f"Successfully installed dependency {dep}", 'Homebrew')
            else:
                self._warning('install', dep, cmd.returncode, cmd.stderr)
                success = False

        return success

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

    def _warning(self, action: str, package: str, err_no: int = 0, err_msg: str = '') -> None:
        message = 'failed because the Xcode license agreement has not been accepted'
        if err_no:
            cleaned = self._strip_intel_warning(err_msg)
            truncated = self._truncate_lines(cleaned)
            message = f"exited with code {err_no}: {truncated}".strip(': ')
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
        cmd = RunCMD([str(self.brew_path), 'info', '--json=v2', '--cask', cask_token], timeout=30)
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
                self.log(f"[Self-Healing] Found stranded rebase lock in {repo}.  Aborting rebase...")
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
            ver_str = outdated.get('casks', {}).pop('freshenmac', None)
            if not ver_str:
                return False

        curr_ver = ver_str.split('═▷')[-1].strip() if '═▷' in ver_str else ver_str
        formatted_ver = f"freshenmac ({ver_str})" if ver_str else 'freshenmac'
        self.log(f"════ Updating FreshenMac First: {formatted_ver} ════")
        try:
            upgrade_cmd = RunCMD(
                [str(self.brew_path), 'upgrade', '--cask', '--yes', 'freshenmac'],
                **self.cmd_options({'timeout': 300}),
            )
            if upgrade_cmd:
                self.updates_performed = True
                self.summary_results['updated']['freshenmac'] = ver_str
                self.logger.print_store(
                    f"✔︎ [Self-Update] FreshenMac successfully upgraded to {curr_ver} (takes effect on next run).",
                    'SelfUpdate',
                )
                return True

            self._warning('upgrade --cask', 'freshenmac', upgrade_cmd.returncode, upgrade_cmd.stderr)
            return False

        except Exception as err:
            self.logger.store(f"Self-update check error: {err}", 'SelfUpdate')
            return False

    def snapshot_installed(self) -> dict[str, str]:
        """Snapshots all installed formulae and casks with their primary version."""
        brew_list_dict: dict[str, list[Version]] = {}
        snapshots: dict[str, str] = {}
        cmd_list = RunCMD([str(self.brew_path), 'list', '--versions'], **self.debug_vars)
        for line in cmd_list.splitlines():
            parts = line.strip().split()
            if parts:
                if len(parts) > 1:
                    versions = [parse_version(v) for v in parts[1:]]
                    latest = max(versions, default=parse_version(parts[1]))
                    snapshots[parts[0]] = str(latest)
                    brew_list_dict[parts[0]] = versions
                else:
                    snapshots[parts[0]] = 'installed'
                    brew_list_dict[parts[0]] = [parse_version('installed')]
        if 'brew_list' not in self.__dict__:
            self.__dict__['brew_list'] = brew_list_dict
        return snapshots

    def update(self) -> bool:
        self.log(f"════ Updating Homebrew ({'Apple Silicon' if self.is_arm else 'Intel'}) ════")

        if self.dry_run:
            self.log("[Dry-run] Would run Homebrew update.")
            self.log("[Dry-run] Would inspect outdated formulae and install missing dependencies.", )
            self.log("[Dry-run] Would upgrade outdated formulae and casks individually.")
            self.log("[Dry-run] Would run Homebrew cleanup.")
            return False

        start_snapshot = self.snapshot_installed()

        # 1. Update Homebrew metadata
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

        # 2. Check for outdated formulae and casks, find CMake updates, Update FreshenMac
        outdated = self._get_outdated()
        if 'freshenmac' in outdated.get('casks', {}):
            self.self_update_freshenmac(outdated['casks'].pop('freshenmac'))

        # 3. Find and perform CMake updates
        cmake_formulae = self._get_cmake_formulae(outdated.get('formulae', {}))
        # # TODO: Only update Major and Minor versions of formulae that use cmake (unless something needs the Micro)
        if cmake_formulae:
            self._upgrade_cmake_formulae(cmake_formulae, outdated['formulae'])

        remaining_formulae = {k: v for k, v in outdated['formulae'].items() if k not in cmake_formulae}

        # 3. Perform updates
        if remaining_formulae:
            self.log(
                f"Found {len(remaining_formulae)} remaining formula{'e' if len(remaining_formulae) != 1 else ''} "
                "(upgrading individually):",
            )
            for f_name, ver in remaining_formulae.items():
                ver_str = f" ({ver})" if ver else ""
                self.logger.printf(f"  • {f_name}{ver_str}")
            self._upgrade_formulae(remaining_formulae)
        elif not cmake_formulae:
            self.log("All Homebrew formulae are up to date.")

        if outdated['casks']:
            self.log(
                f"Found {len(outdated['casks'])} outdated Homebrew cask{'s' if len(outdated['casks']) != 1 else ''} "
                "(upgrading individually):",
            )
            for c_name, ver in outdated['casks'].items():
                ver_str = f" ({ver})" if ver else ""
                self.logger.printf(f"  • {c_name}{ver_str}")
            self._upgrade_casks(outdated['casks'])
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
                self.logger.printf(f"  ✔︎ {pkg} ({ver})")
        if self.summary_results['installed']:
            self.log(f"\nNewly installed {len(self.summary_results['installed'])} Homebrew package(s):")
            for pkg, ver in self.summary_results['installed'].items():
                self.logger.printf(f"  ✚ {pkg} ({ver})")
        if self.summary_results['unchanged']:
            self.log(f"\nAttempted but unchanged {len(self.summary_results['unchanged'])} Homebrew package(s):")
            for pkg, ver in self.summary_results['unchanged'].items():
                self.logger.printf(f"  • {pkg} ({ver})")

        return bool(self.summary_results['updated'] or self.summary_results['installed'] or self.updates_performed)
