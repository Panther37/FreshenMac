"""
Autonomous failure diagnosis and self-healing recovery engine.
Detects known failure signatures across Homebrew, Mac App Store, and macOS updates,
executes safe targeted remediations, and re-attempts operations.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from freshenmac.util import Logger, RunCMD, Wave

DEFAULT_BACKOFF: int = 10
FILE_BUILD = 20261109
MAX_RECOVERY_ATTEMPTS: int = 1
PATTERN: dict[str, re.Pattern[str]] = {
    'Brew Link Conflict':   re.compile(
        r"The `brew link` step did not complete successfully|"
        r"files already exist in the target directory|"
        r"run `brew link --overwrite`",
        re.IGNORECASE,
    ),
    'Brew Python Mismatch': re.compile(
        r"Could not provide build dependency '(?P<pkg1>[a-zA-Z0-9_\-]+)(?:==|>=|<=)?[^']*'|"
        r"'?(?P<pkg2>[a-zA-Z0-9_\-]+)'? was detected on your system [^,]+, "
        r"but the Python package '[^']+' was not found by this Python interpreter|"
        r"Python package '(?P<pkg3>[a-zA-Z0-9_\-]+)' was not found nor installed",
        re.IGNORECASE,
    ),
    'Brew Rebase Lock':     re.compile(
        r"another git process is running|"
        r"git-rebase-todo|"
        r"rebase-merge|"
        r"fatal: Unable to create .*index\.lock",
        re.IGNORECASE,
    ),
    'Transient Network':    re.compile(
        r"curl: \((?:35|56)\)|"
        r"LibreSSL SSL_read|"
        r"502 Bad Gateway|"
        r"503 Service Unavailable|"
        r"GitHub API rate limit exceeded",
        re.IGNORECASE,
    ),
    'Xcode License':        re.compile(
        r"agree to the terms of the Xcode(?:/iOS)? license|"
        r"xcodebuild\s+-license",
        re.IGNORECASE,
    ),
}


@dataclass(frozen=True)
class FailureInfo:
    """
    Immutable context describing a command failure.

    Captures diagnostic telemetry from a failed subprocess or API operation
    and presents unified output to diagnostic rules.
    """

    cmd_args: tuple[str, ...] = ()  # RunCMD.cmd_args
    log_file: Path = Path()  # .      Log path (e.g. ~/Library/Logs/Homebrew/pkg/01.configure.log)
    package: str = ''  # .            Target formula, cask, or app name (e.g. 'qemu')
    returncode: int = 0  # .          RunCMD.returncode
    stderr: str = ''  # .             RunCMD.stderr
    stdout: str = ''  # .             RunCMD.stdout
    subsystem: str = ''  # .          Originating module/subsystem (e.g. 'Homebrew', 'MAS', 'SoftwareUpdate')

    def combined_output(self) -> str:
        """Combines stdout, stderr, and the trailing lines of log_file into a single string."""
        parts: list[str] = []
        if self.stdout:
            parts.append(str(self.stdout))
        if self.stderr:
            parts.append(str(self.stderr))
        if self.log_file.is_file():
            try:
                content = self.log_file.read_text(encoding='utf-8', errors='replace')
                lines = content.splitlines()
                tail = lines[-100:] if len(lines) > 100 else lines
                parts.append('\n'.join(tail))
            except OSError:
                parts.append('')

        return '\n'.join(parts)

    @classmethod
    def from_runcmd(
        cls,
        cmd: RunCMD,
        *,
        log_file: Path = Path(),
        package: str = '',
        subsystem: str = '',
    ) -> FailureInfo:
        """Constructs an immutable FailureInfo from an executed RunCMD instance."""
        return cls(
            cmd_args=tuple(cmd.cmd_args),
            log_file=log_file,
            package=package,
            returncode=cmd.returncode,
            stderr=cmd.stderr,
            stdout=cmd.stdout,
            subsystem=subsystem,
        )


class RecoveryEngine:
    """Coordinates diagnostic rules and executes bounded self-healing remediations."""

    def __init__(
        self,
        *,
        debug_vars: dict[str, int] | None = None,
        dry_run: bool = False,
        logger: Any | None = None,
        target: Any | None = None,
    ) -> None:
        self.debug_vars = debug_vars or {'debug': 0, 'debug_limit': 0}
        self.dry_run = dry_run
        self.healed_packages: set[str] = set()
        self.logger = logger or Logger(dry_run=self.dry_run)
        self.rules: dict[str, tuple[Any, Callable[[FailureInfo], Any]]] = {}
        if target is not None:
            self.add_rules(target)

    def add_rules(self, some_rules: dict[str, tuple[Any, Callable[[FailureInfo], Any]]] | Any) -> None:
        """Registers diagnostic recovery rules mapping rule names to (matcher, action) tuples."""
        if isinstance(some_rules, dict):
            self.rules.update(some_rules)
            return

        rules_dict = {}
        target_name = (
            some_rules.__name__
            if isinstance(some_rules, type)
            else some_rules.__class__.__name__
        )
        rule_name = f"rules_{target_name.lower()}"
        if rule_name in globals():
            if callable(provider := globals()[rule_name]):
                rules_dict = provider(some_rules)
        elif hasattr(some_rules, 'fix_rules'):
            rules_attr = some_rules.fix_rules
            rules_dict = rules_attr() if isinstance(rules_attr, Callable) else rules_attr
        else:
            raise AttributeError(
                f"[RecoveryEngine.add_rules] Could not find any rules for '{target_name}'.  "
                f"Expected {target_name}.fix_rules or 'fix.{rule_name}'.",
            )

        if not isinstance(rules_dict, dict):
            raise TypeError(
                f"[RecoveryEngine.add_rules] Rules for '{target_name}' are incorrect.  "
                f"Expected {type(dict())} but got {type(rules_dict)}.  Value: {rules_dict}\n",
            )

        self.rules.update(rules_dict)

    def diagnose(self, some_info: FailureInfo) -> tuple[str, Callable[[FailureInfo], Any]] | None:
        """Evaluates all registered rules against the failure context."""
        output = some_info.combined_output()
        for name, (matcher, action) in self.rules.items():
            matched = bool(matcher.search(output)) if isinstance(matcher, re.Pattern) else bool(matcher(some_info))
            if matched:
                return name, action

        return None

    def log(self, message: str) -> None:
        """Logs an operational message through the active logger."""
        if hasattr(self.logger, 'store'):
            self.logger.store(message, 'Self-Heal')
        elif hasattr(self.logger, 'log_freshenmac'):
            self.logger.log_freshenmac(message)
        else:
            print(message)

    def recover(self, some_info: FailureInfo, retry_callback: Callable[[], Any]) -> Any | None:
        """Diagnoses failure, applies targeted remediation, and re-attempts the operation."""
        if not some_info.package or some_info.package in self.healed_packages:
            return None

        if not (diagnosed := self.diagnose(some_info)):
            return None

        name, action = diagnosed
        self.log(f"[Self-Heal] Diagnosed '{name}' for package '{some_info.package}'.  Applying remediation...")
        if not action(some_info):
            self.log(f"[Self-Heal Failed] Remediation '{name}' failed for '{some_info.package}'")
            return None

        self.healed_packages.add(some_info.package)
        self.log(f"[Self-Healed] Remediation '{name}' succeeded for '{some_info.package}'.  Retrying operation...")
        return retry_callback()


'''Functions'''


def extract_dependency(text: str) -> str:
    """Extracts the missing or desynchronized build tool package name from error text."""
    if match := PATTERN['Brew Python Mismatch'].search(text):
        pkg = match.group('pkg1') or match.group('pkg2') or match.group('pkg3')
        return pkg.strip() if pkg else ''

    return ''


def rules_homebrew(brew: Any) -> dict[str, tuple[Any, Callable[[FailureInfo], Any]]]:
    """Returns the standard diagnostic rules dictionary for Homebrew operations."""
    return {
        'BrewGitRebase':      (
            PATTERN['Brew Rebase Lock'],
            lambda info: brew.recover_rebase(),
        ),
        'BrewLinkConflict':   (
            PATTERN['Brew Link Conflict'],
            lambda info: bool(
                RunCMD([str(brew.brew_path), 'link', '--overwrite', info.package], **brew.cmd_options()),
            ),
        ),
        'BrewPythonMismatch': (
            PATTERN['Brew Python Mismatch'],
            lambda info: brew.reinstall_dependency(extract_dependency(info.combined_output())),
        ),
        'XcodeLicense':       (
            lambda info: brew.xcode.is_license_error(info.combined_output()),
            lambda info: brew.xcode.check_and_fix_license(),
        ),
        'TransientNetwork':   (
            PATTERN['Transient Network'],
            lambda info: wave_timer('Waiting for network issues', DEFAULT_BACKOFF),
        ),
    }


def wave_timer(message: str, delay: int) -> bool:
    with Wave(message, force=True, persist=True):
        time.sleep(delay)

    return True
