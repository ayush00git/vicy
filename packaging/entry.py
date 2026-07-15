"""PyInstaller entry point for the vicy package."""

import multiprocessing

# In a frozen app, multiprocessing workers re-execute this binary;
# freeze_support() routes those runs to the worker code instead of
# letting them fall through to the CLI parser.
multiprocessing.freeze_support()

from vicy.__main__ import main

main()
