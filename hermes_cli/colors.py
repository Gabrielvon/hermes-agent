"""Shared ANSI color utilities for Hermes CLI modules."""

import os
import sys


def should_use_color(stream=None) -> bool:
    """Return True when colored output is appropriate.

    Respects the NO_COLOR environment variable (https://no-color.org/)
    and TERM=dumb, in addition to the existing TTY check.  When ``stream``
    is provided, that stream's TTY state is checked instead of stdout —
    pass ``sys.stderr`` for diagnostics written to stderr.
    """
    if stream is None:
        stream = sys.stdout
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    if not stream.isatty():
        return False
    return True


class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"


def color(text: str, *codes, stream=None) -> str:
    """Apply color codes to text (only when color output is appropriate).

    ``stream`` selects which stream's TTY state is consulted (default
    stdout); pass ``sys.stderr`` when the colored text goes to stderr.
    """
    if not should_use_color(stream):
        return text
    return "".join(codes) + text + Colors.RESET
