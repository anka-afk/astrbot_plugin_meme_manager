"""Meme manager plugin runtime requirements."""

import sys

if sys.version_info < (3, 12):
    raise RuntimeError("Meme manager requires Python 3.12 or newer.")
