from __future__ import annotations

import itertools
import json
import plistlib
import re
import sys
from dataclasses import dataclass, field
from functools import cached_property
from os import getuid
from pathlib import Path
from shutil import copy2
from typing import Any, ClassVar

from freshenmac import config
from freshenmac.util import Logger, PrivilegedCMD, RunCMD

FILE_BUILD = 20261019

PATTERN: dict[str, re.Pattern[str]] = {
    'File Build': re.compile(r"^FILE_BUILD = (?P<build>(\d+(\.\d+)?))", re.MULTILINE),
}


@dataclass
class CronSchedule:
    """
    Encapsulates LaunchAgent calendar schedule intervals.
    Internally stores fields as dict[str, list[int]] mapping each dimension to a list of matching integers.
    """
    KEYS: ClassVar[tuple[str, ...]] = ('Minute', 'Hour', 'Day', 'Month', 'Weekday')
    fields: dict[str, list[int]] = field(default_factory=lambda: {k: [] for k in CronSchedule.KEYS})

    def __bool__(self) -> bool:
        return any(self.fields.values())

    def __iter__(self):
        return iter(self.intervals)

    def __len__(self) -> int:
        return len(self.intervals)

    @classmethod
    def from_cron(cls, expr: str) -> CronSchedule:
        """Parses a 5-part cron string into a CronSchedule."""
        values = expr.split()
        if len(values) != 5:
            if str(values).lower() == 'help':
                print(cls.help())
                return cls()

            raise ValueError(
                f"Invalid cron expression:\t'{expr}'\n\tExpected 5 fields, got {len(values)}.\n{cls.help()}",
            )

        def parse_part(val: str) -> list[int]:
            if val == '*':
                return []
            if ',' in val:
                return sorted(
                    {int(x) for x in val.split(',') if x.strip().isdigit()}
                )
            if val.isdigit():
                return [int(val)]
            return []

        return cls(
            fields={
                k: parse_part(v) for k, v in zip(cls.KEYS, values)
            }
        )

    @classmethod
    def from_dicts(cls, data: dict[str, Any] | list[dict[str, Any]]) -> CronSchedule:
        """Normalizes a launchd dict or list of dicts into a CronSchedule."""
        fields: dict[str, list[int]] = {k: [] for k in cls.KEYS}
        for a_dict in ([data] if isinstance(data, dict) else data):
            for k in a_dict.keys() & cls.KEYS:  # Intersection of input keys and all allowed keys
                fields[k].append(a_dict[k])
        for j in cls.KEYS:
            fields[j] = sorted(set(fields[j]))
        return cls(fields=fields)

    @staticmethod
    def help() -> str:
        return '\n\t'.join(
            [
                '',
                '─────── Schedule: HELP ───────',
                '5 expected fields (minute hour day month weekday).',
                'Example: "0 18 * * 1" (Mondays at 6:00 PM)',
                '──────────────────────────────',
            ],
        )

    @property
    def intervals(self) -> list[dict[str, int]]:
        """Generates Cartesian product of active schedule fields for launchd."""
        active = [k for k in self.KEYS if self.fields[k]]
        if not active:
            return []
        return [dict(zip(active, combo)) for combo in itertools.product(*(self.fields[k] for k in active))]

    @classmethod
    def parse(cls, data: str | dict[str, Any] | list[dict[str, Any]] | CronSchedule) -> CronSchedule:
        """Parses a cron string, launchd dict, or list of dicts into a CronSchedule."""
        if isinstance(data, cls):
            return data
        if isinstance(data, str) and data.strip():
            return cls.from_cron(data)
        if isinstance(data, (dict, list)):
            return cls.from_dicts(data)
        raise ValueError(f"Invalid schedule input:\t'{data}'")

    @classmethod
    def resolve(cls, a_schedule: Any | None = None, *, logger: Any | None = None) -> CronSchedule:
        """
        Resolves schedule with 3-tier precedence:
        1. Explicit argument (CronSchedule, dict, list of dicts, or cron string)
        2. Saved preferences (load_preferences()['schedule'])
        3. Config defaults (config.DEFAULTS['schedule'])
        """
        if isinstance(a_schedule, (cls, dict, list)):
            return cls.parse(a_schedule)
        if isinstance(a_schedule, str) and a_schedule.strip():
            return cls.parse(a_schedule)

        if pref_sched := load_preferences().get('schedule', ''):
            try:
                return cls.parse(pref_sched)
            except Exception as err:
                log = logger or Logger()
                log.print_store(
                    f"[Warning] Invalid schedule in preferences: '{pref_sched}' ({err}).  "
                    f"Falling back to default: {config.DEFAULTS['schedule']}",
                    component='Preferences',
                )

        return cls.from_dicts(config.DEFAULTS['schedule'])

    def to_cron(self) -> str:
        """Reconstructs a 5-part cron string from the schedule fields."""
        parts = []
        for key in self.KEYS:
            if values := self.fields[key]:
                parts.append(','.join(str(x) for x in values))
            else:
                parts.append('*')
        return ' '.join(parts)

    def to_launchd(self) -> dict[str, int] | list[dict[str, int]]:
        """Returns single dict if 1 interval, or list of dicts for launchd plist."""
        inter = self.intervals
        return inter[0] if len(inter) == 1 else inter


class LaunchAgent(PrivilegedCMD):
    """
    Manages macOS LaunchAgent registration, plist generation,
    schedule preferences, and launchd service activation.
    """

    def __init__(
        self,
        label: str = config.DEFAULTS['plist'],
        *,
        debug_vars: dict[str, int] | None = None,
        dry_run: bool = False,
        launch_agents_dir: Path = Path('/Library/LaunchAgents'),
        log_dir: Path = Path('/Library/Logs'),
        logger: Any | None = None,
        package_sync: Any | None = None,
        run_at_load: bool = False,
        schedule: Any | None = None,
        target_script: Path = config.PATHS['Script']['Target'],
    ) -> None:
        self._target_plist: Path | None = None
        self.debug_vars = debug_vars or {'debug': 0, 'debug_limit': 0}
        self.dry_run = dry_run
        self.label = label
        self.launch_agents_dir = launch_agents_dir
        self.log = logger or Logger(dry_run=self.dry_run)
        self.log_dir = log_dir
        self.logger = self.log
        self.package_sync = package_sync or PackageSync(
            debug_vars=self.debug_vars,
            dry_run=self.dry_run,
            logger=self.log,
            target_script=target_script,
        )
        self.run_at_load = run_at_load
        self.schedule = CronSchedule.resolve(schedule, logger=self.log)
        self.sudoers = f"/etc/sudoers.d/{config.APP_INFO['Name'].lower()}"

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
            self.log.print_store(f"Installed {self.launch_agents_dir}/{self.target_plist.name}", 'LaunchAgent')
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
        session_types = ['Aqua', 'Standard']
        if not self.run_at_load:
            session_types += ['Background', 'LoginWindow']
        plist_dict: dict[str, Any] = {
            'EnvironmentVariables':   {
                'PATH':       '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin',
                'PYTHONPATH': str(self.package_sync.target_dir.parent),
            },
            'Label':                  self.label,
            'LimitLoadToSessionType': session_types,
            'ProgramArguments':       [self.python_bin, str(self.package_sync.target_script)],
            'StandardErrorPath':      f"{self.log_dir}/{self.label}.stderr.log",
            'StandardOutPath':        f"{self.log_dir}/{self.label}.stdout.log",
        }
        if self.run_at_load:
            plist_dict['RunAtLoad'] = True
        else:
            plist_dict['StartCalendarInterval'] = self.schedule.to_launchd()
        return plistlib.dumps(plist_dict)

    def check_and_install(self) -> bool:
        """Verifies package files and LaunchAgent installation, activating if needed."""
        if self.old_user_plist.is_file() and not self.dry_run:
            if RunCMD(['launchctl', 'unload', '-w', str(self.old_user_plist)], **self.debug_vars):
                self.log.print_store(f"Unloaded legacy user LaunchAgent: {self.old_user_plist.name}", 'LaunchAgent')
            else:
                self.log.print_store(f"{self.old_user_plist.name} was not active in launchd", 'LaunchAgent')
            try:
                self.old_user_plist.unlink()
            except OSError as err_os:
                self.log.print_store(f"Failed to remove {self.old_user_plist.name}: {err_os}", 'LaunchAgent')
        if self.package_sync.files_to_update:
            if not self.package_sync.sync(self.package_sync.files_to_update):
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

    def install(
        self,
        *,
        privileged: bool = True,
        sync_package: bool = True,
    ) -> bool:
        """Installs the LaunchAgent plist file and synchronizes package files."""
        if sync_package and self.package_sync.files_to_update:
            if not self.package_sync.sync(self.package_sync.files_to_update):
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
            RunCMD(['launchctl', 'print', gui_target], **self.debug_vars)
            or
            RunCMD(['launchctl', 'list', self.label], **self.debug_vars),
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

    def remove_script(self) -> bool:
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
            '/usr/bin/true',
            '/usr/bin/xcodebuild',
            '/usr/local/bin/brew',
            '/usr/local/bin/mas',
            '/usr/sbin/softwareupdate',
        ]
        return (
            f"# Created by {config.APP_INFO['Name']} for automated maintenance\n"
            f"%admin ALL=(ALL) NOPASSWD: {', '.join(commands)}\n"
        )

    @property
    def target_plist(self) -> Path:
        """Returns the system-wide LaunchAgent path in /Library/LaunchAgents."""
        return self._target_plist or (self.launch_agents_dir / f"{self.label}.plist")

    @target_plist.setter
    def target_plist(self, val: Path) -> None:
        self._target_plist = val

    def uninstall(self) -> bool:
        """Removes the launchd service from startup and unlinks installed files."""
        try:
            StartupRun(debug_vars=self.debug_vars, dry_run=self.dry_run, logger=self.log).agent.remove_script()
            self.remove_script()
            self._remove_sudoers()
            self.package_sync.uninstall()
            return True

        except Exception as err_uninst:
            self.log.print_store(f"Failed to uninstall {config.APP_INFO['Name']}:\n{err_uninst}", 'LaunchAgent')
            return False


class PackageSync(PrivilegedCMD):
    """
    Manages synchronization of FreshenMac package files from source to
    the system installation directory in /Library/scripts/User.
    """

    def __init__(
        self,
        *,
        debug_vars: dict[str, int] | None = None,
        dry_run: bool = False,
        logger: Any | None = None,
        target_dir: Path = config.PATHS['Dir']['Target'],
        target_script: Path = config.PATHS['Script']['Target'],
    ) -> None:
        self.debug_vars = debug_vars or {'debug': 0, 'debug_limit': 0}
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
        else:
            self.log.print_store(f"Unable to find {self.target_dir}", 'PackageSync')
            self.log.print_store(f"Unable to find {self.target_script}", 'PackageSync')
            self.log.print_store(f"Uninstall of {config.APP_INFO['Name']} FAILED", 'PackageSync')
            return False

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
        debug_vars: dict[str, int] | None = None,
        dry_run: bool = False,
        logger: Any | None = None,
        user_pref: bool = False,
    ) -> None:
        self._parse_error = False
        self.debug_vars = debug_vars or {'debug': 0, 'debug_limit': 0}
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
                    if isinstance(value, str) and value.lower() == 'help':
                        self.log.printf(CronSchedule.help())
                        return None

                    result[key] = CronSchedule.parse(value).to_launchd()
                except ValueError as err:
                    self.log.print_store(f"[Error] Invalid schedule: {err}", component='Preferences')
                    self._parse_error = True
                    return None

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
        self.log.printf(
            f"\n{config.APP_INFO['Name']} Preferences:\n"
            f"▷ {config.PATHS['Prefs']['Default']}\n▷ {config.PATHS['Prefs']['User']}\n",
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
        self.log.printf(f"\n".join(help_display))

    def save(self) -> bool:
        if not self.updates:
            self.print_help()
            return not self._parse_error

        if self.debug_vars['debug'] > 1:
            self.log.printf(f"SavePreferences.save: Saving preferences:\n{json.dumps(self.updates, indent=2)}")

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
            agent = LaunchAgent(debug_vars=self.debug_vars, dry_run=self.dry_run, logger=self.log)
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
        debug_vars: dict[str, int] | None = None,
        dry_run: bool = False,
        label: str = f"{config.DEFAULTS['plist']}_runonce",
        launch_agents_dir: Path | None = None,
        log_dir: Path | None = None,
        logger: Any | None = None,
        target_plist: Path | None = None,
    ) -> None:
        self.debug_vars = debug_vars or {'debug': 0, 'debug_limit': 0}
        self.dry_run = dry_run
        self.label = label
        self.log = logger or Logger(dry_run=self.dry_run)
        self.logger = self.log
        self.agent = LaunchAgent(
            label=self.label,
            debug_vars=self.debug_vars,
            dry_run=self.dry_run,
            launch_agents_dir=launch_agents_dir or (Path.home() / 'Library/LaunchAgents'),
            log_dir=log_dir or (Path.home() / 'Library/Logs'),
            logger=self.log,
            run_at_load=True,
        )
        if target_plist is not None:
            self.agent.target_plist = target_plist

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
    debug_vars: dict[str, int] | None = None,
    dry_run: bool = False,
    logger: Any | None = None,
) -> bool:
    """Checks if .plist for this file is in /Library/LaunchAgents and active, if not, creates file and activates it."""
    return LaunchAgent(
        label=a_plist,
        debug_vars=debug_vars,
        dry_run=dry_run,
        logger=logger,
        schedule=cron_dict,
    ).check_and_install()


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
            except Exception as err:
                log = Logger()
                log.print_store(
                    f"[Warning] Unable to load preferences file: '{pref_file}' ({err})",
                    component='Preferences',
                )
    return prefs


def write_file(a_path: Path, data: bytes, mode: int = 0o644) -> None:
    a_path.parent.mkdir(parents=True, exist_ok=True)
    a_path.write_bytes(data)
    a_path.chmod(mode)
