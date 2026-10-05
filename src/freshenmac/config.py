"""
Configuration settings, path definitions, and operational thresholds for FreshenMac.
"""

from pathlib import Path

# Debug & Execution Limits
DEBUG_LIMIT: int = 10000
DEFAULT_PLIST: str = 'com.panther37.FreshenMac'
DEFAULT_PREFERENCES: Path = Path(f'/Library/Preferences/{DEFAULT_PLIST}.plist')
DEFAULT_TIMEOUT: int = 1800

# Module build number
FILE_BUILD: int = 1

# Idle Thresholds (seconds & minutes)
IDLE_THRESHOLD: dict[str, int] = {
    'min': 60,
    'Wait for Reboot': 120,
}
IDLE_THRESHOLD['sec'] = IDLE_THRESHOLD['min'] * 60

# Backwards-compatible aliases
MAX_IDLE_MIN: int = IDLE_THRESHOLD['min']
MAX_IDLE_SEC: int = IDLE_THRESHOLD['sec']

# Filesystem Paths
PACKAGE_DIR: Path = Path(__file__).resolve().parent

# Program Metadata
PROGRAM_DESCRIPTION: str = 'Updates macOS operating system, App store and homebrew'
PROGRAM_NAME: str = 'FreshenMac'
PROGRAM_VERSION: str = '1.0.0'

PROGRAM_INFO: dict[str, str | int] = {
    'debug_limit': DEBUG_LIMIT,
    'default_plist': DEFAULT_PLIST,
    'description': PROGRAM_DESCRIPTION,
    'name': PROGRAM_NAME,
    'version': PROGRAM_VERSION,
}

REBOOT_WAIT_SECONDS: int = IDLE_THRESHOLD['Wait for Reboot']
SOURCE_SCRIPT: Path = PACKAGE_DIR / f"{PROGRAM_NAME.lower()}.py"
TARGET_DIR: Path = Path(f'/Library/scripts/User/{PROGRAM_NAME}')
TARGET_SCRIPT: Path = TARGET_DIR / f"{PROGRAM_NAME.lower()}.py"
UPTIME_DAYS_LIMIT: int = 15
USER_PREFERENCES: Path = Path.home() / 'Library/Preferences' / f'{DEFAULT_PLIST}.plist'

PATHS: dict[str, Path] = {
    'package_dir': PACKAGE_DIR,
    'preferences': DEFAULT_PREFERENCES,
    'source_script': SOURCE_SCRIPT,
    'target_dir': TARGET_DIR,
    'target_script': TARGET_SCRIPT,
    'user_preferences': USER_PREFERENCES,
}
