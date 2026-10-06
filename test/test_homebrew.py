import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from freshenmac.homebrew import HomeBrew
from freshenmac.util import Logger, parse_version
from freshenmac.xcode import Xcode


class TestHomeBrew(unittest.TestCase):
    """
    Unit tests for HomeBrew package manager automation.

    Tests path discovery (Intel vs Apple Silicon), JSON outdated parsing,
    CMake-dependent build handling, license detection and retry loops,
    Git rebase auto-recovery, and cask / formula upgrading.
    """

    def test_brew_path_resolution(self):
        """Verifies Homebrew binary path discovery for Apple Silicon and Intel."""
        # Case 1: Apple Silicon prefers /opt/homebrew/bin/brew
        brew_arm = HomeBrew(is_arm=True)
        with patch('freshenmac.homebrew.Path.exists', return_value=True), \
            patch('freshenmac.homebrew.shutil.which', return_value='/opt/homebrew/bin/brew'):

            self.assertEqual(brew_arm.brew_path, Path('/opt/homebrew/bin/brew'))

        # Case 2: Intel prefers /usr/local/bin/brew
        brew_intel = HomeBrew(is_arm=False)
        with patch('freshenmac.homebrew.Path.exists', side_effect=lambda: True):
            self.assertEqual(brew_intel.brew_path, Path('/usr/local/bin/brew'))

    def test_cmd_options(self):
        """Verifies cmd_options compiles execution dictionary with custom timeout and debug settings."""
        # Arrange
        brew = HomeBrew(is_arm=True, timeout=1200, debug=True)

        # Act
        opts = brew.cmd_options()

        # Assert
        self.assertEqual(
            opts,
            {'cancelable': True, 'debug': True, 'debug_limit': 0, 'taskpolicy': 'utility', 'timeout': 1200},
        )

    def test_get_and_upgrade_casks(self):
        """Verifies _get_outdated_casks parses cask JSON and _upgrade_casks upgrades each cask."""
        # Arrange
        brew = HomeBrew(is_arm=True, dry_run=False)
        executed_cmds = []
        fake_casks_json = json.dumps(
            {
                'casks': [
                    {'name': 'vlc', 'installed_versions': ['3.0.18'], 'current_version': '3.0.19'},
                ],
            },
        )

        def fake_runcmd(cmd, *args, **kwargs):
            cmd_list = list(cmd)
            executed_cmds.append(cmd_list)
            mock = MagicMock()
            mock.returncode = 0
            mock.__contains__.side_effect = lambda item: item in mock.stdout
            if 'outdated' in cmd_list:
                mock.stdout = fake_casks_json
            else:
                mock.stdout = "==> Upgrading vlc"
            mock.stderr = ""
            mock.std_all = mock.stdout
            mock.__bool__.return_value = True
            return mock

        with patch('freshenmac.homebrew.RunCMD', side_effect=fake_runcmd), \
            patch.object(HomeBrew, 'brew_path', Path('/opt/homebrew/bin/brew')):

            # Act: fetch outdated casks
            casks = brew._get_outdated_casks()

            # Assert: formatted version diff
            self.assertEqual(casks, {'vlc': '3.0.18 ═▷ 3.0.19'})

            # Act: perform upgrade
            brew._upgrade_casks(casks)

            # Assert: upgrade command called with --cask flag
            self.assertTrue(brew.updates_performed)
            self.assertTrue(any(all(['upgrade' in c, '--cask' in c, 'vlc' in c]) for c in executed_cmds))

    def test_get_cmake_formulae(self):
        """Verifies _get_cmake_formulae identifies formulae having cmake in direct or build dependencies."""
        # Arrange
        brew = HomeBrew(is_arm=True, dry_run=True)
        fake_info = json.dumps(
            {
                'formulae': [
                    {'name': 'cmake_pkg', 'build_dependencies': ['cmake', 'ninja'], 'dependencies': []},
                    {'name': 'regular_pkg', 'build_dependencies': ['go'], 'dependencies': []},
                    {'name': 'dep_cmake', 'build_dependencies': [], 'dependencies': ['cmake']},
                ],
            },
        )

        with patch('freshenmac.homebrew.RunCMD') as mock_cmd:
            mock_cmd.return_value.stdout = fake_info
            mock_cmd.return_value.returncode = 0

            # Act
            res = brew._get_cmake_formulae(['cmake_pkg', 'regular_pkg', 'dep_cmake'])

            # Assert: filters only packages needing cmake
            self.assertEqual(res, ['cmake_pkg', 'dep_cmake'])

    def test_get_outdated_formulae(self):
        """Verifies _get_outdated_formulae parses brew outdated JSON and excludes pinned packages."""
        # Arrange
        brew = HomeBrew(is_arm=True, dry_run=True)
        fake_outdated = json.dumps(
            {
                'formulae': [
                    {'name': 'cmake_pkg', 'installed_versions': ['1.0'], 'current_version': '1.1', 'pinned': False},
                    {'name': 'pinned_pkg', 'installed_versions': ['2.0'], 'current_version': '2.1', 'pinned': True},
                    {'name': 'regular_pkg', 'installed_versions': ['3.0'], 'current_version': '3.1', 'pinned': False},
                ],
            },
        )

        with patch('freshenmac.homebrew.RunCMD') as mock_cmd:
            mock_cmd.return_value.stdout = fake_outdated
            mock_cmd.return_value.returncode = 0

            # Act
            res = brew._get_outdated_formulae()

            # Assert: pinned package excluded, version formatted
            self.assertEqual(res, {'cmake_pkg': '1.0 ═▷ 1.1', 'regular_pkg': '3.0 ═▷ 3.1'})

    def test_init_architecture_inputs(self):
        """Verifies HomeBrew requires positional architecture input and sets is_arm correctly."""
        # arm_installed is a required positional argument (no default)
        with self.assertRaises(TypeError):
            HomeBrew()  # type: ignore[call-arg]

        # Positional arguments
        self.assertTrue(HomeBrew(True).is_arm)
        self.assertFalse(HomeBrew(False).is_arm)

        # Keyword arguments
        self.assertTrue(HomeBrew(is_arm=True).is_arm)
        self.assertFalse(HomeBrew(is_arm=False).is_arm)

    def test_logger_default_initialization(self):
        """Verifies HomeBrew initializes default Logger instance when none is passed."""
        brew = HomeBrew(is_arm=True, dry_run=True)
        self.assertIsInstance(brew.logger, Logger)

    def test_logger_integration(self):
        """Verifies HomeBrew delegates log messages to provided custom Logger instance."""
        # Arrange
        mock_logger = MagicMock()
        brew = HomeBrew(is_arm=True, dry_run=True, logger=mock_logger)
        self.assertIs(brew.logger, mock_logger)

        # Act
        brew.update()

        # Assert: component tags and dry-run headers logged
        self.assertTrue(mock_logger.print_store.called)
        calls = [c[0][0] for c in mock_logger.print_store.call_args_list]
        self.assertTrue(any('Updating Homebrew' in msg for msg in calls))
        self.assertTrue(any('[Dry-run]' in msg for msg in calls))
        for call_args in mock_logger.print_store.call_args_list:
            self.assertEqual(call_args[1].get('component'), 'Homebrew')

    def test_read_outdated_skips_identical_and_newer_versions(self):
        """Verifies _read_outdated filters out identical or newer versions reported by brew."""
        # Arrange
        brew = HomeBrew(is_arm=True, dry_run=False)
        fake_json = json.dumps(
            {
                'formulae': [
                    # Identical versions (e.g. llvm@22 pinned/used by rust)
                    {'name': 'llvm@22', 'installed_versions': ['22.1.8'], 'current_version': '22.1.8', 'pinned': False},
                    # Newer installed version than upstream
                    {
                        'name':               'future_pkg',
                        'installed_versions': ['2.0.0'],
                        'current_version':    '1.9.0',
                        'pinned':             False,
                    },
                    # Truly outdated version
                    {
                        'name':               'real_outdated',
                        'installed_versions': ['1.0.0'],
                        'current_version':    '1.1.0',
                        'pinned':             False,
                    },
                    # Multiple installed versions where latest equals current
                    {
                        'name':               'multi_ver',
                        'installed_versions': ['1.0.0', '2.0.0'],
                        'current_version':    '2.0.0',
                        'pinned':             False,
                    },
                ],
            },
        )

        # Act
        outdated = brew._read_outdated(fake_json)

        # Assert: only genuinely outdated packages included
        self.assertNotIn('llvm@22', outdated)
        self.assertNotIn('future_pkg', outdated)
        self.assertNotIn('multi_ver', outdated)
        self.assertIn('real_outdated', outdated)
        self.assertEqual(outdated['real_outdated'], '1.0.0 ═▷ 1.1.0')

    def test_recover_rebase_aborts_locks(self):
        """Verifies _recover_rebase aborts active rebase and resets repository state."""
        # Arrange: create temporary repo directory with stuck rebase-merge lock
        brew = HomeBrew(is_arm=True, dry_run=False)
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_path = Path(tmp_dir) / 'Homebrew'
            lock_dir = repo_path / '.git' / 'rebase-merge'
            lock_dir.mkdir(parents=True)

            executed_git_cmds = []

            def fake_runcmd(cmd, *args, **kwargs):
                cmd_list = list(cmd)
                executed_git_cmds.append(cmd_list)
                mock = MagicMock()
                mock.returncode = 0
                mock.stdout = str(repo_path)
                mock.stderr = ""
                mock.std_all = str(repo_path)
                mock.strip.return_value = str(repo_path)
                mock.__bool__.return_value = True
                return mock

            with patch('freshenmac.homebrew.RunCMD', side_effect=fake_runcmd), \
                patch.object(HomeBrew, 'brew_path', Path('/opt/homebrew/bin/brew')):
                # Act
                res = brew._recover_rebase()

                # Assert: aborted rebase and reset repository
                self.assertTrue(res)
                self.assertTrue(any(all(['rebase' in c, '--abort' in c]) for c in executed_git_cmds))
                self.assertTrue(any('update-reset' in c for c in executed_git_cmds))

    def test_resolve_and_update_dependencies_tri_state(self):
        """Verifies _resolve_and_update_dependencies handles installed, outdated, and missing packages."""
        # Arrange
        brew = HomeBrew(is_arm=True, dry_run=False)

        # Mock brew_list tri-state:
        # 'pkg_installed_current': installed & up-to-date
        # 'pkg_installed_outdated': installed & outdated
        # 'pkg_missing': not installed
        brew.__dict__['brew_list'] = {
            'pkg_installed_current':  [parse_version('1.0')],
            'pkg_installed_outdated': [parse_version('1.0')],
        }

        executed_cmds = []

        def fake_runcmd(cmd, *args, **kwargs):
            cmd_list = list(cmd)
            executed_cmds.append(cmd_list)
            mock = MagicMock()
            mock.returncode = 0
            mock.__contains__.side_effect = lambda item: item in mock.stdout
            if 'deps' in cmd_list:
                mock.stdout = "pkg_installed_current\npkg_installed_outdated\npkg_missing\n"
            elif 'install' in cmd_list:
                mock.stdout = "==> Installing pkg_missing"
            elif 'upgrade' in cmd_list:
                mock.stdout = "==> Upgrading pkg_installed_outdated"
            else:
                mock.stdout = ""
            mock.stderr = ""
            mock.std_all = mock.stdout
            mock.__bool__.return_value = True
            return mock

        outdated_dict = {'pkg_installed_outdated': '1.0 ═▷ 2.0'}

        with patch('freshenmac.homebrew.RunCMD', side_effect=fake_runcmd), \
            patch.object(HomeBrew, 'brew_path', Path('/opt/homebrew/bin/brew')):

            # Act
            brew._resolve_and_update_dependencies('test_formula', outdated_dict)

            # Assert: missing package installed
            self.assertTrue(any(all(['install' in c, 'pkg_missing' in c]) for c in executed_cmds))
            # Outdated package upgraded
            self.assertTrue(any(all(['upgrade' in c, 'pkg_installed_outdated' in c]) for c in executed_cmds))
            # Current package untouched
            self.assertFalse(
                any(
                    all(['pkg_installed_current' in c, any(a in c for a in ('install', 'upgrade'))])
                    for c in executed_cmds
                ),
            )

    def test_strip_intel_warning(self):
        """Verifies _strip_intel_warning removes Homebrew's deprecation banner for Intel Macs."""
        # Arrange
        warning = (
            "Warning: You are using macOS on Intel x86_64.\n"
            "We do not provide support for this platform (as-of September 2026, announced August 2025).\n\n"
            "Apple have dropped Intel x86_64 support in macOS Golden Gate (27).\n"
            "GitHub Actions are dropping macOS Intel x86_64 runners in 2027.\n"
            "Homebrew is a non-profit project run entirely by volunteers, not employees.\n"
            "If the biggest companies in the world cannot support macOS Intel x86_64\n"
            "any longer, sadly neither can we.\n\n"
            "Homebrew no longer builds bottles for this configuration.\n"
            "Consider MacPorts, which provides binary packages for this macOS version:\n"
            "  https://www.macports.org\n\n"
            "This is a Tier 3 configuration:\n"
            "  https://docs.brew.sh/Support-Tiers\n"
            "Read the above document before opening any issues or PRs.\n"
        )

        # Act & Assert: warning completely stripped
        self.assertEqual(HomeBrew._strip_intel_warning(warning), "")

        # Preserves legitimate errors appended after the warning
        error_output = f"{warning}\nError: Formula build failed with exit code 2"
        self.assertEqual(
            HomeBrew._strip_intel_warning(error_output),
            "Error: Formula build failed with exit code 2",
        )

        # Non-matching strings remain unchanged
        self.assertEqual(
            HomeBrew._strip_intel_warning("Error: File not found"),
            "Error: File not found",
        )
        self.assertEqual(HomeBrew._strip_intel_warning(""), "")

    def test_update_brew_dry_run(self):
        """Verifies update performs no subprocess commands when dry_run is True."""
        # Arrange
        brew = HomeBrew(is_arm=True, dry_run=True)

        with patch('freshenmac.homebrew.RunCMD') as mock_cmd:
            # Act
            brew.update()

            # Assert
            mock_cmd.assert_not_called()

    def test_update_brew_retries_on_xcode_license_error(self):
        """Verifies update detects Xcode license error, triggers license acceptance, and retries."""
        # Arrange
        brew = HomeBrew(is_arm=True, dry_run=False)
        upgrade_attempts = []

        def fake_runcmd(cmd, *args, **kwargs):
            cmd_list = list(cmd)
            mock = MagicMock()
            mock.__contains__.side_effect = lambda item: item in mock.stdout
            mock.returncode = 0
            mock.stdout = ''
            mock.stderr = ''
            if 'outdated' in cmd_list:
                mock.stdout = json.dumps({'formulae': [{'name': 'qtbase', 'pinned': False}]})
            elif 'info' in cmd_list:
                mock.stdout = json.dumps(
                    {'formulae': [{'name': 'qtbase', 'build_dependencies': ['cmake'], 'dependencies': []}]},
                )
            elif all(['upgrade' in cmd_list, 'qtbase' in cmd_list]):
                upgrade_attempts.append(cmd_list)
                if len(upgrade_attempts) == 1:
                    # First attempt fails with Xcode license error
                    mock.returncode = 1
                    mock.__bool__.return_value = False
                    mock.stderr = (
                        "CMake Error at cmake/QtPublicAppleHelpers.cmake:909 (message):\n"
                        "  Can't determine darwin macosx SDK path. Error: You have not agreed to the\n"
                        "  Xcode license agreements. Please run 'sudo xcodebuild -license' from\n"
                        "  within a Terminal window to review and agree to the Xcode and Apple SDKs license."
                    )
                else:
                    # Second attempt succeeds after license is accepted
                    mock.returncode = 0
                    mock.__bool__.return_value = True
                    mock.stdout = '==> Upgrading qtbase'
            mock.std_all = '\n'.join([mock.stdout, mock.stderr]).strip()
            return mock

        with patch('freshenmac.homebrew.RunCMD', side_effect=fake_runcmd), \
            patch.object(brew.xcode, 'check_and_fix_license', return_value=True) as mock_check:

            # Act
            brew.update()

            # Assert: check_and_fix_license called, failed upgrade retried, updates recorded
            self.assertGreaterEqual(mock_check.call_count, 2)
            self.assertEqual(len(upgrade_attempts), 2)
            self.assertTrue(brew.updates_performed)

    def test_update_brew_with_cmake_and_regular(self):
        """Verifies update separates CMake-dependent packages and upgrades remaining formulae."""
        # Arrange
        brew = HomeBrew(is_arm=True, dry_run=False)
        executed_cmds = []

        def fake_runcmd(cmd, *args, **kwargs):
            cmd_list = list(cmd)
            executed_cmds.append(cmd_list)
            mock = MagicMock()
            mock.__contains__.side_effect = lambda item: item in mock.stdout
            mock.returncode = 0
            mock.stderr = ''
            if 'outdated' in cmd_list:
                mock.stdout = json.dumps(
                    {'formulae': [{'name': 'qtbase', 'pinned': False}, {'name': 'automake', 'pinned': False}]},
                )
            elif 'info' in cmd_list:
                mock.stdout = json.dumps(
                    {
                        'formulae': [
                            {'name': 'qtbase', 'build_dependencies': ['cmake', 'ninja'], 'dependencies': []},
                            {'name': 'automake', 'build_dependencies': [], 'dependencies': []},
                        ],
                    },
                )
            elif 'upgrade' in cmd_list:
                mock.stdout = '==> Upgrading qtbase'
            else:
                mock.stdout = ''
            return mock

        with patch('freshenmac.homebrew.RunCMD', side_effect=fake_runcmd), \
            patch.object(brew.xcode, 'check_and_fix_license', return_value=True):

            # Act
            brew.update()

            # Assert: update executed, qtbase upgraded individually, automake upgraded, cleanup performed
            self.assertTrue(brew.updates_performed)
            self.assertTrue(any('update' in c for c in executed_cmds))
            self.assertTrue(any(all(['upgrade' in c, 'qtbase' in c]) for c in executed_cmds))
            self.assertTrue(any(all(['upgrade' in c, 'automake' in c]) for c in executed_cmds))
            self.assertTrue(any('cleanup' in c for c in executed_cmds))

    def test_update_rebase_self_healing(self):
        """Verifies update detects interrupted Git rebase during brew update and recovers."""
        # Arrange
        brew = HomeBrew(is_arm=True, dry_run=False)
        executed_cmds = []
        call_count = 0

        def fake_runcmd(cmd, *args, **kwargs):
            nonlocal call_count
            cmd_list = list(cmd)
            executed_cmds.append(cmd_list)
            mock = MagicMock()
            mock.__contains__.side_effect = lambda item: item in mock.stdout
            if all(['update' in cmd_list, '--force' in cmd_list]):
                call_count += 1
                if call_count == 1:
                    mock.returncode = 1
                    mock.stderr = "fatal: It seems that there is already a rebase-merge directory"
                    mock.stdout = ""
                    mock.std_all = mock.stderr
                    mock.__bool__.return_value = False
                    return mock

                mock.returncode = 0
                mock.stdout = "Already up to date."
                mock.stderr = ""
                mock.std_all = mock.stdout
                mock.__bool__.return_value = True
                return mock

            mock.returncode = 0
            mock.stdout = ""
            mock.stderr = ""
            mock.std_all = ""
            mock.__bool__.return_value = True
            return mock

        with patch('freshenmac.homebrew.RunCMD', side_effect=fake_runcmd), \
            patch.object(brew, '_recover_rebase', return_value=True) as mock_recover, \
            patch.object(brew, '_get_outdated_formulae', return_value={}):

            # Act
            brew.update()

            # Assert: recovered rebase and retried brew update --force
            mock_recover.assert_called_once()
            update_calls = [c for c in executed_cmds if all(['update' in c, '--force' in c])]
            self.assertEqual(len(update_calls), 2)

    def test_upgrade_cmake_formulae(self):
        """Verifies _upgrade_cmake_formulae checks license agreement and upgrades packages."""
        # Arrange
        brew = HomeBrew(is_arm=True, dry_run=False)
        executed_cmds = []

        def fake_runcmd(cmd, *args, **kwargs):
            executed_cmds.append(cmd)
            mock = MagicMock()
            mock.__contains__.side_effect = lambda item: item in mock.stdout
            mock.returncode = 0
            mock.stdout = '==> Upgrading cmake_pkg'
            mock.stderr = ''
            mock.std_all = '==> Upgrading cmake_pkg'
            return mock

        with patch('freshenmac.homebrew.RunCMD', side_effect=fake_runcmd), \
            patch.object(brew.xcode, 'check_and_fix_license', return_value=True):

            # Act
            brew._upgrade_cmake_formulae(['cmake_pkg'], {'cmake_pkg': '1.0 -> 1.1'})

            # Assert
            self.assertTrue(brew.updates_performed)
            self.assertTrue(any(all(['upgrade' in c, 'cmake_pkg' in c]) for c in executed_cmds))

    def test_upgrade_keyboard_interrupt_skips_item(self):
        """Verifies KeyboardInterrupt during cask or formula upgrade is caught cleanly and skips."""
        # Arrange
        brew = HomeBrew(is_arm=True, dry_run=False)

        with patch.object(HomeBrew, 'brew_path', Path('/opt/homebrew/bin/brew')), \
            patch('freshenmac.homebrew.RunCMD', side_effect=KeyboardInterrupt):

            # Act & Assert: catches KeyboardInterrupt without unhandled crash
            try:
                brew._upgrade_casks({'test_cask': '1.0 ═▷ 2.0'})
                brew._upgrade_formula('test_formula', '1.0 ═▷ 2.0')
            except KeyboardInterrupt:
                self.fail("KeyboardInterrupt was not caught and skipped cleanly in upgrade methods")

    def test_xcode_property(self):
        """Verifies xcode property creates and caches an Xcode instance with matching settings."""
        # Arrange & Act
        brew = HomeBrew(is_arm=True)

        # Assert
        self.assertIsInstance(brew.xcode, Xcode)
        self.assertEqual(brew.xcode.debug, brew.debug)
        self.assertEqual(brew.xcode.dry_run, brew.dry_run)


if __name__ == '__main__':
    unittest.main()
