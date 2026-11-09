import json
import plistlib
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from freshenmac.computer import MacOS
from freshenmac.fix import FailureInfo, RecoveryEngine, extract_dependency, rules_homebrew
from freshenmac.homebrew import HomeBrew
from freshenmac.mas import AppStore
from freshenmac.plist import CronSchedule, LaunchAgent, SavePreferences, StartupRun, load_preferences
from freshenmac.softwareupdate import SoftwareUpdate
from freshenmac.xcode import Xcode
from test.sim_run_cmd import SimRunCMD

FIXTURES_DIR = Path(__file__).resolve().parent / 'fixtures'
BREW_FIXTURES_DIR = FIXTURES_DIR / 'brew'
FDESETUP_FIXTURES_DIR = FIXTURES_DIR / 'fdesetup'
MAS_FIXTURES_DIR = FIXTURES_DIR / 'mas'
PLIST_FIXTURES_DIR = FIXTURES_DIR / 'plist'
SW_VERS_FIXTURES_DIR = FIXTURES_DIR / 'sw_vers'
XCODEBUILD_FIXTURES_DIR = FIXTURES_DIR / 'xcodebuild'

CLEANUP_FIXTURE = BREW_FIXTURES_DIR / 'cleanup' / '20261012-1426'
FDESETUP_AUTHRESTART_FIXTURE = FDESETUP_FIXTURES_DIR / 'supportsauthrestart' / '20261103-0746'
FDESETUP_STATUS_FIXTURE = FDESETUP_FIXTURES_DIR / 'status' / '20261103-0745'
MAS_INFO_FIXTURE = MAS_FIXTURES_DIR / 'info' / '20261103-0743'
MAS_OUTDATED_FIXTURE = MAS_FIXTURES_DIR / 'outdated' / '20261103-0743'
MESON_REINSTALL_FIXTURE = BREW_FIXTURES_DIR / 'reinstall' / 'meson' / '20261104-0926'
OUTDATED_FIXTURE = BREW_FIXTURES_DIR / 'outdated' / '20261023-2233'
PORTABLE_RUBY_UPGRADE_FIXTURE = BREW_FIXTURES_DIR / 'upgrade' / 'portable_ruby' / '20261105-1459'
QEMU_DEPS_FIXTURE = BREW_FIXTURES_DIR / 'deps' / '_n' / '__include_build' / 'qemu' / '20261105-1518'
QEMU_UPGRADE_FIXTURE = BREW_FIXTURES_DIR / 'upgrade__yes' / 'qemu' / '20261103-2305'
REPOSITORY_FIXTURE = BREW_FIXTURES_DIR / 'repository' / '20261012-1424'
SW_VERS_PRODUCT_VERSION_FIXTURE = SW_VERS_FIXTURES_DIR / '__productVersion' / '20261103-0819'
UPDATE_FORCE_FIXTURE = BREW_FIXTURES_DIR / 'update' / 'force' / '20261026-1159'
XCODEBUILD_LICENSE_ACCEPT_FIXTURE = XCODEBUILD_FIXTURES_DIR / '_license' / 'accept' / '20261103-0803'
XCODEBUILD_LICENSE_STATUS_FIXTURE = XCODEBUILD_FIXTURES_DIR / '_license' / 'status' / '20261103-0744'


def load_plist_datasets() -> dict[str, dict]:
    """Loads attributes.json from each plist fixture directory."""
    return {
        d.name: json.loads((d / 'attributes.json').read_text(encoding='utf-8'))
        for d in sorted(PLIST_FIXTURES_DIR.iterdir())
        if d.is_dir() and (d / 'attributes.json').is_file()
    }


class TestFixtures(unittest.TestCase):
    """
    Integration tests using real recorded CLI fixtures loaded through SimRunCMD.
    """

    def test_brew_cleanup_fixture(self):
        """Verifies HomeBrew cleanup fixture captures both stdout and stderr."""
        sim = SimRunCMD.load(CLEANUP_FIXTURE)
        self.assertEqual(sim.returncode, 0)
        self.assertTrue(len(sim.stdout) > 0)
        self.assertTrue(len(sim.stderr) > 0)

    def test_brew_deps_qemu_fixture(self):
        """Verifies HomeBrew deps fixture parses dependencies and ignores warning banner."""
        sim = SimRunCMD.load(QEMU_DEPS_FIXTURE)
        self.assertEqual(sim.returncode, 0)
        self.assertEqual(sim.cmd_args, ['brew', 'deps', '-n', '--include-build', 'qemu'])
        self.assertEqual(sim.cmd_str, 'brew deps -n --include-build qemu')
        self.assertIn('--include-build was passed', sim.stderr)

        deps = [
            line.strip() for line in sim.stdout.splitlines()
            if line.strip() and not line.startswith(('Warning:', 'Hide these hints', 'This means'))
        ]
        self.assertEqual(len(deps), 46)
        self.assertIn('cmake', deps)
        self.assertIn('meson', deps)
        self.assertIn('ninja', deps)
        self.assertIn('python@3.15', deps)

        hb = HomeBrew(is_arm=False, dry_run=True)
        with patch.object(hb, 'brew_list', {dep: ['1.0.0'] for dep in deps}), patch(
            'freshenmac.homebrew.RunCMD', return_value=sim,
        ):
            self.assertTrue(hb._resolve_and_update_dependencies('qemu', outdated_formulae={}))

    def test_brew_outdated_fixture(self):
        """Verifies HomeBrew._get_outdated parses real unified brew outdated JSON containing casks and formulae."""
        sim = SimRunCMD.load(OUTDATED_FIXTURE)
        hb = HomeBrew(is_arm=True, dry_run=True)
        outdated = hb._get_outdated(sim.stdout)

        # Casks parsed correctly
        self.assertIn('casks', outdated)
        self.assertIn('freshenmac', outdated['casks'])
        self.assertEqual(outdated['casks']['freshenmac'], '1.1.0 ═▷ 1.1.1')

        # Unpinned formulae parsed correctly
        self.assertIn('formulae', outdated)
        self.assertIn('ada-url', outdated['formulae'])
        self.assertEqual(outdated['formulae']['ada-url'], '4.0.0 ═▷ 4.0.0_1')
        self.assertIn('brotli', outdated['formulae'])
        self.assertEqual(outdated['formulae']['brotli'], '1.2.0 ═▷ 1.2.1')
        self.assertIn('c-ares', outdated['formulae'])
        self.assertEqual(outdated['formulae']['c-ares'], '1.34.8 ═▷ 1.34.9')

        # Pinned packages must be excluded
        self.assertNotIn('qt', outdated['formulae'])
        self.assertNotIn('qt3d', outdated['formulae'])

    def test_brew_reinstall_meson_fixture(self):
        """Verifies HomeBrew reinstall fixture and Intel warning stripping."""
        sim = SimRunCMD.load(MESON_REINSTALL_FIXTURE)
        self.assertEqual(sim.returncode, 0)
        self.assertEqual(sim.cmd_args, ['brew', 'reinstall', 'meson'])
        self.assertEqual(sim.cmd_str, 'brew reinstall meson')
        self.assertIn('✔︎ Bottle meson (1.12.1)', sim.stdout)
        self.assertIn('🍺  /usr/local/Cellar/meson/1.12.1', sim.stdout)
        self.assertIn('Warning: You are using macOS on Intel x86_64', sim.stderr)

        # Intel deprecation notice is cleanly stripped
        stripped_stderr = HomeBrew._strip_intel_warning(sim.stderr)
        self.assertEqual(stripped_stderr, '')

        hb = HomeBrew(is_arm=False, dry_run=False)
        with patch('freshenmac.homebrew.RunCMD', return_value=sim):
            self.assertTrue(hb.reinstall_dependency('meson'))

    def test_brew_repository_fixture(self):
        """Verifies HomeBrew repository fixture output."""
        sim = SimRunCMD.load(REPOSITORY_FIXTURE)
        self.assertEqual(sim.stdout.strip(), '/usr/local/Homebrew')

    def test_brew_update_force_fixture(self):
        """Verifies HomeBrew update --force fixture captures output, versions, and warnings."""
        sim = SimRunCMD.load(UPDATE_FORCE_FIXTURE)
        self.assertEqual(sim.returncode, 0)
        self.assertEqual(sim.cmd_args, ['brew', 'update', '--force'])
        self.assertEqual(sim.cmd_str, 'brew update --force')
        self.assertIn('Updated Homebrew from 7.0.9 (7a056ffd71) to 7.0.11 (c6069d2a94).', sim.stderr)
        self.assertIn('You have 87 outdated formulae and 1 outdated cask installed.', sim.stderr)
        self.assertIn('Warning: Not automatically tapping zarf-dev/tap', sim.stderr)

    def test_brew_upgrade_portable_ruby_fixture(self):
        """Verifies HomeBrew upgrade fixture for bottle installation and Intel warning stripping."""
        sim = SimRunCMD.load(PORTABLE_RUBY_UPGRADE_FIXTURE)
        self.assertEqual(sim.returncode, 0)
        self.assertEqual(sim.cmd_args, ['brew', 'upgrade', 'portable-ruby'])
        self.assertEqual(sim.cmd_str, 'brew upgrade portable-ruby')
        self.assertIn('==> Upgrading portable-ruby', sim.stdout)
        self.assertIn('4.0.7_1 -> 4.0.9', sim.stdout)
        self.assertIn('🍺  /usr/local/Cellar/portable-ruby/4.0.9', sim.stdout)
        self.assertIn('Warning: You are using macOS on Intel x86_64', sim.stderr)

        stripped = HomeBrew._strip_intel_warning(sim.stderr)
        self.assertNotIn('Warning: You are using macOS on Intel x86_64', stripped)
        self.assertIn('✔︎ Bottle portable-ruby (4.0.9)', stripped)

        hb = HomeBrew(is_arm=False, dry_run=False)
        with patch('freshenmac.homebrew.RunCMD', return_value=sim):
            cmd = hb._make_brew_cmd('portable-ruby')
            self.assertEqual(cmd.returncode, 0)
            self.assertIn('🍺  /usr/local/Cellar/portable-ruby/4.0.9', cmd.stdout)

    def test_brew_upgrade_qemu_python_mismatch_fixture(self):
        """Verifies HomeBrew upgrade fixture triggers BrewPythonMismatch diagnosis and recovery."""
        sim = SimRunCMD.load(QEMU_UPGRADE_FIXTURE)
        self.assertEqual(sim.cmd_args, ['brew', 'upgrade', '--yes', 'qemu'])
        self.assertEqual(sim.cmd_str, 'brew upgrade --yes qemu')
        self.assertIn("Could not provide build dependency 'meson==1.11.1'", sim.stdout)
        self.assertIn('Warning: You are using macOS on Intel x86_64', sim.stderr)

        context = FailureInfo.from_runcmd(sim, package='qemu', subsystem='Homebrew')
        self.assertEqual(context.package, 'qemu')
        self.assertEqual(context.subsystem, 'Homebrew')
        self.assertEqual(extract_dependency(context.combined_output()), 'meson')

        hb = HomeBrew(is_arm=False, dry_run=True)
        engine = RecoveryEngine()
        engine.add_rules(rules_homebrew(hb))

        diagnosed = engine.diagnose(context)
        self.assertIsNotNone(diagnosed)
        self.assertEqual(diagnosed[0], 'BrewPythonMismatch')

        with patch.object(hb, 'reinstall_dependency', return_value=True) as mock_reinstall:
            retry_mock = MagicMock(return_value=sim)
            result = engine.recover(context, retry_mock)
            self.assertEqual(result, sim)
            mock_reinstall.assert_called_once_with('meson')
            retry_mock.assert_called_once()

    def test_fdesetup_authrestart_fixture(self):
        """Verifies fdesetup supportsauthrestart fixture enables FileVault bypass restart."""
        sim = SimRunCMD.load(FDESETUP_AUTHRESTART_FIXTURE)
        self.assertEqual(sim.returncode, 0)
        self.assertEqual(sim.cmd_args, ['fdesetup', 'supportsauthrestart'])
        self.assertEqual(sim.cmd_str, 'fdesetup supportsauthrestart')
        self.assertEqual(sim.stdout.strip(), 'true')

        mac = MacOS(dry_run=True)
        with patch('freshenmac.computer.util.RunCMD', return_value=sim):
            self.assertTrue(mac.supports_authrestart)

    def test_fdesetup_status_fixture(self):
        """Verifies fdesetup status fixture correctly identifies active FileVault encryption."""
        sim = SimRunCMD.load(FDESETUP_STATUS_FIXTURE)
        self.assertEqual(sim.returncode, 0)
        self.assertEqual(sim.cmd_args, ['fdesetup', 'status'])
        self.assertEqual(sim.cmd_str, 'fdesetup status')
        self.assertEqual(sim.stdout.strip(), 'FileVault is On.')

        mac = MacOS(dry_run=True)
        with patch('freshenmac.computer.util.RunCMD', return_value=sim):
            self.assertTrue(mac.filevault_enabled)

    def test_mas_info_fixture(self):
        """Verifies AppStore info fixture extracts app size and calculates dynamic timeout."""
        sim = SimRunCMD.load(MAS_INFO_FIXTURE)
        self.assertEqual(sim.returncode, 0)
        self.assertEqual(sim.cmd_args, ['mas', 'info', '1447605203'])
        self.assertEqual(sim.cmd_str, 'mas info 1447605203')
        self.assertIn('CorelDRAW', sim.stdout)
        self.assertIn('Size ▁▁▁▁▁▁▁ 770 MB', sim.stdout)

        app_store = AppStore(dry_run=True, download_speed_mbps=10.0)
        app_store.__dict__['mas_path'] = Path('/usr/local/bin/mas')

        with patch.object(AppStore, 'has_mas', return_value=True), patch(
            'freshenmac.mas.util.RunCMD', return_value=sim,
        ):
            self.assertEqual(app_store.get_app_size_mb('1447605203'), 770.0)
            self.assertEqual(app_store.calculate_timeout('1447605203'), 1070)

    def test_mas_outdated_fixture(self):
        """Verifies mas outdated fixture parses available application upgrades."""
        sim = SimRunCMD.load(MAS_OUTDATED_FIXTURE)
        self.assertEqual(sim.returncode, 0)
        self.assertEqual(sim.cmd_args, ['mas', 'outdated'])
        self.assertEqual(sim.cmd_str, 'mas outdated')
        self.assertIn('1447605203', sim.stdout)
        self.assertIn('CorelDRAW', sim.stdout)

        app_store = AppStore(dry_run=False)
        app_store.__dict__['mas_path'] = Path('/usr/local/bin/mas')

        def fake_runcmd(cmd, *args, **kwargs):
            if 'outdated' in cmd:
                return sim
            if 'upgrade' in cmd:
                return SimRunCMD(stdout='Upgraded CorelDRAW')
            return SimRunCMD(returncode=0)

        with patch('freshenmac.plist.load_preferences', return_value={'no-mas': []}), patch.object(
            AppStore, 'has_mas', return_value=True,
        ), patch(
            'freshenmac.mas.util.is_app_running', return_value=False,
        ), patch(
            'freshenmac.mas.util.RunCMD', side_effect=fake_runcmd,
        ):
            app_store.update()
            self.assertIn('CorelDRAW', app_store.summary['updated'])
            self.assertEqual(app_store.summary['updated']['CorelDRAW'], '2024.0.230 ═▷ 2026.2')

    def test_plist_launch_agent(self):
        """Runs LaunchAgent.build_plist_data() across plist fixture sets and compares against golden truth."""
        for k, v in load_plist_datasets().items():
            with self.subTest(dataset=k):
                fixture_dir = PLIST_FIXTURES_DIR / k
                pref_plist_path = fixture_dir / 'com.panther37.FreshenMac.plist'
                truth_plist = (fixture_dir / 'launch_agent.plist').read_bytes()
                plist_json = json.loads((fixture_dir / 'plist.json').read_text(encoding='utf-8'))
                truth_json = plist_json['launch_agent.plist']

                with patch.dict(
                    'freshenmac.config.PATHS',
                    {'Prefs': {'Default': pref_plist_path, 'User': fixture_dir / 'nonexistent.plist'}},
                ):
                    agent = LaunchAgent(target_script=Path(v['target_script']), dry_run=True)
                    agent.python_bin = '/usr/local/bin/python3'
                    actual_plist = agent.build_plist_data()

                self.assertEqual(plistlib.loads(actual_plist), truth_json, k)
                self.assertEqual(actual_plist, truth_plist, k)

    def test_plist_preferences(self):
        """Runs SavePreferences, load_preferences, and CronSchedule across plist fixture sets against golden truth."""
        for k, v in load_plist_datasets().items():
            with self.subTest(dataset=k):
                fixture_dir = PLIST_FIXTURES_DIR / k
                pref_plist_path = fixture_dir / 'com.panther37.FreshenMac.plist'
                truth_plist = pref_plist_path.read_bytes()
                plist_json = json.loads((fixture_dir / 'plist.json').read_text(encoding='utf-8'))
                truth_json = plist_json['com.panther37.FreshenMac.plist']

                # 1. Input -> SavePreferences -> compare against golden JSON and plist
                pref = SavePreferences({'no-mas': v['no-mas'], 'schedule': v['schedule']}, dry_run=True)
                self.assertEqual(pref.updates, truth_json, k)
                self.assertEqual(plistlib.dumps(pref.updates), truth_plist, k)

                # 2. Golden plist -> load_preferences() -> compare against golden JSON
                with patch.dict(
                    'freshenmac.config.PATHS',
                    {'Prefs': {'Default': pref_plist_path, 'User': fixture_dir / 'nonexistent.plist'}},
                ):
                    loaded = load_preferences()
                self.assertEqual(loaded, truth_json, k)

                # 3. Golden schedule -> CronSchedule.parse().to_cron() -> round-trips back to input cron string
                sched = CronSchedule.parse(loaded['schedule'])
                self.assertEqual(sched.to_cron(), v['schedule'], k)

    def test_plist_staged_updates(self):
        """Runs SoftwareUpdate.check_staged_reboot() against index.plist fixture sets and verifies expected count."""
        for k, v in load_plist_datasets().items():
            with self.subTest(dataset=k):
                fixture_dir = PLIST_FIXTURES_DIR / k
                index_file = fixture_dir / 'index.plist'
                truth_plist = index_file.read_bytes()
                plist_json = json.loads((fixture_dir / 'plist.json').read_text(encoding='utf-8'))
                truth_json = plist_json['index.plist']

                self.assertEqual(plistlib.loads(truth_plist), truth_json, k)

                with patch(
                    'freshenmac.softwareupdate.Path.is_file',
                    return_value=True,
                ), patch(
                    'freshenmac.softwareupdate.open',
                    side_effect=lambda *args, **kwargs: open(index_file, 'rb'),
                ), patch(
                    'freshenmac.softwareupdate.Path.exists',
                    autospec=True,
                    side_effect=lambda p: p.parent.name in v['staged_dirs'],
                ), patch(
                    'freshenmac.softwareupdate.Path.is_dir',
                    autospec=True,
                    side_effect=lambda p: p.name in v['staged_dirs'],
                ):
                    actual_count = SoftwareUpdate.check_staged_reboot()

                self.assertEqual(actual_count, v['staged_expected'], k)

    def test_plist_startup_run(self):
        """Runs StartupRun.agent.build_plist_data() across plist fixture sets and compares against golden truth."""
        for k, v in load_plist_datasets().items():
            with self.subTest(dataset=k):
                fixture_dir = PLIST_FIXTURES_DIR / k
                truth_plist = (fixture_dir / 'com.panther37.FreshenMac_runonce.plist').read_bytes()
                plist_json = json.loads((fixture_dir / 'plist.json').read_text(encoding='utf-8'))
                truth_json = plist_json['com.panther37.FreshenMac_runonce.plist']

                user_home = Path(v['user_home'])
                startup = StartupRun(
                    dry_run=True,
                    launch_agents_dir=user_home / 'Library/LaunchAgents',
                    log_dir=user_home / 'Library/Logs',
                )
                startup.agent.target_script = Path(v['target_script'])
                startup.agent.python_bin = '/usr/local/bin/python3'
                actual_plist = startup.agent.build_plist_data()

                self.assertEqual(plistlib.loads(actual_plist), truth_json, k)
                self.assertEqual(actual_plist, truth_plist, k)

    def test_sw_vers_product_version_fixture(self):
        """Verifies sw_vers --productVersion fixture accurately reports installed macOS version."""
        sim = SimRunCMD.load(SW_VERS_PRODUCT_VERSION_FIXTURE)
        self.assertEqual(sim.returncode, 0)
        self.assertEqual(sim.cmd_args, ['sw_vers', '--productVersion'])
        self.assertEqual(sim.cmd_str, 'sw_vers --productVersion')
        self.assertEqual(sim.stdout.strip(), '15.8.1')

        with patch('freshenmac.softwareupdate.util.RunCMD', return_value=sim):
            swu = SoftwareUpdate(dry_run=True)
            self.assertEqual(str(swu.os_ver['Installed']), '15.8.1')

    def test_xcodebuild_license_accept_fixture(self):
        """Verifies xcodebuild license accept fixture captures non-interactive password requirement error."""
        sim = SimRunCMD.load(XCODEBUILD_LICENSE_ACCEPT_FIXTURE)
        self.assertEqual(sim.returncode, 1)
        self.assertEqual(sim.cmd_args, ['sudo', 'xcodebuild', '-license', 'accept'])
        self.assertEqual(sim.cmd_str, 'sudo xcodebuild -license accept')
        self.assertIn('sudo: a password is required', sim.stderr)

        xc = Xcode(dry_run=False)
        xc.__dict__['xcodebuild_path'] = Path('/usr/bin/xcodebuild')

        with patch('freshenmac.xcode.is_interactive', return_value=False), patch(
            'freshenmac.xcode.RunCMD', return_value=sim,
        ), patch.object(Path, 'is_file', return_value=True):
            self.assertFalse(xc.accept_license())

    def test_xcodebuild_license_status_fixture(self):
        """Verifies xcodebuild license status fixture treats CommandLineTools developer dir as accepted."""
        sim = SimRunCMD.load(XCODEBUILD_LICENSE_STATUS_FIXTURE)
        self.assertEqual(sim.returncode, 1)
        self.assertEqual(sim.cmd_args, ['xcodebuild', '-license', 'status'])
        self.assertEqual(sim.cmd_str, 'xcodebuild -license status')
        self.assertIn('is a command line tools instance', sim.stderr)

        xc = Xcode(dry_run=False)
        xc.__dict__['xcodebuild_path'] = Path('/usr/bin/xcodebuild')

        with patch('freshenmac.xcode.RunCMD', return_value=sim), patch.object(Path, 'is_file', return_value=True):
            self.assertTrue(xc.is_license_accepted)


if __name__ == '__main__':
    unittest.main()
