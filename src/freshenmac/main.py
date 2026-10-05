#!/usr/bin/env python3
"""
FreshenMac - A Python script to automate macOS updates
"""

import argparse
import json
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from functools import cached_property
from pathlib import Path
from typing import Any

DEBUG_LIMIT = 10000
DEFAULT_PLIST = 'com.panther37.update_mac'
MAX_IDLE_MIN = 60
MAX_IDLE_SEC = MAX_IDLE_MIN * 60
PROGRAM_BUILD = 30
PROGRAM_DESCRIPTION = 'Updates macOS operating system, App store and homebrew'
PROGRAM_NAME = 'FreshenMac'
PROGRAM_VERSION = '1.0.0'
UPTIME_DAYS_LIMIT = 15
options: argparse.Namespace | None = None


class HomeBrew:
    """
    Manages Homebrew updates, bottle vs source build detection, and package cleanup.
    Takes an input specifying whether the computer architecture is ARM (Apple Silicon) or x86 (Intel).
    """

    def __init__(
        self,
        is_arm: bool = True,
        debug: bool = False,
        dry_run: bool = False,
        timeout: int = 1800,
        reboot: RebootState | None = None,
        xcode: Any | None = None,
    ):
        self.is_arm = bool(is_arm)
        self.debug = debug
        self.dry_run = dry_run
        self.reboot = reboot or RebootState()
        self.timeout = timeout
        self.updates_performed = False
        self.xcode = xcode or Xcode(debug=self.debug, dry_run=self.dry_run)
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
        if not names or not self.brew_path:
            return []

        brew_bin = str(self.brew_path)
        cmake_list: list[str] = []
        chunk_size = 50
        for i in range(0, len(names), chunk_size):
            chunk = names[i:i + chunk_size]
            a_cmd = [brew_bin, 'info', '--json=v2'] + chunk
            info_cmd = RunCMD(a_cmd, timeout=min(self.timeout, 120), debug=self.debug)
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
                if self.debug:
                    print(f"[Warning] Failed to parse brew info JSON: {err}")

        if 'cmake' in cmake_list:
            cmake_list.remove('cmake')
            cmake_list.insert(0, 'cmake')
        if self.debug:
            print(f"HomeBrew._get_cmake_formulae() found: {cmake_list}")
        return cmake_list

    def _get_outdated_formulae(self) -> dict[str, str]:
        """Queries Homebrew for unpinned outdated formulae with version transitions."""
        if not self.brew_path:
            return {}

        brew_bin = str(self.brew_path)
        a_cmd = [brew_bin, 'outdated', '--json=v2']
        outdated_cmd = RunCMD(a_cmd, timeout=60, debug=self.debug)
        if not outdated_cmd or not outdated_cmd.stdout.strip():
            return {}

        try:
            return self._read_outdated(outdated_cmd.stdout)
        except Exception as err:
            if self.debug:
                print(f"[Warning] Failed to parse brew outdated JSON: {err}")
            return {}

    def _read_outdated(self, json_str: str) -> dict[str, str]:
        data = json.loads(json_str)
        if self.debug:
            print(f"HomeBrew._read_outdated.json_str read as:\n{json.dumps(data, indent=2)}")
        outdated: dict[str, str] = {}
        for i in data.get('formulae', []):
            if i.get('pinned') or 'name' not in i:
                continue
            installed = ', '.join(i.get('installed_versions', []))
            current = i.get('current_version', '')
            value = ''
            if installed and current:
                value = f"{installed} ═▷ {current}"
            outdated[i['name']] = value or current or installed
        return outdated

    @staticmethod
    def _strip_intel_warning(text: str) -> str:
        """Strip Homebrew Intel x86_64 deprecation notice from output."""
        if not text:
            return ""

        pattern = re.compile(
            r"Warning: You are using macOS on Intel x86_64\..*?"
            r"https://(?:www\.)?macports\.org/?\s*"
            r"(?:This is a Tier 3 configuration:.*?"
            r"Read the above document before opening any issues or PRs\.\s*)?",
            re.DOTALL,
        )
        return pattern.sub('', text).strip()

    def _upgrade_cmake_formulae(self, cmake_formulae: list[str], outdated_formulae: dict[str, str]) -> None:
        """Upgrades formulae that depend on cmake individually, handling Xcode license acceptance."""
        brew_bin = str(self.brew_path)
        print(
            f"Found {len(cmake_formulae)} formula{'e' if len(cmake_formulae) != 1 else ''} "
            f"requiring cmake (building/upgrading individually):",
        )
        for f_name in cmake_formulae:
            ver = outdated_formulae.get(f_name)
            ver_str = f" ({ver})" if ver else ""
            print(f"  • {f_name}{ver_str}")

        self.xcode.check_and_fix_license()

        for f_name in cmake_formulae:
            ver = outdated_formulae.get(f_name)
            ver_str = f" ({ver})" if ver else ""
            print(f"═══ Upgrading {f_name}{ver_str} (requires cmake) ═══")
            a_cmd = [brew_bin, 'upgrade', '--yes', f_name]
            brew_upgrade = RunCMD(a_cmd, **self.cmd_options)
            if not brew_upgrade:
                output = brew_upgrade.allout
                if re.search(
                    r'xcodebuild\s+-license|Xcode license agreements|agreed to the.*Xcode',
                    output,
                    re.IGNORECASE,
                ):
                    print(
                        f"[Warning] 'brew upgrade {f_name}{ver_str}' "
                        "failed because the Xcode license agreement has not been accepted.",
                    )
                    if self.xcode.check_and_fix_license():
                        print(f"Retrying 'brew upgrade {f_name}{ver_str}' after accepting Xcode license...")
                        brew_upgrade = RunCMD(a_cmd, **self.cmd_options)
            if not brew_upgrade:
                clean_stderr = self._strip_intel_warning(brew_upgrade.stderr)
                error_detail = f": {clean_stderr}" if clean_stderr else ""
                print(
                    f"[Warning] 'brew upgrade {f_name}{ver_str}' exited with code {brew_upgrade.errno}{error_detail}",
                )
            elif brew_upgrade.stdout:
                if '==> Upgrading' in brew_upgrade.stdout or '==> Installing' in brew_upgrade.stdout:
                    self.updates_performed = True
                if re.search(r'restart|reboot', brew_upgrade.stdout, re.IGNORECASE):
                    self.reboot.suggested += 1

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

        print(
            '\nHomebrew is not installed.\n'
            '\tHomebrew homepage: https://brew.sh/\n'
            '\tDirect install:   `/bin/bash -c "$(curl -fsSL '
            'https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"`',
        )
        sys.exit(2)

    @property
    def cmd_options(self) -> dict[str, Any]:
        """Common options dictionary for long-running update commands under utility QoS."""
        return {
            'debug':      self.debug,
            'taskpolicy': 'utility',
            'timeout':    self.timeout,
        }

    def update(self) -> None:
        if not self.brew_path:
            print("Skipping 'brew' update.")
            return

        print(f"═══ Updating Homebrew ({'Apple Silicon' if self.is_arm else 'Intel'}) ═══")
        brew_bin = str(self.brew_path)

        if self.dry_run:
            print("[Dry-run] Would run Homebrew update.")
            print("[Dry-run] Would inspect outdated formulae for cmake dependencies and upgrade each individually.")
            print("[Dry-run] Would run Homebrew upgrade for remaining packages.")
            print("[Dry-run] Would run Homebrew cleanup.")
            return

        # 1. Update Homebrew metadata
        RunCMD([brew_bin, 'update', '--force'], **self.cmd_options)

        # 2. Check for outdated formulae needing cmake
        outdated_formulae = self._get_outdated_formulae()
        cmake_formulae = self._get_cmake_formulae(outdated_formulae) if outdated_formulae else []

        if cmake_formulae:
            self._upgrade_cmake_formulae(cmake_formulae, outdated_formulae)

        # 3. Upgrade remaining packages and casks
        print("═══ Upgrading remaining Homebrew packages ═══")
        a_cmd = [brew_bin, 'upgrade', '--yes']
        brew_upgrade = RunCMD(a_cmd, **self.cmd_options)
        if not brew_upgrade:
            output = brew_upgrade.allout
            if re.search(r'xcodebuild\s+-license|Xcode license agreements|agreed to the.*Xcode', output, re.IGNORECASE):
                print("[Warning] Homebrew upgrade failed because the Xcode license agreement has not been accepted.")
                if self.xcode.check_and_fix_license():
                    print("Retrying Homebrew upgrade after accepting Xcode license...")
                    brew_upgrade = RunCMD(a_cmd, **self.cmd_options)
        if brew_upgrade.stdout:
            if '==> Upgrading' in brew_upgrade or '==> Installing' in brew_upgrade:
                self.updates_performed = True
            if re.search(r'restart|reboot', brew_upgrade.stdout, re.IGNORECASE):
                self.reboot.suggested += 1

        # 4. Clean up old downloads and kegs
        print("═══ Cleaning up Homebrew ═══")
        RunCMD([brew_bin, 'cleanup', '--prune=all'], **self.cmd_options)


class MacOSComputer:
    """
    Represents the host Mac computer, storing hardware facts and executing
    system updates tailored for this machine.
    """

    def __init__(self, debug: bool = False, dry_run: bool = False, timeout: int = 1800):
        self._updates_performed = False
        self.debug = debug
        self.dry_run = dry_run
        self.timeout = timeout
        self.reboot = RebootState(required=int(getattr(options, 'force_reboot', 0)))

    def _update_mas(self) -> None:
        if not self.has_mas or self.mas_path == Path(''):
            print("Mac App Store CLI ('mas') is not installed.  Skipping 'mas' update.")
            return

        print('═══ Updating Mac App Store Apps ═══')
        if self.dry_run:
            print("[Dry-run] Would check and update Mac App Store apps.")
            return

        outdated_cmd = RunCMD([str(self.mas_path), 'outdated'], timeout=60)
        if not outdated_cmd or not outdated_cmd.stdout.strip():
            print("All Mac App Store apps are up to date.")
            return

        parsed_updates = []
        for line in outdated_cmd.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            match = re.match(r'^\s*(\d+)\s+(.+?)\s+(\([^)]+\))', line)
            if match:
                parsed_updates.append((match.group(1), match.group(2).strip(), match.group(3)))
            else:
                parts = line.split(maxsplit=1)
                if len(parts) >= 2 and parts[0].isdigit():
                    parsed_updates.append((parts[0], parts[1].strip(), ''))

        print(f"Found {len(parsed_updates)} Mac App Store update{'s' if len(parsed_updates) != 1 else ''}:")
        for app_id, app_name, version_info in parsed_updates:
            print(f"  • {app_id}  {app_name}  {version_info}".rstrip())

        has_running_app = False
        updatable_apps = []
        for app_id, app_name, _ in parsed_updates:
            if self.is_app_running(app_name):
                has_running_app = True
                print(f"  [Skipping] '{app_name}' is currently running on the GUI.")
            else:
                updatable_apps.append((app_id, app_name))

        if has_running_app:
            self.reboot.required += 1
            self.schedule_startup_run()

        if not updatable_apps:
            print("All pending Mac App Store updates are currently running.  Skipping for now.")
            return

        for app_id, app_name in updatable_apps:
            print(f"═══ Updating {app_name} ({app_id}) ═══")
            mas_upgrade = RunCMD([str(self.mas_path), 'upgrade', app_id], **self.cmd_options)
            if not mas_upgrade:
                print(f"[Warning] 'mas upgrade {app_id}' exited with code {mas_upgrade.errno}: {mas_upgrade.stderr}")
            elif mas_upgrade.stdout and 'Everything' not in mas_upgrade:
                self._updates_performed = True
                if 'xcode' in app_name.lower() or app_id == '497799835':
                    self.xcode.check_and_fix_license()

    def _update_os(self) -> None:
        print('═══ Staging macOS Software Updates ═══')
        if not self.dry_run:
            os_update = RunCMD(['softwareupdate', '-d', '-a'], **self.cmd_options)
            output = os_update.allout
            if re.search(r'Action:\s*restart|\[restart\]|restart required|must restart', output, re.IGNORECASE):
                self.reboot.required += 1
            if 'Downloaded' in os_update:
                self._updates_performed = True
            self.check_staged_reboot()

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
                        continue  # Console already covered by IOHIDSystem
                    if idle_field == 'old':
                        idles.append(86400)
                    elif ':' in idle_field:
                        h, m = idle_field.split(':')
                        idles.append(int(h) * 3600 + int(m) * 60)
                    elif idle_field.isdigit():
                        idles.append(int(idle_field) * 60)
        return min(idles) if idles else self.idle_time

    @cached_property
    def brew(self) -> HomeBrew:
        """HomeBrew management instance configured for this machine's architecture."""
        return HomeBrew(
            is_arm=self.is_arm,
            debug=self.debug,
            dry_run=self.dry_run,
            timeout=self.timeout,
            reboot=self.reboot,
            xcode=self.xcode,
        )

    def check_and_reboot(self) -> None:
        """Checks reboot status and launches Reboot workflow if required or suggested."""
        self.check_reboot_status()
        if self.reboot.required or self.reboot.suggested:
            rebooter = Reboot(self)
            rebooter.start()
        else:
            print("No reboot required or suggested.")

    def check_reboot_status(self) -> RebootState:
        """Refreshes and returns the current reboot status dataclass."""
        self.check_staged_reboot()
        if self._updates_performed and self.uptime_days >= UPTIME_DAYS_LIMIT:
            if not self.reboot.suggested:
                self.reboot.suggested += 1
        return self.reboot

    def check_staged_reboot(self) -> bool:
        """Check /Library/Updates/index.plist for staged updates that require a reboot."""
        index_path = Path('/Library/Updates/index.plist')
        if index_path.is_file():
            try:
                with open(index_path, 'rb') as f:
                    data = plistlib.load(f)
                    if data.get('InstallAtLogout'):
                        self.reboot.required += 1
                        return True
            except Exception:
                pass
        return bool(self.reboot.required)

    @staticmethod
    def cleanup_startup_run() -> None:
        """Unloads and removes the temporary one-shot startup LaunchAgent if present."""
        runonce_plist = Path.home() / 'Library/LaunchAgents/com.panther37.update_mac_runonce.plist'
        if runonce_plist.is_file():
            RunCMD(['launchctl', 'unload', '-w', str(runonce_plist)])
            try:
                runonce_plist.unlink(missing_ok=True)
                print(f"Cleaned up one-shot startup LaunchAgent ({runonce_plist.name}).")
            except OSError as err_os:
                print(f"Clean up of one-shot startup LaunchAgent FAILED: {err_os}")

                pass

    @property
    def cmd_options(self) -> dict[str, Any]:
        """Common options dictionary for long-running system/app updates under utility QoS."""
        return {
            'debug':      self.debug,
            'taskpolicy': 'utility',
            'timeout':    self.timeout,
        }

    @cached_property
    def filevault_enabled(self) -> bool:
        """Check if FileVault disk encryption is enabled."""
        if not self.is_mac:
            return False

        status_cmd = RunCMD(['fdesetup', 'status'])
        return 'FileVault is On.' in status_cmd

    @cached_property
    def has_mas(self) -> bool:
        """Check if Mac App Store CLI (mas) is installed."""
        if self.mas_path == Path(''):
            install_mas = RunCMD([str(self.brew.brew_path), 'install', 'mas'], **self.cmd_options)
            if install_mas:
                del self.mas_path
        return self.mas_path != Path('')

    @property
    def idle_time(self) -> int:
        """Get current user idle time in seconds from IOHIDSystem."""
        hid_cmd = RunCMD(['ioreg', '-c', 'IOHIDSystem'])
        match = re.search(r"'HIDIdleTime'\s*=\s*(\d+)", hid_cmd.stdout)
        return 0 if not match else int(match.group(1)) // int(1e9)

    def is_app_running(self, app_name: str) -> bool:
        """Checks if an application is currently running on the system."""
        if not self.is_mac:
            return False

        if RunCMD(['pgrep', '-f', f'{app_name}.app']):
            return True

        if RunCMD(['pgrep', '-x', app_name]):
            return True

        a_cmd = [
            'osascript', '-e',
            'tell application "System Events" to get name of every process whose background only is false',
        ]
        gui_cmd = RunCMD(a_cmd)
        if gui_cmd:
            running_names = [name.strip().lower() for name in gui_cmd.stdout.split(',')]
            if app_name.lower() in running_names:
                return True

        return False

    @cached_property
    def is_arm(self) -> bool:
        """Query sysctl directly to avoid Rosetta 2 translation traps."""
        if not self.is_mac:
            return False

        arm_cmd = RunCMD(['sysctl', '-in', 'hw.optional.arm64'])
        return arm_cmd.strip() == '1'

    @property
    def is_idle(self) -> bool:
        """Returns True if all users/sessions have been idle for >= MAX_IDLE_SEC."""
        return self.all_user_idle_time >= MAX_IDLE_SEC

    @cached_property
    def is_mac(self) -> bool:
        return platform.system() == 'Darwin'

    @cached_property
    def mas_path(self) -> Path:
        """Locate the mas binary based on brew_path, PATH, or filesystem presence."""
        if self.brew.brew_path:
            candidate = self.brew.brew_path.parent / 'mas'
            if candidate.is_file():
                return candidate

        which_mas = shutil.which('mas')
        if which_mas:
            return Path(which_mas)

        for candidate_str in ['/opt/homebrew/bin/mas', '/usr/local/bin/mas']:
            p = Path(candidate_str)
            if p.is_file():
                return p

        return Path('')

    @property
    def reboot_required(self) -> bool:
        return bool(self.reboot.required)

    @property
    def reboot_suggested(self) -> bool:
        return bool(self.reboot.suggested)

    def schedule_startup_run(self) -> bool:
        """Creates a one-shot LaunchAgent to rerun update_mac on next user login."""
        if self.dry_run:
            print("[Dry-run] Would schedule one-shot LaunchAgent to rerun update_mac on next startup.")
            return True

        launch_dir = Path.home() / 'Library/LaunchAgents'
        launch_dir.mkdir(parents=True, exist_ok=True)
        runonce_plist = launch_dir / 'com.panther37.update_mac_runonce.plist'
        target_script = Path('/Library/scripts/User/update_mac.py')
        if not target_script.is_file():
            target_script = Path(__file__).resolve()

        python_bin = '/usr/local/bin/python3' if Path('/usr/local/bin/python3').is_file() else (
            sys.executable or '/usr/local/bin/python3')
        plist_dict = {
            'Label':                  'com.panther37.update_mac_runonce',
            'LimitLoadToSessionType': ['Aqua', 'Standard'],
            'ProgramArguments':       [python_bin, str(target_script)],
            'RunAtLoad':              True,
            'StandardErrorPath':      str(Path.home() / 'Library/Logs/update_mac_runonce.stderr.log'),
            'StandardOutPath':        str(Path.home() / 'Library/Logs/update_mac_runonce.stdout.log'),
        }
        try:
            with open(runonce_plist, 'wb') as f:
                plistlib.dump(plist_dict, f)
            runonce_plist.chmod(0o644)
            RunCMD(['launchctl', 'unload', str(runonce_plist)])
            RunCMD(['launchctl', 'load', '-w', str(runonce_plist)])
            print(f"Scheduled one-shot update run on next startup: {runonce_plist.name}")
            return True
        except Exception as err:
            print(f"[Warning] Failed to schedule one-shot startup run: {err}")
            return False

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

        target = target.lower().strip()
        if target in ('brew', 'homebrew'):
            self.brew.update()
            if self.brew.updates_performed:
                self._updates_performed = True
        elif target in ('mas', 'appstore'):
            self._update_mas()
        elif target in ('os', 'mac-os', 'macos', 'softwareupdate'):
            self._update_os()
        elif target == 'all':
            self.brew.update()
            if self.brew.updates_performed:
                self._updates_performed = True
            self._update_mas()
            self._update_os()
        else:
            raise ValueError(f"Unknown update target: '{target}'.  Choose from 'brew', 'mas', 'os', or 'all'.")

    @property
    def uptime_days(self) -> float:
        """Get system uptime in days."""
        return self.uptime_seconds / 86400.0

    @property
    def uptime_seconds(self) -> int:
        """Get system uptime in seconds from kern.boottime."""
        boottime_cmd = RunCMD(['sysctl', '-n', 'kern.boottime'])
        match = re.search(r'sec = (\d+)', boottime_cmd.stdout)
        if match:
            return int(time.time()) - int(match.group(1))

        return 0

    @cached_property
    def xcode(self) -> Any:
        """Xcode management instance for checking and fixing Xcode tools and license."""
        return Xcode(debug=self.debug, dry_run=self.dry_run)


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
        1. Check all-user idle.  If idle for >= MAX_IDLE_SEC, triggers the 'Now' flow immediately.
        2. Wait an hour, then ask again.
        3. If deferred ``self.max_snooze`` times, triggers the 'Now' flow immediately.
    - Force reboot:
        1. Escalate: sudo shutdown -r +1
        2. guard active backups and kill all programs and daemons
        3. attempt to turn off the computer
        4. wait wait_time between each one
    """

    def __init__(self, computer: MacOSComputer | None = None, dry_run: bool = False, debug: bool = False):
        self.debug = debug
        self.max_now_attempts = 10
        self.max_snooze = 3
        self.snoozes_left = self.max_snooze
        self.computer = computer or MacOSComputer(debug=self.debug, dry_run=dry_run)
        self.dry_run = dry_run or self.computer.dry_run

        log_dir = Path('/var/log')
        if os.access(log_dir, os.W_OK):
            self.log_file = log_dir / 'mac_reboot.log'
        else:
            self.log_file = Path.home() / 'Library/Logs/mac_reboot.log'

    def _sleep(self, seconds: int | float) -> None:
        """Helper to sleep, respecting dry_run for testing."""
        if self.dry_run:
            self.log(f"[Dry-run] Simulated wait: {seconds}s")
            time.sleep(0.01)
        else:
            time.sleep(seconds)

    def attempt_authrestart(self) -> bool:
        """
        Attempts "fdesetup authrestart" if FileVault is active and supported.
        Allows the Mac to reboot past the FileVault screen directly into macOS to apply updates.
        """
        if not (self.computer.filevault_enabled and self.computer.supports_authrestart):
            return False

        self.log("FileVault is active and supports authrestart.  Attempting fdesetup authrestart...")
        if self.dry_run:
            self.log("[Dry-run] Would execute: sudo fdesetup authrestart")
            return True

        authrestart_cmd = RunCMD(['fdesetup', 'authrestart'], need_root=True)
        if authrestart_cmd:
            self.log("fdesetup authrestart initiated successfully.")
            return True
        else:
            self.log(
                f"fdesetup authrestart unavailable without credentials (code {authrestart_cmd.errno}).  "
                f"Falling back to standard restart.",
            )
            return False

    def check_and_stop_backup(self) -> None:
        """Checks if Time Machine backup is running and cleanly stops it before force reboot."""
        if self.dry_run:
            self.log("[Dry-run] Checked Time Machine status (simulated)")
            return

        tm_cmd = ['tmutil', 'status']
        tm_status = RunCMD(tm_cmd)
        if 'Running = 1' in tm_status or 'Running = "1"' in tm_status:
            self.log("Time Machine backup is active.  Requesting clean stop before escalation...")
            stop_cmd = ['tmutil', 'stopbackup']
            RunCMD(stop_cmd, timeout=30)
            for _ in range(9):
                self._sleep(5)
                poll_status = RunCMD(tm_cmd)
                if 'Running = 0' in poll_status or 'Running = "0"' in poll_status:
                    self.log("Time Machine backup stopped successfully.")
                    break
            else:
                self.log("Time Machine backup did not stop within 45s.  Proceeding with escalation.")

    def ensure_root(self) -> bool:
        """
        Verifies administrator privileges for force reboot actions.
        Prompts for sudo in interactive sessions if not already cached.
        """
        if self.dry_run or os.geteuid() == 0:
            return True

        if RunCMD(['true'], need_root=True):
            return True

        if sys.stdin.isatty():
            self.log("Administrator privileges are required to force restart.  Prompting for sudo password...")
            return bool(RunCMD(['-v'], interactive=True, need_root=True))

        return False

    def force_reboot(self, wait_time: int | float = 60) -> None:
        """
        Executes the Force Reboot sequence:
        1. Escalate: sudo shutdown -r +1
        2. guard active backups and kill all programs and daemons
        3. attempt to turn off the computer
        4. wait wait_time between each one
        """
        has_root = self.ensure_root()
        a_reboot_cmd = ['shutdown', '-r', '+1', 'Mandatory system restart in 1 minute.']
        iteration = 1
        while True:
            self.log(f"═══ Force Reboot Sequence (Iteration {iteration}) ═══")
            for i in range(6):
                match i:
                    case i if i % 2 == 1:
                        self._sleep(wait_time)
                    case 0:
                        # 1. Escalate: sudo shutdown -r +1
                        self.log("Escalating: sudo shutdown -r +1")
                        if not self.dry_run:
                            if has_root:
                                RunCMD(a_reboot_cmd, timeout=wait_time, need_root=True)
                            else:
                                applescript = (
                                    'do shell script "shutdown -r +1 \\"Mandatory system restart in 1 minute.\\"" '
                                    'with administrator privileges'
                                )
                                RunCMD(['osascript', '-e', applescript], timeout=wait_time + 45)
                    case 2:
                        # 2. attempt to kill all programs and daemons
                        self.check_and_stop_backup()
                        self.log("Attempting to kill all programs and daemons...")
                        self.kill_programs_and_daemons()
                    case 4:
                        # 3. attempt to turn off the computer
                        self.log("Attempting to turn off the computer...")
                        self.turn_off()
                        iteration += 1
            if self.dry_run:
                break

    @staticmethod
    def format_uptime(seconds: float | int) -> str:
        days = int(seconds // 86400)
        hours = int((seconds % 86400) // 3600)
        minutes = int((seconds % 3600) // 60)
        parts = []
        if days > 0:
            parts.append(f"{days} day{'s' if days != 1 else ''}")
        if hours > 0:
            parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
        return ", ".join(parts) if parts else "< 1 minute"

    def graceful_restart(self) -> None:
        """Attempts a standard graceful restart via System Events, first auto-saving documents."""
        if not self.dry_run:
            # 1. Tell scriptable apps to cleanly save existing open documents
            save_script = (
                'tell application "System Events"\n'
                '    set appList to (name of every process whose background only is false and name is not "Finder")\n'
                'end tell\n'
                'repeat with appName in appList\n'
                '    try\n'
                '        tell application appName to close every document saving yes\n'
                '    end try\n'
                'end repeat'
            )
            RunCMD(['osascript', '-e', save_script], timeout=20)

            # 2. Check FileVault authrestart first; fallback to System Events
            if not self.attempt_authrestart():
                RunCMD(['osascript', '-e', 'tell application "System Events" to restart'])
        else:
            self.log("[Dry-run] Would auto-save open documents and attempt authrestart / System Events restart")

    def is_idle(self) -> bool:
        """Checks if all users/sessions have been idle for at least MAX_IDLE_SEC."""
        return self.computer.is_idle

    @contextmanager
    def keep_awake(self):
        """Spawns caffeinate via RunCMD to prevent sleep while reboot workflow runs."""
        pid = os.getpid()
        started = False
        if not self.dry_run and shutil.which('caffeinate'):
            a_cmd = [f'caffeinate -dim -w {pid} >/dev/null 2>&1 &']
            caffeinate_cmd = RunCMD(a_cmd, need_shell=True)
            if caffeinate_cmd:
                self.log(f"Spawned caffeinate attached to PID {pid} via RunCMD.")
                started = True
            else:
                self.log(f"Failed to spawn caffeinate (code {caffeinate_cmd.errno}): {caffeinate_cmd.error}")
        try:
            yield
        finally:
            if started:
                if RunCMD(['pkill', '-f', f'caffeinate.*-w {pid}']):
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
        a_cmd = ['osascript', '-e', applescript]
        RunCMD(a_cmd, timeout=15)

        # 2. Terminate user processes
        user = os.environ.get('USER')
        can_sudo = RunCMD.can_sudo()
        if user:
            a_cmd = ['pkill', '-9', '-u', user]
            if can_sudo:
                RunCMD(a_cmd, timeout=30, need_root=True)
            else:
                RunCMD(a_cmd, timeout=30)

        # 3. Kill WindowServer to terminate GUI sessions immediately
        if can_sudo:
            a_cmd = ['killall', '-9', 'WindowServer']
            RunCMD(a_cmd, timeout=30, need_root=True)
        else:
            applescript = 'do shell script "killall -9 WindowServer" with administrator privileges'
            a_cmd = ['osascript', '-e', applescript]
            RunCMD(a_cmd, timeout=60)

    def log(self, message: str) -> None:
        """Logs message to console and persistent log file."""
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        line = f"[{timestamp}] [Reboot] {message}"
        print(line)
        try:
            with open(self.log_file, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
        except Exception:
            pass

    def notify_final_warning(self) -> None:
        """Informs the user that the computer will force reboot in 2 minutes."""
        self.play_chime('Sosumi')
        giving_up_sec = 120
        applescript = (
            'tell application "System Events"\n'
            '    activate\n'
            '    display dialog "Graceful restart attempts failed.\\n\\nThe computer will force reboot in 2 minutes.\\n'
            'Please save your work immediately." '
            f'buttons {{"OK"}} default button "OK" giving up after {giving_up_sec} with icon stop\n'
            'end tell'
        )
        if self.dry_run:
            self.log("[Dry-run] Displayed 2-minute final warning dialog")
            return

        RunCMD(['osascript', '-e', applescript], timeout=giving_up_sec + 10)

    def play_chime(self, sound_name: str = 'Ping') -> None:
        """Plays a gentle system alert chime before presenting dialogs."""
        if self.dry_run:
            return

        sound_path = Path(f'/System/Library/Sounds/{sound_name}.aiff')
        if sound_path.exists():
            RunCMD(['afplay', str(sound_path)])

    def prompt_initial(self) -> str:
        """
        Displays initial prompt showing uptime with 'Restart Now' and 'Snooze'.
        If all users are already idle for >= MAX_IDLE_SEC, automatically returns 'now'.
        Otherwise, displays dialog with 'giving up after' set to remaining idle seconds.
        If the dialog times out and MAX_IDLE_SEC of idle is reached, automatically returns 'now'.
        Returns 'now' or 'snooze'.
        """

        def run_snooze() -> str:
            self.snoozes_left -= 1
            return 'snooze'

        if self.is_idle():
            self.log(
                f"All users have been idle for {self.computer.all_user_idle_time // 60}m "
                f"(>= {MAX_IDLE_MIN}m).  Automatically selecting 'Restart Now'.",
            )
            return 'now'

        self.play_chime('Ping')
        if self.snoozes_left <= 0:
            self.log("No snoozes left.  Automatically selecting 'Restart Now'.")
            return 'now'

        remaining_until_idle = self.remaining_idle_time()
        uptime_str = self.format_uptime(self.computer.uptime_seconds)
        applescript = (
            'tell application "System Events"\n'
            '    activate\n'
            f'    display dialog "The computer has been up for {uptime_str}.\\n\\n'
            f'A system restart is required for updates.\\n'
            f'{self.snoozes_left} Snooze{'s' if self.snoozes_left != 1 else ''} for {MAX_IDLE_MIN} minutes available.'
            '" buttons {"Snooze", "Restart Now"} default button "Restart Now" giving up after '
            f'{remaining_until_idle} with icon caution\n'
            'end tell'
        )
        if self.dry_run:
            self.log(
                f"[Dry-run] Prompting initial dialog (Uptime: {uptime_str}, giving up after {remaining_until_idle}s) "
                f"-> default: 'now'",
            )
            return 'now'

        dialog_cmd = RunCMD(['osascript', '-e', applescript], timeout=remaining_until_idle + 10)
        if 'button returned:Restart Now' in dialog_cmd:
            self.log("User clicked 'Restart Now'.")
            return 'now'

        if 'button returned:Snooze' in dialog_cmd:
            self.log("User clicked 'Snooze'.")
            return run_snooze()

        # Check if dialog closed because user reached MAX_IDLE_SEC idle threshold
        if 'gave up:true' in dialog_cmd or self.is_idle():
            self.log(
                f"Prompt timed out and user has been idle for >= {MAX_IDLE_MIN} minutes.  "
                f"Automatically selecting 'Restart Now'.",
            )
            return 'now'

        if not dialog_cmd:
            self.log(f"Dialog failed to display (code {dialog_cmd.errno}): {dialog_cmd.error or 'unknown error'}")
            return run_snooze()

        self.log("User dismissed dialog or selected 'Snooze'.")
        return run_snooze()

    def prompt_retry(self, attempt: int) -> str:
        """
        Asks user to 'Reboot Now (Force)' or 'Try Again Gracefully'.
        If all users are idle for >= MAX_IDLE_SEC, automatically returns 'force'.
        Returns 'force' or 'graceful'.
        """
        if self.is_idle():
            self.log(
                f"All users idle for {self.computer.all_user_idle_time // 60}m "
                f"(>= {MAX_IDLE_MIN}m).  Automatically selecting 'Reboot Now (Force)'.",
            )
            return 'force'

        self.play_chime('Ping')
        remaining_until_idle = self.remaining_idle_time()
        applescript = (
            'tell application "System Events"\n'
            '    activate\n'
            f'    display dialog "Restart attempt {attempt}/{self.max_now_attempts} was incomplete.\\n'
            f'\\nAn application may be blocking the restart." '
            'buttons {"Try Again Gracefully", "Reboot Now (Force)"} default button "Try Again Gracefully" '
            f'giving up after {remaining_until_idle} with icon caution\n'
            'end tell'
        )
        if self.dry_run:
            self.log(
                f"[Dry-run] Retry prompt {attempt}/{self.max_now_attempts} displayed (giving up after "
                f"{remaining_until_idle}s) -> default: 'graceful'",
            )
            return 'graceful'

        dialog_cmd = RunCMD(['osascript', '-e', applescript], timeout=remaining_until_idle + 10)
        if 'button returned:Reboot Now (Force)' in dialog_cmd:
            self.log("User selected 'Reboot Now (Force)'.")
            return 'force'

        if 'button returned:Try Again Gracefully' in dialog_cmd:
            self.log("User selected 'Try Again Gracefully'.")
            return 'graceful'

        if 'gave up:true' in dialog_cmd or self.is_idle():
            self.log(
                f"Prompt timed out and user has been idle for >= {MAX_IDLE_MIN} minutes.  "
                f"Automatically selecting 'Reboot Now (Force)'.",
            )
            return 'force'

        if not dialog_cmd:
            self.log(f"Dialog failed to display (code {dialog_cmd.errno}): {dialog_cmd.error or 'unknown error'}")
            return 'graceful'

        self.log("User selected 'Try Again Gracefully'.")
        return 'graceful'

    def remaining_idle_time(self, min_seconds: int = 10) -> int:
        """Calculates remaining seconds until MAX_IDLE_SEC, capped at min_seconds."""
        return max(min_seconds, MAX_IDLE_SEC - self.computer.all_user_idle_time)

    def run_snooze_flow(self) -> None:
        """
        Executes the 'Snooze' flow:
        1. Check all-user idle.  If idle for >= MAX_IDLE_SEC, start 'Now' flow immediately.
        2. Wait an hour, ask user again.
        3. If user has been asked ``self.max_snooze`` times, start 'Now' flow immediately.
        """
        check_idle_seconds = 5 * 60

        while True:
            if self.is_idle():
                self.log(
                    f"All users idle for {self.computer.all_user_idle_time}s "
                    f"(>= {MAX_IDLE_MIN}m).  Starting 'Now' flow.",
                )
                self.run_now_flow()
                return

            if self.snoozes_left < 0:
                self.log(f"Reboot deferred {self.max_snooze} times.  Escalating to 'Now' flow.")
                self.run_now_flow()
                return

            self.log(f"User selected 'Snooze'.  Waiting {MAX_IDLE_MIN} min before asking again...")
            if not self.dry_run:
                for i in range(MAX_IDLE_SEC // check_idle_seconds):
                    time.sleep(check_idle_seconds)
                    if self.is_idle():
                        self.log(
                            f"All users became idle (>= {MAX_IDLE_MIN}m) after "
                            f"{(i + 1) * check_idle_seconds // 60}m.  Starting 'Now' flow.",
                        )
                        self.run_now_flow()
                        return
            else:
                self._sleep(MAX_IDLE_SEC)

            choice = self.prompt_initial()
            if choice == 'now':
                self.run_now_flow()
                return

    def run_now_flow(self) -> None:
        """
        Executes the 'Now' restart flow:
        1. Start graceful restart and check after 2 minutes.
        2. If still up, ask to reboot now or try again gracefully (up to 10 times).
        3. After 10 attempts, notify user of forced reboot in 2 minutes and force reboot.
        """
        for attempt in range(self.max_now_attempts):
            self.log(f"Starting graceful restart (attempt {attempt + 1}/{self.max_now_attempts})...")
            self.graceful_restart()

            # Wait 2 minutes to check if computer is still up
            self._sleep(120)

            # If still running, prompt user
            choice = self.prompt_retry(attempt + 1)
            if choice == 'force':
                self.log("User chose 'Reboot Now (Force)'.")
                self.force_reboot()
                return

        self.log(
            f"{self.max_now_attempts} graceful restart attempts exhausted.  Notifying user of forced reboot in 2 minutes.",
        )
        self.notify_final_warning()
        self._sleep(120)
        self.force_reboot()

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
            RunCMD(['osascript', '-e', 'do shell script "shutdown -h now" with administrator privileges'], timeout=60)
            return

        RunCMD(['shutdown', '-h', 'now'], timeout=30, need_root=True)


@dataclass
class RebootState:
    """
    Pure data container holding reboot state flags.
    Contains no execution logic or system side effects.
    """

    required: int = 0
    requested: int = 0
    suggested: int = 0
    reason: str | None = None

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item.lower())

    def __setitem__(self, item: str, value: Any) -> None:
        attr = item.lower()
        if hasattr(self, attr):
            setattr(self, attr, value)
        else:
            raise KeyError(f"Invalid RebootState key: {item}")


class RunCMD:
    """
    Run a bash command and provide output and error
    """

    def __init__(
        self,
        cmd_args: list[str],
        timeout: int | float = 10,
        debug: bool = False,
        download: bool = False,
        environ: dict | None = None,
        interactive: bool = False,
        need_root: bool = False,
        need_shell: bool = False,
        shell_cmd: str = '/bin/bash',
        spinner: bool | None = None,
        taskpolicy: str | None = None,
    ):
        """
        :param interactive: If True, do not capture stdout/stderr to allow interactive TTY prompts
        :param spinner: If True (or if None and timeout >= 30), show a terminal spinner during execution
        :param taskpolicy: Limit command: Lowest 'maintenance', 'background', 'utility', ``None`` is normal, or highest
        """
        self.cmd_args = cmd_args
        self.debug = debug or getattr(options, 'debug', False)
        self.debug_limit = None
        self.download = download
        self.environ = environ or {}
        self.errno = 0
        self.initial_need_root = need_root
        self.interactive = interactive
        self.need_root = need_root
        self.need_shell = need_shell
        self.permissions_checked = False
        self.shell_cmd = shell_cmd if need_shell else None
        self.spinner = spinner
        self.stderr = ''
        self.stdout = ''
        self.timeout = timeout
        if taskpolicy is not None:
            clamp = taskpolicy.strip().lower() if isinstance(taskpolicy, str) else None
            if clamp in ('maintenance', 'background', 'utility'):
                self.cmd_args = self._prepend(['taskpolicy', '-c', clamp])
        if self.debug:
            debug_limit = getattr(options, 'debug_limit', 0)
            if debug_limit:
                self.debug_limit = None if debug_limit < 0 else debug_limit
            else:
                self.debug_limit = DEBUG_LIMIT

        self.run()
        if not self.permissions_checked:
            self._check_permission()
        if self.debug:
            limit = self.debug_limit
            print(
                "\n───────── RunCMD ─────────\n"
                f"Command: {self.cmd_args}\n"
                f"Output: \n{self.stdout[:limit]}◊\n"
                f"Error: \n{self.stderr[:limit]}◊\n"
                f"───────── {self.shell_cmd or ''} ─────────\n",
            )

    def __bool__(self):
        return self.errno == 0

    def __contains__(self, item):
        return True if item in self.stdout else False

    def __getitem__(self, item):
        return self.stdout.splitlines()[item]

    def __iadd__(self, other):
        self.stdout = self.stdout + other

    def __iter__(self):
        return iter(self.stdout.splitlines() if self.stdout else [])

    def __len__(self):
        return len(self.stdout.splitlines())

    def __repr__(self):
        return self.stdout

    def _check_permission(self) -> None:
        self.permissions_checked = True
        if self.errno in [13, 126] and not self.need_root:
            self.need_root = True
            self.run()
        elif self.stderr and self.need_root and not self.initial_need_root:
            self.need_root = False
            self.run()

    def _prepend(self, argument: str | list[str], a_cmd: list[str] | None = None) -> list[str]:
        """Prepend argument to the command"""
        a_cmd = a_cmd or self.cmd_args
        if isinstance(argument, str):
            argument = [argument]
        if self.need_shell:
            return [' '.join(argument + a_cmd)]

        else:
            return argument + a_cmd

    @contextmanager
    def _spinner(self):
        should_spin = not any([self.interactive, self.spinner is False])
        should_spin = should_spin and (self.spinner or self.timeout >= 30)
        if not should_spin:
            yield None
            return

        if isinstance(self.cmd_args, list):
            if len(self.cmd_args) >= 3 and self.cmd_args[0] == 'osascript' and self.cmd_args[1] == '-e':
                first_line = self.cmd_args[2].strip().splitlines()[0] if self.cmd_args[2].strip() else ''
                display = f"osascript ({first_line[:40]}...)" if len(first_line) > 40 else f"osascript ({first_line})"
            elif len(self.cmd_args) >= 3 and self.cmd_args[0] == 'sh' and self.cmd_args[1] == '-c':
                first_line = self.cmd_args[2].strip().splitlines()[0] if self.cmd_args[2].strip() else ''
                display = f"sh ({first_line[:40]}...)" if len(first_line) > 40 else f"sh ({first_line})"
            else:
                display = ' '.join(str(a) for a in self.cmd_args)
        else:
            display = str(self.cmd_args)
        if len(display) > 100:
            display = display[:97] + '...'
        msg = f"Running {display}..."

        with Wave(msg) as sp:
            yield sp

    @property
    def allout(self) -> str:
        return '\n'.join([self.stdout, self.stderr]).strip()

    @classmethod
    def can_sudo(cls) -> bool:
        """Checks whether root privileges are available without prompting."""
        if os.geteuid() == 0:
            return True

        return bool(cls(['true'], need_root=True))

    @property
    def error(self) -> str:
        return self.stderr.strip()

    def parse_dict(self, delimiter: str = ':') -> dict:
        """
        Make dictionary from stdout
        """
        some_dict = {}
        some_keys = {}
        for some_line in self.stdout.splitlines():
            if delimiter in some_line:
                some_split_line = some_line.split(delimiter, 1)
                if self.debug:
                    print(some_split_line)
                try:
                    a_key = some_split_line[0].strip()
                    key_times = some_keys.get(a_key, 0) + 1
                    some_keys[a_key] = key_times
                    # Add a number if duplicate keys
                    if key_times > 1:
                        a_key += f' {key_times:04d}'
                    some_dict[a_key] = some_split_line[1].strip()
                except IndexError as err_ind:
                    print(
                        f'\nThis line cannot be split into Key and Value with "{delimiter}"\n'
                        f'{some_split_line}\n{err_ind}\n',
                    )
        if self.debug:
            print(json.dumps(some_dict, indent=2, ensure_ascii=False))
        return some_dict

    def run(self):
        popen_env = None
        run_cmd_args = self.cmd_args[:]
        verbose_environ = ''

        if self.need_root and os.geteuid() != 0:
            prefix = ['sudo'] if self.interactive else ['sudo', '-n']
            run_cmd_args = self._prepend(prefix, run_cmd_args)
        if self.environ:
            popen_env = dict(os.environ, **self.environ)
            if self.debug:
                clean_environ = {}
                for k, v in self.environ.items():
                    clean_environ[k] = f"Sensitive: {bool(v) if 'pass' in k.lower() else v}"
                verbose_environ = f"\ncustom environ: {clean_environ}"
        if self.debug:
            print(
                "\n───────── RunCMD ─────────\n"
                f"Command: {run_cmd_args}\n"
                f"Root: {self.need_root}{verbose_environ}",
            )
        with self._spinner():
            try:
                capture = not self.interactive
                result = subprocess.run(
                    run_cmd_args,
                    shell=self.need_shell,
                    capture_output=capture,
                    text=True,
                    timeout=self.timeout,
                    executable=self.shell_cmd,
                    env=popen_env,
                )
                self.stdout = result.stdout or ''
                self.stderr = result.stderr or ''
                self.errno = result.returncode
            except subprocess.TimeoutExpired:
                self.errno = -2
                self.stderr = f"Command timed out after {self.timeout}s: {run_cmd_args}"
            except Exception as err_msg_run:
                self.errno = -1
                self.stderr = f"Unable to run command: {err_msg_run}\n\t{run_cmd_args}\n"

    def splitlines(self) -> list[str]:
        return self.stdout.splitlines()

    def strip(self):
        return self.stdout.strip()


class Wave:
    """
    Terminal spinner using curved arc wave animation for long-running operations.
    """

    ANIMATION: str = '‿◞◜⁀◝◟'
    DELAY: float = 0.25
    WIDTH: int = 3

    def __init__(
        self,
        message: str = "Running command...",
        stream: Any = None,
        force: bool = False,
    ):
        self.message = message
        self.stream = stream or sys.stdout
        self.force = force
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
        return False

    def _undulate(self):
        index = 0
        stream_len = len(self.ANIMATION)
        frame = ' ' * self.WIDTH
        while not self._stop_event.is_set():
            # frame = (self.ANIMATION[index] + frame)[:self.WIDTH]
            frame += self.ANIMATION[index]
            frame = frame[-self.WIDTH:]
            self.stream.write(f"\r{frame}  {self.message}")
            self.stream.flush()
            index = (index + 1) % stream_len
            self._stop_event.wait(self.DELAY)

        self.stream.write("\r\x1b[K")
        self.stream.flush()

    def start(self):
        if not self.force and not getattr(self.stream, 'isatty', lambda: False)():
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._undulate, daemon=True)
        self._thread.start()

    def stop(self):
        if self._thread and self._thread.is_alive():
            self._stop_event.set()
            self._thread.join(timeout=0.5)
            self._thread = None


Spinner = Wave


class Xcode:
    """
    Manages Xcode installation checks, developer tools inspection, and
    license agreement acceptance.
    """

    def __init__(self, debug: bool = False, dry_run: bool = False):
        self.debug = debug
        self.dry_run = dry_run

    def accept_license(self) -> bool:
        """
        Attempts to accept the Xcode license via interactive sudo prompt or cached sudo.
        Returns True if license was accepted, False otherwise.
        """
        if not self.is_installed or not self.xcodebuild_path:
            return True

        print("\n[Xcode License Required] The Xcode license agreement has not been accepted.")
        if self.dry_run:
            print(f"[Dry-run] Would accept Xcode license agreement via 'sudo {self.xcodebuild_path} -license accept'.")
            return False

        xcode_license_cmd = [str(self.xcodebuild_path), '-license', 'accept']
        if sys.stdin.isatty():
            print("CMake and Darwin SDK builds will fail until the Xcode license is accepted.")
            print("Prompting for administrator privileges to accept the license...")
            license_cmd = RunCMD(
                xcode_license_cmd,
                interactive=True,
                need_root=True,
                timeout=60,
                debug=self.debug,
            )
            if license_cmd:
                print("Xcode license agreement accepted successfully.")
                return True
        else:
            license_cmd = RunCMD(
                xcode_license_cmd,
                timeout=15,
                debug=self.debug,
                need_root=True,
            )
            if license_cmd:
                print("Xcode license agreement accepted successfully via sudo.")
                return True

        print(
            f"\n[WARNING] Xcode license agreement has not been accepted.\n"
            f"Builds requiring macOS SDKs (e.g. formulae with 'cmake') will fail.\n"
            f"To accept manually, run in Terminal:\n"
            f"\tsudo {self.xcodebuild_path} -license accept\n",
        )
        return False

    def check_and_fix_license(self) -> bool:
        """
        Verifies if the Xcode license is accepted.  If not, attempts to accept it.
        Returns True if license is accepted (or Xcode not installed), False otherwise.
        """
        if not self.is_installed:
            return True

        if self.is_license_accepted:
            return True

        return self.accept_license()

    @property
    def is_installed(self) -> bool:
        """Returns True if Xcode or xcodebuild binary is installed on the system."""
        return self.xcodebuild_path is not None

    @property
    def is_license_accepted(self) -> bool:
        """
        Checks if Xcode license agreement has been accepted.
        Returns True if accepted (exit code 0), False if pending (exit code 69 or non-zero).
        """
        if not self.xcodebuild_path:
            return True

        return bool(RunCMD([str(self.xcodebuild_path), '-license', 'status'], debug=self.debug))

    @cached_property
    def xcodebuild_path(self) -> Path | None:
        """Locates xcodebuild binary from /Applications/Xcode.app or PATH."""
        xcodebuild_candidate = Path('/Applications/Xcode.app/Contents/Developer/usr/bin/xcodebuild')
        if xcodebuild_candidate.is_file():
            return xcodebuild_candidate

        which_xcode = shutil.which('xcodebuild')
        if which_xcode:
            return Path(which_xcode)

        return None


def check_plist(
    a_plist: str = DEFAULT_PLIST,
    weekday: int = 0,
    hour: int = 6,
    minute: int = 0,
) -> bool:
    """Checks if .plist for this file is in /Library/LaunchAgents and active, if not, creates file and activates it."""
    current_script = Path(__file__).resolve()
    launch_agents_dir = Path('/Library/LaunchAgents')
    old_user_plist = Path.home() / 'Library/LaunchAgents' / f'{a_plist}.plist'
    target_plist = launch_agents_dir / f'{a_plist}.plist'
    target_script = Path('/Library/scripts/User/update_mac.py')

    # Unload and remove obsolete per-user plist if present
    if old_user_plist.is_file():
        RunCMD(['launchctl', 'unload', '-w', str(old_user_plist)])
        try:
            old_user_plist.unlink()
        except OSError:
            pass

    # 1. Check if update_mac.py is located in /Library/scripts/User/update_mac.py; if not, copy itself there
    script_needs_copy = (
        current_script != target_script
        and (not target_script.is_file() or current_script.read_bytes() != target_script.read_bytes())
    )

    # 2. Build plist data
    python_bin = '/usr/local/bin/python3' if Path('/usr/local/bin/python3').is_file() else (
        sys.executable or '/usr/local/bin/python3')
    plist_dict = {
        'EnvironmentVariables':   {
            'PATH': '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin',
        },
        'Label':                  a_plist,
        'LimitLoadToSessionType': ['Aqua', 'Background', 'Standard', 'LoginWindow'],
        'ProgramArguments':       [python_bin, str(target_script)],
        'StandardErrorPath':      '/Library/Logs/update_mac.stderr.log',
        'StandardOutPath':        '/Library/Logs/update_mac.stdout.log',
        'StartCalendarInterval':  {
            'Hour':    hour,
            'Minute':  minute,
            'Weekday': weekday,
        },
    }
    plist_data = plistlib.dumps(plist_dict)
    plist_needs_install = not target_plist.is_file() or target_plist.read_bytes() != plist_data
    if not plist_needs_install and target_plist.is_file():
        stat = target_plist.stat()
        if stat.st_uid != 0 or (stat.st_mode & 0o777) != 0o644:
            plist_needs_install = True

    # Perform privileged installation if needed
    if script_needs_copy or plist_needs_install:
        installed_with_root = False
        try:
            if script_needs_copy:
                target_script.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(current_script, target_script)
                target_script.chmod(0o755)
                print(f"Copied {current_script.name} to {target_script}")
            if plist_needs_install:
                launch_agents_dir.mkdir(parents=True, exist_ok=True)
                target_plist.write_bytes(plist_data)
                target_plist.chmod(0o644)
                print(f"Installed {target_plist.name} in {launch_agents_dir}")
            for log_file in [Path('/Library/Logs/update_mac.stdout.log'), Path('/Library/Logs/update_mac.stderr.log')]:
                log_file.touch(exist_ok=True)
                log_file.chmod(0o666)
            installed_with_root = True
        except PermissionError:
            tmp_plist = Path('/tmp') / target_plist.name
            if plist_needs_install:
                tmp_plist.write_bytes(plist_data)

            commands = []
            if script_needs_copy:
                commands.append(
                    f"mkdir -p '{target_script.parent}' && cp '{current_script}' '{target_script}' && "
                    f"chmod 755 '{target_script}'",
                )
            if plist_needs_install:
                commands.append(
                    f"cp '{tmp_plist}' '{target_plist}' && chown root:wheel '{target_plist}' && "
                    f"chmod 644 '{target_plist}'",
                )
            commands.append(
                "touch /Library/Logs/update_mac.stdout.log /Library/Logs/update_mac.stderr.log && "
                "chmod 666 /Library/Logs/update_mac.*.log",
            )
            full_cmd = ' && '.join(commands)

            a_cmd = ['sh', '-c', full_cmd]
            if sys.stdin.isatty():
                items = []
                if script_needs_copy:
                    items.append(f"  - Script: {target_script}")
                if plist_needs_install:
                    items.append(f"  - Agent:  {target_plist}")
                items_str = '\n'.join(items)
                print(
                    f"\n[Permission Required] Administrator privileges needed to install:\n"
                    f"{items_str}\n"
                    f"Prompting for sudo password...",
                )
                if RunCMD(a_cmd, interactive=True, need_root=True):
                    installed_with_root = True
                    if script_needs_copy:
                        print(f"Copied {current_script.name} to {target_script}")
                    if plist_needs_install:
                        print(f"Installed {target_plist.name} in {launch_agents_dir}")
            else:
                if RunCMD(a_cmd, timeout=15, need_root=True):
                    installed_with_root = True
                    if script_needs_copy:
                        print(f"Copied {current_script.name} to {target_script}")
                    if plist_needs_install:
                        print(f"Installed {target_plist.name} in {launch_agents_dir}")
            if installed_with_root:
                tmp_plist.unlink(missing_ok=True)
            else:
                print(
                    f"\n[WARNING] Unable to install system-wide agent to {target_plist} or {target_script} "
                    "(permission denied).\n"
                    "To install manually, run:\n"
                    f"  sudo mkdir -p '{target_script.parent}' && sudo cp '{current_script}' '{target_script}' "
                    f"&& sudo chmod 755 '{target_script}'\n"
                    f"  sudo cp '{tmp_plist}' '{target_plist}' && sudo chown root:wheel '{target_plist}' "
                    f"&& sudo chmod 644 '{target_plist}'\n"
                    "  sudo touch /Library/Logs/update_mac.stdout.log /Library/Logs/update_mac.stderr.log "
                    "&& sudo chmod 666 /Library/Logs/update_mac.*.log\n",
                )

    # 3. Check if the job is active in launchd
    uid = os.getuid()
    domain = f'gui/{uid}' if RunCMD(['launchctl', 'print', f'gui/{uid}']) else f'user/{uid}'
    gui_target = f'{domain}/{a_plist}'
    is_active = RunCMD(['launchctl', 'print', gui_target]) or RunCMD(['launchctl', 'list', a_plist])

    # 4. Activate or reload if needed
    if (plist_needs_install and target_plist.is_file()) or not is_active:
        # Always bootout/unload existing registration before bootstrapping to prevent error 5
        RunCMD(['launchctl', 'bootout', f'gui/{uid}/{a_plist}'])
        RunCMD(['launchctl', 'bootout', f'user/{uid}/{a_plist}'])
        if target_plist.is_file():
            RunCMD(['launchctl', 'unload', str(target_plist)])

        # Modern bootstrap first, fallback to legacy load -w
        bootstrap_cmd = ['launchctl', 'bootstrap', domain, str(target_plist)]
        launch_load_cmd = ['launchctl', 'load', '-w', str(target_plist)]
        load_cmd = RunCMD(bootstrap_cmd) or RunCMD(launch_load_cmd)

        if load_cmd:
            print(f"Activated launchd service: {a_plist}")
            return True
        else:
            err = load_cmd.allout
            if not err:
                desc = RunCMD(['launchctl', 'error', str(load_cmd.errno)]).strip()
                err = desc or f"exit code {load_cmd.errno}"
            print(f"Failed to activate {a_plist}: {err}")
            return False

    return True


def main() -> bool:
    global options
    if options is None:
        options = yield_arguments()

    if options.spinner:
        try:
            with Wave("Testing spinner wave animation (Ctrl-C to stop)...", force=True):
                while True:
                    time.sleep(1)
        except KeyboardInterrupt:
            pass

        return True

    try:
        MacOSComputer.cleanup_startup_run()

        if options.uninstall:
            uninstall_plist()
            return True

        if options.update:
            update_installed_script()

        if not sys.argv[1:]:
            check_plist()

        mac = MacOSComputer(debug=options.debug)
        mac.update('all')
        if options.reboot:
            mac.check_and_reboot()
        else:
            print('Not rebooting due to command-line argument.')
        return True

    except Exception as err:
        print(err)
        return False


def uninstall_plist(a_plist: str = DEFAULT_PLIST) -> bool:
    """Removes the launchd service from startup and unlinks installed files."""
    MacOSComputer.cleanup_startup_run()
    launch_agents_dir = Path('/Library/LaunchAgents')
    old_user_plist = Path.home() / 'Library/LaunchAgents' / f'{a_plist}.plist'
    target_plist = launch_agents_dir / f'{a_plist}.plist'
    target_script = Path('/Library/scripts/User/update_mac.py')

    # 1. Unload from launchd
    uid = os.getuid()
    is_active = (
        (RunCMD(['launchctl', 'print', f'gui/{uid}/{a_plist}']).errno == 0)
        or (RunCMD(['launchctl', 'print', f'user/{uid}/{a_plist}']).errno == 0)
        or (RunCMD(['launchctl', 'list', a_plist]).errno == 0)
    )
    if is_active:
        RunCMD(['launchctl', 'bootout', f'gui/{uid}/{a_plist}'])
        RunCMD(['launchctl', 'bootout', f'user/{uid}/{a_plist}'])
        if target_plist.is_file():
            RunCMD(['launchctl', 'unload', '-w', str(target_plist)])
        if old_user_plist.is_file():
            RunCMD(['launchctl', 'unload', '-w', str(old_user_plist)])
        RunCMD(['launchctl', 'remove', a_plist])
        print(f"Unloaded launchd service: {a_plist}")
    else:
        print(f"Service {a_plist} is not currently running.")

    # 2. Remove plist files
    for plist_path in [target_plist, old_user_plist]:
        if plist_path.is_file():
            try:
                plist_path.unlink()
                print(f"Removed {plist_path}")
            except PermissionError:
                if sys.stdin.isatty():
                    print(f"Administrator privileges needed to remove {plist_path}.  Prompting for sudo...")
                    if RunCMD(['rm', '-f', str(plist_path)], interactive=True, need_root=True):
                        print(f"Removed {plist_path}")
                else:
                    print(f"Note: {plist_path} remains.  To remove: sudo rm {plist_path}")

    # 3. Clean up installed script if present
    if target_script.is_file():
        try:
            target_script.unlink()
            print(f"Removed {target_script}")
        except PermissionError:
            if sys.stdin.isatty():
                print(f"Administrator privileges needed to remove {target_script}.  Prompting for sudo...")
                if RunCMD(['rm', '-f', str(target_script)], interactive=True, need_root=True):
                    print(f"Removed {target_script}")
            else:
                print(
                    f"Note: {target_script} remains.  To remove:\n"
                    f"  sudo rm {target_script}",
                )

    return True


def update_installed_script(a_script: str = '/Library/scripts/User/update_mac.py') -> bool:
    """Updates installed script if the running script has a higher PROGRAM_BUILD."""
    current_script = Path(__file__).resolve()
    target_script = Path(a_script)

    if current_script == target_script:
        print(f"Running script is already {target_script}.")
        return True

    target_build = None
    if target_script.is_file():
        try:
            content = target_script.read_text(encoding='utf-8')
            match = re.search(r"^PROGRAM_BUILD(?:\s*:\s*\w+)?\s*=\s*(\d+)", content, re.MULTILINE)
            if match:
                target_build = int(match.group(1))
        except Exception as err:
            if options.debug:
                print(f"Error reading {target_script}: {err}")

    if target_build is not None and PROGRAM_BUILD <= target_build:
        print(
            f"Installed script {target_script} is already up to date "
            f"(installed build: {target_build}, running build: {PROGRAM_BUILD}).",
        )
        return True

    if target_build is not None:
        print(
            f"Updating {target_script}: running build {PROGRAM_BUILD} > installed build {target_build}...",
        )
    else:
        print(f"Installing {target_script} (running build {PROGRAM_BUILD})...")

    try:
        target_script.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(current_script, target_script)
        target_script.chmod(0o755)
        print(f"Successfully updated {target_script} to build {PROGRAM_BUILD}.")
        return True
    except PermissionError:
        permission_cmd = (
            f"mkdir -p '{target_script.parent}' && "
            f"cp '{current_script}' '{target_script}' && "
            f"chmod 755 '{target_script}'"
        )
        a_cmd = ['sh', '-c', permission_cmd]
        if sys.stdin.isatty():
            print(
                f"\n[Permission Required] Administrator privileges needed to update:\n"
                f"  - Script: {target_script}\n"
                f"Prompting for sudo password...",
            )
            if RunCMD(a_cmd, interactive=True, need_root=True):
                print(f"Successfully updated {target_script} to build {PROGRAM_BUILD}.")
                return True
            else:
                print(f"Failed to update {target_script}.")
                return False
        else:
            if RunCMD(a_cmd, timeout=15, need_root=True):
                print(f"Successfully updated {target_script} to build {PROGRAM_BUILD}.")
                return True

            print(
                f"\n[WARNING] Unable to update {target_script} (permission denied).\n"
                f"To update manually, run:\n"
                f"  sudo mkdir -p '{target_script.parent}' && sudo cp '{current_script}' "
                f"'{target_script}' && sudo chmod 755 '{target_script}'\n",
            )
            return False


def yield_arguments(some_args: list[str] | None = None):
    """
    Processes runtime arguments
    """
    if some_args is None:
        some_args = sys.argv[1:]

    parser = argparse.ArgumentParser()

    parser.add_argument(
        '-d', '--debug', action='store_true', default=False,
        help=(
            'Outputs far too much information to attempt to debug any errors.  '
            'Limited to {0} characters unless specified with `--debug-limit`'.format(DEBUG_LIMIT)
        ),
    )
    parser.add_argument(
        '-f', '--force-reboot', action='store_true', default=False,
        help='Force a reboot at the end of updates, bypassing requirement checks',
    )
    parser.add_argument(
        '-l', '--debug-limit', type=int, default=None,
        help=(
            'Like --debug but truncates character output.  Only `--debug` or `--debug-limit` is needed.  '
            'Use a negative number for no character limit.'
        ),
    )
    parser.add_argument(
        '-r', '--no-reboot', dest='reboot', action='store_false', default=True,
        help='Do not attempt to reboot',
    )
    parser.add_argument(
        '-s', '--spinner', action='store_true', default=False,
        help='Display the spinner animation indefinitely for visual inspection (Ctrl-C to stop)',
    )
    parser.add_argument(
        '-u', '--update', dest='update', action='store_true', default=None,
        help='Update /Library/scripts/User/update_mac.py if running script has a higher PROGRAM_BUILD',
    )
    parser.add_argument(
        '--no-update', dest='update', action='store_false',
        help='Do not update /Library/scripts/User/update_mac.py even if running interactively',
    )
    parser.add_argument(
        '--uninstall', action='store_true', default=False,
        help='Remove update_mac service from startup (unloads and deletes launch agent)',
    )

    args = parser.parse_args(some_args)
    if args.update is None:
        args.update = bool(sys.stdin and getattr(sys.stdin, 'isatty', lambda: False)())

    return args


if __name__ == '__main__':
    options = yield_arguments(sys.argv[1:])
    if getattr(options, 'debug_limit', False):
        options.debug = True

    # The End
    try:
        success = main()
    except KeyboardInterrupt:
        success = False
        print('\n\n          User Cancelled           \n')
    if success:
        print('\n\n          Script finished          \n')
    else:
        print('\n\n          Script FAILED!           \n')
        sys.exit(16)
