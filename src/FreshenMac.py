#!/usr/bin/env python3
"""
FreshenMac - A Python script to automate macOS updates
"""

import sys

from freshenmac.main import main

FILE_BUILD = 20261008

if __name__ == '__main__':
    if '--trace' in sys.argv:
        main()
        sys.exit(0)

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
