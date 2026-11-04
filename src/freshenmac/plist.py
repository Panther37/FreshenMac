from __future__ import annotations

import itertools
import json
import plistlib
import pwd
import shutil
import sys
from dataclasses import dataclass, field
from functools import cached_property
from os import getuid
from pathlib import Path
from typing import Any, ClassVar

from freshenmac import config, util

FILE_BUILD = 20261103


@dataclass
class CronSchedule:
    """
    Encapsulates LaunchAgent calendar schedule intervals.
    Internally stores fields as dict[str, list[int]] mapping each dimension to a list of matching integers.
    """
    KEYS: ClassVar = ('Minute', 'Hour', 'Day', 'Month', 'Weekday')
    # lambda to make sure they all have different blank lists
    fields: dict[str, list[int]] = field(default_factory=lambda: {k: [] for k in CronSchedule.KEYS})

    def __bool__(self) -> bool:
        return any(self.fields.values())

    def __iter__(self):
        return iter(self.intervals)

    def __len__(self) -> int:
        return len(self.intervals)

    @staticmethod
    def _parse_part(a_schedule: str, time_unit_name: str) -> list[int]:
        if a_schedule == '*':
            return []

        res: set[int] = set()
        for i in a_schedule.split(','):
            part = i.strip()
            if not part.isdigit():
                raise ValueError(
                    f"CronSchedule: Invalid integer '{part}' for field '{time_unit_name}'"
                )

            res.add(int(part))

        if time_unit_name == 'Weekday' and 7 in res:
            res.discard(7)
            res.add(0)

        return sorted(res)

    @classmethod
    def from_cron(cls, expr: str) -> CronSchedule:
        """Parses a 5-part cron string into a CronSchedule."""
        values = expr.split()

        # Check Length
        if len(values) != 5:
            if expr.strip().lower() == 'help':
                print(cls.help())
                return cls()

            raise ValueError(
                f"Invalid cron expression:\t'{expr}'\n\tExpected 5 fields, got {len(values)}.\n{cls.help()}",
            )

        # Put it all together
        return cls(fields={k: cls._parse_part(v, k) for k, v in zip(cls.KEYS, values)})

    @classmethod
    def from_dicts(cls, data: dict[str, Any] | list[dict[str, Any]]) -> CronSchedule:
        """Normalizes a launchd dict or list of dicts into a CronSchedule."""
        raw_fields: dict[str, set[int]] = {k: set() for k in cls.KEYS}
        for a_dict in ([data] if isinstance(data, dict) else data):
            for k in a_dict.keys() & cls.KEYS:  # Intersection of input keys and all allowed keys
                val = a_dict[k]
                if isinstance(val, (list, tuple, set)):
                    raw_fields[k].update(int(x) for x in val if str(x).isdigit())
                elif str(val).isdigit():
                    raw_fields[k].add(int(val))
        return cls(fields={k: sorted(raw_fields[k]) for k in cls.KEYS})

    @staticmethod
    def help() -> str:
        return '\n\t'.join(
            [
                '',
                '─────── Schedule: HELP ───────',
                '5 expected fields (minute hour day month weekday).',
                'Example: "0 18 * * 1,4" (Mondays and Thursdays at 6:00 PM)',
                '──────────────────────────────',
            ],
        )

    @property
    def intervals(self) -> list[dict[str, int]]:
        """Generates Cartesian product of active schedule fields for launchd."""
        active = [k for k in self.KEYS if self.fields.get(k)]
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
    def resolve(cls, *, logger: Any | None = None) -> CronSchedule:
        """
        Resolves schedule with 2-tier precedence:
        1. Saved preferences (load_preferences()['schedule'])
        2. Config defaults (config.DEFAULTS['schedule'])
        """
        if pref_sched := load_preferences().get('schedule', ''):
            try:
                return cls.parse(pref_sched)

            except Exception as err:
                log = logger or util.Logger()
                log.print_store(
                    f"[Warning] Invalid schedule in preferences: '{pref_sched}' ({err}).  "
                    f"Falling back to default: {config.DEFAULTS['schedule']}",
                    component='Preferences',
                )

        return cls.from_dicts(config.DEFAULTS['schedule'])

    def to_cron(self) -> str:  # noqa P37-UNUSED_OKAY
        """Reconstructs a 5-part cron string from the schedule fields."""
        cron_list = []
        for i in self.KEYS:
            if int_list := self.fields.get(i):
                cron_list.append(','.join([str(x) for x in int_list]))
            else:
                cron_list.append('*')
        return ' '.join(cron_list)

    def to_launchd(self) -> dict[str, int] | list[dict[str, int]]:
        """Returns single dict if 1 interval, or list of dicts for launchd plist."""
        inter = self.intervals
        return inter[0] if len(inter) == 1 else inter


class LaunchAgent(util.PrivilegedCMD):
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
        run_at_load: bool = False,
        target_script: Path | None = None,
    ) -> None:
        self._target_plist: Path | None = None
        self._target_script: Path | None = target_script
        self.debug_vars = debug_vars or {'debug': 0, 'debug_limit': 0}
        self.dry_run = dry_run
        self.label = label
        self.launch_agents_dir = launch_agents_dir
        self.log = logger or util.Logger(dry_run=self.dry_run)
        self.log_dir = log_dir
        self.run_at_load = run_at_load
        self.schedule = CronSchedule.resolve(logger=self.log)
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
            pass  # Best-effort pre-creation; privileged install or Logger fallback handles permissions later

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
            f"touch '{config.PATHS['Log']['System']}'",
            f"chown root:admin '{config.PATHS['Log']['System']}'",
            f"chmod 664 '{config.PATHS['Log']['System']}'",
        ]
        items = [f"  - Agent: {self.target_plist}"]
        if sudoers_cmds:
            commands.extend(sudoers_cmds)
            items.append(f"  - Sudoers: {self.sudoers}")

        installed = self.run_privileged(['sh', '-c', ' && '.join(commands)], name_running=' '.join(items))
        tmp_plist.unlink(missing_ok=True)
        if tmp_sudoers != Path(''):
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

    @cached_property
    def _launchctrl_print_vars(self) -> dict[str, int]:
        if self.debug_vars['debug'] > 1:
            return {'debug': self.debug_vars['debug'], 'debug_limit': 100}

        else:
            return self.debug_vars

    def _prepare_sudoers_commands(self) -> tuple[list[str], Path]:
        """Validates sudoers content with visudo and returns (commands, tmp_file)."""
        tmp_sudoers = Path(f"/tmp/{config.APP_INFO['Name'].lower()}_sudoers")
        tmp_sudoers.write_text(self.sudoers_content, encoding='utf-8')
        check_cmd = util.RunCMD(['/usr/sbin/visudo', '-c', '-f', str(tmp_sudoers)], **self.debug_vars)
        if not check_cmd:
            tmp_sudoers.unlink(missing_ok=True)
            return [], Path('')

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
        load_cmd = util.RunCMD(bootstrap_cmd, **self.debug_vars) or util.RunCMD(launch_load_cmd, **self.debug_vars)

        if load_cmd:
            self.log.print_store(f"Activated launchd service: {self.label}", component='LaunchAgent')
            return True

        err = load_cmd.std_all
        if not err:
            desc = util.RunCMD(['launchctl', 'error', str(load_cmd.returncode)], **self.debug_vars).strip()
            err = desc or f"exit code {load_cmd.returncode}"
        self.log.print_store(f"Failed to activate {self.label}: {err}", component='LaunchAgent')
        return False

    def bootout(self) -> bool:
        """Unregisters the launchd service from gui/user domains and unloads plist."""
        if self.dry_run:
            self.log.print_store(f"[Dry-run] Would unload launchd service: {self.label}", component='LaunchAgent')
            return True

        uid = self.console_uid
        util.RunCMD(['launchctl', 'bootout', f"gui/{uid}/{self.label}"], **self.debug_vars)
        util.RunCMD(['launchctl', 'bootout', f"user/{uid}/{self.label}"], **self.debug_vars)
        if self.target_plist.is_file():
            util.RunCMD(['launchctl', 'unload', str(self.target_plist)], **self.debug_vars)
        util.RunCMD(['launchctl', 'remove', self.label], **self.debug_vars)
        return True

    def build_plist_data(self) -> bytes:
        """Builds the binary plist representation for the launchd agent."""
        session_types = ['Aqua', 'Standard']
        if not self.run_at_load:
            session_types += ['Background', 'LoginWindow']
        plist_dict: dict[str, Any] = {
            'EnvironmentVariables':   {
                'PATH': '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin',
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
            plist_dict['StartCalendarInterval'] = self.schedule.to_launchd()
        return plistlib.dumps(plist_dict)

    def check_and_install(self) -> bool:
        """Verifies LaunchAgent installation, activating if needed."""
        if self.plist_needs_install:
            if not self.install():
                return False

        if not self.is_active:
            return self.activate()

        return True

    @cached_property
    def console_uid(self) -> int:
        """Returns the active console user's UID, even when running as root under sudo."""
        uid = getuid()
        if uid == 0:
            try:
                return pwd.getpwnam(util.TortimerCreds.get_console_user()).pw_uid

            except KeyError:
                pass  # Console user not in pwd database; fall through to return root UID

        return uid

    @cached_property
    def domain(self) -> str:
        """Identifies active launchd domain (gui/{uid} or user/{uid})."""
        uid = self.console_uid
        a_cmd = ['launchctl', 'print', f"gui/{uid}"]
        return f"gui/{uid}" if util.RunCMD(a_cmd, **self._launchctrl_print_vars) else f"user/{uid}"

    def install(
        self,
        *,
        privileged: bool = True,
    ) -> bool:
        """Installs the LaunchAgent plist file."""
        plist_data = self.build_plist_data()

        if self.dry_run:
            self.log.print_store(
                f"[Dry-run] Would install {self.target_plist.name} in {self.launch_agents_dir}",
                'LaunchAgent',
            )
            return True

        if privileged and getuid() == 0:
            return self._install_privileged(plist_data)

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
            util.RunCMD(['launchctl', 'print', gui_target], **self._launchctrl_print_vars)
            or util.RunCMD(['launchctl', 'list', self.label], **self.debug_vars),
        )

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

        if not self.target_plist.is_file():
            return True

        if self.dry_run:
            self.log.print_store(f"[Dry-run] Would remove {self.target_plist}", 'LaunchAgent')
            return True

        try:
            self.target_plist.unlink()
            self.log.print_store(f"Removed {self.target_plist}", 'LaunchAgent')
        except PermissionError:
            if self.run_privileged(
                ['rm', '-f', str(self.target_plist)],
                name_running=f"  - Plist: {self.target_plist}",
            ):
                self.log.print_store(f"Removed {self.target_plist}", 'LaunchAgent')
            else:
                self.log.print_store(
                    f"Note: {self.target_plist} remains.  To remove: sudo rm {self.target_plist}",
                    'LaunchAgent',
                )
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

    @property
    def target_script(self) -> Path:
        """Resolves the target executable script for LaunchAgent ProgramArguments."""
        if self._target_script:
            return self._target_script

        targets = config.PATHS['Script']['Target']
        if isinstance(targets, Path):
            return targets

        for candidate in targets:
            if candidate.is_file():
                return candidate

        for candidate in targets:
            if candidate.parent.is_dir():
                return candidate

        if brew_bin := shutil.which('freshenmac'):
            return Path(brew_bin)

        if brew_prefix := util.RunCMD(['brew', '--prefix'], **self.debug_vars).strip():
            return Path(brew_prefix) / 'bin/freshenmac'

        return config.PATHS['Script']['Source']

    @target_script.setter
    def target_script(self, val: Path) -> None:
        self._target_script = val

    def uninstall(self) -> bool:
        """Removes the launchd service from startup and cleans up sudoers."""
        try:
            StartupRun(debug_vars=self.debug_vars, dry_run=self.dry_run, logger=self.log).agent.remove_script()
            self.remove_script()
            self._remove_sudoers()
            return True

        except Exception as err_uninst:
            self.log.print_store(f"Failed to uninstall {config.APP_INFO['Name']}:\n{err_uninst}", 'LaunchAgent')
            return False


class SavePreferences(util.PrivilegedCMD):
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
        'download-speed': {
            'short':   'Minimum expected download speed in Mbps',
            'type':    'float',
            'help':    'Minimum download speed in Mbps used to calculate dynamic App Store timeouts (default: 5.0)',
            'example': 'download-speed=10',
        },
        'no-mas':         {
            'short':   'Skip updating an App Store item',
            'type':    'list',
            'help':    'App Store apps to skip by name or numeric ID (space-separated)',
            'example': 'no-mas=iPhoto Aperture',
            'note':    'in the example above, it looks for both "iPhoto" and "Aperture".',
        },
        'schedule':       {
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
        self.log = logger or util.Logger(dry_run=self.dry_run)
        self.user_pref = user_pref
        self.updates = self._parse(updates)

    def _normalize(self, updates: dict[str, Any]) -> dict[str, Any]:
        result = {}
        for raw_key, value in updates.items():
            key = raw_key.lower()
            if key not in self.SCHEMA:
                self.log.print_store(f"[Error] Unknown preference key: '{raw_key}'", component='Preferences')
                self._parse_error = True
                self.print_help()
                return {}

            if self.SCHEMA[key]['type'] == 'list':
                items = value.split() if isinstance(value, str) else [str(x) for x in value]
                result[key] = list(dict.fromkeys(x.strip().lower() for x in items if x.strip()))
            elif self.SCHEMA[key]['type'] == 'cron':
                try:
                    if isinstance(value, str) and value.lower() == 'help':
                        self.log.printf(CronSchedule.help())
                        return {}

                    result[key] = CronSchedule.parse(value).to_launchd()
                except ValueError as err:
                    self.log.print_store(f"[Error] Invalid schedule: {err}", component='Preferences')
                    self._parse_error = True
                    return {}

            elif self.SCHEMA[key]['type'] == 'float':
                try:
                    result[key] = float(value)
                except (TypeError, ValueError):
                    self.log.print_store(f"[Error] Invalid float for '{key}': '{value}'", component='Preferences')
                    self._parse_error = True
                    return {}

            else:
                result[key] = value

        return result

    def _parse(self, raw: dict[str, Any] | str | None) -> dict[str, Any]:
        if not raw:
            return {}

        if isinstance(raw, dict):
            return self._normalize(raw)

        if raw.strip().lower() == 'help':
            return {}

        if '=' not in raw:
            self.log.print_store(
                f"[Error] Could not parse preference: '{raw}' (expected 'Key=Value')",
                component='Preferences',
            )
            self._parse_error = True
            self.print_help()
            return {}

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
                f"\tExample:  `--save-pref '{meta['example']}'`",
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
        self.log = logger or util.Logger(dry_run=self.dry_run)
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
        return self.agent.install(privileged=False) and self.agent.activate()

    @property
    def target_plist(self) -> Path:
        """Returns the target plist path."""
        return self.agent.target_plist

    @target_plist.setter
    def target_plist(self, val: Path) -> None:
        self.agent.target_plist = val


def load_preferences() -> dict[str, Any]:
    """Loads preferences from /Library/Preferences and ~/Library/Preferences (user overrides default)."""
    prefs: dict[str, Any] = {}
    for pref_file in [config.PATHS['Prefs']['Default'], config.PATHS['Prefs']['User']]:
        if pref_file.is_file():
            try:
                prefs.update(plistlib.loads(pref_file.read_bytes()))
            except Exception as err:
                log = util.Logger()
                log.print_store(
                    f"[Warning] Unable to load preferences file: '{pref_file}' ({err})",
                    component='Preferences',
                )
    return prefs


def write_file(a_path: Path, data: bytes, mode: int = 0o644) -> None:
    a_path.parent.mkdir(parents=True, exist_ok=True)
    a_path.write_bytes(data)
    a_path.chmod(mode)
