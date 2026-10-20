# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Model-owned numerical controls shared by the Go and Rust API engines."""

import math

from .schema import ModelError


# Automatic pruning strategies: the weighted level that pruning protects. The
# engine sets every threshold itself; the user never enters a number.
PRUNING_STRATEGIES = ("dba", "dbc")
# Editor memory of the chosen basis while pruning is switched off.
PRUNING_PRESETS = {
    "pruning_strategy": "dba",
}


NUMERICAL_DEFAULTS = {
    "area_raster_factor": 0.5,
    "area_max_extent_m": None,
    "area_relative_tolerance": 0.001,
    "area_absolute_tolerance": 1e-12,
    "area_max_depth": 16,
}


def validate_numerical_settings(value):
    result = {**NUMERICAL_DEFAULTS, **value}
    if set(result) != set(NUMERICAL_DEFAULTS):
        raise ModelError("Unsupported numerical parameter.")
    for key, number in result.items():
        if number is None and key == "area_max_extent_m":
            continue
        try:
            finite = type(number) in (int, float) and math.isfinite(number)
        except OverflowError:
            finite = False
        if not finite:
            raise ModelError(f"{key}: enter a finite number.")
        if key == "area_absolute_tolerance":
            if number < 0:
                raise ModelError(f"{key}: enter a nonnegative value.")
        elif number <= 0:
            raise ModelError(f"{key}: enter a positive value.")
        maximum = {"area_raster_factor": 0.5, "area_relative_tolerance": 1,
                   "area_max_depth": 24}.get(key)
        if maximum is not None and number > maximum:
            raise ModelError(f"{key}: enter a value no greater than {maximum}.")
        if key == "area_max_depth" and type(number) is not int:
            raise ModelError(f"{key}: enter an integer.")
    return result
