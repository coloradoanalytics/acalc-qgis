# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Object authoring options and common roof elevations."""
import math
from .schema import ModelError

HEIGHT_MODES = {"average": "Relative to average Z", "highest": "Relative to highest Z",
                "lowest": "Relative to lowest Z", "absolute": "Absolute",
                "individual": "Relative per point", "absolute_individual": "Absolute per point"}
PER_POINT_HEIGHT_MODES = {"individual", "absolute_individual"}
ABSOLUTE_HEIGHT_MODES = {"absolute", "absolute_individual"}
GROUND_MODES = {"per_point": "Specify per point", "uniform": "Uniform"}


def feature_value(feature, name, default=None):
    if feature is None or feature.fields().indexFromName(name) < 0:
        return default
    value = feature[name]
    return default if value is None or str(value) == "NULL" else value


def building_heights(rows, mode, height):
    """Resolve stored Z/M rows, where M always means height above stored Z."""
    if mode not in HEIGHT_MODES:
        raise ModelError("Choose a building height mode.")
    if mode in PER_POINT_HEIGHT_MODES:
        return rows
    if not math.isfinite(height) or (mode != "absolute" and height < 0):
        raise ModelError("H must be finite, and relative heights must be non-negative.")
    if not rows:
        raise ModelError("Enter at least three perimeter vertices.")
    zs = [row[3] for row in rows]
    base = (0.0 if mode == "absolute" else min(zs) if mode == "lowest" else
            max(zs) if mode == "highest" else sum(z / len(zs) for z in zs))
    roof = base + height
    if not math.isfinite(roof):
        raise ModelError("The roof elevation must be finite.")
    if any(roof < z for z in zs):
        raise ModelError("The shared roof elevation is below a ground vertex. Increase the building height.")
    return [(ring, x, y, z, roof - z) for ring, x, y, z, _ in rows]


def building_editor_rows(rows, mode, height):
    """Convert explicit editor heights/elevations to nonnegative stored M."""
    if mode not in HEIGHT_MODES:
        raise ModelError("Choose a building height mode.")
    if mode in ABSOLUTE_HEIGHT_MODES:
        converted = []
        for ring, x, y, z, value in rows:
            roof = height if mode == "absolute" else value
            if not math.isfinite(roof):
                raise ModelError("Every roof elevation must be finite.")
            relative = roof - z
            if not math.isfinite(relative) or relative < 0:
                raise ModelError("A roof elevation is below its ground point. Increase the roof elevation.")
            converted.append((ring, x, y, z, relative))
        return converted
    return building_heights(rows, mode, height)
