import io
import time
import unittest
from unittest.mock import patch

from freshenmac.util import RunCMD, Wave


class TestSpinner(unittest.TestCase):
    """Unit tests for the counter-clockwise braille Wave class and RunCMD integration."""

    def test_frames_wave(self):
        self.assertFalse(hasattr(Wave, 'FRAMES'))
        self.assertFalse(hasattr(Wave, 'INTRO_FRAMES'))
        self.assertIsInstance(Wave.DELAY, float)
        self.assertGreater(Wave.DELAY, 0)
        self.assertEqual(Wave.WIDTH, 3)
        self.assertGreater(len(Wave.ANIMATION), 0)

        # Verify dynamic sliding produces sliding frames
        frame = ' ' * Wave.WIDTH
        stream_len = len(Wave.ANIMATION)
        generated_frames = []
        for i in range(10):
            frame = (frame + Wave.ANIMATION[i % stream_len])[-Wave.WIDTH:]
            generated_frames.append(frame)
        self.assertEqual(len(generated_frames), 10)

    def test_spinner_context_manager_captured(self):
        # 1. Fast command (<0.5s) exits immediately with empty output
        buf_fast = io.StringIO()
        with Wave("Testing fast command...", stream=buf_fast, force=True):
            time.sleep(0.05)
        self.assertEqual(buf_fast.getvalue(), "")

        # 2. Commands exceeding 0.5s render animation and default clear
        buf_clear = io.StringIO()
        with Wave("Testing default clear...", stream=buf_clear, force=True):
            time.sleep(0.55)

        output_clear = buf_clear.getvalue()
        self.assertIn("Testing default clear...", output_clear)
        self.assertIn("\r\033[K", output_clear)

        # 3. Commands exceeding 0.5s with persist=True render final frame and newline
        buf_persist = io.StringIO()
        with Wave("Testing persist...", stream=buf_persist, force=True, persist=True):
            time.sleep(0.55)
        output_persist = buf_persist.getvalue()
        self.assertIn("Testing persist...", output_persist)
        self.assertIn("\n", output_persist)

    def test_spinner_start_stop(self):
        buf = io.StringIO()
        spinner = Wave("Manual start stop", stream=buf, force=True)
        spinner.start()
        self.assertIsNotNone(spinner._thread)
        self.assertTrue(spinner._thread.is_alive())
        time.sleep(0.55)
        spinner.stop()
        self.assertIsNone(spinner._thread)
        self.assertIn("Manual start stop", buf.getvalue())

    def test_spinner_non_tty_skips_animation(self):
        buf = io.StringIO()
        # io.StringIO has isatty() returning False
        spinner = Wave("Non TTY", stream=buf, force=False)
        spinner.start()
        self.assertIsNone(spinner._thread)
        spinner.stop()
        self.assertEqual(buf.getvalue(), "")

    def test_run_cmd_spinner_auto_trigger(self):
        # A command with timeout >= 30 automatically configures the spinner in _spinner()
        cmd = RunCMD.__new__(RunCMD)
        cmd.interactive = False
        cmd.spinner = None
        cmd.timeout = 30
        cmd.cmd_args = ['echo', 'spinner test']
        with patch('freshenmac.util.Wave') as mock_sp_cls:
            mock_sp = mock_sp_cls.return_value
            mock_sp.__enter__.return_value = mock_sp
            with cmd._spinner() as sp:
                self.assertIsNotNone(sp)
            mock_sp_cls.assert_called_once_with('echo spinner test', direction='right', persist=True)

    def test_run_cmd_spinner_disabled(self):
        cmd = RunCMD.__new__(RunCMD)
        cmd.interactive = False
        cmd.spinner = False
        cmd.timeout = 60
        cmd.cmd_args = ['echo', 'no spin']
        with cmd._spinner() as sp:
            self.assertIsNone(sp)

    def test_run_cmd_spinner_context_disabled(self):
        cmd = RunCMD.__new__(RunCMD)
        cmd.interactive = False
        cmd.spinner = False
        cmd.timeout = 0
        with cmd._spinner() as sp:
            self.assertIsNone(sp)

    def test_run_cmd_spinner_context_enabled(self):
        cmd = RunCMD.__new__(RunCMD)
        cmd.interactive = False
        cmd.spinner = True
        cmd.timeout = 0
        cmd.cmd_args = ['echo', 'on']
        with patch('freshenmac.util.Wave') as mock_sp_cls:
            mock_sp = mock_sp_cls.return_value
            mock_sp.__enter__.return_value = mock_sp
            with cmd._spinner() as sp:
                self.assertIsNotNone(sp)
            mock_sp_cls.assert_called_once_with('echo on', direction='right', persist=True)

    def test_run_cmd_spinner_context_formatting(self):
        cmd_sh = RunCMD.__new__(RunCMD)
        cmd_sh.interactive = False
        cmd_sh.spinner = True
        cmd_sh.timeout = 0
        cmd_sh.cmd_args = ['sh', '-c', 'sleep 1\necho hi']

        with patch('freshenmac.util.Wave') as mock_sp_cls:
            mock_sp = mock_sp_cls.return_value
            mock_sp.__enter__.return_value = mock_sp
            with cmd_sh._spinner():
                pass
            mock_sp_cls.assert_called_once_with('sh -c sleep 1 echo hi', direction='right', persist=True)

        cmd_osa = RunCMD.__new__(RunCMD)
        cmd_osa.interactive = False
        cmd_osa.spinner = True
        cmd_osa.timeout = 0
        cmd_osa.cmd_args = ['osascript', '-e', 'display notification "test"']

        with patch('freshenmac.util.Wave') as mock_sp_cls:
            mock_sp = mock_sp_cls.return_value
            mock_sp.__enter__.return_value = mock_sp
            with cmd_osa._spinner():
                pass
            mock_sp_cls.assert_called_once_with('osascript -e display notification "test"', direction='right', persist=True)


if __name__ == '__main__':
    unittest.main()
