"""CLI entry point.

The socket verbs (--toggle/--status) are handled with stdlib-only
imports so the global hotkey feels instant; the GUI only loads when
actually starting the app.
"""

import sys

from . import config
from .ipc import send_command


def main():
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if arg == "--toggle":
            try:
                print(send_command("toggle"))
                return
            except OSError:
                # Not running — wake up and go straight into recording.
                from .window import run

                run(autostart=True)
                return
        elif arg == "--status":
            try:
                print(send_command("status"))
            except OSError:
                print("not running")
            return
        elif arg == "--install-hotkey":
            from .hotkey import install_hotkey

            install_hotkey(
                sys.argv[2] if len(sys.argv) > 2 else config.HOTKEY_DEFAULT
            )
            return
        else:
            sys.exit(
                f"Unknown option: {arg} "
                "(use --toggle | --status | --install-hotkey [binding])"
            )

    from .window import run

    run()


if __name__ == "__main__":
    main()
