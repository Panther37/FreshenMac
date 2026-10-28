import unittest
from pathlib import Path

from freshenmac.homebrew import HomeBrew
from test.sim_run_cmd import SimRunCMD

FIXTURES_DIR = Path(__file__).resolve().parent / 'fixtures'
BREW_FIXTURES_DIR = FIXTURES_DIR / 'brew'

CLEANUP_FIXTURE = BREW_FIXTURES_DIR / 'cleanup' / '20261012-1426'
OUTDATED_FIXTURE = BREW_FIXTURES_DIR / 'outdated' / '20261023-2233'
REPOSITORY_FIXTURE = BREW_FIXTURES_DIR / 'repository' / '20261012-1424'
UPDATE_FORCE_FIXTURE = BREW_FIXTURES_DIR / 'update' / 'force' / '20261026-1159'


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

    def test_load_all_standard_fixtures(self):
        """Verifies all standard fixtures load successfully through SimRunCMD.load."""
        expected_fixtures = [
            CLEANUP_FIXTURE,
            OUTDATED_FIXTURE,
            REPOSITORY_FIXTURE,
            UPDATE_FORCE_FIXTURE,
        ]
        for fixture_path in expected_fixtures:
            self.assertTrue(fixture_path.is_dir(), f"Fixture directory missing: {fixture_path}")
            sim = SimRunCMD.load(fixture_path)
            self.assertEqual(sim.returncode, 0)
            self.assertTrue(bool(sim))
            self.assertTrue(sim.cmd_str)


if __name__ == '__main__':
    unittest.main()
