#!/usr/bin/env python3
"""
FreshenMac - A Python script to automate macOS updates
"""

from __future__ import annotations

import argparse
import getpass
import io
import os
import sys
from time import sleep
from typing import Any

from freshenmac import computer, plist, util
from freshenmac.config import APP_INFO, DEFAULTS

FILE_BUILD = 20261005


def main(some_options: argparse.Namespace | list[str] | dict[str, Any] | None = None) -> bool:
    if isinstance(some_options, list):
        options = yield_arguments(some_options)
    elif some_options:
        # Make sure we have all the expected default options
        options = yield_arguments([])
        # Override defaults from above
        overrides = vars(some_options) if isinstance(some_options, argparse.Namespace) else dict(some_options)
        vars(options).update(overrides)
    else:
        options = yield_arguments()

    if options.uninstall:
        return plist.uninstall_plist()

    if schedule := options.schedule:
        return plist.SavePreferences(
            {'schedule': schedule},
            debug=options.debug,
            debug_limit=options.debug_limit,
        ).save()

    if (save_prefs := options.save_prefs) is not None:
        return plist.SavePreferences(save_prefs, debug=options.debug, debug_limit=options.debug_limit).save()

    if options.spinner:
        return spinner_test()

    user_password = None
    console_user = os.environ.get('SUDO_USER') or getpass.getuser()
    if console_user == 'root':
        try:
            if dev_console_user := util.RunCMD(['stat', '-f', '%Su', '/dev/console']).strip():
                if dev_console_user != 'root':
                    console_user = dev_console_user
        except Exception:
            pass

    if util.is_interactive():
        if not util.RunCMD.can_sudo():
            print("\nAdministrator privileges required for system maintenance. Priming sudo credentials...")
            util.RunCMD(['sudo', '-v'], interactive=True, timeout=120)
        if sys.stdin and hasattr(sys.stdin, 'isatty') and sys.stdin.isatty():
            try:
                user_password = getpass.getpass(f"Password for {console_user} (macOS updates / authrestart): ")
            except (io.UnsupportedOperation, EOFError):
                user_password = None

    with util.SudoKeepAlive():
        if options.debug > 1:
            print(f"Console user: {console_user}")
            print(f"Arguments: {sys.argv}")
            print(f"Options: {vars(options)}")
            return perform_updates(options, console_user, user_password)

        try:
            return perform_updates(options, console_user, user_password)

        except Exception as err:
            print(err)
            return False


def perform_updates(
    some_options: argparse.Namespace,
    console_user: str | None = None,
    user_password: str | None = None,
) -> bool:
    debug_vars = {'debug': some_options.debug, 'debug_limit': some_options.debug_limit}
    dry_run = getattr(some_options, 'dry_run', False)
    logger = util.Logger(dry_run=dry_run)
    logger.store(
        f"{APP_INFO['Name']} {APP_INFO['Version']} started by {console_user} (options: {vars(some_options)})",
        component='Main',
    )

    if not plist.cleanup_startup_run(logger=logger):
        logger.print_store("Failed to clean up previous startup run.  Continuing anyway...", component='Main')

    if some_options.update:
        plist.update_installed_script(**debug_vars)
    # Check if ran with no flags
    if len(sys.argv) == 1:
        plist.check_plist()
    mac = computer.MacOS(
        console_user=console_user,
        force_reboot=some_options.force_reboot,
        ignore_mas=some_options.ignore_mas,
        logger=logger,
        user_password=user_password,
        **debug_vars,
    )
    mac.update('all')
    if some_options.reboot:
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
        pass
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


def yield_arguments(some_args: list[str] | None = None):
    """Processes runtime arguments"""
    if some_args is None:
        some_args = sys.argv[1:]

    parser = argparse.ArgumentParser(prog=APP_INFO['Name'])

    out_group = parser.add_argument_group(
        "Output Options",
        "These options change the output to your terminal"
    )
    out_group.add_argument(
        '-v', '--verbose', dest='debug', action='count', default=0,
        help='Increase output verbosity. '
             f"Limits output to {DEFAULTS['Debug Limit']} characters unless specified with `--debug-limit`",
    )
    out_group.add_argument(
        '-l', '--debug-limit', type=int, default=0,
        help=(
            'Like -vv but truncates character output.  Only `-vv` or `--debug-limit` is needed.  '
            'Use a negative number for no character limit.'
        ),
    )
    out_group.add_argument(
        '--trace', action='store_true', default=False,
        help=f"Maximum verbosity without a character limit.  Equivalent to `--debug-limit=-1 -vvvv`",
    )
    out_group.add_argument(
        '--spinner', action='store_true', default=False,
        help='Display the spinner animation indefinitely for visual inspection (Ctrl-C to stop)',
    )

    pref_group = parser.add_argument_group(
        "Preference Options",
        "These options change your persistent preferences"
    )
    pref_group.add_argument(
        '-s', '--save-prefs', nargs='?', const='', default=None,
        metavar='PREF',
        help=(
            'Save a preference, e.g. "schedule=0 6 * * 1" or "no-mas=iPhoto 408981381". '
            'Run with no value to see all options.'
        ),
    )
    pref_group.add_argument(
        '--schedule', '--cron', dest='schedule', type=str, default=None,
        metavar='CRON',
        help='Set or update the LaunchAgent schedule using cron syntax (e.g. "0 6 * * 1" or "0 6 1,15 * *")',
    )

    boot_group = parser.add_argument_group(
        "Reboot Options",
        "These options change if your computer is going to reboot or not.  Pick one, don't use them both."
    )
    boot_group.add_argument(
        '-f', '--force-reboot', action='store_true', default=False,
        help='Force a reboot at the end of updates, bypassing requirement checks',
    )
    boot_group.add_argument(
        '-r', '--no-reboot', dest='reboot', action='store_false', default=True,
        help='Do not attempt to reboot',
    )

    update_group = parser.add_mutually_exclusive_group()
    update_group.add_argument(
        '-u', '--update', dest='update', action='store_true', default=None,
        help=f"Update installed {APP_INFO['Name']} if running files have a higher FILE_BUILD",
    )
    update_group.add_argument(
        '--no-update', dest='update', action='store_false',
        help=f"Do not update installed {APP_INFO['Name']}",
    )

    cli_group = parser.add_argument_group(
        "CLI Options",
        "Options only available using the command line interface"
    )
    cli_group.add_argument(
        '-i', '--ignore-mas', '--skip-mas', '--ignore-app-store', '--skip-app-store',
        dest='ignore_mas', nargs='+', default=None,
        metavar='APP',
        help='Skip specified App Store application(s) by name or ID for this execution only.',
    )
    cli_group.add_argument(
        '--uninstall', action='store_true', default=False,
        help=f"Remove {APP_INFO['Name']} service from startup (unloads and deletes launch agent)",
    )

    args = parser.parse_args(some_args)
    if getattr(args, 'trace', False):
        args.debug = 9
        args.debug_limit = -1
    elif getattr(args, 'debug_limit', False) and not args.debug:
        args.debug = 2
    if getattr(args, 'update', None) is None:
        args.update = util.is_interactive()
    return args


if __name__ == '__main__':
    sys.exit(0 if main() else 16)
