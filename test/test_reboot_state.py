import unittest

from freshenmac.boot import RebootState


class TestRebootState(unittest.TestCase):
    """
    Unit tests for the RebootState data container.

    RebootState manages reboot counts (required, requested, suggested), escalation reasons,
    and supports case-insensitive dictionary access for backwards compatibility.
    """

    def test_counter_truthiness(self):
        """Verifies boolean evaluation of zero vs non-zero reboot counter values."""
        # Arrange: Fresh instance starts with all counts at 0 (falsy)
        state = RebootState()
        self.assertFalse(bool(state.required))
        self.assertFalse(bool(state.requested))
        self.assertFalse(bool(state.suggested))

        # Act & Assert: Incrementing required counter makes it truthy
        state.required += 1
        self.assertTrue(bool(state.required))

        # Act & Assert: Incrementing suggested counter makes it truthy
        state.suggested += 2
        self.assertTrue(bool(state.suggested))

    def test_default_values(self):
        """Verifies default initialization values for counters and reason string."""
        state = RebootState()
        self.assertEqual(state.required, 0)
        self.assertEqual(state.requested, 0)
        self.assertEqual(state.suggested, 0)
        self.assertIsNone(state.reason)

    def test_getitem_case_insensitive(self):
        """Verifies dictionary lookup by key handles uppercase, lowercase, and titlecase keys."""
        state = RebootState(required=2, requested=1, suggested=3, reason="Test")

        # Dictionary access should normalize key names to lowercase internally
        self.assertEqual(state['required'], 2)
        self.assertEqual(state['REQUIRED'], 2)
        self.assertEqual(state['Requested'], 1)
        self.assertEqual(state['suggested'], 3)
        self.assertEqual(state['reason'], "Test")

    def test_setitem_invalid_key(self):
        """Verifies KeyError is raised when assigning to an unrecognized key."""
        state = RebootState()
        with self.assertRaises(KeyError):
            state['nonexistent_key'] = 123

    def test_setitem_valid_keys(self):
        """Verifies dictionary assignment updates counters and reason with case-insensitivity."""
        state = RebootState()

        # Assignment via lowercase key
        state['required'] = 1
        self.assertEqual(state.required, 1)

        # In-place increment via attribute
        state['required'] += 1
        self.assertEqual(state.required, 2)

        # Assignment via uppercase key
        state['SUGGESTED'] = 5
        self.assertEqual(state.suggested, 5)

        # String assignment for reason
        state['reason'] = "Updates applied"
        self.assertEqual(state.reason, "Updates applied")


if __name__ == '__main__':
    unittest.main()
