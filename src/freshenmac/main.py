#!/usr/bin/env python3
"""
FreshenMac - A Python script to automate macOS updates
"""

from __future__ import annotations

import argparse
import json
import sys
from time import sleep
from typing import Any

from freshenmac import computer, plist, util
from freshenmac.config import APP_INFO, DEFAULTS

FILE_BUILD = 20261103
REBOOT_STR = {
    'Default': ['DEFAULT'],
    'Force':   ['FORCE', 'YES'],
    'Skip':    ['NO', 'SKIP'],
}


def actions(some_options: argparse.Namespace, logger: Any | None = None) -> bool:
    if some_options.action == 'Uninstall':
        return plist.LaunchAgent(debug_vars=some_options.debug_vars).uninstall()

    if some_options.action == 'Install':
        return plist.LaunchAgent(debug_vars=some_options.debug_vars).check_and_install()

    if some_options.action in ['Spinner', 'Wave']:
        return spinner_test()

    logger = logger or util.Logger(dry_run=some_options.dry_run)
    logger.print_store(
        f"\n[Error] main.actions.some_options.action: No instructions for action: {some_options.action}\n",
        component='Main',
    )
    return False


def main(some_options: argparse.Namespace | list[str] | dict[str, Any] | None = None) -> bool:
    if isinstance(some_options, list):
        options = yield_arguments(some_options)
    elif some_options:
        # Make sure we have all the expected default options
        options = yield_arguments([])  # Do not look at `sys.argv[1:]`
        # Override defaults from above
        overrides = vars(some_options) if isinstance(some_options, argparse.Namespace) else dict(some_options)
        vars(options).update(overrides)
    else:
        options = yield_arguments()
    options = yield_rules(options)
    if options.debug_vars['debug'] > 1:
        print(f"Arguments: {sys.argv}")
        print(f"Options: {vars(options)}")

    logger = util.Logger(dry_run=options.dry_run)
    logger.store('-', component='- Start -')

    if options.action:
        return actions(options, logger)

    if options.schedule is not None:
        if 'default' in options.schedule.lower():
            options.schedule = DEFAULTS['schedule']

        return plist.SavePreferences({'schedule': options.schedule}, debug_vars=options.debug_vars).save()

    save_pref = getattr(options, 'save_pref', None)
    if save_pref is None:
        save_pref = getattr(options, 'save_prefs', None)
    if save_pref is not None:
        return plist.SavePreferences(save_pref, debug_vars=options.debug_vars).save()

    if getattr(options, 'spinner', False):
        return spinner_test()

    credentials = util.TortimerCreds.prompt()

    with util.SudoKeepAlive():
        if options.debug_vars['debug'] > 1:
            print(f"Console user: {credentials.username}")
            return perform_updates(options, credentials.username, credentials.password)

        try:
            return perform_updates(options, credentials.username, credentials.password)

        except Exception as err:
            print(err)
            return False


def perform_updates(
    some_options: argparse.Namespace,
    console_user: str | None = None,
    user_password: str | None = None,
    logger: Any | None = None,
) -> bool:
    logger = logger or util.Logger(dry_run=some_options.dry_run)
    logger.store(
        f"{APP_INFO['Name']} {APP_INFO['Version']} started by {console_user} (options: {vars(some_options)})",
        component='Main',
    )

    if not plist.StartupRun(debug_vars=some_options.debug_vars, logger=logger).agent.remove_script():
        logger.print_store("Failed to clean up previous startup run.  Continuing anyway...", component='Main')

    # Check if ran with no flags (and not --standalone)
    if not some_options.standalone:
        if not plist.LaunchAgent(debug_vars=some_options.debug_vars, logger=logger).check_and_install():
            logger.print_store('Failed to verify or install LaunchAgent.  Continuing anyway...', component='Main')
    mac = computer.MacOS(
        console_user=console_user,
        debug_vars=some_options.debug_vars,
        force_reboot=True if some_options.reboot > 0 else False,
        ignore_mas=some_options.ignore_mas,
        logger=logger,
        user_password=user_password,
    )
    mac.update('all')
    if some_options.reboot > -1:
        mac.check_and_reboot()
    else:
        logger.print_store('Not rebooting due to command-line argument.')
    logger.store(f"{APP_INFO['Name']} update run completed successfully.", component='Main')
    return True


def spinner_test() -> bool:
    try:
        with util.Wave('Testing spinner wave animation'):
            sleep(600)
    except KeyboardInterrupt:
        pass  # Control^C stops the first demo so execution continues to the second demo below

    except Exception as err_spin:
        print(f"{APP_INFO['Name']}: {__name__}.{sys._getframe().f_code.co_name} no arguments (Failed): {err_spin}")
        return False

    try:
        with util.Wave('Testing spinner wave animation with arguments', direction='right', force=True, persist=True):
            sleep(600)
    except KeyboardInterrupt:
        return True

    except Exception as err_spin:
        print(f"{APP_INFO['Name']}: {__name__}.{sys._getframe().f_code.co_name} with arguments (Failed): {err_spin}")
        return False

    return True


def yield_arguments(some_args: list[str] | None = None) -> argparse.Namespace:
    """Processes runtime arguments"""
    parser = argparse.ArgumentParser(prog=APP_INFO['Name'])

    out_group = parser.add_argument_group(
        'Output Options',
        'These options change the output to your terminal',
    )
    out_group.add_argument(
        '-v', '--verbose',
        action='count',
        default=0,
        dest='debug',
        help=(
            'Increase output verbosity.  '
            f"Limits output to {DEFAULTS['Debug Limit']} characters unless specified with `--debug-limit`"
        ),
    )
    out_group.add_argument(
        '-l', '--debug-limit',
        default=0,
        help=(
            'Like -vv but truncates character output.  Only `-vv` or `--debug-limit` is needed.  '
            'Use a negative number for no character limit.'
        ),
        type=int,
    )
    out_group.add_argument(
        '--trace',
        action='store_true',
        help='Maximum verbosity without a character limit.  Equivalent to `--debug-limit=-1 -vvvv`',
    )
    out_group.add_argument(
        '--spinner',
        action='store_true',
        help='Display the spinner animation indefinitely for visual inspection (Ctrl-C to stop)',
    )

    pref_group = parser.add_argument_group(
        'Preference Options',
        'These options change your persistent preferences',
    )
    pref_group.add_argument(
        '-s', '--save-pref', '--save-prefs',
        const='help',
        dest='save_pref',
        help=(
            'Save a preference, e.g. `download-speed=10`, `no-mas=iPhoto`, or `schedule=0 6 * * 1`.  '
            'Run with no value to see all options.'
        ),
        metavar='PREF',
        nargs='?',
    )
    pref_group.add_argument(
        '--schedule', '--cron',
        const='help',
        help='Set or update the LaunchAgent schedule using cron syntax (e.g. `0 6 * * 1` or `0 6 1,15 * *`)',
        metavar='CRON',
        nargs='?',
    )

    cli_group = parser.add_argument_group(
        'CLI Options',
        'Options only available using the command line interface',
    )
    cli_group.add_argument(
        '-a', '--action',
        choices=['Install', 'Spinner', 'Uninstall', 'Wave'],
        help=f"`Install` or `Uninstall` {APP_INFO['Name']}.  Will not run script.  Case insensitive.",
        metavar='VERB',
        type=str.title,
    )
    cli_group.add_argument(
        '-r', '--reboot',
        choices=[a_val for v in REBOOT_STR.values() for a_val in v],  # Add all lists together in REBOOT_STR.values()
        default=0,
        help='Reboot: FORCE, or SKIP reboot at the end of updates, bypassing checks.  Case insensitive.',
        type=str.upper,
    )
    cli_group.add_argument('--dry-run', action='store_true', help='Dry run, for testing')
    cli_group.add_argument(
        '-i', '--ignore-mas', '--skip-mas', '--skip-app',
        help='Skip specified App Store application(s) by name or ID for this execution only.',
        metavar='APP',
        nargs='+',
    )
    cli_group.add_argument(
        '-z', '--standalone', action='store_true', help='Run once without installing anything or setting up a schedule',
    )
    cli_group.add_argument('--version', action='version', version=f"{APP_INFO['Name']} {APP_INFO['Version']}")
    return parser.parse_args(some_args)


def yield_rules(flags: argparse.Namespace) -> argparse.Namespace:
    flags.dry_run = getattr(flags, 'dry_run', False)
    flags.ignore_mas = set(flags.ignore_mas or [])
    # Reboot
    if flags.reboot:
        reboot_check = str(flags.reboot).upper()
        if reboot_check in REBOOT_STR['Force']:
            flags.reboot = 1
        elif reboot_check in REBOOT_STR['Skip']:
            flags.reboot = -1
        else:
            flags.reboot = 0
    # Debug, Trace, Verbose
    if flags.trace:
        flags.debug_vars = {'debug': 9, 'debug_limit': -1}
        del flags.debug, flags.debug_limit
        print(json.dumps({k: list(v) if isinstance(v, set) else v for k, v in vars(flags).items()}, indent=2))
    else:
        if flags.debug_limit and not flags.debug:
            flags.debug = 2
        flags.debug_vars = {'debug': vars(flags).pop('debug'), 'debug_limit': vars(flags).pop('debug_limit')}
    return flags


if __name__ == '__main__':
    sys.exit(0 if main() else 16)
