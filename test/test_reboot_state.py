import unittest

from freshenmac.boot import RebootState


class TestRebootState(unittest.TestCase):
    """Tests for RebootState data container."""

    def test_default_values(self):
        state = RebootState()
        self.assertEqual(state.required, 0)
        self.assertEqual(state.requested, 0)
        self.assertEqual(state.suggested, 0)
        self.assertIsNone(state.reason)

    def test_getitem_case_insensitive(self):
        state = RebootState(required=2, requested=1, suggested=3, reason="Test")
        self.assertEqual(state['required'], 2)
        self.assertEqual(state['REQUIRED'], 2)
        self.assertEqual(state['Requested'], 1)
        self.assertEqual(state['suggested'], 3)
        self.assertEqual(state['reason'], "Test")

    def test_setitem_valid_keys(self):
        state = RebootState()
        state['required'] = 1
        self.assertEqual(state.required, 1)
        state['required'] += 1
        self.assertEqual(state.required, 2)
        state['SUGGESTED'] = 5
        self.assertEqual(state.suggested, 5)
        state['reason'] = "Updates applied"
        self.assertEqual(state.reason, "Updates applied")

    def test_setitem_invalid_key(self):
        state = RebootState()
        with self.assertRaises(KeyError):
            state['nonexistent_key'] = 123

    def test_counter_truthiness(self):
        state = RebootState()
        self.assertFalse(bool(state.required))
        self.assertFalse(bool(state.requested))
        self.assertFalse(bool(state.suggested))

        state.required += 1
        self.assertTrue(bool(state.required))
        state.suggested += 2
        self.assertTrue(bool(state.suggested))


if __name__ == '__main__':
    unittest.main()
