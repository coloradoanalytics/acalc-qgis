# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Explicit input contract for the ISO 9613 calculation engine.

These shared model parameters are edited in the calculation windows. Variants
only select the layers to evaluate.
"""

import math

from .schema import ModelError
from .api_parameters import NUMERICAL_DEFAULTS, PRUNING_PRESETS, PRUNING_STRATEGIES, validate_numerical_settings


ISO_ENGINE = ENGINE_ID = "iso9613-2:2024"
REFLECTION_DEFAULTS = {
    "reflection_order": 3,
    "building_reflection_coefficient": 0.9,
    "barrier_reflection_coefficient": 0.9,
}


def default_calculation_settings():
    return {
        "schema_version": 2,
        "temperature_c": 10.0,
        "relative_humidity_percent": 70.0,
        "pressure_kpa": 101.325,
        "ground_factor": 0.0,
        "meteorology": "downwind",
        "ground_method": "general",
        "c0_db": 0.0,
        **REFLECTION_DEFAULTS,
        **NUMERICAL_DEFAULTS,
        "pruning_strategy": None,
        "pruning_values": dict(PRUNING_PRESETS),
    }


default_settings = default_calculation_settings


# Version-1 controls of the retired in-plugin solver. The API engines define no
# equivalent; old results are already stale because the engine changed.
RETIRED_V1_SETTINGS = ("ground_elevation_m", "lateral_diffraction", "source_directivity", "line_segment_max_m",
                       "reflection_search_distance_m", "subdivision_tolerance_db", "contribution_cutoff_db")


def _upgrade_v1(value):
    """Carry the shared physical and reflection inputs of version 1 forward."""
    value = {key: item for key, item in value.items() if key not in RETIRED_V1_SETTINGS}
    defaults = default_calculation_settings()
    # Early version-1 files predate the reflection controls.
    for key in ("ground_method", *REFLECTION_DEFAULTS, *NUMERICAL_DEFAULTS, "pruning_strategy", "pruning_values"):
        value.setdefault(key, defaults[key])
    value["schema_version"] = 2
    return value


def validate_settings(value):
    """Validate the native Go/Rust model parameter format (version 2).

    Version-1 settings are upgraded in memory; saving persists version 2.
    """
    if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
            or value["schema_version"] not in (1, 2)):
        raise ModelError("Unsupported calculation settings version.")

    value = _upgrade_v1(value) if value["schema_version"] == 1 else dict(value)
    # Discard removed controls when reading saved native project settings.
    # Saving the normalized settings removes it from the project permanently.
    value.pop("receiver_omission_budget_db", None)
    value.pop("receiver_margin_db", None)
    # The numeric absolute cutoff was replaced by automatic pruning. A project
    # that had it switched on keeps pruning, now on the dBA strategy.
    legacy_cutoff = value.pop("minimum_contribution_db", None)
    if "pruning_strategy" not in value:
        value["pruning_strategy"] = None if legacy_cutoff is None else "dba"
    if value["pruning_strategy"] not in (None, *PRUNING_STRATEGIES):
        raise ModelError("pruning_strategy: choose dBA, dBC or no pruning.")
    # Optional editor memory in the native format; omitted by projects saved
    # before pruning switches were added. Never sent to either solver.
    remembered = value.setdefault("pruning_values", dict(PRUNING_PRESETS))
    if isinstance(remembered, dict):
        remembered = {key: item for key, item in remembered.items()
                      if key not in ("receiver_omission_budget_db", "receiver_margin_db", "minimum_contribution_db")}
        remembered.setdefault("pruning_strategy", PRUNING_PRESETS["pruning_strategy"])
    if not isinstance(remembered, dict) or set(remembered) != set(PRUNING_PRESETS):
        raise ModelError("Invalid remembered pruning values.")
    if remembered["pruning_strategy"] not in PRUNING_STRATEGIES:
        raise ModelError("Invalid remembered pruning basis.")
    value["pruning_values"] = dict(remembered)
    expected = set(default_calculation_settings())
    missing, unknown = expected - set(value), set(value) - expected
    if missing:
        raise ModelError("Missing calculation settings: " + ", ".join(sorted(missing)) + ".")
    if unknown:
        raise ModelError("Unsupported calculation settings: " + ", ".join(sorted(unknown)) + ".")
    limits = {
        "temperature_c": (-20.0, 50.0),
        "relative_humidity_percent": (10.0, 100.0),
        "pressure_kpa": (50.0, 120.0),
        "ground_factor": (0.0, 1.0),
        "c0_db": (0.0, 5.0),
        "building_reflection_coefficient": (0.0, 1.0),
        "barrier_reflection_coefficient": (0.0, 1.0),
    }
    for key, (minimum, maximum) in limits.items():
        number = value[key]
        try:
            finite = type(number) in (int, float) and math.isfinite(number)
        except OverflowError:
            finite = False
        if not finite:
            raise ModelError(f"{key}: enter a finite number.")
        if ((minimum is not None and number < minimum)
                or (maximum is not None and number > maximum)):
            bounds = f"between {minimum:g} and {maximum:g}" if maximum is not None else f"at least {minimum:g}"
            raise ModelError(f"{key}: enter a value {bounds}.")
    if value["meteorology"] not in ("downwind", "long_term"):
        raise ModelError("Meteorology must be downwind or long_term.")
    if value["ground_method"] not in ("general", "mean_path_height"):
        raise ModelError("Ground method must be general or mean_path_height.")
    value.update(validate_numerical_settings({key: value[key] for key in NUMERICAL_DEFAULTS}))
    if type(value["reflection_order"]) is not int or not 0 <= value["reflection_order"] <= 5:
        raise ModelError("reflection_order: enter an integer between 0 and 5 (0 disables reflections).")
    return value
