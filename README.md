# FreshenMac

**Automated macOS System, App Store, and Homebrew Maintenance Utility**

FreshenMac is a unified maintenance and update automation tool for macOS. It orchestrates updates across Homebrew (formulae and casks), the Mac App Store, and macOS system software updates, pairing them with an intelligent reboot escalation sequence and `launchd` automation.

---

## Table of Contents

- [Overview](#overview)
- [Key Features](#key-features)
- [Architecture & Modules](#architecture--modules)
- [Installation & Setup](#installation--setup)
- [Command-Line Usage](#command-line-usage)
  - [Options Reference](#options-reference)
  - [Examples](#examples)
- [Configuration & Preferences](#configuration--preferences)
  - [Scheduling (Cron Syntax)](#scheduling-cron-syntax)
  - [Ignoring Mac App Store Apps](#ignoring-mac-app-store-apps)
- [Subsystems Deep Dive](#subsystems-deep-dive)
  - [Homebrew Maintenance](#homebrew-maintenance)
  - [macOS Software Updates & Apple Silicon](#macos-software-updates--apple-silicon)
  - [Reboot Escalation & FileVault AuthRestart](#reboot-escalation--filevault-authrestart)
  - [LaunchAgent & Package Synchronization](#launchagent--package-synchronization)
- [Development & Testing](#development--testing)
  - [Coding Style & Ordering Invariants](#coding-style--ordering-invariants)
  - [Running Tests](#running-tests)

---

## Overview

Keeping a Mac fully updated unattended requires coordinating multiple package managers, privilege levels, user sessions, and hardware constraints. FreshenMac solves this by:

1. **Unifying Update Layers:** Sequentially updates Homebrew, Mac App Store apps (`mas`), and macOS system software.
2. **Handling Privilege Escalation:** Manages background `sudo` timestamp refresh (`SudoKeepAlive`) and bypasses password re-prompts safely.
3. **Smart Rebooting:** Detects when restarts are required, respects user activity and system idle time, and supports FileVault authenticated restarts (`fdesetup authrestart`).
4. **Self-Deploying Automation:** Synchronizes its own package code to `/Library/scripts/User/freshenmac` and schedules recurring execution via `launchd`.

---

## Key Features

- **Homebrew Orchestration:**
  - Updates and upgrades both formulae and GUI casks.
  - Automatically identifies packages requiring `cmake` or build dependencies.
  - Reinstalls unpinned or broken packages and runs cleanups.
  - Non-interactive execution (`HOMEBREW_NO_ASK=1`, `HOMEBREW_NO_ENV_HINTS=1`).

- **Mac App Store (`mas`):**
  - Detects installed GUI applications currently running to avoid abruptly killing active apps.
  - Configurable blacklist (`Ignore MAS`) to skip specific application IDs or names.

- **macOS System Software Updates:**
  - Two-stage execution: downloads updates first, then stages installation.
  - Apple Silicon Volume Owner handling: automatically detects manual authentication requirements, alerts the user, and opens System Settings directly to the Software Update pane.

- **Intelligent Reboot Engine:**
  - Evaluates system uptime (default threshold: 15 days) and update restart requirements.
  - Escalation ladder: interactive prompt $\rightarrow$ snooze support $\rightarrow$ system idle detection ($\ge 60$ minutes) $\rightarrow$ authenticated restart (`authrestart`) $\rightarrow$ clean Time Machine backup check $\rightarrow$ graceful shutdown.

- **Native Terminal Experience:**
  - Undulating wave spinner (`util.Wave`) for long-running CLI operations.
  - Robust logging to `/Library/Logs/FreshenMac.log` or fallback to user logs.

---

## Architecture & Modules

The codebase is organized under `src/freshenmac/`:

| Module | Primary Classes / Components | Description |
| :--- | :--- | :--- |
| [`FreshenMac.py`](file:///Users/matt/workspace/FreshenMac/src/FreshenMac.py) | Entrypoint script | Top-level CLI executable that invokes `main.main()`. |
| [`config.py`](file:///Users/matt/workspace/FreshenMac/src/freshenmac/config.py) | Configuration constants | Metadata (`APP_INFO`), filesystem paths (`PATHS`), timeouts, and idle thresholds. |
| [`main.py`](file:///Users/matt/workspace/FreshenMac/src/freshenmac/main.py) | `main()`, `perform_updates()` | CLI argument parsing, interactive `sudo` priming, and execution orchestration. |
| [`computer.py`](file:///Users/matt/workspace/FreshenMac/src/freshenmac/computer.py) | `MacOS` | High-level macOS interface: system idle calculation, hardware detection, MAS execution, OS updates, and summary generation. |
| [`homebrew.py`](file:///Users/matt/workspace/FreshenMac/src/freshenmac/homebrew.py) | `HomeBrew` | Formula and cask update lifecycle, bottle/source detection, and dependency inspection. |
| [`boot.py`](file:///Users/matt/workspace/FreshenMac/src/freshenmac/boot.py) | `Reboot`, `RebootState` | Reboot escalation sequence, snooze management, FileVault `authrestart`, and caffeinate wrapper. |
| [`plist.py`](file:///Users/matt/workspace/FreshenMac/src/freshenmac/plist.py) | `LaunchAgent`, `PackageSync`, `SavePreferences`, `StartupRun` | `launchd` plist generation, cron expression parsing, preferences management, and one-shot startup re-runs. |
| [`util.py`](file:///Users/matt/workspace/FreshenMac/src/freshenmac/util.py) | `RunCMD`, `Logger`, `Wave`, `SudoKeepAlive`, `Version`, `PlaySound` | Subprocess execution, logging, natural version comparison, terminal wave spinner, and background sudo daemon. |
| [`xcode.py`](file:///Users/matt/workspace/FreshenMac/src/freshenmac/xcode.py) | `Xcode` | Developer tools / Xcode app bundle inspection and automated license agreement acceptance. |

---

## Installation & Setup

### Prerequisites
- macOS 12+ (Apple Silicon or Intel x86_64)
- Python 3.10+
- [Homebrew](https://brew.sh) (optional, but recommended)
- [`mas-cli`](https://github.com/mas-cli/mas) (for App Store automation)

### Running Directly
```bash
# Run from repository root
PYTHONPATH=src python3 src/FreshenMac.py
```

---

## Command-Line Usage

```text
usage: FreshenMac.py [-h] [-f] [-i APP [APP ...]] [-l DEBUG_LIMIT] [-r]
                     [-s [PREF]] [--spinner] [--schedule CRON] [-u | --no-update]
                     [--uninstall] [-v]
```

### Options Reference

| Flag | Long Option | Description |
| :--- | :--- | :--- |
| `-v` | `--verbose` | Increase output verbosity (`-v`, `-vv`, `-vvv`). |
| `-l` | `--debug-limit` | Maximum character length for debug command dumps (default: `10000`). |
| `-f` | `--force-reboot` | Force a system reboot at the end of updates, bypassing uptime and requirement checks. |
| `-r` | `--no-reboot` | Disable all reboot attempts regardless of update requirements. |
| `-i` | `--ignore-mas` | Space-separated list of Mac App Store app IDs or names to skip during updates. |
| `-s` | `--save-prefs` | Save a preference (e.g. `-s "Schedule=0 6 * * 1"`). Run with no argument to view options. |
| `--schedule` | `--cron` | Schedule execution using standard 5-part cron syntax (e.g. `'0 4 * * 1'`). |
| `-u` | `--update` | Automatically sync updated package files to `/Library/scripts/User/freshenmac`. |
| `--uninstall`| | Unloads and removes the background `launchd` service. |
| `--spinner` | | Test and visually inspect the terminal wave animation. |

### Examples

**Standard full update (Homebrew, MAS, and macOS):**
```bash
./src/FreshenMac.py
```

**Run updates without rebooting:**
```bash
./src/FreshenMac.py --no-reboot
```

**Set up weekly automated runs every Monday at 4:00 AM:**
```bash
./src/FreshenMac.py --schedule "0 4 * * 1"
```

**Verbose debug run skipping a specific App Store app:**
```bash
./src/FreshenMac.py -vv -i "GarageBand"
```

---

## Configuration & Preferences

Preferences are stored in:
- System default: `/Library/Preferences/com.panther37.FreshenMac.plist`
- User overrides: `~/Library/Preferences/com.panther37.FreshenMac.plist`

### Scheduling (Cron Syntax)
FreshenMac converts 5-field cron syntax into native `launchd` `StartCalendarInterval` dictionary structures:

```bash
# Run daily at 6:30 AM
./src/FreshenMac.py --schedule "30 6 * * *"

# Run on the 1st and 15th of every month at midnight
./src/FreshenMac.py --schedule "0 0 1,15 * *"
```

### Ignoring Mac App Store Apps
Apps can be permanently ignored in preferences:
```bash
./src/FreshenMac.py -s "Ignore MAS=1447605203, Xcode"
```

---

## Subsystems Deep Dive

### Homebrew Maintenance
[`freshenmac.homebrew.HomeBrew`](file:///Users/matt/workspace/FreshenMac/src/freshenmac/homebrew.py#L33) ensures system packages stay clean:
- Pre-checks `xcodebuild -license` so formulae requiring build tools do not fail mid-installation.
- Sorts formulae and identifies build dependencies (e.g. `cmake`).
- Upgrades outdated packages and casks.
- Captures output and generates a structured summary dictionary:
  ```python
  self.summary_results = {'updated': {...}, 'unchanged': {...}, 'installed': {...}}
  ```

### macOS Software Updates & Apple Silicon
[`freshenmac.computer.MacOS`](file:///Users/matt/workspace/FreshenMac/src/freshenmac/computer.py#L35) manages OS-level updates:
- Runs `softwareupdate --list` to locate eligible update labels.
- Downloads updates first via `softwareupdate -d <label>`.
- Attempts silent installation with elevated credentials.
- On Apple Silicon, if manual Volume Owner authentication is required:
  1. Displays an alert banner in the terminal log.
  2. Launches an alert dialog via AppleScript.
  3. Immediately opens `System Settings > General > Software Update`.

### Reboot Escalation & FileVault AuthRestart
[`freshenmac.boot.Reboot`](file:///Users/matt/workspace/FreshenMac/src/freshenmac/boot.py#L20) coordinates restarts:
1. Wraps operations in `caffeinate` to prevent machine sleep during execution.
2. Plays a sound chime (`Ping`, `Sosumi`) and displays an interactive dialog with uptime statistics.
3. If FileVault is enabled, attempts `fdesetup authrestart` using encrypted memory keys to bypass the pre-boot login screen directly into macOS to apply updates.
4. If snoozed, monitors system idle time (`all_user_idle_time`). If the computer is left idle for $\ge 60$ minutes, it proceeds automatically.

### LaunchAgent & Package Synchronization
[`freshenmac.plist.LaunchAgent`](file:///Users/matt/workspace/FreshenMac/src/freshenmac/plist.py#L20) and [`PackageSync`](file:///Users/matt/workspace/FreshenMac/src/freshenmac/plist.py#L461):
- Keeps running script builds synchronized with `/Library/scripts/User/freshenmac/`.
- Inspects `FILE_BUILD` integers across source and destination files.
- Generates launchd plists with standard logging redirected to `/Library/Logs/com.panther37.FreshenMac.stdout.log` and `.stderr.log`.

---

## Development & Testing

### Coding Style & Ordering Invariants
The codebase adheres to strict ordering rules enforced by [`test_architecture.py`](file:///Users/matt/workspace/FreshenMac/test/test_architecture.py) and [`.amazonq/rules/style.md`](file:///Users/matt/workspace/FreshenMac/.amazonq/rules/style.md):
1. **Keyword arguments in signatures:** Alphabetical order.
2. **Instance variable assignments in `__init__`:** Alphabetical order.
3. **Class methods:** Alphabetical order (`__init__` first, then `_private`, then `public`).
4. **Module constants:** Alphabetical order.

### Running Tests
Execute the entire test suite using `unittest`:

```bash
PYTHONPATH=src python3 -m unittest discover -s test
```

To run individual test modules:
```bash
# Architecture & ordering verification
python3 -m unittest test/test_architecture.py

# Computer & OS update test suite
PYTHONPATH=src python3 -m unittest test/test_mac_computer.py

# Homebrew test suite
PYTHONPATH=src python3 -m unittest test/test_homebrew.py

# LaunchAgent and plist test suite
PYTHONPATH=src python3 -m unittest test/test_plist.py
```
