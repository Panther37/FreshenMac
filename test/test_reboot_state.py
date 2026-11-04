import unittest

from freshenmac.boot import RebootState


class TestRebootState(unittest.TestCase):
    """
    Unit tests for the RebootState data container.

    RebootState manages reboot lists (required, suggested) and truthiness evaluation.
    """

    def test_counter_truthiness(self):
        """Verifies boolean evaluation of zero vs non-zero reboot counter values."""
        # Arrange: Fresh instance starts with all counts at 0 (falsy)
        state = RebootState()
        self.assertFalse(bool(state.required))
        self.assertFalse(bool(state.suggested))
        self.assertFalse(bool(state))

        # Act & Assert: Incrementing required counter makes it truthy
        state.required.append('Update')
        self.assertTrue(bool(state.required))
        self.assertTrue(bool(state))

        # Act & Assert: Incrementing suggested counter makes it truthy
        state.suggested.append('Cask')
        self.assertTrue(bool(state.suggested))

    def test_default_values(self):
        """Verifies default initialization values for counters and reason string."""
        state = RebootState()
        self.assertEqual(state.required, [])
        self.assertEqual(state.suggested, [])


if __name__ == '__main__':
    unittest.main()
