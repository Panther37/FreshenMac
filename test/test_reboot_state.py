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
        self.assertEqual(state.requested, [])
        self.assertEqual(state.suggested, [])

    def test_getitem_case_insensitive(self):
        """Verifies dictionary lookup by key handles uppercase, lowercase, and titlecase keys."""
        state = RebootState(required=['A', 'B'], requested=['C'], suggested=['D', 'E', 'F'])

        # Dictionary access should normalize key names to lowercase internally
        self.assertEqual(state['required'], ['A', 'B'])
        self.assertEqual(state['REQUIRED'], ['A', 'B'])
        self.assertEqual(state['Requested'], ['C'])
        self.assertEqual(state['suggested'], ['D', 'E', 'F'])

    def test_setitem_invalid_key(self):
        """Verifies KeyError is raised when assigning to an unrecognized key."""
        state = RebootState()
        with self.assertRaises(KeyError):
            state['nonexistent_key'] = ['123']

    def test_setitem_valid_keys(self):
        """Verifies dictionary assignment updates counters and reason with case-insensitivity."""
        state = RebootState()

        # Assignment via lowercase key
        state['required'] = ['OS Update']
        self.assertEqual(state.required, ['OS Update'])

        # In-place append via attribute
        state.required.append('App Update')
        self.assertEqual(state.required, ['OS Update', 'App Update'])

        # Assignment via uppercase key
        state['SUGGESTED'] = ['Cask 1', 'Cask 2']
        self.assertEqual(state.suggested, ['Cask 1', 'Cask 2'])


if __name__ == '__main__':
    unittest.main()
