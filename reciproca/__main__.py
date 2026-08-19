"""`python -m reciproca` - the command-line interface.

The GUI keeps its own entry point (`python -m reciproca.gui` or the gui.py
shim at the repo root); this is the terminal one.
"""
import sys

from reciproca.cli import main

sys.exit(main())
