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
    'Version':     '2.1.0',
}

# Debug & Execution Limits
DEFAULTS: dict[str, Any] = {
    'Debug Limit':           10000,
    'Download Speed Buffer': 0.25,  # 25% safety buffer on calculated download times
    'Download Speed Mbps':   5.0,  # Default: 5 Mbps (Slow DSL / Rural / Hotspot)
    'no-mas':                ['408981381', 'iPhoto'],  # Skip these App Store Updates
    'plist':                 f"com.panther37.{APP_INFO['Name']}",
    'schedule':              {  # noqa: P37-SORT_CRON
        'Minute':  0,
        'Hour':    2,
        'Weekday': 0,
    },
}

# Module build number
FILE_BUILD = 20261109

# Idle Thresholds (seconds)
IDLE_THRESHOLD: dict[str, int] = {
    'Minutes':        60,
    'Reboot Wait':    120,
    'Uptime in Days': 30,
}
IDLE_THRESHOLD['Seconds'] = IDLE_THRESHOLD['Minutes'] * 60

# Filesystem Paths
PATHS: dict[str, dict[str, Any]] = {
    'Log':    {
        'System': Path(f"/Library/Logs/{APP_INFO['Name']}.log"),
        'User':   Path.home() / 'Library/Logs' / f"{APP_INFO['Name']}.log",
    },
    'Prefs':  {
        'Default': Path(f"/Library/Preferences/{DEFAULTS['plist']}.plist"),
        'User':    Path.home() / 'Library/Preferences' / f"{DEFAULTS['plist']}.plist",
    },
    'Script': {
        'Source': Path(__file__).resolve().parent.parent / 'FreshenMac.py',
        'Target': [
            Path('/opt/homebrew/bin/freshenmac'),
            Path('/usr/local/bin/freshenmac'),
        ],
    },
}

# Timeout Values (in seconds)
TIMEOUT: dict[str, dict[str, int]] = {
    'All':             {
        'Maximum': 10800,  # max(list(x for k, v in TIMEOUT.items() if k != 'All' for x in v.values()))
        'Minimum': 300,  # min(list(x for k, v in TIMEOUT.items() if k != 'All' for x in v.values()))
    },
    'App Store':       {
        'Base Overhead': 300,  # (5 minutes) Baseline buffer for authentication, licensing, and package verification
        'Unknown Size':  1800,  # (30 minutes) Fallback timeout when download size cannot be retrieved
    },
    'Homebrew':        {
        'Base Overhead': 300,  # (5 minutes) Baseline buffer for environment setup, licensing, and post-install
        'Cask':          1800,  # (30 minutes) Fallback timeout for cask downloads and installations
        'CMake Build':   10800,  # (3 hours) Dedicated compile timeout for source packages requiring cmake
        'Formula':       1800,  # (30 minutes) Timeout for standard pre-compiled bottle upgrades
    },
    'Software Update': {
        'Base Overhead': 300,  # (5 minutes) Baseline buffer for catalog check, authentication, and package verification
        'Default':       1800,  # (30 minutes) Fallback timeout when download size cannot be retrieved
    },
}
