"""Text normalisation shared by locators, detectors and the in-page selector engines.

The JavaScript engines in ``surface/js/engines.js`` implement the same normalisation;
keep the two in sync (covered by ``tests/test_textutil.py`` + browser tests).
"""

from __future__ import annotations

import re

_WS = re.compile(r"\s+")
_TRAILING_PUNCT = re.compile(r"[\s:*]+$")


def collapse_ws(text: str) -> str:
    return _WS.sub(" ", text.replace("\u00a0", " ")).strip()


def normalize_label(text: str) -> str:
    """Canonical comparison form for UI labels: whitespace-collapsed, case-folded,
    trailing ``:``/``*`` removed (``"Member Number: *"`` == ``"member number"``)."""
    return _TRAILING_PUNCT.sub("", collapse_ws(text)).casefold()


def labels_equal(a: str, b: str) -> bool:
    return normalize_label(a) == normalize_label(b)
