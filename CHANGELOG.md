# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Please wrap at 120 characters.

## [2.1.1] - 2026-11-10

### Fixed
- Fixed password prompting and process hangs during `HomeBrew.self_update_freshenmac()` by adding `validate=True`
  to `RunCMD.can_sudo()` (testing `sudo -n -v`) in `src/freshenmac/util.py` to ensure interactive sessions validate
  and prime sudo credentials when general cached root access is not active, even when `/usr/bin/true` is passwordless
  in `/etc/sudoers.d/freshenmac` (resolves [#27]).
- Suppressed environment hints during cask self-updates by forwarding `HOMEBREW_NO_ENV_HINTS=1` in
  `self_update_freshenmac()`.
- Removed destructive `uninstall_preflight_steps` block from `panther37/homebrew-tap/Casks/freshenmac.rb` that
  erroneously executed `freshenmac --action Uninstall` with `sudo: true` during `brew upgrade --cask freshenmac`.

### Changed
- Cleaned up redundant tests across `test/test_run_cmd.py`, `test/test_spinner.py`, and `test/test_reboot_state.py`:
  removed duplicate `TestSudoKeepAlive.test_context_manager` (covered by `TestRunner.test_context_manager`), removed
  duplicate `TestRunAppleScript.test_debug_limit_default_and_custom` (covered by `TestRunCMD`), removed
  duplicate `euid == 0` check from `test_can_sudo_validate`, consolidated `spinner=False` timeout checks
  in `test/test_spinner.py`, and streamlined truthiness and initialization tests in `test/test_reboot_state.py`.

## [2.1.0] - 2026-11-09

### Added
- Added `cwd` parameter to `RunCMD` and `SimRunCMD` in `src/freshenmac/util.py` and `test/sim_run_cmd.py`, forwarding
  explicit working directory settings directly to `subprocess.run()`.
- Added working directory isolation to `PrivilegedCMD.run_privileged` in `src/freshenmac/util.py`, defaulting `cwd`
  to `/` so privileged system commands do not inherit invalid, deleted, or unmounted caller directories and avoid
  spurious `shell-init: getcwd` warnings.
- Added unit tests in `test/test_run_cmd.py` verifying `RunCMD` working directory inheritance and `PrivilegedCMD`
  root path isolation.
- Added autonomous diagnostic and self-healing recovery engine in `src/freshenmac/fix.py` with `RecoveryEngine`
  (supporting dynamic subsystem rule resolution via `target=self`), concrete `FailureInfo` (featuring
  `from_runcmd()`), and `fix.rules_homebrew()` providing lightweight dictionary-based diagnostic rules for Python
  interpreter mismatches (`BrewPythonMismatch`), unaccepted Xcode licenses (`XcodeLicense`), stranded git tap locks
  (`BrewGitRebase`), unlinked keg conflicts (`BrewLinkConflict`), and transient network dropouts (`TransientNetwork`,
  [#26], resolves [#25]).
- Added `HomeBrew.reinstall_dependency()` in `src/freshenmac/homebrew.py` to automatically reinstall and repair
  broken build tools (such as `meson` or `ninja`) when runtime interpreter mismatches occur during source builds
  ([#26], resolves [#25]).
- Added `Xcode.is_license_error()` and unified Darwin SDK build license detection in `src/freshenmac/xcode.py`,
  allowing any subsystem to detect and recover from unaccepted Xcode license agreement errors.
- Added comprehensive unit and integration tests in `test/test_fix.py`, `test/test_xcode.py`, and
  `test/test_homebrew.py` verifying failure diagnosis, automated dependency repair, bounded single-retry execution,
  and concrete type safety.
- Added real recorded CLI fixture integration tests in `test/test_fixtures.py` (including
  `test_brew_deps_qemu_fixture`, `test_brew_reinstall_meson_fixture`, `test_brew_upgrade_portable_ruby_fixture`,
  `test_brew_upgrade_qemu_python_mismatch_fixture`, `test_fdesetup_authrestart_fixture`, `test_fdesetup_status_fixture`,
  `test_mas_info_fixture`, `test_mas_outdated_fixture`, `test_sw_vers_product_version_fixture`,
  `test_xcodebuild_license_accept_fixture`, and `test_xcodebuild_license_status_fixture`) validating dependency graph
  parsing, Intel x86_64 warning filtering, bottle upgrades, FileVault status/authrestart detection, App Store
  size/outdated parsing, and Xcode license accept/status handling.

## [2.0.0] - 2026-11-03

### Added
- Added dynamic Apple Software Update download timeouts via `SoftwareUpdate.calculate_timeout()` and
  `SoftwareUpdate.get_update_size_mb()` in `src/freshenmac/softwareupdate.py` using configured `download_speed_mbps`
  and `'Software Update'` section in `config.TIMEOUT`.
- Added Homebrew Cask installation and management instructions to Quick Start in `README.md`.
- Added `HomeBrew.self_update_freshenmac()` in `src/freshenmac/homebrew.py` to check for and apply FreshenMac cask
  updates as Step 0 before performing any other software updates ([#24], resolves [#23]).
- Added `download-speed` preference key in `src/freshenmac/plist.py` (`SavePreferences.SCHEMA`) to configure minimum
  expected download speed for dynamic Mac App Store timeouts.
- Added dynamic Homebrew Cask download timeouts via `HomeBrew.calculate_cask_timeout()` and
  `HomeBrew.get_cask_size_mb()` in `src/freshenmac/homebrew.py` based on `download-speed` preferences.
- Added dynamic MAS timeouts via `AppStore.calculate_timeout()` and `AppStore.get_app_size_mb()` in
  `src/freshenmac/mas.py` using configurable download speed (default 5.0 Mbps) plus a 25% safety buffer.
- Added Reboot Windows architectural exploration and future specification in `plans/todo/reboot_windows.md`.
- Added AI context documentation in `.ai/` (`README.md`, `ARCHITECTURE.md`, `MODULES.md`, `WORKFLOWS.md`, and
  `CONVENTIONS.md`) and Homebrew Cask self-update architecture plan in `plans/self_update_homebrew_cask_plan.md`.
- Added `LaunchAgent.console_uid` and `LaunchAgent.target_script` properties in `src/freshenmac/plist.py` to resolve
  the active console user's UID under `sudo` and locate the Homebrew `freshenmac` CLI binary from
  `PATHS['Script']['Target']`.
- Added `test_jump_statements_followed_by_empty_line` (`P37-JUMP_LINE`) in `test/test_architecture.py` to enforce empty
  lines after `break`, `continue`, `pass`, `raise`, `return`, and `yield` statements, and inline comments on `pass`.
- Added `test_timeout_all_values` in `test/test_architecture.py` to verify that `TIMEOUT['All']` bounds dynamically
  match the maximum and minimum of all defined package manager timeouts in `src/freshenmac/config.py`.
- Added golden truth integration tests and recorded plist datasets in `test/test_fixtures.py`
  (`test_plist_launch_agent`, `test_plist_preferences`, `test_plist_staged_updates`, `test_plist_startup_run`).
- Added visual `--action Spinner` and `--action Wave` options in `src/freshenmac/main.py` for interactive progress
  spinner inspection.
- Added `util.get_running_gui_apps()` to query System Events once in batch for active GUI applications, with
  pre-fetched process caching in `AppStore.update()`.
- Added pending update enumeration in `Boot.prompt_initial()` to list specific staged macOS updates and Apple software
  in the user reboot confirmation dialog.
- Added reboot suggestions in `AppStore.update()` (`src/freshenmac/mas.py`) for deferred running apps and
  upgrade caveats.
- Added regression tests in `test/test_homebrew.py`, `test/test_mac_computer.py`, and `test/test_reboot.py` for
  duplicate upgrade suppression, in-place version caching, and reboot dialog update lists.

### Changed
- Updated `Reboot.prompt_initial()` in `src/freshenmac/boot.py` to default the initial reboot prompt to "Snooze" instead
  of "Restart Now" and route dialog timeouts (`gave up:true`) to the snooze flow rather than force-rebooting.
- Converted `MacOS.app_store` and `MacOS.softwareupdate` into lazy `@cached_property` properties in
  `src/freshenmac/computer.py`, passing `download_speed_mbps` to all worker instances.
- Standardized `download_speed` and `download_speed_mbps` across `MacOS`, `AppStore`, `HomeBrew`, and
  `SoftwareUpdate` to strictly typed `float = 0.0`.
- Restructured `TIMEOUT` in `src/freshenmac/config.py` into typed, nested sections (`App Store` and `Homebrew`) with
  human-readable duration comments, dedicated CMake compile timeouts (3 hours), and dynamic cask timeouts.
- Renamed CLI argument `--save-prefs` to `--save-pref` in `src/freshenmac/main.py` and `src/freshenmac/plist.py` (with
  backward-compatible `--save-prefs` CLI alias support) and documented preference options in `README.md`.
- Updated `Reboot.graceful_restart()` in `src/freshenmac/boot.py` to trigger
  `softwareupdate --install <label> --restart` with 5-minute `fdesetup authrestart` priming when macOS updates are
  staged to disk.
- Updated `LaunchAgent._install_privileged()` in `src/freshenmac/plist.py` to pre-create `/Library/Logs/FreshenMac.log`
  with ownership `root:admin` and permissions `0664`.
- Updated `HomeBrew._upgrade_casks()` to exclude `freshenmac` from general cask upgrades since it is updated prior to
  the update run.
- Pointed `LaunchAgent` directly at Homebrew's `freshenmac` executable (`PATHS['Script']['Target']`) and removed
  `PackageSync`, `check_plist()`, `file_needs_update()`, `get_file_build()`, and `/Library/scripts/User/freshenmac`
  file-copying logic from `src/freshenmac/plist.py` and `src/freshenmac/main.py`.
- Replaced `brew info --json=v2` dependency chunking in `HomeBrew._get_cmake_formulae()` with native
  `brew deps --direct --include-build --for-each` inspection parsed via `RunCMD.parse_dict()`.
- Removed redundant `brew list --cask --versions` subprocess execution in `HomeBrew.snapshot_installed()` and warmed
  `self.brew_list` during initial snapshot to eliminate duplicate `brew list --versions` calls.
- Updated `HomeBrew._resolve_and_update_dependencies()` to maintain `self.brew_list` in place rather than
  invalidating the cached property.
- Streamlined `HomeBrew.self_update_freshenmac()` to inspect the consolidated `brew outdated --json=v2` results,
  eliminating redundant subprocess invocations and stale tap metadata races.
- Refactored pending reboot update gathering in `Reboot._get_pending_updates()` (`src/freshenmac/boot.py`), replacing
  nested `hasattr` checks with direct attribute lookups and introducing clear deduplication logic.
- Standardized `AppStore.get_app_size_mb()` (`src/freshenmac/mas.py`) and `HomeBrew.get_cask_size_mb()`
  (`src/freshenmac/homebrew.py`) to return `float` with `0.0` for unavailable sizes, checking `size_mb == 0.0`.
- Removed `None` returns in favor of typed empty sentinels across multiple core properties and helper methods:
  `MacOS.pending_os_update` (`str`, returns `""`), `Xcode.xcodebuild_path` (`Path`, returns `Path('')`),
  `SavePreferences._parse()` and `SavePreferences._normalize()` (`dict`, returns `{}`), and
  `LaunchAgent._prepare_sudoers_commands()` (`tuple[list[str], Path]`, returns `([], Path(''))`).
- Updated `HomeBrew.update()`, `AppStore.update()`, `SoftwareUpdate.update()`, and `MacOS.update()` to return `bool`
  indicating whether updates were performed, eliminating stateful attribute inspections in `MacOS._update_brew()`,
  `MacOS._update_mas()`, and `MacOS._update_os()`.
- Standardized `ignore_mas` to `set[str]` across `MacOS`, `AppStore`, and CLI rule parsing in `main.py`, eliminating
  duplicate app names and numeric IDs.
- Bumped FreshenMac version to 2.0.0 in `src/freshenmac/config.py` (`APP_INFO['Version']`).
- Renamed `SudoKeepAlive` parameter `interval` to `interval_seconds` in `src/freshenmac/util.py`.
- Refactored `CronSchedule` integer parsing into `_parse_part()` in `src/freshenmac/plist.py` with Sunday day
  normalization (7 to 0).
- Optimized `SimRunCMD.save_folder()` in `test/sim_run_cmd.py` to inline `stdout` and `stderr` into `attributes.json`
  for outputs under 80 characters.
- Refactored `TestLogging` in `test/test_run_cmd.py` to mock directory creation and file writes in-memory, eliminating
  disk I/O and temporary directory overhead during test execution.

### Removed
- Removed dictionary subscript access (`__getitem__` and `__setitem__`) from `ActionState` in `src/freshenmac/boot.py`.
- Removed `--action Update` choice from `src/freshenmac/main.py` (supporting `Install`, `Uninstall`, `Spinner`, `Wave`).
- Removed duplicate `MacOS.notify_manual_os_update()` from `src/freshenmac/computer.py` and consolidated notification
  logic into `SoftwareUpdate.notify_manual()` in `src/freshenmac/softwareupdate.py`.
- Removed unused `DeferState` dataclass from `src/freshenmac/boot.py` and obsolete `self.defer` tracking in
  `src/freshenmac/mas.py`.
- Removed unused `RunAppleScript.notify()` from `src/freshenmac/util.py`.
- Removed unused `RunCMD.error_info` dictionary and helper `_read_bytes()` from `src/freshenmac/util.py`.
- Removed unused `Runner.is_alive` property and `RunCMD.canceled` attribute from `src/freshenmac/util.py`.
- Removed unused `RebootState.requested` attribute from `src/freshenmac/boot.py`.
- Removed redundant `TestFixtures.test_load_all_standard_fixtures` from `test/test_fixtures.py` and non-deterministic,
  unmocked `TestMacOS.test_is_app_running_real_finder` from `test/test_mac_computer.py`.

### Fixed
- Fixed `RunAppleScript.dialog()` in `src/freshenmac/util.py` to wrap System Events dialog execution in
  `with timeout of ... seconds`, preventing AppleScript -1712 AppleEvent timeouts on long-duration prompts.
- Fixed silent AppleScript dialog failure loop in `Reboot.prompt_initial()` by recognizing idle system state and
  rebooting immediately instead of falling into a 3-hour snooze when locked or headless.
- Fixed `Reboot.prompt_initial()` error logging to properly capture `dialog_cmd.stderr or dialog_cmd.error`.
- Fixed `LaunchAgent.install()` in `src/freshenmac/plist.py` skipping `/etc/sudoers.d/freshenmac` setup and targeting
  `gui/0` when invoked as `root` via `sudo`.
- Fixed modern macOS Sequoia update download detection in `SoftwareUpdate._update_macos()` by broadening
  `PATTERN['Downloaded Ver']` and adding fallbacks for `Downloading: 100.00%` and returncode completion.
- Fixed `MacOS.print_summary()` reporting macOS as `(up to date)` when a newer version has been staged to disk.
- Fixed duplicate `brew upgrade` attempts on packages previously upgraded as dependencies in `_upgrade_formula()` and
  `_upgrade_cmake_formulae()`.

## [1.5.0] - 2026-10-27

### Added
- Added `TortimerCreds` dataclass in `src/freshenmac/util.py` to encapsulate administrative credentials, securely
  discover the console user via `os.stat`/`pwd.getpwuid`, and mask secrets in string representations (`repr=False`,
  [#22], resolves [#20]).
- Added real Homebrew forced update test fixture under `test/fixtures/brew/update/force/20261026-1159/` and integration
  tests in `test/test_fixtures.py`.

### Changed
- Adopted `TortimerCreds` in `src/freshenmac/main.py` and `src/freshenmac/computer.py` for FileVault and Volume
  Ownership credential resolution ([#22], resolves [#20]).

## [1.4.2] - 2026-10-26

### Added
- Added `CronSchedule` dataclass in `src/freshenmac/plist.py` to parse 5-part cron syntax and launchd dictionaries
  into Cartesian calendar intervals ([#21]).

### Changed
- Centralized Xcode Command Line Tools regex parsing into module-level `PATTERN['Command Line Tools']` in
  `src/freshenmac/xcode.py` ([#21], resolves [#10]).
- Increased default reboot suggestion uptime threshold (`IDLE_THRESHOLD['Uptime in Days']`) from 15 to 30 days in
  `src/freshenmac/config.py` ([#21]).
- Standardized logger injection into `Xcode` instances across `src/freshenmac/homebrew.py` and
  `src/freshenmac/mas.py` ([#21]).

## [1.4.1] - 2026-10-25

### Added
- Real combined test fixture under `test/fixtures/brew/outdated/20261023-2233/` for `brew outdated --json=v2`
  integration testing ([#18]).
- Extended test coverage with unit tests in `test/test_homebrew.py` for malformed JSON error handling and missing key
  validation ([#18]).

### Changed
- Consolidated Homebrew outdated package inspection into a single-pass `brew outdated --json=v2` query for both
  formulae and casks, optimizing performance and reducing subprocess invocations ([#18], resolves [#17]).
- Replaced separate `_get_outdated_formulae` and `_get_outdated_casks` methods with a unified `_read_outdated()`,
  `_parse_outdated()`, and `_get_outdated()` pipeline in `src/freshenmac/homebrew.py`, while preserving version diff
  normalization (`installed ═▷ current`), pinned exclusions, and threshold filtering ([#18]).

## [1.4.0] - 2026-10-24

### Added
- Added `-z` / `--standalone` CLI flag to run updates once without self-updating or configuring LaunchAgent
  schedules ([#16]).
- Architectural invariant test suite in `test/test_architecture.py` now supports `# noqa: P37-RULE` directives to
  permit intentional exceptions to sorting invariants ([#16]).

### Changed
- Replaced separate `-f/--force-reboot` and `-r/--no-reboot` flags with unified, case-insensitive `-r` / `--reboot`
  option accepting `DEFAULT`, `FORCE`, `YES`, `NO`, or `SKIP` ([#16], resolves [#15]).
- Made `--action` (`install`, `update`, `uninstall`) CLI options case-insensitive ([#16]).
- Standardized logger method names to `printf` across all modules to eliminate shadowing of Python's built-in
  `print` ([#16]).
- Removed legacy per-user LaunchAgent unloading and unlinking from `~/Library/LaunchAgents` ([#16]).

## [1.3.0] - 2026-10-19

### Changed
- Refactored `debug_vars` parameter passing across modules ([#12], resolves [#11]).

## [1.2.0] - 2026-10-17

### Added
- Added `install` action option for background service setup ([#8], resolves [#7]).

## [1.1.1] - 2026-10-14

### Fixed
- Fixed update plist bug and custom LaunchAgent schedule handling ([#6], resolves [#5]).

## [1.1.0] - 2026-10-13

### Added
- Feature release for FreshenMac update orchestrator.

### Fixed
- Fixed background updates plist generation and reboot trigger tracking ([#4], resolves [#3]).

## [1.0.0] - 2026-10-05

### Added
- Initial release.

[2.1.1]: https://github.com/Panther37/FreshenMac/compare/v2.1.0...v2.1.1
[2.1.0]: https://github.com/Panther37/FreshenMac/compare/v2.0.0...v2.1.0
[2.0.0]: https://github.com/Panther37/FreshenMac/compare/v1.5.0...v2.0.0
[1.5.0]: https://github.com/Panther37/FreshenMac/compare/v1.4.2...v1.5.0
[1.4.2]: https://github.com/Panther37/FreshenMac/compare/v1.4.1...v1.4.2
[1.4.1]: https://github.com/Panther37/FreshenMac/compare/v1.4.0...v1.4.1
[1.4.0]: https://github.com/Panther37/FreshenMac/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/Panther37/FreshenMac/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/Panther37/FreshenMac/compare/v1.1.1...v1.2.0
[1.1.1]: https://github.com/Panther37/FreshenMac/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/Panther37/FreshenMac/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/Panther37/FreshenMac/releases/tag/v1.0.0
[#27]: https://github.com/Panther37/FreshenMac/issues/27
[#26]: https://github.com/Panther37/FreshenMac/pull/26
[#25]: https://github.com/Panther37/FreshenMac/issues/25
[#24]: https://github.com/Panther37/FreshenMac/pull/24
[#23]: https://github.com/Panther37/FreshenMac/issues/23
[#22]: https://github.com/Panther37/FreshenMac/pull/22
[#21]: https://github.com/Panther37/FreshenMac/pull/21
[#20]: https://github.com/Panther37/FreshenMac/issues/20
[#18]: https://github.com/Panther37/FreshenMac/pull/18
[#17]: https://github.com/Panther37/FreshenMac/issues/17
[#16]: https://github.com/Panther37/FreshenMac/pull/16
[#15]: https://github.com/Panther37/FreshenMac/issues/15
[#12]: https://github.com/Panther37/FreshenMac/pull/12
[#11]: https://github.com/Panther37/FreshenMac/issues/11
[#10]: https://github.com/Panther37/FreshenMac/issues/10
[#8]: https://github.com/Panther37/FreshenMac/pull/8
[#7]: https://github.com/Panther37/FreshenMac/issues/7
[#6]: https://github.com/Panther37/FreshenMac/pull/6
[#5]: https://github.com/Panther37/FreshenMac/issues/5
[#4]: https://github.com/Panther37/FreshenMac/pull/4
[#3]: https://github.com/Panther37/FreshenMac/issues/3
