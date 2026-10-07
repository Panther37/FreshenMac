#!/usr/bin/env python3
"""
FreshenMac - A Python script to automate macOS updates
"""

import sys

from freshenmac.main import main

FILE_BUILD = 20261006

if __name__ == '__main__':
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
