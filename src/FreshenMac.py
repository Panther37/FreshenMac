#!/usr/bin/env python3
"""
FreshenMac - A Python script to automate macOS updates
"""

from pathlib import Path
import sys

# Ensure src/ is in sys.path when executed directly from the repo
sys.path.insert(0, str(Path(__file__).resolve().parent / 'src'))

from freshenmac.main import main

if __name__ == '__main__':
    sys.exit(0 if main() else 1)
