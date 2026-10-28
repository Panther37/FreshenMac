# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Please wrap at 120 characters.

## [Unreleased] - `main`

## [1.5.0] - 2026-10-27

### Added
- Added `TortimerCreds` dataclass in `src/freshenmac/util.py` to encapsulate administrative credentials, securely
  discover the console user via `os.stat`/`pwd.getpwuid`, and mask secrets in string representations (`repr=False`,
  resolves [#20]).
- Added real Homebrew forced update test fixture under `test/fixtures/brew/update/force/20261026-1159/` and integration
  tests in `test/test_fixtures.py`.

### Changed
- Adopted `TortimerCreds` in `src/freshenmac/main.py` and `src/freshenmac/computer.py` for FileVault and Volume
  Ownership credential resolution ([#20]).

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
- Refactored `debug_vars` parameter passing across modules ([#12]).

## [1.2.0] - 2026-10-17

### Added
- Added `install` action option for background service setup ([#8]).

## [1.1.1] - 2026-10-14

### Fixed
- Fixed update plist bug and minor execution issues ([#6]).

## [1.1.0] - 2026-10-13

### Added
- Feature release for FreshenMac update orchestrator.

### Fixed
- Fixed background updates plist generation and handling ([#4]).

## [1.0.0] - 2026-10-05

### Added
- Initial release.

[Unreleased]: https://github.com/Panther37/FreshenMac/compare/v1.5.0...HEAD
[1.5.0]: https://github.com/Panther37/FreshenMac/compare/v1.4.2...v1.5.0
[1.4.2]: https://github.com/Panther37/FreshenMac/compare/v1.4.1...v1.4.2
[1.4.1]: https://github.com/Panther37/FreshenMac/compare/v1.4.0...v1.4.1
[1.4.0]: https://github.com/Panther37/FreshenMac/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/Panther37/FreshenMac/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/Panther37/FreshenMac/compare/v1.1.1...v1.2.0
[1.1.1]: https://github.com/Panther37/FreshenMac/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/Panther37/FreshenMac/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/Panther37/FreshenMac/releases/tag/v1.0.0
[#21]: https://github.com/Panther37/FreshenMac/pull/21
[#20]: https://github.com/Panther37/FreshenMac/issues/20
[#18]: https://github.com/Panther37/FreshenMac/pull/18
[#17]: https://github.com/Panther37/FreshenMac/issues/17
[#16]: https://github.com/Panther37/FreshenMac/pull/16
[#15]: https://github.com/Panther37/FreshenMac/issues/15
[#12]: https://github.com/Panther37/FreshenMac/pull/12
[#10]: https://github.com/Panther37/FreshenMac/issues/10
[#8]: https://github.com/Panther37/FreshenMac/pull/8
[#6]: https://github.com/Panther37/FreshenMac/pull/6
[#4]: https://github.com/Panther37/FreshenMac/pull/4
