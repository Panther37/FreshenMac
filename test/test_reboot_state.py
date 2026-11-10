import unittest

from freshenmac.boot import RebootState


class TestRebootState(unittest.TestCase):
    """
    Unit tests for the RebootState data container.

    RebootState manages reboot lists (required, suggested) and truthiness evaluation.
    """

    def test_counter_truthiness(self):
        """Verifies truthiness evaluation when required or suggested updates are populated."""
        # Populating required list makes state truthy
        state_required = RebootState()
        state_required.required.append('Update')
        self.assertTrue(bool(state_required.required))
        self.assertTrue(bool(state_required))

        # Populating suggested list makes state truthy
        state_suggested = RebootState()
        state_suggested.suggested.append('Cask')
        self.assertTrue(bool(state_suggested.suggested))
        self.assertTrue(bool(state_suggested))

    def test_default_values(self):
        """Verifies default initialization values for counters and truthiness evaluation."""
        state = RebootState()
        self.assertEqual(state.required, [])
        self.assertEqual(state.suggested, [])
        self.assertFalse(bool(state))


if __name__ == '__main__':
    unittest.main()
