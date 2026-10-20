# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Rounding at user-facing decibel result boundaries."""

from decimal import Decimal, ROUND_HALF_UP, localcontext
import math

from .schema import ModelError


def rounded_db(value):
    """Round a finite calculated decibel value to one decimal, half away from zero."""
    if type(value) not in (int, float):
        raise ModelError("A calculated decibel level must be a finite number.")
    try:
        if not math.isfinite(value):
            raise ModelError("A calculated decibel level must be a finite number.")
    except OverflowError as error:
        raise ModelError("A calculated decibel level exceeds the supported numeric range.") from error
    number = Decimal(str(value))
    with localcontext() as context:
        context.prec = max(28, number.adjusted() + 3, len(number.as_tuple().digits) + 2)
        result = float(number.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
    if not math.isfinite(result):
        raise ModelError("A calculated decibel level exceeds the supported numeric range.")
    return 0.0 if result == 0 else result
