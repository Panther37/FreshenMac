from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from freshenmac.fix import (
    DEFAULT_BACKOFF,
    PATTERN,
    FailureInfo,
    RecoveryEngine,
    extract_dependency,
    rules_homebrew,
)
from freshenmac.util import RunCMD


class TestRecoveryEngine(unittest.TestCase):
    """Tests for FailureInfo, diagnostic patterns, and RecoveryEngine."""

    def test_extract_dependency(self):
        qemu_error = (
            "mkvenv: checking for meson>=1.5.0\n"
            "Could not provide build dependency 'meson==1.11.1':\n"
            " • Python package 'meson' was not found nor installed."
        )
        self.assertEqual(extract_dependency(qemu_error), 'meson')
        self.assertEqual(extract_dependency('syntax error'), '')

    def test_failure_context_combined_output(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.log', delete=False) as f:
            f.write("Line 1 from log\nLine 2 from log\n")
            log_path = Path(f.name)

        try:
            context = FailureInfo(
                log_file=log_path,
                package='test_pkg',
                stderr="Stderr message",
                stdout="Stdout message",
            )
            combined = context.combined_output()
            self.assertIn("Stdout message", combined)
            self.assertIn("Stderr message", combined)
            self.assertIn("Line 1 from log", combined)
        finally:
            log_path.unlink(missing_ok=True)

    def test_failure_context_defaults(self):
        context = FailureInfo()
        self.assertEqual(context.cmd_args, ())
        self.assertEqual(context.log_file, Path())
        self.assertEqual(context.package, '')
        self.assertEqual(context.returncode, 0)
        self.assertEqual(context.stderr, '')
        self.assertEqual(context.stdout, '')
        self.assertEqual(context.subsystem, '')

        custom = FailureInfo(cmd_args=('brew', 'upgrade', 'qemu'), package='qemu')
        self.assertEqual(custom.cmd_args, ('brew', 'upgrade', 'qemu'))
        self.assertEqual(custom.package, 'qemu')

    def test_failure_context_from_runcmd(self):
        """Verifies FailureInfo.from_runcmd extracts attributes from a RunCMD instance."""
        mock_cmd = MagicMock()
        mock_cmd.cmd_args = ['brew', 'upgrade', 'qemu']
        mock_cmd.returncode = 1
        mock_cmd.stderr = "Error output"
        mock_cmd.stdout = "Standard output"

        ctx = FailureInfo.from_runcmd(
            mock_cmd,
            log_file=Path('/tmp/test.log'),
            package='qemu',
            subsystem='Homebrew',
        )
        self.assertEqual(ctx.cmd_args, ('brew', 'upgrade', 'qemu'))
        self.assertEqual(ctx.log_file, Path('/tmp/test.log'))
        self.assertEqual(ctx.package, 'qemu')
        self.assertEqual(ctx.returncode, 1)
        self.assertEqual(ctx.stderr, "Error output")
        self.assertEqual(ctx.stdout, "Standard output")
        self.assertEqual(ctx.subsystem, 'Homebrew')

    def test_failure_context_from_runcmd_uninitialized_fails(self):
        """Verifies FailureInfo.from_runcmd raises AttributeError if given an uninitialized RunCMD object."""
        uninit_cmd = RunCMD.__new__(RunCMD)
        with self.assertRaises(AttributeError):
            FailureInfo.from_runcmd(uninit_cmd)

    def test_failure_context_nonexistent_log_file(self):
        context = FailureInfo(
            log_file=Path('/nonexistent/path/never_created.log'),
            package='test_pkg',
            stdout="Just stdout",
        )
        self.assertEqual(context.combined_output(), "Just stdout")

    def test_pattern_brew_git_rebase(self):
        err = "fatal: Unable to create '/opt/homebrew/.git/index.lock': File exists."
        self.assertTrue(bool(PATTERN['Brew Rebase Lock'].search(err)))
        self.assertFalse(bool(PATTERN['Brew Rebase Lock'].search("Everything is fine.")))

    def test_pattern_brew_link_conflict(self):
        err = "Error: The `brew link` step did not complete successfully"
        self.assertTrue(bool(PATTERN['Brew Link Conflict'].search(err)))

    def test_pattern_brew_python_mismatch(self):
        err = "Could not provide build dependency 'meson==1.11.1': Python package 'meson' was not found"
        self.assertTrue(bool(PATTERN['Brew Python Mismatch'].search(err)))

    def test_pattern_transient_network(self):
        err = "curl: (56) LibreSSL SSL_read: error"
        self.assertTrue(bool(PATTERN['Transient Network'].search(err)))

    def test_pattern_xcode_license(self):
        err = "You have not agreed to the Xcode license agreements. Please run 'sudo xcodebuild -license'."
        self.assertTrue(bool(PATTERN['Xcode License'].search(err)))

    def test_recovery_engine_add_rules(self):
        """Verifies add_rules registers rules into the internal dictionary."""
        engine = RecoveryEngine()
        engine.add_rules(
            {
                'Rule1': (PATTERN['Transient Network'], lambda ctx: True),
                'Rule2': (PATTERN['Brew Rebase Lock'], lambda ctx: True),
            }
        )
        self.assertEqual(len(engine.rules), 2)
        self.assertIn('Rule1', engine.rules)
        self.assertIn('Rule2', engine.rules)

    def test_recovery_engine_add_rules_from_target(self):
        """Verifies add_rules and target parameter resolve rules automatically for subsystem instances."""
        class HomeBrew:
            pass

        mock_brew = HomeBrew()
        engine = RecoveryEngine(target=mock_brew)
        self.assertIn('BrewGitRebase', engine.rules)
        self.assertIn('BrewLinkConflict', engine.rules)
        self.assertIn('BrewPythonMismatch', engine.rules)

        class CustomProvider:
            def fix_rules(self):
                return {'CustomRule': (PATTERN['Transient Network'], lambda ctx: True)}

        engine_custom = RecoveryEngine(target=CustomProvider())
        self.assertIn('CustomRule', engine_custom.rules)

        class InvalidTypeProvider:
            fix_rules = "not a dict"

        with self.assertRaises(TypeError):
            RecoveryEngine(target=InvalidTypeProvider())

        class UnknownTarget:
            pass

        with self.assertRaises(AttributeError):
            RecoveryEngine(target=UnknownTarget())

    def test_recovery_engine_diagnose(self):
        """Verifies diagnose returns the matching rule name and action, or None."""
        engine = RecoveryEngine()
        action_mock = MagicMock(return_value=True)
        engine.add_rules({'Transient': (PATTERN['Transient Network'], action_mock)})

        context_match = FailureInfo(stderr="curl: (35) SSL connect error")
        diagnosed = engine.diagnose(context_match)
        self.assertIsNotNone(diagnosed)
        self.assertEqual(diagnosed[0], 'Transient')
        self.assertEqual(diagnosed[1], action_mock)

        context_no_match = FailureInfo(stderr="Everything is fine")
        self.assertIsNone(engine.diagnose(context_no_match))

    def test_recovery_engine_diagnose_custom_callable(self):
        """Verifies diagnose supports custom callable detectors as matchers."""
        engine = RecoveryEngine()
        detector_mock = MagicMock(return_value=True)
        action_mock = MagicMock(return_value=True)
        engine.add_rules({'Custom': (detector_mock, action_mock)})

        context = FailureInfo(stdout="Custom error message")
        diagnosed = engine.diagnose(context)
        self.assertIsNotNone(diagnosed)
        self.assertEqual(diagnosed[0], 'Custom')
        detector_mock.assert_called_once_with(context)

    def test_recovery_engine_full_lifecycle(self):
        engine = RecoveryEngine()
        reinstall_mock = MagicMock(return_value=True)
        engine.add_rules(
            {
                'PythonMismatch': (
                    PATTERN['Brew Python Mismatch'],
                    lambda ctx: reinstall_mock(extract_dependency(ctx.combined_output())),
                ),
            }
        )

        context = FailureInfo(
            package='qemu',
            stdout="Could not provide build dependency 'meson==1.11.1': Python package 'meson' was not found",
        )

        retry_result = MagicMock(returncode=0)
        retry_mock = MagicMock(return_value=retry_result)

        # First recovery attempt should succeed and return the retry result
        result = engine.recover(context, retry_mock)
        self.assertEqual(result, retry_result)
        reinstall_mock.assert_called_once_with('meson')
        retry_mock.assert_called_once()
        self.assertIn('qemu', engine.healed_packages)

        # Second recovery attempt for the same package must be skipped (bounded to 1 attempt)
        retry_mock.reset_mock()
        second_attempt = engine.recover(context, retry_mock)
        self.assertIsNone(second_attempt)
        retry_mock.assert_not_called()

    def test_recovery_engine_no_match(self):
        engine = RecoveryEngine()
        context = FailureInfo(package='some_pkg', stdout="Unknown fatal error")
        retry_mock = MagicMock()

        self.assertIsNone(engine.recover(context, retry_mock))
        retry_mock.assert_not_called()

    def test_recovery_engine_remediation_failure(self):
        engine = RecoveryEngine()
        action_mock = MagicMock(return_value=False)
        engine.add_rules({'FailedRule': (PATTERN['Brew Python Mismatch'], action_mock)})

        context = FailureInfo(
            package='qemu',
            stdout="Could not provide build dependency 'meson==1.11.1'",
        )
        retry_mock = MagicMock()

        self.assertIsNone(engine.recover(context, retry_mock))
        action_mock.assert_called_once_with(context)
        retry_mock.assert_not_called()

    def test_rules_homebrew(self):
        """Verifies rules_homebrew registers all expected rules and executes actions on mock."""
        mock_brew = MagicMock()
        mock_brew.brew_path = Path('/opt/homebrew/bin/brew')
        mock_brew.cmd_options.return_value = {}
        mock_brew.xcode.is_license_error.return_value = True
        mock_brew.xcode.check_and_fix_license.return_value = True

        rules = rules_homebrew(mock_brew)
        expected_keys = {
            'BrewGitRebase',
            'BrewLinkConflict',
            'BrewPythonMismatch',
            'TransientNetwork',
            'XcodeLicense',
        }
        self.assertEqual(set(rules.keys()), expected_keys)

        ctx = FailureInfo(package='pkg_a')
        mock_brew.recover_rebase.return_value = True
        self.assertTrue(rules['BrewGitRebase'][1](ctx))
        mock_brew.recover_rebase.assert_called_once()

        self.assertTrue(rules['XcodeLicense'][0](ctx))
        self.assertTrue(rules['XcodeLicense'][1](ctx))
        mock_brew.xcode.check_and_fix_license.assert_called_once()

        with patch('freshenmac.fix.Wave'), patch('freshenmac.fix.time.sleep'):
            self.assertTrue(rules['TransientNetwork'][1](ctx))


if __name__ == '__main__':
    unittest.main()
