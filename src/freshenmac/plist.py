from __future__ import annotations

import plistlib
import re
import sys
from functools import cached_property
from os import getuid
from pathlib import Path
from shutil import copy2
from typing import Any

from freshenmac import config
from freshenmac.util import Logger, PrivilegedCMD, RunCMD

FILE_BUILD = 20261005

PATTERN: dict[str, re.Pattern[str]] = {
    'File Build': re.compile(r"^FILE_BUILD = (?P<build>(\d+(\.\d+)?))", re.MULTILINE),
}


class LaunchAgent(PrivilegedCMD):
    """
    Manages macOS LaunchAgent registration, plist generation,
    schedule preferences, and launchd service activation.
    """

    def __init__(
        self,
        label: str = config.DEFAULTS['plist'],
        *,
        debug: int = 0,
        debug_limit: int = 0,
        dry_run: bool = False,
        hour: int | None = None,
        launch_agents_dir: Path = Path('/Library/LaunchAgents'),
        log_dir: Path = Path('/Library/Logs'),
        logger: Any | None = None,
        minute: int | None = None,
        package_sync: Any | None = None,
        run_at_load: bool = False,
        schedule_dict: dict | list[dict] | None = None,
        target_script: Path = config.PATHS['Script']['Target'],
        weekday: int | None = None,
    ) -> None:
        default_schedule = config.DEFAULTS.get('schedule', {})
        self._target_plist: Path | None = None
        self.cron_schedule = schedule_dict
        self.debug = debug
        self.debug_limit = debug_limit
        self.debug_vars = {'debug': debug, 'debug_limit': debug_limit}
        self.dry_run = dry_run
        self.hour = default_schedule.get('Hour', 2) if hour is None else hour
        self.label = label
        self.launch_agents_dir = launch_agents_dir
        self.log = logger or Logger(dry_run=self.dry_run)
        self.log_dir = log_dir
        self.logger = self.log
        self.minute = default_schedule.get('Minute', 0) if minute is None else minute
        self.package_sync = package_sync or PackageSync(
            dry_run=self.dry_run,
            logger=self.log,
            target_script=target_script,
            **self.debug_vars,
        )
        self.run_at_load = run_at_load
        self.sudoers = f"/etc/sudoers.d/{config.APP_INFO['Name'].lower()}"
        self.weekday = default_schedule.get('Weekday', 0) if weekday is None else weekday
        if self.cron_schedule is None:
            self.cron_schedule = {
                'Hour':    self.hour,
                'Minute':  self.minute,
                'Weekday': self.weekday,
            }

    def _init_log_files(self) -> None:
        """Ensures stdout and stderr log files exist in log_dir with write permissions."""
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            for log_file in [
                self.log_dir / f"{self.label}.stdout.log",
                self.log_dir / f"{self.label}.stderr.log",
            ]:
                log_file.touch(exist_ok=True)
                log_file.chmod(0o666)
        except OSError:
            pass

    def _install_privileged(self, plist_data: bytes) -> bool:
        """
        Installs the target plist, configures log files, and sets up sudoers using elevated administrator privileges.
        """
        tmp_plist = Path('/tmp') / self.target_plist.name
        tmp_plist.write_bytes(plist_data)

        sudoers_cmds, tmp_sudoers = self._prepare_sudoers_commands()

        commands = [
            f"mkdir -p '{self.launch_agents_dir}'",
            f"cp '{tmp_plist}' '{self.target_plist}'",
            f"chown root:wheel '{self.target_plist}'",
            f"chmod 644 '{self.target_plist}'",
            f"mkdir -p '{self.log_dir}'",
            f"touch '{self.log_dir}/{self.label}.stdout.log' '{self.log_dir}/{self.label}.stderr.log'",
            f"chmod 666 '{self.log_dir}/{self.label}.'*.log",
        ]
        items = [f"  - Agent: {self.target_plist}"]
        if sudoers_cmds:
            commands.extend(sudoers_cmds)
            items.append(f"  - Sudoers: {self.sudoers}")

        installed = self.run_privileged(['sh', '-c', ' && '.join(commands)], name_running=' '.join(items))
        tmp_plist.unlink(missing_ok=True)
        if tmp_sudoers:
            tmp_sudoers.unlink(missing_ok=True)

        if installed:
            self.log.print_store(f"Installed {self.target_plist.name} in {self.launch_agents_dir}", 'LaunchAgent')
            if sudoers_cmds:
                self.log.print_store(f"Installed {self.sudoers} for unattended maintenance.", 'LaunchAgent')
        else:
            self.log.print_store(
                f"\n[WARNING] Unable to install system-wide agent to {self.target_plist} "
                "(permission denied).\n",
                'LaunchAgent',
            )
        return installed

    def _prepare_sudoers_commands(self) -> tuple[list[str], Path | None]:
        """Validates sudoers content with visudo and returns (commands, tmp_file)."""
        tmp_sudoers = Path(f"/tmp/{config.APP_INFO['Name'].lower()}_sudoers")
        tmp_sudoers.write_text(self.sudoers_content, encoding='utf-8')
        check_cmd = RunCMD(['/usr/sbin/visudo', '-c', '-f', str(tmp_sudoers)], **self.debug_vars)
        if not check_cmd:
            tmp_sudoers.unlink(missing_ok=True)
            return [], None

        commands = [
            "mkdir -p /etc/sudoers.d",
            f"cp '{tmp_sudoers}' {self.sudoers}",
            f"chown root:wheel {self.sudoers}",
            f"chmod 0440 {self.sudoers}",
        ]
        return commands, tmp_sudoers

    def _remove_sudoers(self) -> bool:
        """Removes {self.sudoers} configuration if present."""
        sudoers_file = Path(self.sudoers)
        if not sudoers_file.exists():
            return True

        if self.dry_run:
            self.log.print_store(f"[Dry-run] Would remove {self.sudoers}", 'LaunchAgent')
            return True

        removed = self.run_privileged(
            ['rm', '-f', self.sudoers],
            name_running=f"  - Sudoers: {self.sudoers}",
        )
        if removed:
            self.log.print_store(f"Removed {self.sudoers}", 'LaunchAgent')
        return removed

    def activate(self) -> bool:
        """Bootstraps or loads the launchd agent."""
        if self.dry_run:
            self.log.print_store(f"[Dry-run] Would activate launchd service: {self.label}", component='LaunchAgent')
            return True

        self.bootout()
        bootstrap_cmd = ['launchctl', 'bootstrap', self.domain, str(self.target_plist)]
        launch_load_cmd = ['launchctl', 'load', '-w', str(self.target_plist)]
        load_cmd = RunCMD(bootstrap_cmd, **self.debug_vars) or RunCMD(launch_load_cmd, **self.debug_vars)

        if load_cmd:
            self.log.print_store(f"Activated launchd service: {self.label}", component='LaunchAgent')
            return True

        err = load_cmd.std_all
        if not err:
            desc = RunCMD(['launchctl', 'error', str(load_cmd.returncode)], **self.debug_vars).strip()
            err = desc or f"exit code {load_cmd.returncode}"
        self.log.print_store(f"Failed to activate {self.label}: {err}", component='LaunchAgent')
        return False

    def bootout(self) -> bool:
        """Unregisters the launchd service from gui/user domains and unloads plist."""
        if self.dry_run:
            self.log.print_store(f"[Dry-run] Would unload launchd service: {self.label}", component='LaunchAgent')
            return True

        uid = getuid()
        RunCMD(['launchctl', 'bootout', f"gui/{uid}/{self.label}"], **self.debug_vars)
        RunCMD(['launchctl', 'bootout', f"user/{uid}/{self.label}"], **self.debug_vars)
        if self.target_plist.is_file():
            RunCMD(['launchctl', 'unload', str(self.target_plist)], **self.debug_vars)
        if self.old_user_plist.is_file():
            RunCMD(['launchctl', 'unload', '-w', str(self.old_user_plist)], **self.debug_vars)
        RunCMD(['launchctl', 'remove', self.label], **self.debug_vars)
        return True

    def build_plist_data(self) -> bytes:
        """Builds the binary plist representation for the launchd agent."""
        schedule_cron = load_preferences().get('schedule', '')
        if schedule_cron:
            try:
                self.cron_schedule = parse_cron(schedule_cron)
            except Exception:
                pass

        session_types = ['Aqua', 'Standard']
        if not self.run_at_load:
            session_types += ['Background', 'LoginWindow']
        plist_dict: dict[str, Any] = {
            'EnvironmentVariables':   {
                'PATH':       '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin',
                'PYTHONPATH': str(self.target_dir.parent),
            },
            'Label':                  self.label,
            'LimitLoadToSessionType': session_types,
            'ProgramArguments':       [self.python_bin, str(self.target_script)],
            'StandardErrorPath':      f"{self.log_dir}/{self.label}.stderr.log",
            'StandardOutPath':        f"{self.log_dir}/{self.label}.stdout.log",
        }
        if self.run_at_load:
            plist_dict['RunAtLoad'] = True
        else:
            plist_dict['StartCalendarInterval'] = self.cron_schedule or config.DEFAULTS['schedule']
        return plistlib.dumps(plist_dict)

    def check_and_install(self) -> bool:
        """Verifies package files and LaunchAgent installation, activating if needed."""
        if self.old_user_plist.is_file() and not self.dry_run:
            RunCMD(['launchctl', 'unload', '-w', str(self.old_user_plist)], **self.debug_vars)
            try:
                self.old_user_plist.unlink()
            except OSError:
                pass

        if self.files_to_update:
            if not self.package_sync.sync(self.files_to_update):
                return False

        if self.plist_needs_install:
            if not self.install():
                return False

        if not self.is_active:
            return self.activate()

        return True

    @cached_property
    def domain(self) -> str:
        """Identifies active launchd domain (gui/{uid} or user/{uid})."""
        uid = getuid()
        return f"gui/{uid}" if RunCMD(['launchctl', 'print', f"gui/{uid}"], **self.debug_vars) else f"user/{uid}"

    @property
    def files_to_update(self) -> list[Path]:
        """Returns list of package files in config.PATHS['Dir']['Package'] that are missing/outdated in target_dir."""
        return self.package_sync.files_to_update

    def install(
        self,
        *,
        privileged: bool = True,
        sync_package: bool = True,
    ) -> bool:
        """Installs the LaunchAgent plist file and synchronizes package files."""
        if sync_package and self.files_to_update:
            if not self.package_sync.sync(self.files_to_update):
                return False

        plist_data = self.build_plist_data()
        if self.dry_run:
            self.log.print_store(
                f"[Dry-run] Would install {self.target_plist.name} in {self.launch_agents_dir}",
                'LaunchAgent',
            )
            return True

        try:
            write_file(self.target_plist, plist_data)
            self._init_log_files()
            self.log.print_store(f"Installed {self.target_plist.name} in {self.launch_agents_dir}", 'LaunchAgent')
            return True

        except PermissionError:
            if privileged:
                return self._install_privileged(plist_data)

            self.log.print_store(
                f"\n[WARNING] Unable to install agent to {self.target_plist} (permission denied).\n", 'LaunchAgent',
            )
            return False

    @property
    def is_active(self) -> bool:
        """Checks if the service is currently registered with launchd."""
        gui_target = f"{self.domain}/{self.label}"
        return bool(
            RunCMD(['launchctl', 'print', gui_target], **self.debug_vars) or RunCMD(
                ['launchctl', 'list', self.label],
                **self.debug_vars,
            ),
        )

    @property
    def old_user_plist(self) -> Path:
        """Returns the obsolete per-user LaunchAgent path in ~/Library/LaunchAgents."""
        return Path.home() / 'Library/LaunchAgents' / f"{self.label}.plist"

    @property
    def plist_needs_install(self) -> bool:
        """Checks if the target plist file is missing, modified, or has incorrect permissions."""
        if not self.target_plist.is_file():
            return True

        if self.target_plist.read_bytes() != self.build_plist_data():
            return True

        stat = self.target_plist.stat()
        return stat.st_uid != 0 or (stat.st_mode & 0o777) != 0o644

    @cached_property
    def python_bin(self) -> str:
        """Locates the Python 3 binary for LaunchAgent ProgramArguments."""
        if Path('/usr/local/bin/python3').is_file():
            return '/usr/local/bin/python3'

        return sys.executable or '/usr/local/bin/python3'

    def remove(self) -> bool:
        """Unloads the service and deletes the plist file."""
        if self.is_active:
            if not self.dry_run:
                self.bootout()
            self.log.print_store(f"Unloaded launchd service: {self.label}", 'LaunchAgent')

        for plist_path in [self.target_plist, self.old_user_plist]:
            if not plist_path.is_file():
                continue

            if self.dry_run:
                self.log.print_store(f"[Dry-run] Would remove {plist_path}", 'LaunchAgent')
                continue

            try:
                plist_path.unlink()
                self.log.print_store(f"Removed {plist_path}", 'LaunchAgent')
            except PermissionError:
                if self.run_privileged(['rm', '-f', str(plist_path)], name_running=f"  - Plist: {plist_path}"):
                    self.log.print_store(f"Removed {plist_path}", 'LaunchAgent')
                else:
                    self.log.print_store(f"Note: {plist_path} remains.  To remove: sudo rm {plist_path}", 'LaunchAgent')
        return True

    @property
    def sudoers_content(self) -> str:
        """Content for /etc/sudoers.d unattended maintenance configuration."""
        commands = [
            '/Applications/Xcode.app/Contents/Developer/usr/bin/xcodebuild',
            '/opt/homebrew/bin/brew',
            '/opt/homebrew/bin/mas',
            '/sbin/shutdown',
            '/usr/bin/fdesetup',
            '/usr/bin/xcodebuild',
            '/usr/local/bin/brew',
            '/usr/local/bin/mas',
            '/usr/sbin/softwareupdate',
        ]
        return (
            f"# Created by {config.APP_INFO['Name']} for automated maintenance\n"
            f"%admin ALL=(ALL) NOPASSWD: {', '.join(commands)}\n"
        )

    def sync_package(self, files: list[Path] | None = None) -> bool:
        """Synchronizes specified files (or all outdated files) to the target installation directory."""
        return self.package_sync.sync(files)

    @property
    def target_dir(self) -> Path:
        """Returns the package target installation directory."""
        return self.package_sync.target_dir

    @target_dir.setter
    def target_dir(self, val: Path) -> None:
        self.package_sync.target_dir = val

    @property
    def target_plist(self) -> Path:
        """Returns the system-wide LaunchAgent path in /Library/LaunchAgents."""
        return self._target_plist or (self.launch_agents_dir / f"{self.label}.plist")

    @target_plist.setter
    def target_plist(self, val: Path) -> None:
        self._target_plist = val

    @property
    def target_script(self) -> Path:
        """Returns the target script path."""
        return self.package_sync.target_script

    @target_script.setter
    def target_script(self, val: Path) -> None:
        self.package_sync.target_script = val

    def uninstall(self) -> bool:
        """Removes the launchd service from startup and unlinks installed files."""
        try:
            StartupRun.cleanup()
            self.remove()
            self._remove_sudoers()
            self.package_sync.uninstall()
            return True

        except Exception as err_uninst:
            self.log.print_store(f"Failed to uninstall {config.APP_INFO['Name']}:\n{err_uninst}", 'LaunchAgent')
            return False

    def update_files(self, target: Path | None = None) -> bool:
        """Updates specific target file or synchronizes package files if outdated."""
        return self.package_sync.update_files(target)


class PackageSync(PrivilegedCMD):
    """
    Manages synchronization of FreshenMac package files from source to
    the system installation directory in /Library/scripts/User.
    """

    def __init__(
        self,
        *,
        debug: int = 0,
        debug_limit: int = 0,
        dry_run: bool = False,
        logger: Any | None = None,
        target_dir: Path = config.PATHS['Dir']['Target'],
        target_script: Path = config.PATHS['Script']['Target'],
    ) -> None:
        self.debug = debug
        self.debug_limit = debug_limit
        self.debug_vars = {'debug': debug, 'debug_limit': debug_limit}
        self.dry_run = dry_run
        self.log = logger or Logger(dry_run=self.dry_run)
        self.logger = self.log
        self.target_dir = target_dir
        self.target_script = target_script

    @property
    def files_to_update(self) -> list[Path]:
        """Returns list of package files in config.PATHS['Dir']['Package'] that are missing/outdated in target_dir."""
        if config.PATHS['Dir']['Package'] == self.target_dir:
            return []

        return [
            f for f in config.PATHS['Dir']['Package'].glob('*.py')
            if file_needs_update(f, self.target_dir / f.name)
        ]

    def sync(self, files: list[Path] | None = None) -> bool:
        """Synchronizes specified files (or all outdated files) to the target installation directory."""
        if files is None:
            files = self.files_to_update
        if not files:
            self.log.print_store(f"Installed {config.APP_INFO['Name']} is already up to date.", 'PackageSync')
            return True

        if self.dry_run:
            self.log.print_store(
                f"[Dry-run] Would update {len(files)} file(s) in {self.target_dir}",
                component='PackageSync',
            )
            return True

        self.log.print_store(
            f"Updating {config.APP_INFO['Name']} ({len(files)} file(s) changed: {', '.join(f.name for f in files)})...",
            'PackageSync',
        )
        try:
            self.target_dir.mkdir(parents=True, exist_ok=True)
            for py_file in files:
                dest_file = self.target_dir / py_file.name
                copy2(py_file, dest_file)
                dest_file.chmod(0o755 if py_file.name == self.target_script.name else 0o644)
                self.log.print_store(f"Successfully updated {py_file.name} in {self.target_dir}.", 'PackageSync')
            return True

        except PermissionError:
            copy_cmds = [f"cp '{f}' '{self.target_dir}/'" for f in files]
            permission_cmd = (
                f"mkdir -p '{self.target_dir}' && "
                f"{' && '.join(copy_cmds)} && "
                f"chmod 755 '{self.target_script}'"
            )
            a_cmd = ['sh', '-c', permission_cmd]
            if self.run_privileged(a_cmd, name_running=f"  - Package: {self.target_dir}"):
                self.log.print_store(
                    f"Successfully updated {config.APP_INFO['Name']} in {self.target_dir}.",
                    'PackageSync',
                )
                return True

            self.log.print_store(
                f"\n[WARNING] Unable to update {self.target_dir} (permission denied).\n"
                f"To update manually, run:\n  sudo {permission_cmd}\n",
                'PackageSync',
            )
            return False

    def uninstall(self) -> bool:
        """Removes installed package directory or script from target installation directory."""
        if self.target_dir.is_dir():
            if self.dry_run:
                self.log.print_store(f"[Dry-run] Would remove {self.target_dir}", 'PackageSync')
                return True

            try:
                for py_file in self.target_dir.glob('*.py'):
                    py_file.unlink()
                try:
                    self.target_dir.rmdir()
                except OSError:
                    pass
                self.log.print_store(
                    f"Removed installed {config.APP_INFO['Name']} package from {self.target_dir}",
                    'PackageSync',
                )
            except PermissionError:
                if self.run_privileged(
                    ['rm', '-rf', str(self.target_dir)],
                    name_running=f"  - Package: {self.target_dir}",
                ):
                    self.log.print_store(f"Removed {self.target_dir}", 'PackageSync')
                else:
                    self.log.print_store(
                        f"Note: {self.target_dir} remains.  To remove:\n  sudo rm -rf {self.target_dir}",
                        'PackageSync',
                    )
        elif self.target_script.is_file():
            if self.dry_run:
                self.log.print_store(f"[Dry-run] Would remove {self.target_script}", 'PackageSync')
                return True

            try:
                self.target_script.unlink()
                self.log.print_store(f"Removed {self.target_script}", 'PackageSync')
            except PermissionError:
                if self.run_privileged(
                    ['rm', '-f', str(self.target_script)],
                    name_running=f"  - Script: {self.target_script}",
                ):
                    self.log.print_store(f"Removed {self.target_script}", 'PackageSync')
                else:
                    self.log.print_store(
                        f"Note: {self.target_script} remains.  To remove:\n  sudo rm {self.target_script}",
                        'PackageSync',
                    )
        return True

    def update_files(self, target: Path | None = None) -> bool:
        """Updates specific target file or synchronizes package files if outdated."""
        if target is None:
            if config.PATHS['Dir']['Package'] == self.target_dir:
                self.log.print_store(f"Running script is already {self.target_script}.", 'PackageSync')
                return True

            return self.sync()

        target_script = Path(target)
        source_script = config.PATHS['Script']['Source'] if config.PATHS['Script']['Source'].is_file() else Path(
            __file__,
        ).resolve()

        if source_script == target_script:
            self.log.print_store(f"Running script is already {target_script}.", 'PackageSync')
            return True

        if not file_needs_update(source_script, target_script):
            self.log.print_store(f"Installed script {target_script} is already up to date.", 'PackageSync')
            return True

        src_build = f"{get_file_build(source_script):.6g}"  # Just to keep the floats from being 82518238110 digits long
        if self.dry_run:
            self.log.print_store(f"[Dry-run] Would update {target_script} to build {src_build}", 'PackageSync')
            return True

        self.log.print_store(f"Updating {target_script} to build {src_build}...", 'PackageSync')
        try:
            target_script.parent.mkdir(parents=True, exist_ok=True)
            copy2(source_script, target_script)
            target_script.chmod(0o755)
            self.log.print_store(f"Successfully updated {target_script} to build {src_build}.", 'PackageSync')
            return True

        except PermissionError:
            permission_cmd = (
                f"mkdir -p '{target_script.parent}' && "
                f"cp '{source_script}' '{target_script}' && "
                f"chmod 755 '{target_script}'"
            )
            a_cmd = ['sh', '-c', permission_cmd]
            if self.run_privileged(a_cmd, name_running=f"  - Script: {target_script}"):
                self.log.print_store(f"Successfully updated {target_script} to build {src_build}.", 'PackageSync')
                return True

            self.log.print_store(
                f"\n[WARNING] Unable to update {target_script} (permission denied).\n"
                f"To update manually, run:\n  sudo {permission_cmd}\n",
                'PackageSync',
            )
            return False


class SavePreferences(PrivilegedCMD):
    """
    Parses and saves FreshenMac preferences to the system or user plist.

    Accepts a dict or a 'Key=Value' string.  If input is blank or unparseable,
    prints help describing all supported preferences.

    Usage:
        SavePreferences('schedule=0 6 * * 1').save()
        SavePreferences({'no-mas': ['iPhoto', '408981381']}).save()
        SavePreferences('no-mas=iPhoto 408981381').save()
    """

    SCHEMA: dict[str, dict[str, str]] = {
        'no-mas':   {
            'short':   'Skip updating an App Store item',
            'type':    'list',
            'help':    'App Store apps to skip by name or numeric ID (space-separated)',
            'example': 'no-mas=iPhoto Aperture',
            'note':    'in the example above, it looks for both "iPhoto" and "Aperture".',
        },
        'schedule': {
            'short':   'Set a schedule like cron',
            'type':    'cron',
            'help':    'LaunchAgent schedule in cron syntax (minute hour day month weekday)',
            'example': 'schedule=0 23 * * 1',
        },
    }

    def __init__(
        self,
        updates: dict[str, Any] | str | None = None,
        *,
        debug: int = 0,
        debug_limit: int = 0,
        dry_run: bool = False,
        logger: Any | None = None,
        user_pref: bool = False,
    ) -> None:
        self._parse_error = False
        self.debug_vars = {'debug': debug, 'debug_limit': debug_limit}
        self.dry_run = dry_run
        self.log = logger or Logger(dry_run=self.dry_run)
        self.logger = self.log
        self.user_pref = user_pref
        self.updates = self._parse(updates)

    def _normalize(self, updates: dict[str, Any]) -> dict[str, Any] | None:
        result = {}
        for raw_key, value in updates.items():
            key = raw_key.lower()
            if key not in self.SCHEMA:
                self.log.print_store(f"[Error] Unknown preference key: '{raw_key}'", component='Preferences')
                self._parse_error = True
                self.print_help()
                return None

            if self.SCHEMA[key]['type'] == 'list':
                items = value.split() if isinstance(value, str) else [str(x) for x in value]
                result[key] = list(dict.fromkeys(x.strip().lower() for x in items if x.strip()))
            elif self.SCHEMA[key]['type'] == 'cron':
                try:
                    parse_cron(str(value))
                except ValueError as err:
                    self.log.print_store(f"[Error] Invalid schedule: {err}", component='Preferences')
                    self._parse_error = True
                    return None

                result[key] = str(value)
            else:
                result[key] = value
        return result

    def _parse(self, raw: dict[str, Any] | str | None) -> dict[str, Any] | None:
        if not raw:
            return None

        if isinstance(raw, dict):
            return self._normalize(raw)

        if raw.strip().lower() == 'help':
            return None

        if '=' not in raw:
            self.log.print_store(
                f"[Error] Could not parse preference: '{raw}' (expected 'Key=Value')",
                component='Preferences',
            )
            self._parse_error = True
            self.print_help()
            return None

        key, _, value = raw.partition('=')
        return self._normalize({key: value})

    def print_help(self) -> None:
        self.log.print(
            f"\nFreshenMac Preferences:\n▷ {config.PATHS['Prefs']['Default']}\n▷ {config.PATHS['Prefs']['User']}\n",
        )
        a_line = f"    {'⠒' * 28}"
        help_display = [a_line]
        for key, meta in self.SCHEMA.items():
            help_line = [
                f"  {key}:\t{meta['short']}\n",
                f"    {meta['help']}",
                f"\tExample:  `--save-prefs '{meta['example']}'`",
            ]
            if a_note := meta.get('note'):
                help_line.append(f"\tNote:     {a_note}")
            help_line.append(a_line)
            help_display.append('\n'.join(help_line))
        self.log.print(f"\n".join(help_display))

    def save(self) -> bool:
        if not self.updates:
            self.print_help()
            return not self._parse_error

        target_pref = config.PATHS['Prefs']['User'] if self.user_pref else config.PATHS['Prefs']['Default']
        prefs = load_preferences()
        prefs.update(self.updates)
        data = plistlib.dumps(prefs)

        if self.dry_run:
            self.log.print_store(
                f"[Dry-run] Would save preferences to {target_pref}: {self.updates}",
                component='Preferences',
            )
            if 'schedule' in self.updates:
                self.log.print_store("[Dry-run] Would reinstall and reactivate LaunchAgent", component='Preferences')
            return True

        try:
            write_file(target_pref, data)
        except PermissionError:
            if not self._sudo_write_pref(data, target_pref):
                return False

        self.log.print_store(f"Saved preferences to {target_pref}: {self.updates}", component='Preferences')

        if 'schedule' in self.updates:
            agent = LaunchAgent(dry_run=self.dry_run, logger=self.log, **self.debug_vars)
            agent.install()
            agent.activate()

        return True


class StartupRun:
    """
    Manages the one-shot user LaunchAgent scheduled to rerun FreshenMac
    on next user login when updates were deferred due to running applications.
    """

    def __init__(
        self,
        *,
        debug: int = 0,
        debug_limit: int = 0,
        dry_run: bool = False,
        label: str = f"{config.DEFAULTS['plist']}_runonce",
        launch_agents_dir: Path | None = None,
        log_dir: Path | None = None,
        logger: Any | None = None,
        target_plist: Path | None = None,
    ) -> None:
        self.debug_vars = {'debug': debug, 'debug_limit': debug_limit}
        self.dry_run = dry_run
        self.label = label
        self.log = logger or Logger(dry_run=self.dry_run)
        self.logger = self.log
        self.agent = LaunchAgent(
            label=self.label,
            dry_run=self.dry_run,
            launch_agents_dir=launch_agents_dir or (Path.home() / 'Library/LaunchAgents'),
            log_dir=log_dir or (Path.home() / 'Library/Logs'),
            logger=self.log,
            run_at_load=True,
            **self.debug_vars,
        )
        if target_plist is not None:
            self.agent.target_plist = target_plist

    @classmethod
    def cleanup(cls, *, logger: Any | None = None) -> bool:
        """Unloads and removes the temporary one-shot startup LaunchAgent if present."""
        return cls(logger=logger).remove()

    @property
    def launch_agents_dir(self) -> Path:
        """Returns the user launch agents directory."""
        return self.agent.launch_agents_dir

    @launch_agents_dir.setter
    def launch_agents_dir(self, val: Path) -> None:
        self.agent.launch_agents_dir = val

    @property
    def log_dir(self) -> Path:
        """Returns the user logs directory."""
        return self.agent.log_dir

    @log_dir.setter
    def log_dir(self, val: Path) -> None:
        self.agent.log_dir = val

    @property
    def plist_path(self) -> Path:
        """Returns the target plist path (backwards-compatible alias for target_plist)."""
        return self.target_plist

    @plist_path.setter
    def plist_path(self, val: Path) -> None:
        self.target_plist = val

    def remove(self) -> bool:
        """Unloads and unlinks the runonce LaunchAgent."""
        return self.agent.remove()

    def schedule(self) -> bool:
        """Creates and activates the one-shot LaunchAgent to rerun FreshenMac on next login."""
        return self.agent.install(privileged=False, sync_package=False) and self.agent.activate()

    @property
    def target_plist(self) -> Path:
        """Returns the target plist path."""
        return self.agent.target_plist

    @target_plist.setter
    def target_plist(self, val: Path) -> None:
        self.agent.target_plist = val


def check_plist(
    a_plist: str = config.DEFAULTS['plist'],
    cron_dict: dict | list[dict] = config.DEFAULTS['schedule'],
    *,
    dry_run: bool = False,
    logger: Any | None = None,
) -> bool:
    """Checks if .plist for this file is in /Library/LaunchAgents and active, if not, creates file and activates it."""
    agent = LaunchAgent(label=a_plist, schedule_dict=cron_dict, dry_run=dry_run, logger=logger)
    return agent.check_and_install()


def cleanup_startup_run(*, logger: Any | None = None) -> bool:
    """Unloads and removes the temporary one-shot startup LaunchAgent if present."""
    return StartupRun.cleanup(logger=logger)


def file_needs_update(source_file: Path, target_file: Path) -> bool:
    """Checks whether target_file is missing or has a lower build than source_file."""
    if not target_file.is_file():
        return True

    dst_build = get_file_build(target_file)
    return dst_build < 1 or get_file_build(source_file) > dst_build


def get_file_build(path: Path) -> float:
    """Extracts FILE_BUILD float from a Python file if defined."""
    result = 0.0
    if path.is_file():
        try:
            content = path.read_text(encoding='utf-8')
            match = PATTERN['File Build'].search(content)
            if match:
                result = float(match.group('build'))
        except Exception:
            pass
    return result


def load_preferences() -> dict[str, Any]:
    """Loads preferences from /Library/Preferences and ~/Library/Preferences (user overrides default)."""
    prefs: dict[str, Any] = {}
    for pref_file in [config.PATHS['Prefs']['Default'], config.PATHS['Prefs']['User']]:
        if pref_file.is_file():
            try:
                prefs.update(plistlib.loads(pref_file.read_bytes()))
            except Exception:
                pass
    return prefs


def parse_cron(expr: str) -> dict[str, int] | list[dict[str, int]]:
    """Converts a 5-part cron expression into a launchd interval dict (or list of dicts)."""

    def star_int(a_val: str) -> int | list[int] | None:
        if ',' in a_val:
            return [int(x) for x in a_val.split(',')]

        return int(a_val) if a_val.isdigit() else None

    keys = ['Minute', 'Hour', 'Day', 'Month', 'Weekday']
    values = expr.split()
    if len(values) != 5:
        raise ValueError(
            f"Invalid cron expression '{expr}': expected 5 fields "
            f"(minute hour day month weekday), got {len(values)}. "
            f"Example: '0 18 * * 1' (Mondays at 6:00 PM)",
        )

    values = [star_int(x) for x in values]

    cron_dict = {k: v for k, v in zip(keys, values) if v is not None}
    if not any(isinstance(v, list) for v in cron_dict.values()):
        return cron_dict

    # Multiple values, make a list of dicts for each combination
    list_len = 1
    for v in cron_dict.values():
        if isinstance(v, list):
            list_len *= len(v)
    cron_list = [dict.fromkeys(cron_dict.keys(), 0) for _ in range(list_len)]
    multi_dict = {}
    for k, v in cron_dict.items():
        if isinstance(v, int):
            for i in range(list_len):
                cron_list[i][k] = v
        else:
            multi_dict[k] = v
    stride = list_len
    for k, v in multi_dict.items():
        stride //= len(v)
        for i, val in enumerate(v):
            for j in range(stride):
                cron_list[i * stride + j][k] = val
    return cron_list


def schedule_startup_run(*, dry_run: bool = False, logger: Any | None = None) -> bool:
    """Creates a one-shot LaunchAgent to rerun FreshenMac on next user login."""
    return StartupRun(dry_run=dry_run, logger=logger).schedule()


def uninstall_plist(
    a_plist: str = config.DEFAULTS['plist'],
    *,
    dry_run: bool = False,
    logger: Any | None = None,
) -> bool:
    """Removes the launchd service from startup and unlinks installed files."""
    agent = LaunchAgent(label=a_plist, dry_run=dry_run, logger=logger)
    return agent.uninstall()


def update_installed_script(
    a_script: str | Path | None = None,
    *,
    debug: int = 0,
    debug_limit: int = 0,
    dry_run: bool = False,
    logger: Any | None = None,
) -> bool:
    """Updates installed script or package if the running files have higher builds."""
    agent = LaunchAgent(debug=debug, debug_limit=debug_limit, dry_run=dry_run, logger=logger)
    return agent.update_files(target=Path(a_script) if a_script else None)


def write_file(a_path: Path, data: bytes, mode: int = 0o644) -> None:
    a_path.parent.mkdir(parents=True, exist_ok=True)
    a_path.write_bytes(data)
    a_path.chmod(mode)
