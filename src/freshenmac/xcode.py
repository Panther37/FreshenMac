from __future__ import annotations

import os
import re
import shutil
from functools import cached_property
from pathlib import Path

from freshenmac.util import RunCMD, is_interactive

FILE_BUILD = 20261005


class Xcode:
    """
    Manages Xcode installation checks, developer tools inspection, and
    license agreement acceptance.
    """

    def __init__(self, *, debug: int = 0, debug_limit: int = 0, dry_run: bool = False):
        self.debug = debug
        self.debug_limit = debug_limit
        self.debug_vars = {'debug': debug, 'debug_limit': debug_limit}
        self.dry_run = dry_run

    def accept_license(self) -> bool:
        """
        Attempts to accept the Xcode license via interactive sudo prompt or cached sudo.
        :return: True if license was accepted, False otherwise.
        """
        if not self.is_installed or not self.xcodebuild_path:
            return True

        print("\n[Xcode License Required] The Xcode license agreement has not been accepted.")
        if self.dry_run:
            print(f"[Dry-run] Would accept Xcode license agreement via 'sudo {self.xcodebuild_path} -license accept'.")
            return False

        xcode_license_cmd = [str(self.xcodebuild_path), '-license', 'accept']
        if is_interactive():
            print("CMake and Darwin SDK builds will fail until the Xcode license is accepted.")
            print("Prompting for administrator privileges to accept the license...")
            license_cmd = RunCMD(
                xcode_license_cmd,
                120,
                interactive=True,
                need_root=True,
                **self.debug_vars,
            )
            if license_cmd:
                print("Xcode license agreement accepted successfully.")
                return True
        else:
            license_cmd = RunCMD(
                xcode_license_cmd,
                15,
                need_root=True,
                **self.debug_vars,
            )
            if license_cmd:
                print("Xcode license agreement accepted successfully via sudo.")
                return True

        print(
            f'''
[WARNING] Xcode license agreement has not been accepted.
Builds requiring macOS SDKs (e.g. formulae with 'cmake') will fail.
To accept manually, run in Terminal:
\tsudo {self.xcodebuild_path} -license accept
''',
        )
        return False

    def check_and_fix_license(self) -> bool:
        """
        Verifies if the Xcode license is accepted.  If not, attempts to accept it.
        Also triggers first launch component installation when license is accepted.
        Returns True if license is accepted (or Xcode not installed), False otherwise.
        """
        if not self.is_installed and not self.is_clt_installed:
            return self.check_and_install_clt()

        if not self.is_installed:
            return True

        accepted = self.is_license_accepted or self.accept_license()
        if accepted:
            self.run_first_launch()
        return accepted

    def check_and_install_clt(self) -> bool:
        """
        Checks if Command Line Tools (or full Xcode) are installed.
        If missing:
        - In interactive mode: Prompts user and triggers 'xcode-select --install'.
        - In headless mode: Silently installs via softwareupdate if root/sudo is available.
        :return: True if tools are installed or successfully initiated, False otherwise.
        """
        if self.is_clt_installed or self.is_installed:
            return True

        print("\n[Xcode Command Line Tools Required] Command Line Tools are not installed.")
        if self.dry_run:
            print("[Dry-run] Would trigger Command Line Tools installation.")
            return False

        if is_interactive():
            return self.install_clt_interactive()

        if RunCMD.can_sudo() or os.geteuid() == 0:
            print("[Xcode CLT] Running in unattended mode with administrator access. Attempting silent install...")
            return self.install_clt_headless()

        print(
            '''
[ERROR] Xcode Command Line Tools are not installed and unattended credentials are unavailable.
To install manually, run in Terminal:
\txcode-select --install
''',
        )
        return False

    def install_clt_headless(self) -> bool:
        """
        Installs Command Line Tools headlessly via softwareupdate using the in-progress flag.
        Requires root/sudo privileges.
        :return: True if installation succeeded, False otherwise.
        """
        flag_file = Path('/tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress')
        try:
            flag_file.touch()
            print("[Xcode CLT] Searching for Command Line Tools package via softwareupdate...")
            sw_list = RunCMD(['softwareupdate', '-l'], timeout=180, **self.debug_vars)

            clt_label = None
            if sw_list and sw_list.std_all:
                for line in sw_list.splitlines():
                    if 'command line tools' in line.lower():
                        match = re.search(r"\*\s*(?:Label:\s*)?([^\n\r*]+Command Line Tools[^\n\r*]*)", line, re.IGNORECASE)
                        if match:
                            clt_label = match.group(1).strip()
                            break

                if not clt_label:
                    match = re.search(r"\*\s*(?:Label:\s*)?([^\n\r*]+Command Line Tools[^\n\r*]*)", sw_list.std_all, re.IGNORECASE)
                    if match:
                        clt_label = match.group(1).strip()

            if not clt_label:
                print("[Xcode CLT] No Command Line Tools package found in softwareupdate list.")
                return False

            print(f"[Xcode CLT] Installing '{clt_label}' silently via softwareupdate...")
            install_cmd = RunCMD(
                ['softwareupdate', '-i', clt_label, '--verbose'],
                timeout=1800,
                need_root=True,
                **self.debug_vars,
            )
            if install_cmd:
                clt_dir = Path('/Library/Developer/CommandLineTools')
                if clt_dir.is_dir():
                    RunCMD(['xcode-select', '-s', str(clt_dir)], need_root=True, **self.debug_vars)
                print("[Xcode CLT] Command Line Tools installed successfully.")
                return True

            print(f"[Xcode CLT] Failed to install {clt_label}: {install_cmd.stderr}")
            return False

        finally:
            if flag_file.is_file():
                try:
                    flag_file.unlink()
                except OSError:
                    pass

    def install_clt_interactive(self) -> bool:
        """
        Triggers the macOS native GUI installer dialog for Command Line Tools via 'xcode-select --install'.
        :return: True if dialog was triggered or already active, False otherwise.
        """
        print("Homebrew and source builds (e.g. formulae requiring 'cmake') require a compiler.")
        print("Triggering macOS developer tools installer dialog ('xcode-select --install')...")
        cmd = RunCMD(['xcode-select', '--install'], **self.debug_vars)
        if cmd:
            print("Please complete the macOS installation dialog on your screen to proceed.")
            return True

        if 'already installed' in cmd.std_all.lower():
            clt_dir = Path('/Library/Developer/CommandLineTools')
            if clt_dir.is_dir():
                RunCMD(['xcode-select', '--reset'], **self.debug_vars)
                return True

        return False

    @property
    def is_clt_installed(self) -> bool:
        """
        Checks if Xcode Command Line Tools are installed and active.
        Verifies active developer directory via 'xcode-select -p' or standard location.
        """
        cmd = RunCMD(['xcode-select', '-p'], **self.debug_vars)
        if cmd and cmd.stdout.strip():
            dev_path = Path(cmd.stdout.strip())
            if dev_path.is_dir() and (dev_path / 'usr/bin/clang').is_file():
                return True

        return Path('/Library/Developer/CommandLineTools/usr/bin/clang').is_file()

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

        license_cmd = RunCMD([str(self.xcodebuild_path), '-license', 'status'], **self.debug_vars)
        if 'is a command line tools instance' in license_cmd.std_all.lower():
            return True

        return bool(license_cmd)

    def run_first_launch(self) -> bool:
        """Installs required additional Xcode packages silently via root to avoid post-reboot GUI prompts."""
        if not self.is_installed or not self.xcodebuild_path or self.dry_run:
            return True
        cmd = RunCMD([str(self.xcodebuild_path), '-runFirstLaunch'], need_root=True, timeout=120, **self.debug_vars)
        return bool(cmd)

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
