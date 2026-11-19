#!/usr/bin/env python3
"""
FreshenMac - A Python script to automate macOS updates
"""

import os
import sys

from freshenmac.main import main

FILE_BUILD = 20261115

if __name__ == '__main__':
    try:
        os.chdir('/')
    except OSError as err_ose:
        print(f"\n[Error] FreshenMac.py: Unable to set working dir to root (`/`) but continuing anyway:\n{err_ose}\n")

    if '--trace' in sys.argv:
        sys.exit(0 if main() else 1)

    try:
        success = main()
    except KeyboardInterrupt:
        success = False
        print('\n          User Canceled            \n')
        sys.exit(130)

    if success:
        print('\n          Script finished          \n')
        sys.exit(0)
    else:
        print('\n          Script FAILED!           \n')
        sys.exit(16)
