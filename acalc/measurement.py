# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Authored sound-pressure levels for Measurement receivers."""

import math

from .schema import ModelError
from .spectra import OCTAVE_BANDS
from .decibels import rounded_db


MEASUREMENT_FIELDS = tuple(f"level_{band}_db" for band in OCTAVE_BANDS)
MEASUREMENT_BROADBAND_FIELDS = ("broadband_a_db",)
# Retain the old Z column when reading existing GeoPackages.
ALL_MEASUREMENT_FIELDS = MEASUREMENT_BROADBAND_FIELDS + ("broadband_z_db",) + MEASUREMENT_FIELDS
A_WEIGHTING_DB = (-26.2, -16.1, -8.6, -3.2, 0.0, 1.2, 1.0, -1.1)


def _finite_level(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def broadband_from_octaves(levels):
    """Return the A-weighted dBA total from entered 63–8000 Hz octave levels.

    Unknown bands contribute no value to this sum. With no entered bands,
    the broadband value is left for independent manual entry.
    """
    if len(levels) != len(OCTAVE_BANDS):
        raise ModelError("A Measurement needs eight octave-band positions.")
    if all(level is None for level in levels):
        return None
    if any(level is not None and not _finite_level(level) for level in levels):
        raise ModelError("Octave levels must be finite dB values or unknown.")
    weighted = [level + weight for level, weight in zip(levels, A_WEIGHTING_DB) if level is not None]
    if not all(math.isfinite(level) for level in weighted):
        raise ModelError("The octave levels exceed the supported numeric range.")

    def total(values):
        peak = max(values)
        result = peak + 10 * math.log10(math.fsum(10 ** ((level - peak) / 10) for level in values))
        if not math.isfinite(result):
            raise ModelError("The octave levels exceed the supported numeric range.")
        return result

    return total(weighted)


def resolved_measurement(values):
    """Validate authored values and derive dBA when octaves exist."""
    result = dict(values)
    dba = broadband_from_octaves([result.get(field) for field in MEASUREMENT_FIELDS])
    if dba is not None:
        result["broadband_a_db"] = rounded_db(dba)
    return validate_measurement(result)


def validate_measurement(values):
    """NULL means unknown; numeric zero is an actual 0 dB measurement."""
    result = {}
    for field in ALL_MEASUREMENT_FIELDS:
        value = values.get(field)
        if value is None:
            result[field] = None
        elif not _finite_level(value):
            raise ModelError(f"{field}: enter a finite dB level or leave it blank.")
        else:
            result[field] = float(value)
    return result
