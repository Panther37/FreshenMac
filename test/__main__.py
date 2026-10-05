#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path

src_path = str(Path(__file__).resolve().parent.parent / 'src')
if src_path not in sys.path:
    sys.path.insert(0, src_path)

if __name__ == '__main__':
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=str(__file__).rsplit('/', 1)[0], pattern='test_*.py')
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
