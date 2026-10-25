# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased] - `main`

## [1.4.1] - 2026-10-25

### Added
- Real combined test fixture under `test/fixtures/brew/outdated/20261023-2233/` for `brew outdated --json=v2` integration testing.

### Changed
- Consolidated Homebrew outdated package inspection into a single-pass `brew outdated --json=v2` query for both formulae and casks, optimizing performance and reducing subprocess invocations ([#17]).

## [1.4.0] - 2026-10-24

### Added
- Added `-z` / `--standalone` CLI flag to run updates once without self-updating or configuring LaunchAgent schedules ([#16]).
- Architectural invariant test suite in `test/test_architecture.py` now supports `# noqa: P37-RULE` directives to permit intentional exceptions to sorting invariants ([#16]).

### Changed
- Replaced separate `-f/--force-reboot` and `-r/--no-reboot` flags with unified, case-insensitive `-r` / `--reboot` option accepting `DEFAULT`, `FORCE`, `YES`, `NO`, or `SKIP` ([#16], resolves [#15]).
- Made `--action` (`install`, `update`, `uninstall`) CLI options case-insensitive ([#16]).
- Standardized logger method names to `printf` across all modules to eliminate shadowing of Python's built-in `print` ([#16]).
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

[Unreleased]: https://github.com/Panther37/FreshenMac/compare/v1.4.1...HEAD
[1.4.1]: https://github.com/Panther37/FreshenMac/compare/v1.4.0...v1.4.1
[1.4.0]: https://github.com/Panther37/FreshenMac/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/Panther37/FreshenMac/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/Panther37/FreshenMac/compare/v1.1.1...v1.2.0
[1.1.1]: https://github.com/Panther37/FreshenMac/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/Panther37/FreshenMac/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/Panther37/FreshenMac/releases/tag/v1.0.0
[#17]: https://github.com/Panther37/FreshenMac/issues/17
[#16]: https://github.com/Panther37/FreshenMac/pull/16
[#15]: https://github.com/Panther37/FreshenMac/issues/15
[#12]: https://github.com/Panther37/FreshenMac/pull/12
[#8]: https://github.com/Panther37/FreshenMac/pull/8
[#6]: https://github.com/Panther37/FreshenMac/pull/6
[#4]: https://github.com/Panther37/FreshenMac/pull/4
