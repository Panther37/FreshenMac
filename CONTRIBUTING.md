# Contributing to FreshenMac

Thank you for your interest in contributing to **FreshenMac**! FreshenMac is an automated, polite, and unobtrusive macOS
maintenance tool designed to keep macOS, Homebrew, and App Store applications seamlessly up to date.

We welcome bug reports, feature suggestions, documentation enhancements, and pull requests. If you discover a security vulnerability, please follow our [Security Policy](SECURITY.md) to report it privately.

---

## Code of Conduct & Guiding Philosophy

FreshenMac was created to manage Mac systems reliably and quietly in the background without frustrating the user:

- **Polite & Unobtrusive:** Never interrupt active work, never quit open applications without warning, and never reboot
  without either explicit confirmation or verified system-wide idle time.
- **Resilient & Fail-Safe:** An error in one update component (e.g. Homebrew or App Store) must never halt or corrupt
  other update tasks.
- **Zero External Dependencies:** The core codebase runs solely on the standard Python library with zero third-party pip
  dependencies, ensuring seamless execution across standard macOS installations.

---

## Prerequisites & Compatibility

- **Operating System:** macOS Monterey (12.0) or newer (Apple Silicon and Intel supported).
- **Python Compatibility Baseline:** **Python 3.9+**
    - Fully compatible with Apple's default Command Line Tools `/usr/bin/python3` (Python 3.9.6 on Monterey through
      Sequoia).
    - Designed and actively tested against Python 3.14.
    - Avoid any language features or syntax introduced in Python 3.10+ (such as `match/case` statements, `X | Y` union
      syntax in runtime expressions without `from __future__ import annotations`, or `itertools.pairwise`) unless
      guarded for Python 3.9 compatibility.
- **External Tools:**
    - Xcode Command Line Tools (`xcode-select --install`).
    - [Homebrew](https://brew.sh) (optional, if testing Homebrew package management).
    - [mas-cli](https://github.com/mas-cli/mas) (optional, for App Store updates).

---

## Architectural Invariants

The FreshenMac codebase enforces strict architectural invariants verified by automated tests in
`test/test_architecture.py`. Any PR modifying code must adhere to these rules:

### 1. Strict Alphabetical Ordering

All Python modules must maintain strict alphabetical ordering:

- **Module Constants:** Module-level constants (`ALL_CAPS`) must be defined in alphabetical order.
- **Top-Level Classes:** Classes in each file must appear in alphabetical order.
- **Methods Within Classes:**
    - In standard classes: `__init__` comes first, followed by all remaining methods sorted alphabetically.
    - In `unittest.TestCase` classes: `setUp` and `tearDown` come first, followed by all test methods sorted
      alphabetically.
- **Top-Level Helper Functions:** Defined in alphabetical order at the bottom of the module.
- **Keyword-Only Parameters:** Keyword-only arguments in function and method signatures must be sorted alphabetically.
- **Package Exports:** Exported classes in `src/freshenmac/__init__.py` must be sorted alphabetically in `__all__`.

### 2. Shutdown Safety Guard

Any test method that references, exercises, or mocks reboot or shutdown functionality **must** invoke
`ensure_no_active_shutdown()` (imported from `test.test_reboot`). This prevents tests from accidentally executing host
reboots or leaving unmocked shutdown commands. The architecture test inspects the AST of all test methods to enforce
this requirement.

### 3. Readability for Compound Conditions

When writing conditional checks with multiple conditions (two or more `and` keywords / three or more operands), prefer
`all([...])` constructs over long compound expressions to maintain readability.

### 4. Isolation & Mocking

All tests must be completely isolated and hermetic:

- Never invoke live system commands (`shutdown`, `reboot`, `launchctl`, `fdesetup`, `softwareupdate`, `brew`, `mas`) in
  unit tests.
- Always mock subprocess calls, AppleScript dialogs, file system modifications outside temporary directories, and system
  audio alerts.

---

## Development & Testing

### Running Tests

Before submitting a pull request, verify that the entire test suite passes:

```bash
# Run all unit and integration tests
PYTHONPATH=src python3 -m unittest discover -s test

# Run architectural invariant checks
PYTHONPATH=src python3 -m unittest test/test_architecture.py
```

### Trace Mode

You can inspect execution flow and debug CLI argument routing using the `--trace` flag:

```bash
PYTHONPATH=src python3 FreshenMac.py --dry-run --trace
```

---

## Pull Request Guidelines

1. **Create a Feature Branch:** Branch off `main` with a descriptive name (e.g., `fix/idle-detection`,
   `feat/xcode-license`).
2. **Write Unit Tests:** Add unit tests covering all new features or bug fixes.
3. **Check Invariants:** Ensure all modified files adhere to the alphabetical ordering and architectural rules.
4. **Keep Commits Focused:** Make clear, atomic commits with descriptive commit messages.
5. **Update Documentation:** If your change modifies CLI flags, configuration keys, or default behavior, update
   `README.md` accordingly.

---

## Licensing

By contributing to FreshenMac, you agree that your contributions will be licensed under the
project's [BSD 3-Clause License](LICENSE).
