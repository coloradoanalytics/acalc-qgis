# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Wooded-area authoring values; geometry remains a simple XY perimeter."""

import math

from .schema import ModelError


DEFAULT_CANOPY_HEIGHT = 10.0
TREES_HINT = "Use dense, significantly wooded areas that block the view through them. Set ground absorption separately."
TREES_GUIDANCE = (
    "Draw significantly wooded areas where dense vegetation blocks the view through it. "
    "Foliage attenuation uses the simplified method in ISO 9613-2:2024 Annex A.2.2. "
    "Tree height is measured above the terrain; the editable 10 m starting value is an Acalc default, "
    "not an ISO value. Set ground absorption separately."
)


def canopy_height(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ModelError("Tree height must be a finite positive number.") from None
    if not math.isfinite(result) or result <= 0:
        raise ModelError("Tree height must be a finite positive number.")
    return result
