import unittest
from pathlib import Path

from freshenmac.homebrew import HomeBrew
from test.sim_run_cmd import SimRunCMD

FIXTURES_DIR = Path(__file__).resolve().parent / 'fixtures'
BREW_FIXTURES_DIR = FIXTURES_DIR / 'brew'

OUTDATED_CASK_FIXTURE = BREW_FIXTURES_DIR / 'outdated' / 'cask' / '20261012-1422'
OUTDATED_FORMULA_FIXTURE = BREW_FIXTURES_DIR / 'outdated' / 'formula' / '20261012-1422'
REPOSITORY_FIXTURE = BREW_FIXTURES_DIR / 'repository' / '20261012-1424'
CLEANUP_FIXTURE = BREW_FIXTURES_DIR / 'cleanup' / '20261012-1426'


class TestFixtures(unittest.TestCase):
    """
    Integration tests using real recorded CLI fixtures loaded through SimRunCMD.
    """

    def test_load_all_standard_fixtures(self):
        """Verifies all standard fixtures load successfully through SimRunCMD.load."""
        expected_fixtures = [
            OUTDATED_CASK_FIXTURE,
            OUTDATED_FORMULA_FIXTURE,
            REPOSITORY_FIXTURE,
            CLEANUP_FIXTURE,
        ]
        for fixture_path in expected_fixtures:
            self.assertTrue(fixture_path.is_dir(), f"Fixture directory missing: {fixture_path}")
            sim = SimRunCMD.load(fixture_path)
            self.assertEqual(sim.returncode, 0)
            self.assertTrue(bool(sim))
            self.assertTrue(sim.cmd_str)

    def test_brew_outdated_casks_fixture(self):
        """Verifies HomeBrew._read_outdated_casks parses the real outdated casks fixture."""
        sim = SimRunCMD.load(OUTDATED_CASK_FIXTURE)
        outdated_casks = HomeBrew._read_outdated_casks(sim.stdout)
        self.assertIn('fuse-t', outdated_casks)
        self.assertEqual(outdated_casks['fuse-t'], '1.2.7 ═▷ 1.2.9')

    def test_brew_outdated_formulae_fixture(self):
        """Verifies HomeBrew._read_outdated parses unpinned packages and excludes pinned packages."""
        sim = SimRunCMD.load(OUTDATED_FORMULA_FIXTURE)

        hb = HomeBrew.__new__(HomeBrew)
        hb.debug = 0
        outdated = hb._read_outdated(sim.stdout)

        # Unpinned outdated packages should be included
        self.assertIn('gobject-introspection', outdated)
        self.assertEqual(outdated['gobject-introspection'], '1.86.0_3 ═▷ 1.86.0_4')
        self.assertIn('harfbuzz', outdated)
        self.assertEqual(outdated['harfbuzz'], '14.5.1 ═▷ 14.6.0')
        self.assertIn('simdjson', outdated)
        self.assertEqual(outdated['simdjson'], '5.0.3 ═▷ 5.0.4')

        # Pinned packages must be excluded
        self.assertNotIn('qt', outdated)
        self.assertNotIn('qt3d', outdated)
        self.assertNotIn('qt5compat', outdated)
        self.assertNotIn('qtbase', outdated)
        self.assertNotIn('qtcanvaspainter', outdated)

    def test_brew_repository_fixture(self):
        """Verifies HomeBrew repository fixture output."""
        sim = SimRunCMD.load(REPOSITORY_FIXTURE)
        self.assertEqual(sim.stdout.strip(), '/usr/local/Homebrew')

    def test_brew_cleanup_fixture(self):
        """Verifies HomeBrew cleanup fixture captures both stdout and stderr."""
        sim = SimRunCMD.load(CLEANUP_FIXTURE)
        self.assertEqual(sim.returncode, 0)
        self.assertTrue(len(sim.stdout) > 0)
        self.assertTrue(len(sim.stderr) > 0)


if __name__ == '__main__':
    unittest.main()
