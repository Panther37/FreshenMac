import json
import plistlib
import unittest
from pathlib import Path
from unittest.mock import patch

from freshenmac.homebrew import HomeBrew
from freshenmac.plist import CronSchedule, LaunchAgent, SavePreferences, StartupRun, load_preferences
from freshenmac.softwareupdate import SoftwareUpdate
from test.sim_run_cmd import SimRunCMD

FIXTURES_DIR = Path(__file__).resolve().parent / 'fixtures'
BREW_FIXTURES_DIR = FIXTURES_DIR / 'brew'
PLIST_FIXTURES_DIR = FIXTURES_DIR / 'plist'

CLEANUP_FIXTURE = BREW_FIXTURES_DIR / 'cleanup' / '20261012-1426'
OUTDATED_FIXTURE = BREW_FIXTURES_DIR / 'outdated' / '20261023-2233'
REPOSITORY_FIXTURE = BREW_FIXTURES_DIR / 'repository' / '20261012-1424'
UPDATE_FORCE_FIXTURE = BREW_FIXTURES_DIR / 'update' / 'force' / '20261026-1159'


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


if __name__ == '__main__':
    unittest.main()
