"""
Configuration settings, path definitions, and operational thresholds for FreshenMac.
"""

from pathlib import Path
from typing import Any

# Program Metadata
APP_INFO: dict[str, str] = {
    'Author':      'Matt Logan',
    'Description': 'Updates macOS operating system, App store, and homebrew',
    'Email':       'matt@panther37.com',
    'Name':        'FreshenMac',
    'Version':     '1.0.0',
}

# Debug & Execution Limits
DEFAULTS: dict[str, Any] = {
    'Debug Limit': 10000,
    'no-mas':      ['408981381', 'iPhoto'],  # Skip these App Store Updates
    'plist':       f"com.panther37.{APP_INFO['Name']}",
    'schedule': {
            'Hour':    2,
            'Minute':  0,
            'Weekday': 0,
        },
}

# Module build number
FILE_BUILD = 20261005

# Idle Thresholds (seconds)
IDLE_THRESHOLD: dict[str, int] = {
    'Minutes':        60,
    'Reboot Wait':    120,
    'Uptime in Days': 15,
}
IDLE_THRESHOLD['Seconds'] = IDLE_THRESHOLD['Minutes'] * 60

# Filesystem Paths
PATHS: dict[str, dict[str, Path]] = {
    'Dir':    {
        'Package': Path(__file__).resolve().parent,
        'Target':  Path(f"/Library/scripts/User/{APP_INFO['Name'].lower()}"),
    },
    'Log':    {
        'System': Path(f"/Library/Logs/{APP_INFO['Name']}.log"),
        'User':   Path.home() / 'Library/Logs' / f"{APP_INFO['Name']}.log",
    },
    'Prefs':  {
        'Default': Path(f"/Library/Preferences/{DEFAULTS['plist']}.plist"),
        'User':    Path.home() / 'Library/Preferences' / f"{DEFAULTS['plist']}.plist",
    },
    'Script': {
        'Source': Path(__file__).resolve().parent / 'main.py',
        'Target': Path(f"/Library/scripts/User/{APP_INFO['Name'].lower()}/main.py"),
    },
}

# Timeout Values (in seconds)
TIMEOUT: dict[str, int] = {
    'Boot':      1800,
    'App Store': 1800,
    'Upgrade':   10800,
}
