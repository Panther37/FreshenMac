import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from freshenmac.homebrew import HomeBrew
from freshenmac.util import Logger
from freshenmac.xcode import Xcode


class TestHomeBrew(unittest.TestCase):
    """Unit tests for HomeBrew class."""

    def test_init_architecture_inputs(self):
        # arm_installed is a required positional argument (no default)
        with self.assertRaises(TypeError):
            HomeBrew()  # type: ignore[call-arg]
        self.assertTrue(HomeBrew(True).is_arm)
        self.assertFalse(HomeBrew(False).is_arm)
        self.assertTrue(HomeBrew(is_arm=True).is_arm)
        self.assertFalse(HomeBrew(is_arm=False).is_arm)

    def test_brew_path_resolution(self):
        brew_arm = HomeBrew(is_arm=True)
        with patch('freshenmac.homebrew.Path.exists', return_value=True):
            with patch('freshenmac.homebrew.shutil.which', return_value='/opt/homebrew/bin/brew'):
                self.assertEqual(brew_arm.brew_path, Path('/opt/homebrew/bin/brew'))

        brew_intel = HomeBrew(is_arm=False)
        with patch('freshenmac.homebrew.Path.exists', side_effect=lambda: True):
            # For intel, /usr/local/bin/brew should be preferred candidate
            self.assertEqual(brew_intel.brew_path, Path('/usr/local/bin/brew'))

    def test_get_outdated_formulae(self):
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
            res = brew._get_outdated_formulae()
            self.assertEqual(res, {'cmake_pkg': '1.0 ═▷ 1.1', 'regular_pkg': '3.0 ═▷ 3.1'})

    def test_get_cmake_formulae(self):
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
            res = brew._get_cmake_formulae(['cmake_pkg', 'regular_pkg', 'dep_cmake'])
            self.assertEqual(res, ['cmake_pkg', 'dep_cmake'])

    def test_update_brew_dry_run(self):
        brew = HomeBrew(is_arm=True, dry_run=True)
        with patch('freshenmac.homebrew.RunCMD') as mock_cmd:
            brew.update()
            mock_cmd.assert_not_called()

    def test_update_brew_with_cmake_and_regular(self):
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

        with patch('freshenmac.homebrew.RunCMD', side_effect=fake_runcmd):
            with patch.object(brew.xcode, 'check_and_fix_license', return_value=True):
                brew.update()
            self.assertTrue(brew.updates_performed)
            # Verify update was called
            self.assertTrue(any('update' in c for c in executed_cmds))
            # Verify qtbase was upgraded individually
            self.assertTrue(any('upgrade' in c and 'qtbase' in c for c in executed_cmds))
            # Verify remaining packages were upgraded individually (automake)
            self.assertTrue(any('upgrade' in c and 'automake' in c for c in executed_cmds))
            # Verify cleanup was called
            self.assertTrue(any('cleanup' in c for c in executed_cmds))

    def test_xcode_property(self):
        brew = HomeBrew(is_arm=True)
        self.assertIsInstance(brew.xcode, Xcode)
        self.assertEqual(brew.xcode.debug, brew.debug)
        self.assertEqual(brew.xcode.dry_run, brew.dry_run)

    def test_update_brew_retries_on_xcode_license_error(self):
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
            elif 'upgrade' in cmd_list and 'qtbase' in cmd_list:
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

        with patch('freshenmac.homebrew.RunCMD', side_effect=fake_runcmd):
            with patch.object(brew.xcode, 'check_and_fix_license', return_value=True) as mock_check:
                brew.update()
                # check_and_fix_license called before upgrade and upon detecting license error
                self.assertGreaterEqual(mock_check.call_count, 2)
                # Upgraded was retried
                self.assertEqual(len(upgrade_attempts), 2)
                self.assertTrue(brew.updates_performed)

    def test_cmd_options(self):
        brew = HomeBrew(is_arm=True, timeout=1200, debug=True)
        opts = brew.cmd_options()
        self.assertEqual(opts, {'cancelable': True, 'debug': True, 'debug_limit': 0, 'taskpolicy': 'utility', 'timeout': 1200})

    def test_strip_intel_warning(self):
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
        self.assertEqual(HomeBrew._strip_intel_warning(warning), "")

        error_output = f"{warning}\nError: Formula build failed with exit code 2"
        self.assertEqual(
            HomeBrew._strip_intel_warning(error_output),
            "Error: Formula build failed with exit code 2",
        )

        self.assertEqual(
            HomeBrew._strip_intel_warning("Error: File not found"),
            "Error: File not found",
        )

        self.assertEqual(HomeBrew._strip_intel_warning(""), "")

    def test_upgrade_cmake_formulae(self):
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

        with patch('freshenmac.homebrew.RunCMD', side_effect=fake_runcmd):
            with patch.object(brew.xcode, 'check_and_fix_license', return_value=True):
                brew._upgrade_cmake_formulae(['cmake_pkg'], {'cmake_pkg': '1.0 -> 1.1'})
                self.assertTrue(brew.updates_performed)
                self.assertTrue(any('upgrade' in c and 'cmake_pkg' in c for c in executed_cmds))

    def test_update_rebase_self_healing(self):
        brew = HomeBrew(is_arm=True, dry_run=False)
        executed_cmds = []
        call_count = 0

        def fake_runcmd(cmd, *args, **kwargs):
            nonlocal call_count
            cmd_list = list(cmd)
            executed_cmds.append(cmd_list)
            mock = MagicMock()
            mock.__contains__.side_effect = lambda item: item in mock.stdout
            if 'update' in cmd_list and '--force' in cmd_list:
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

        with patch('freshenmac.homebrew.RunCMD', side_effect=fake_runcmd):
            with patch.object(brew, '_recover_rebase', return_value=True) as mock_recover:
                with patch.object(brew, '_get_outdated_formulae', return_value={}):
                    brew.update()
                    mock_recover.assert_called_once()
                    update_calls = [c for c in executed_cmds if 'update' in c and '--force' in c]
                    self.assertEqual(len(update_calls), 2)

    def test_recover_rebase_aborts_locks(self):
        import tempfile
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

            with patch('freshenmac.homebrew.RunCMD', side_effect=fake_runcmd):
                with patch.object(HomeBrew, 'brew_path', Path('/opt/homebrew/bin/brew')):
                    res = brew._recover_rebase()
                    self.assertTrue(res)
                    self.assertTrue(any('rebase' in c and '--abort' in c for c in executed_git_cmds))
                    self.assertTrue(any('update-reset' in c for c in executed_git_cmds))

    def test_get_and_install_missing_dependencies(self):
        brew = HomeBrew(is_arm=True, dry_run=False)
        executed_cmds = []

        def fake_runcmd(cmd, *args, **kwargs):
            cmd_list = list(cmd)
            executed_cmds.append(cmd_list)
            mock = MagicMock()
            mock.returncode = 0
            mock.__contains__.side_effect = lambda item: item in mock.stdout
            if 'deps' in cmd_list:
                mock.stdout = "Warning: hints\nlibgit2\nrust\n"
            else:
                mock.stdout = "==> Installing rust"
            mock.stderr = ""
            mock.std_all = mock.stdout
            mock.__bool__.return_value = True
            return mock

        with patch('freshenmac.homebrew.RunCMD', side_effect=fake_runcmd):
            with patch.object(HomeBrew, 'brew_path', Path('/opt/homebrew/bin/brew')):
                deps = brew._get_missing_dependencies('node')
                self.assertEqual(deps, ['libgit2', 'rust'])

                brew._install_missing_dependencies('node')
                self.assertTrue(brew.updates_performed)
                self.assertTrue(any('install' in c and 'libgit2' in c for c in executed_cmds))
                self.assertTrue(any('install' in c and 'rust' in c for c in executed_cmds))

    def test_get_and_upgrade_casks(self):
        brew = HomeBrew(is_arm=True, dry_run=False)
        executed_cmds = []
        fake_casks_json = json.dumps({
            'casks': [
                {'name': 'vlc', 'installed_versions': ['3.0.18'], 'current_version': '3.0.19'},
            ]
        })

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

        with patch('freshenmac.homebrew.RunCMD', side_effect=fake_runcmd):
            with patch.object(HomeBrew, 'brew_path', Path('/opt/homebrew/bin/brew')):
                casks = brew._get_outdated_casks()
                self.assertEqual(casks, {'vlc': '3.0.18 ═▷ 3.0.19'})

                brew._upgrade_casks(casks)
                self.assertTrue(brew.updates_performed)
                self.assertTrue(any('upgrade' in c and '--cask' in c and 'vlc' in c for c in executed_cmds))

    def test_read_outdated_skips_identical_and_newer_versions(self):
        brew = HomeBrew(is_arm=True, dry_run=False)
        fake_json = json.dumps({
            'formulae': [
                # Identical versions (e.g. llvm@22 pinned/used by rust)
                {'name': 'llvm@22', 'installed_versions': ['22.1.8'], 'current_version': '22.1.8', 'pinned': False},
                # Newer installed version
                {'name': 'future_pkg', 'installed_versions': ['2.0.0'], 'current_version': '1.9.0', 'pinned': False},
                # Truly outdated version
                {'name': 'real_outdated', 'installed_versions': ['1.0.0'], 'current_version': '1.1.0', 'pinned': False},
                # Multiple installed versions where latest equals current
                {'name': 'multi_ver', 'installed_versions': ['1.0.0', '2.0.0'], 'current_version': '2.0.0', 'pinned': False},
            ]
        })
        outdated = brew._read_outdated(fake_json)
        self.assertNotIn('llvm@22', outdated)
        self.assertNotIn('future_pkg', outdated)
        self.assertNotIn('multi_ver', outdated)
        self.assertIn('real_outdated', outdated)
        self.assertEqual(outdated['real_outdated'], '1.0.0 ═▷ 1.1.0')

    def test_resolve_and_update_dependencies_tri_state(self):
        brew = HomeBrew(is_arm=True, dry_run=False)
        from freshenmac.util import parse_version

        # Mock brew_list:
        # 'pkg_installed_current': installed & up-to-date
        # 'pkg_installed_outdated': installed & outdated
        # 'pkg_missing': not installed
        brew.__dict__['brew_list'] = {
            'pkg_installed_current': [parse_version('1.0')],
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

        with patch('freshenmac.homebrew.RunCMD', side_effect=fake_runcmd):
            with patch.object(HomeBrew, 'brew_path', Path('/opt/homebrew/bin/brew')):
                brew._resolve_and_update_dependencies('test_formula', outdated_dict)

                # Missing package should be installed
                self.assertTrue(any('install' in c and 'pkg_missing' in c for c in executed_cmds))
                # Outdated package should be upgraded
                self.assertTrue(any('upgrade' in c and 'pkg_installed_outdated' in c for c in executed_cmds))
                # Up-to-date package should NOT be upgraded or installed
                self.assertFalse(any('pkg_installed_current' in c and ('install' in c or 'upgrade' in c) for c in executed_cmds))

    def test_upgrade_keyboard_interrupt_skips_item(self):
        brew = HomeBrew(is_arm=True, dry_run=False)
        with patch.object(HomeBrew, 'brew_path', Path('/opt/homebrew/bin/brew')):
            with patch('freshenmac.homebrew.RunCMD', side_effect=KeyboardInterrupt):
                # Should not raise KeyboardInterrupt, should catch and skip
                try:
                    brew._upgrade_casks({'test_cask': '1.0 ═▷ 2.0'})
                    brew._upgrade_formula('test_formula', '1.0 ═▷ 2.0')
                except KeyboardInterrupt:
                    self.fail("KeyboardInterrupt was not caught and skipped cleanly in upgrade methods")

    def test_logger_integration(self):
        mock_logger = MagicMock()
        brew = HomeBrew(is_arm=True, dry_run=True, logger=mock_logger)
        self.assertIs(brew.logger, mock_logger)
        brew.update()
        self.assertTrue(mock_logger.print_store.called)
        calls = [c[0][0] for c in mock_logger.print_store.call_args_list]
        self.assertTrue(any('Updating Homebrew' in msg for msg in calls))
        self.assertTrue(any('[Dry-run]' in msg for msg in calls))
        for call_args in mock_logger.print_store.call_args_list:
            self.assertEqual(call_args[1].get('component'), 'Homebrew')

    def test_logger_default_initialization(self):
        brew = HomeBrew(is_arm=True, dry_run=True)
        self.assertIsInstance(brew.logger, Logger)


if __name__ == '__main__':
    unittest.main()
