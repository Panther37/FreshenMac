from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
import threading
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from functools import total_ordering
from glob import glob
from pathlib import Path
from random import choice
from shutil import get_terminal_size
from typing import Any, IO

from freshenmac import config

FILE_BUILD = 20261006


class Logger:
    """
    Unified console and persistent file logger for FreshenMac.
    Provides three primary logging methods:
      1. print: string to screen
      2. store: string to log file
      3. print_store: both print and store
    """

    def __init__(
        self,
        *,
        component: str = config.APP_INFO['Name'],
        dry_run: bool = False,
        log_file: Path | None = None,
        tagged: bool = False,
    ) -> None:
        self.component = component
        self.dry_run = dry_run
        self.log_file = log_file
        self.tagged = tagged
        self.target = log_file or get_log_file()

        is_test = 'unittest' in sys.modules and (log_file is None or log_file == get_log_file())
        self.enabled = not (self.dry_run or is_test)

        if self.enabled:
            try:
                self.target.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass

    def _disable_logger(self, error: Exception | None = None) -> None:
        """Disables persistent file logging and notifies the user."""
        self.enabled = False
        if error:
            print(f"[util.Logger] Path: {self.target}\n\tLog write failed: {error}")

    def _line_append(self, a_line: str) -> None | Exception:
        """Attempts to append a line to self.target. Returns None on success, or the Exception on failure."""
        try:
            with open(self.target, 'a', encoding='utf-8') as f:
                f.write(a_line + '\n')
        except Exception as an_exception:
            return an_exception

    def _write_line(self, formatted: str) -> None:
        """Fast-path line write with multi-stage dynamic recovery."""
        if not (an_error := self._line_append(formatted)):
            return

        if isinstance(an_error, PermissionError):
            # Stage 2: Sudo recovery (if credentials available)
            if RunCMD.can_sudo():
                # Stage 2A: Fix permissions once
                RunCMD(['sh', '-c', f"touch '{self.target}' && chmod 666 '{self.target}'"], need_root=True)
                if self._line_append(formatted) is None:
                    return

                # Stage 2B: Sudo line append
                cmd = RunCMD(['tee', '-a', str(self.target)], to_input=formatted + '\n', need_root=True)
                if not cmd.returncode:
                    return

            # Stage 3: User log directory fallback (~/Library/Logs)
            user_target = config.PATHS['Log']['User']
            if self.target != user_target:
                self.target = user_target
                try:
                    self.target.parent.mkdir(parents=True, exist_ok=True)
                except OSError:
                    pass
                if self._line_append(formatted) is None:
                    return

            # Stage 4: /var/tmp fallback (persists across reboots)
            tmp_target = Path('/var/tmp') / config.PATHS['Log']['User'].name
            if self.target != tmp_target:
                self.target = tmp_target
                if self._line_append(formatted) is None:
                    return

        # Stage 5: Unrecoverable failure - disable logging
        self._disable_logger(an_error)

    @staticmethod
    def print(message: str = '') -> None:
        """Outputs string to screen (stdout)."""
        print(message)

    def print_store(
        self,
        message: str,
        component: str | None = None,
    ) -> None:
        """Both prints to screen and stores formatted string to log file."""
        self.print(message)
        self.store(message, component=component)

    def store(
        self,
        message: str,
        component: str | None = None,
    ) -> None:
        """Stores formatted string to persistent log file."""
        if not self.enabled:
            return

        comp = component or self.component
        timestamp = datetime.now().strftime('%F %T')
        lines = message.splitlines() or [message]
        for line in lines:
            # Don't log empty lines
            if not line.strip():
                continue

            self._write_line(f"[{timestamp}] [{comp}] {line}")


class PlaySound:
    DEFAULT_SOUND: str = '/System/Library/Sounds/Pop.aiff'
    SOUND_DIR: str = '/System/Library/Sounds/'

    def __init__(self, sound: str = 'Ping'):
        self.sound: str = sound
        self.sound_list: list[str] = []
        self.sound = self._normalize_file()

    def __enter__(self):
        self.play()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False

    def _normalize_file(self) -> str:
        # Add path if no path
        if '/' not in self.sound:
            self.sound = self.SOUND_DIR + self.sound
        if self.valid_file() and self.valid_sound():
            self.sound_list = [self.sound]
            return self.sound

        # Remove extension, if it's there
        a_file_name = os.path.splitext(self.sound)[0]
        # Grab all the matches
        matches: list[str] = glob(f"{a_file_name}*")
        # Check all the matches
        for a_match in matches:
            if self.valid_sound(a_match):
                self.sound_list.append(a_match)
        return choice(self.sound_list) if self.sound_list else self.DEFAULT_SOUND

    def play(self):
        RunCMD(['afplay', self.sound], interactive=True)

    def valid_file(self, a_file: str | None = None) -> bool:
        return os.path.isfile(a_file or self.sound)

    def valid_sound(self, a_file: str | None = None) -> bool:
        return bool(RunCMD(['afinfo', '-b', a_file or self.sound]))


class PrivilegedCMD:
    debug_vars: dict

    def _sudo_write_pref(self, data: bytes, target_pref: Path) -> bool:
        """Writes preference data to target_pref via sudo cp."""
        tmp_pref = Path('/tmp') / target_pref.name
        tmp_pref.write_bytes(data)
        ok = self.run_privileged(
            ['sh', '-c', f"cp '{tmp_pref}' '{target_pref}' && chmod 644 '{target_pref}'"],
            name_running=f"  - Prefs: {target_pref}",
        )
        tmp_pref.unlink(missing_ok=True)
        return ok

    def run_privileged(
        self,
        cmd_args: list[str],
        tty_timeout: int | float = 120,
        pipe_timeout: int | float = 15,
        *,
        name_running: str | None = None,
    ) -> bool:
        """Executes a command with root privileges, prompting interactively if in a TTY."""
        is_tty = is_interactive()
        if is_tty and not RunCMD.can_sudo():
            print(
                f"\n[Permission Required] Administrator privileges needed to run:\n"
                f"{name_running or ' '.join(cmd_args)}\n"
                "Prompting for sudo password...\n",
            )
        timeout = tty_timeout if is_tty else pipe_timeout
        return bool(RunCMD(cmd_args, timeout, interactive=is_tty, need_root=True, **self.debug_vars))


class RunAppleScript:
    """
    Lightweight executor for AppleScript commands via osascript.
    Executes synchronously by default, or asynchronously when background=True.
    """

    def __init__(
        self,
        script: str | list[str] | tuple[str, ...],
        timeout: int = 0,
        *,
        background: bool = False,
        debug: int = 0,
        debug_limit: int = 0,
    ):
        self.background = background
        self.debug = debug
        self.debug_limit = debug_limit
        self.error: Exception | None = None
        self.returncode: int = 0
        self.stderr: str = ''
        self.stdout: str = ''
        self.timeout = timeout or None
        if self.debug:
            self.debug_limit = None if int(self.debug_limit) <= 0 else self.debug_limit
        if isinstance(script, (list, tuple)):
            script = '\n'.join(str(x) for x in script)
        self.script = textwrap.dedent(str(script or '')).strip()
        self.run()

    def __bool__(self) -> bool:
        return self.returncode == 0

    def __contains__(self, item: str) -> bool:
        return item in self.stdout

    def __repr__(self) -> str:
        return self.stdout

    def __str__(self) -> str:
        return self.stdout

    @classmethod
    def dialog(
        cls,
        message: str | list[str],
        timeout: int = 0,
        *,
        background: bool = False,
        buttons: list[str] | tuple[str, ...] = ('OK',),
        debug: int = 0,
        debug_limit: int = 0,
        default_button: str | None = None,
        giving_up_after: int = 0,
        icon: str | None = 'caution',
        title: str | None = None,
    ) -> 'RunAppleScript':
        """Displays a System Events dialog and returns a RunAppleScript instance."""
        btn_str = ', '.join(f'"{b}"' for b in buttons)
        opts = f'buttons {{{btn_str}}}'
        opts += f' default button "{default_button}"' if default_button else ''
        opts += f' giving up after {giving_up_after}' if giving_up_after > 0 else ''
        opts += f' with icon {icon}' if icon else ''
        opts += f' with title "{title}"' if title else ''
        script = f'tell application "System Events"\n    activate\n    display dialog "{message}" {opts}\nend tell'
        timeout = timeout or (giving_up_after + 10 if giving_up_after else 0)
        return cls(script, timeout, background=background, debug=debug, debug_limit=debug_limit)

    @classmethod
    def notify(
        cls,
        message: str | list[str],
        timeout: int = 0,
        *,
        background: bool = True,
        debug: int = 0,
        debug_limit: int = 0,
        subtitle: str | None = None,
        title: str | None = None,
    ) -> 'RunAppleScript':
        """Displays a macOS notification and returns a RunAppleScript instance."""
        cmd = f'display notification "{message}"'
        cmd += f' with title "{title}"' if title else ''
        cmd += f' subtitle "{subtitle}"' if subtitle else ''
        return cls(cmd, timeout, background=background, debug=debug, debug_limit=debug_limit)

    def run(self) -> None:
        if self.debug > 0:
            print(
                "\n─────── RunAppleScript ───────\n"
                f"Command:    {self.script}\n",
            )
        try:
            if self.background:
                subprocess.Popen(['osascript', '-e', self.script])
                return

            result = subprocess.run(
                ['osascript', '-e', self.script],
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
            self.stdout = (result.stdout or '').strip()
            self.stderr = (result.stderr or '').strip()
            self.returncode = result.returncode
        except subprocess.TimeoutExpired as timeout_err:
            self.error = timeout_err
            self.returncode = -2
            self.stderr = f"AppleScript timed out after {self.timeout}s"
        except Exception as script_err:
            self.error = script_err
            self.returncode = -3
            self.stderr = str(script_err)

        if self.debug > 1:
            limit = self.debug_limit
            print(
                "\n─────── RunAppleScript ───────\n"
                f"Command:    {self.script}\n"
                f"ReturnCode: {self.returncode}\n"
                f"Output: \n{self.stdout[:limit]}◊\n"
                f"Error: \n{self.stderr[:limit] or str(self.error)[:limit] or 'None '}◊\n"
                f"───────────── 🍎︎ ─────────────\n",
            )


class RunCMD:
    """Run a bash command and provide output and error"""

    def __init__(
        self,
        cmd_args: list[str],
        timeout: int | float = 10,
        *,
        cancelable: bool | None = None,
        debug: int = 0,
        debug_limit: int = 0,
        download: bool = False,
        environ: dict | None = None,
        interactive: bool = False,
        need_root: bool = False,
        need_shell: bool = False,
        # shell_cmd: str = '/bin/bash',
        shell_cmd: str = '/bin/zsh',
        spinner: bool | None = None,
        taskpolicy: str | None = None,
        to_input: str | None = None,
    ):
        """
        :param cancelable: Allow command to be canceled by user (Ctrl+C).
        :param debug: Verbosity/debug level (0=quiet, 1=verbose, 2+=command execution dumps)
        :param interactive: ``True:`` do not capture stdout/stderr to allow interactive TTY prompts
        :param spinner: ``True (or None and timeout >= 30):`` show a terminal spinner during execution
        :param taskpolicy: Limit command: The lowest priority is 'maintenance', then 'background', and 'utility'.
        ``None`` is normal and the highest priority for this option.
        :param to_input: String content to pass to standard input (stdin).
        """
        if cancelable is None:
            cancelable = timeout in config.TIMEOUT.values()
        self.cancelable = cancelable
        self.canceled = False
        self.cmd_args = cmd_args
        self.debug = debug
        self.debug_limit = debug_limit
        self.download = download
        self.environ = environ or {}
        self.initial_need_root = need_root
        self.interactive = interactive
        self.need_root = need_root
        self.need_shell = need_shell
        self.permissions_checked = False
        self.returncode = 0
        self.shell_cmd = shell_cmd if need_shell else None
        self.spinner = spinner
        self.stderr = ''
        self.stdout = ''
        self.timeout = timeout
        self.to_input = to_input
        if self.debug:
            self.debug_limit = None if int(self.debug_limit) <= 0 else self.debug_limit
        if taskpolicy is not None:
            clamp = taskpolicy.strip().lower() if isinstance(taskpolicy, str) else None
            if clamp in ('maintenance', 'background', 'utility'):
                self.cmd_args = self._prepend(['taskpolicy', '-c', clamp])

        self.run()
        if not self.permissions_checked:
            self._check_permission()
        if self.debug > 1:
            limit = self.debug_limit
            print(
                "\n─────────── RunCMD ───────────\n"
                f"Command: {self.cmd_args}\n"
                f"Output: \n{self.stdout[:limit]}◊\n"
                f"Error: \n{self.stderr[:limit]}◊\n"
                f'''{f" {self.shell_cmd or ''} ":─^30}\n''',
            )

    def __bool__(self):
        return self.returncode == 0

    def __contains__(self, item):
        return item in self.stdout

    def __getitem__(self, item):
        return self.stdout.splitlines()[item]

    def __iadd__(self, other):
        self.stdout = self.stdout + other
        return self

    def __iter__(self):
        return iter(self.stdout.splitlines() if self.stdout else [])

    def __len__(self):
        return len(self.stdout.splitlines())

    def __repr__(self):
        return self.stdout

    def _check_permission(self) -> None:
        self.permissions_checked = True
        if self.returncode in [13, 126] and not self.need_root:
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

    def _run_cmd(self, run_cmd_args: list[str], popen_env) -> None:
        try:
            result = subprocess.run(
                run_cmd_args,
                input=self.to_input,
                shell=self.need_shell,
                capture_output=(not self.interactive),
                text=True,
                timeout=self.timeout,
                executable=self.shell_cmd,
                env=popen_env,
            )
            self.stdout = result.stdout or ''
            self.stderr = result.stderr or ''
            self.returncode = result.returncode
        except subprocess.TimeoutExpired:
            self.returncode = -2
            self.stderr = f"Command timed out after {self.timeout}s: {run_cmd_args}"
        except KeyboardInterrupt:
            if self.cancelable:
                self.returncode = 130
                self.canceled = True
                self.stderr = f"Command canceled by user: {run_cmd_args}"
                print(f"\n[Canceled] Skipped by user: {' '.join(str(x) for x in self.cmd_args)}")
            else:
                raise

        except Exception as err_msg_run:
            self.returncode = -3
            self.stderr = f"Unable to run command: {err_msg_run}\n\t{run_cmd_args}\n"

    @contextmanager
    def _spinner(self):
        should_spin = not self.interactive and self.spinner is not False
        should_spin = should_spin and (self.spinner or self.timeout >= 30)
        if not should_spin:
            yield None
            return

        display_cmd = ' '.join(' '.join(str(a) for a in self.cmd_args).split())
        with Wave(display_cmd, direction='right', persist=True) as spinner:
            yield spinner

    @classmethod
    def can_sudo(cls) -> bool:
        """Checks whether root privileges are available without prompting."""
        if os.geteuid() == 0:
            return True

        return bool(cls(['true'], need_root=True))

    @property
    def error(self) -> str:
        return self.stderr.strip()

    def parse_dict(self, delimiter: str = ':') -> dict[str, str]:
        """Make dictionary from stdout"""
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
                        a_key += f" {key_times:04d}"
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
            popen_env = {**os.environ, **self.environ}
            if self.debug:
                clean_environ = {}
                for k, v in self.environ.items():
                    clean_environ[k] = f"Sensitive: {bool(v) if 'pass' in k.lower() else v}"
                verbose_environ = f"\ncustom environ: {clean_environ}"
        if self.debug > 0:
            print(
                "\n─────────── RunCMD ───────────\n"
                f"Command: {run_cmd_args}\n"
                f"Root: {self.need_root}{verbose_environ}",
            )
        with self._spinner():
            self._run_cmd(run_cmd_args, popen_env)

    def splitlines(self) -> list[str]:
        return self.stdout.splitlines()

    @property
    def std_all(self) -> str:
        return '\n'.join([self.stdout, self.stderr]).strip()

    def strip(self):
        return self.stdout.strip()


class Runner:
    """
    Manages the lifecycle of a daemon background thread with graceful cancellation,
    start condition checking, and context manager support.
    """

    def __init__(
        self,
        target: Callable[..., Any] | None = None,
        *,
        args: tuple = (),
        condition: Callable[[], bool] | bool = True,
        join_timeout: float = 1.0,
        kwargs: dict | None = None,
    ) -> None:
        self.args = args
        self.condition = condition
        self.join_timeout = join_timeout
        self.kwargs = kwargs
        self.target = target or self._run
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> 'Runner':
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.stop()

    def _can_run(self) -> bool:
        if callable(self.condition):
            return bool(self.condition())

        return bool(self.condition)

    def _run(self) -> None:
        """Default target if not provided; can be overridden by subclasses."""
        pass

    @property
    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if not self._can_run():
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self.target,
            args=self.args,
            kwargs=self.kwargs or {},
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=self.join_timeout)
        self._thread = None


class SudoKeepAlive(Runner):
    """
    Maintains active sudo credentials in the background by periodically
    refreshing the timestamp via 'sudo -n -v' while FreshenMac is executing.
    """
    DEFAULT_INTERVAL: int = 60

    def __init__(self, interval: int = DEFAULT_INTERVAL) -> None:
        self.interval = interval
        super().__init__(
            condition=True,
            join_timeout=1.0,
            target=self._keepalive,
        )

    @property
    def _can_sudo(self) -> bool:
        return RunCMD.can_sudo()

    def _keepalive(self) -> None:
        while not self._stop_event.wait(self.interval):
            if self._can_sudo:
                RunCMD(['sudo', '-n', '-v'])


@total_ordering
@dataclass(eq=False)
class Version:
    """
    Version data container supporting natural ordering.
    Splits version strings into numeric parts and character ordinals.
    """
    parts: tuple[int, ...]
    raw: str = field(compare=False, default='')

    ver_words = {
        'snapshot': -9,  # Earliest / dev build
        'nightly':  -9,  # Automated dev build
        'dev':      -9,  # Explicit dev release
        'alpha':    -8,  # Early pre-release
        'a':        -8,  # Alpha alias
        'beta':     -7,  # Feature-frozen pre-release
        'b':        -7,  # Beta alias
        'preview':  -7,  # Public preview / late beta
        'gamma':    -6,  # Pre-RC stage
        'pre':      -5,  # Late pre-release
        'rc':       -4,  # Final candidate before GA
        'v':        1500,  # 'Version'
        'final':    1510,  # Base stable release
        'omega':    1520,  # Greek alias for final
        'post':     1600,  # Immediate packaging/post-release tweak
        'patch':    1700,  # Standard code bug fix
        'pl':       1710,  # Patch-level alias
        'p':        1710,  # Patch/post shorthand
        'sp':       1800,  # Cumulative service pack
    }

    def __bool__(self) -> bool:
        return bool(self.parts)

    def __eq__(self, other: Any) -> bool:
        other = self.parse_other(other)
        return self._key == other._key if isinstance(other, Version) else NotImplemented

    def __ge__(self, other: Any) -> bool:
        other = self.parse_other(other)
        return self._key >= other._key if isinstance(other, Version) else NotImplemented

    def __gt__(self, other: Any) -> bool:
        other = self.parse_other(other)
        return self._key > other._key if isinstance(other, Version) else NotImplemented

    def __hash__(self) -> int:
        return hash(self._key)

    def __le__(self, other: Any) -> bool:
        other = self.parse_other(other)
        return self._key <= other._key if isinstance(other, Version) else NotImplemented

    def __lt__(self, other: Any) -> bool:
        other = self.parse_other(other)
        return self._key < other._key if isinstance(other, Version) else NotImplemented

    def __str__(self) -> str:
        return self.raw or '.'.join(str(x) for x in self.parts)

    @property
    def _key(self) -> tuple[tuple[int, ...], tuple[int, ...]]:
        release: list[int] = []
        tag: list[int] = []
        found_tag = False
        for i in self.parts:
            if not found_tag and 0 <= i < self.ver_words.get('v'):
                release.append(i)
            else:
                found_tag = True
                tag.append(i)
        while len(release) > 1 and release[-1] == 0:
            release.pop()
        t1 = (0,) if not tag or tag == [self.ver_words.get('final')] else tuple(tag)
        return tuple(release), t1

    @staticmethod
    def _make_raw(other: Any) -> str:
        if isinstance(other, str):
            return other.strip()

        if isinstance(other, (Version, int, float)):
            return str(other).strip()

        if isinstance(other, (list, tuple)):
            return '.'.join(str(x).strip() for x in other if x is not None)

        if other is None:
            return '0'

        return ''

    @classmethod
    def parse(cls, v_str: Any) -> 'Version':
        has_letters = False
        raw = cls._make_raw(v_str)
        cleaned = re.sub(r'^[vV](?=\d)', '', raw)
        tokens = re.findall(r'\d+|[a-zA-Z]+', cleaned)
        work_list = []
        ver: list[int] = []
        for i in tokens:
            try:
                work_list.append(int(i))
            except ValueError:  # We don't have a number
                has_letters = True
                work_list.append(str(i))
        if has_letters:
            max_j = len(work_list) - 1
            for j in range(len(work_list)):
                chars = work_list[j]
                if isinstance(chars, int):
                    ver.append(chars)
                    continue

                chars = str(chars)
                check_value = None
                if (j < max_j and isinstance(work_list[j + 1], int)) or (j == max_j and len(chars) > 1):
                    check_value = cls.ver_words.get(chars.lower())
                if check_value:
                    ver.append(check_value)
                else:
                    ver.extend([-1 * ord(x) for x in chars])
        else:
            ver = work_list
        return cls(parts=tuple(ver), raw=raw)

    def parse_other(self, other: Any) -> 'Version | None':
        if isinstance(other, Version):
            return other

        if isinstance(other, (int, float, list, str, tuple)) or other is None:
            return self.parse(other)

        return None


class Wave(Runner):
    """
    Terminal spinner using curved arc wave animation for long-running operations.
    """

    ANIMATION: str = ' ⢀⣀⣠⣴⣶⣾⣿⡿⠿⠟⠋⠉⠁  ⠈⠉⠙⠻⠿⢿⣿⣷⣶⣦⣄⣀ '
    ANIMATION_FILL: str = ' '
    DELAY: float = 0.1
    WIDTH: int = 3

    def __init__(
        self,
        message: str | None = None,
        *,
        banner: bool = False,
        direction: str = 'left',
        force: bool = False,
        persist: bool = False,
        static_msg: str = 'Running',
        stream: Any = None,
    ):
        self.banner = banner
        self.direction = direction[0].lower()
        self.force = force
        self.persist = persist
        self.static_msg = static_msg
        self.stream = stream or sys.stdout
        self.message = self._make_message(message or '')
        self.direction_function: Callable[[str, int], str] = getattr(
            self, f"_undulate_{self.direction}", self._undulate_r,
        )
        super().__init__(
            condition=self._can_start,
            join_timeout=0.5,
            target=self._undulate,
        )

    def _can_start(self) -> bool:
        return self.force or is_interactive(self.stream)

    def _end_frame(self) -> str:
        last_frame = f"{datetime.now().strftime('%H%M'):{self.ANIMATION_FILL}^{self.WIDTH + 3}}"
        return f"{last_frame}{self.message}"

    def _make_banner(self, a_msg: str = '') -> str:
        columns = get_terminal_size(fallback=(80, 24)).columns - (self.WIDTH + 3)
        display_list = a_msg.splitlines()
        return_list = []
        for i in display_list:
            return_list.append(textwrap.fill(i, width=columns))
        return '\n'.join(return_list)

    def _make_message(self, a_msg: str = '') -> str:
        if self.banner:
            return self._make_banner(a_msg)

        display_cmd = repr(a_msg)[1:-1]
        if display_cmd.startswith('taskpolicy'):
            display_cmd = ' '.join(display_cmd.split()[3:])
            self.static_msg += ' at lower priority'
        self.static_msg += ': '
        columns = get_terminal_size(fallback=(80, 24)).columns - (len(self.static_msg) + self.WIDTH + 14)
        if len(display_cmd) > columns:
            display_cmd = display_cmd[:columns - 3] + '...'
        else:
            display_cmd += ' ' * (columns - len(display_cmd))
        return f"{self.static_msg}{display_cmd} {datetime.now().strftime('%d/%H%M')}"

    def _print_line(self, some_text: str) -> None:
        self.stream.write(f"\r{some_text}")
        self.stream.flush()

    def _undulate(self):
        # Wait before showing anything
        if not self.banner and self._stop_event.wait(0.25):
            return

        frame_index = 0
        num_animation_frames = len(self.ANIMATION)
        frame = ' ' * self.WIDTH
        while not self._stop_event.is_set():
            # Make next frame
            frame = self.direction_function(a_frame=frame, index_num=frame_index)
            # Print frame
            self._print_line(f" {frame}  {self.message}")
            frame_index = (frame_index + 1) % num_animation_frames
            self._stop_event.wait(self.DELAY)

        # Finished with animation
        if self.persist or self.banner:
            self._print_line(f"{self._end_frame()}\n")
        else:
            self._print_line('\x1b[K')

    def _undulate_l(self, a_frame: str, index_num: int) -> str:
        return (a_frame + self.ANIMATION[index_num])[-self.WIDTH:]  # Left <- R

    def _undulate_r(self, a_frame: str, index_num: int) -> str:
        return (self.ANIMATION[-index_num] + a_frame)[:self.WIDTH]  # L -> Right


''' Functions '''


def get_log_file() -> Path:
    """Returns the writable FreshenMac log path (/Library/Logs or ~/Library/Logs)."""
    sys_log = config.PATHS['Log']['System']
    user_log = config.PATHS['Log']['User']
    if sys_log.exists():
        if os.access(sys_log, os.W_OK):
            return sys_log

    elif os.access(sys_log.parent, os.W_OK):
        return sys_log

    return user_log


def is_interactive(stream: IO | None = sys.stdin) -> bool:
    return bool(stream and stream.isatty())


def parse_version(version: Any) -> Version:
    return Version.parse(version)
