#!/usr/bin/env python3
"""
Simulated RunCMD test double and output recorder.

Provides SimRunCMD, an in-memory and fixture-backed implementation of RunCMD that bypasses subprocess execution while
preserving all RunCMD protocols, methods, and attributes.  Also functions as a standalone command recorder that
executes terminal commands through RunCMD and saves fixtures.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

# Ensure src/ and repo root are in sys.path when executed directly as a script
REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / 'src'
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from freshenmac.util import RunCMD  # noqa: E402

DEFAULT_TIMEOUT: int = 86400  # 1 day (24 * 60 * 60 seconds)
DEFAULT_FIXTURES_DIR: Path = REPO_ROOT / 'test' / 'fixtures'


def _make_command_slug(cmd_str: str) -> str:
    """Creates a clean filesystem slug from a command string."""
    tokens = shlex.split(cmd_str) if cmd_str else []
    if not tokens:
        return 'cmd'
    # Skip 'sudo' if leading
    if tokens[0] == 'sudo' and len(tokens) > 1:
        tokens = tokens[1:]
    base = '_'.join(tokens[:3]) if tokens else 'cmd'
    slug = re.sub(r"\W+", '_', base).strip('_')
    return slug or 'cmd'


class SimRunCMD(RunCMD):
    """
    Simulated RunCMD test double and output recorder.

    Bypasses subprocess execution during initialization while preserving all RunCMD sequence, boolean, and string
    protocols.  Supports live recording via RunCMD to timestamped directories and loading from saved fixtures.
    """

    def __init__(
        self,
        stdout: str = '',
        stderr: str = '',
        returncode: int = 0,
        *,
        cmd_args: list[str] | None = None,
        cmd_str: str = '',
        need_root: bool | None = None,
        need_shell: bool | None = None,
        run_time: int = 0,
    ) -> None:
        if cmd_str and not cmd_args:
            try:
                cmd_args = shlex.split(cmd_str)
            except ValueError:
                cmd_args = cmd_str.split()
        elif cmd_args and not cmd_str:
            cmd_str = ' '.join(cmd_args)

        self.cmd_args: list[str] = cmd_args or []
        self.cmd_str: str = cmd_str

        if need_root is None:
            need_root = bool(self.cmd_args and 'sudo' in self.cmd_args[0])
        self.need_root: bool = need_root

        if need_shell is None:
            need_shell = bool(re.search(r"[|><;&*~$]", self.cmd_str)) if self.cmd_str else False
        self.need_shell: bool = need_shell

        self.returncode: int = returncode
        self.run_time: int = int(run_time)
        self.stderr: str = stderr
        self.stdout: str = stdout

        # Default / unused RunCMD attributes initialized for full protocol compatibility
        self.cancelable: bool = False
        self.canceled: bool = self.returncode == 130
        self.debug: int = 0
        self.debug_limit: int = 0
        self.download: bool = False
        self.environ: dict[str, str] = {}
        self.initial_need_root: bool = self.need_root
        self.interactive: bool = False
        self.permissions_checked: bool = True
        self.shell_cmd: str | None = None
        self.spinner: bool = False
        self.timeout: int = 0
        self.to_input: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SimRunCMD:
        """Constructs a SimRunCMD instance from an attributes dictionary."""
        return cls(
            stdout=data.get('stdout', ''),
            stderr=data.get('stderr', ''),
            returncode=data.get('returncode', 0),
            cmd_args=data.get('cmd_args'),
            cmd_str=data.get('cmd_str', ''),
            need_root=data.get('need_root'),
            need_shell=data.get('need_shell'),
            run_time=data.get('run_time', 0),
        )

    @classmethod
    def load(cls, folder_path: str | Path) -> SimRunCMD:
        """
        Loads a SimRunCMD instance from a saved fixture dir containing attributes.json, stdout.txt, and stderr.txt.
        """
        folder = Path(folder_path)
        attr_file = folder / 'attributes.json'
        if not attr_file.is_file():
            raise FileNotFoundError(f"Fixture attributes file not found: {attr_file}")

        with open(attr_file, 'r', encoding='utf-8') as f:
            data = json.load(f)

        stdout_file = folder / 'stdout.txt'
        if stdout_file.is_file():
            with open(stdout_file, 'r', encoding='utf-8') as f:
                data['stdout'] = f.read()

        stderr_file = folder / 'stderr.txt'
        if stderr_file.is_file():
            with open(stderr_file, 'r', encoding='utf-8') as f:
                data['stderr'] = f.read()

        return cls.from_dict(data)

    @classmethod
    def record(
        cls,
        cmd: str | list[str],
        base_dir: str | Path | None = None,
        *,
        timeout: int | float = DEFAULT_TIMEOUT,
    ) -> SimRunCMD:
        """
        Executes cmd through RunCMD, measures runtime, and saves outputs to a timestamped folder.

        Folder format: YYYYMMDD-HHMM_<command_slug>
        Pre-checks and creates the directory before executing.  Raises FileExistsError
        or OSError if the folder cannot be created.
        """
        if isinstance(cmd, str):
            cmd_str = cmd
            try:
                tokens = shlex.split(cmd_str)
            except ValueError:
                tokens = cmd_str.split()
        else:
            tokens = list(cmd)
            cmd_str = ' '.join(tokens)

        if base_dir is None:
            base_dir = DEFAULT_FIXTURES_DIR

        slug = _make_command_slug(cmd_str)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M")
        folder_name = f"{timestamp}_{slug}"
        folder_path = Path(base_dir) / folder_name

        # Pre-check and create directory atomically before running command
        folder_path.mkdir(parents=True, exist_ok=False)

        need_root = bool(tokens and 'sudo' in tokens[0])
        need_shell = bool(re.search(r"[|><;&*~$]", cmd_str))

        if need_shell:
            # When shell is needed, put arguments all together in a single string for RunCMD
            run_cmd_args = [' '.join(tokens)]
        else:
            run_cmd_args = tokens[:]
            if need_root and run_cmd_args and run_cmd_args[0] == 'sudo':
                run_cmd_args = run_cmd_args[1:]

        start_time = time.monotonic()
        cmd_obj = RunCMD(
            cmd_args=run_cmd_args,
            timeout=timeout,
            need_root=need_root,
            need_shell=need_shell,
        )
        elapsed = time.monotonic() - start_time
        run_time = int(elapsed)

        sim = cls(
            stdout=cmd_obj.stdout or '',
            stderr=cmd_obj.stderr or '',
            returncode=cmd_obj.returncode,
            cmd_args=tokens,
            cmd_str=cmd_str,
            need_root=need_root,
            need_shell=need_shell,
            run_time=run_time,
        )
        sim.save(folder_path=folder_path)
        return sim

    def save(self, folder_path: str | Path) -> Path:
        """
        Saves stdout.txt, stderr.txt, and attributes.json into the target folder.
        """
        target = Path(folder_path)
        target.mkdir(parents=True, exist_ok=True)
        attributes = self.to_dict()
        attributes.update({'stderr': 'See stderr.txt', 'stdout': 'See stdout.txt'})

        with open(target / 'attributes.json', 'w', encoding='utf-8') as f:
            json.dump(attributes, f, indent=2)
        with open(target / 'repr_stderr.txt', 'w', encoding='utf-8') as f:
            f.write(repr(self.stderr))
        with open(target / 'repr_stdout.txt', 'w', encoding='utf-8') as f:
            f.write(repr(self.stdout))
        with open(target / 'stderr.txt', 'w', encoding='utf-8') as f:
            f.write(self.stderr)
        with open(target / 'stdout.txt', 'w', encoding='utf-8') as f:
            f.write(self.stdout)
        return target

    def to_dict(self) -> dict[str, Any]:
        """Serializes instance attributes to a dictionary matching attributes.json schema."""
        return {
            'cmd_args':   self.cmd_args,
            'cmd_str':    self.cmd_str,
            'need_root':  self.need_root,
            'need_shell': self.need_shell,
            'returncode': self.returncode,
            'run_time':   self.run_time,
            'stderr':     self.stderr,
            'stdout':     self.stdout,
        }


def main(argv: list[str] | None = None) -> int:
    """
    CLI entrypoint for recording terminal commands.
    """
    if argv is None:
        argv = sys.argv[1:]

    if not argv:
        print("Usage: ./sim_run_cmd.py <command> [args...]")
        return 1

    sim = SimRunCMD.record(argv)
    return sim.returncode


if __name__ == '__main__':
    sys.exit(main())
