#!/usr/bin/env python3
"""
FreshenMac - A Python script to automate macOS updates
"""

import sys

from freshenmac.main import main

if __name__ == '__main__':
    try:
        success = main()
    except KeyboardInterrupt:
        success = False
        print('\n          User Cancelled           \n')
        sys.exit(130)
    if success:
        print('\n          Script finished          \n')
        sys.exit(0)
    else:
        print('\n          Script FAILED!           \n')
        sys.exit(16)
