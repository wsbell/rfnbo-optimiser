"""Explicit floating-point tolerances (CLAUDE.md conventions)."""

from __future__ import annotations

import math

REL = 1e-6
ABS_KG = 1e-6
THRESHOLD_EPS = 1e-9
# Rounding used to rank GHG for write-off order, so that equal values tie deterministically.
RANK_DECIMALS = 9


def close(a: float, b: float, rel: float = REL, abs_: float = ABS_KG) -> bool:
    return math.isclose(a, b, rel_tol=rel, abs_tol=abs_)


def at_most(value: float, limit: float, rel: float = REL, abs_: float = ABS_KG) -> bool:
    """``value ≤ limit`` allowing for rounding in mass and energy sums."""
    return value <= limit + max(abs_, rel * abs(limit))


def meets_threshold(ghg: float, limit: float) -> bool:
    """GHG threshold checks: ``ghg ≤ limit + 1e-9``."""
    return ghg <= limit + THRESHOLD_EPS
