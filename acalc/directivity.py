# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Versioned directionality authoring, generators and orientation geometry.

This module does not evaluate ISO propagation. Tables retain their level basis;
preview sampling is explicitly nearest-direction and never normalizes values.
Local axes are +X front, +Y left, +Z top. World axes are grid east/north/up.
"""

import base64
from copy import deepcopy
import csv
import hashlib
import io
import json
import math
from pathlib import Path

from .schema import ModelError
from .spectra import OCTAVE_BANDS
from .decibels import rounded_db

IDENTITY = [[1., 0., 0.], [0., 1., 0.], [0., 0., 1.]]
AXES = {"Up": [0., 0., 1.], "Down": [0., 0., -1.], "North": [0., 1., 0.],
        "South": [0., -1., 0.], "East": [1., 0., 0.], "West": [-1., 0., 0.]}
MAX_CELLS = 300000
MAX_ATTACHMENT = 10 * 1024 * 1024
CALCULATION_LIMIT = "Assigned directionality is not yet supported by the calculation engine. Clear the assignment to calculate an omnidirectional source."


def finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def direction(azimuth, elevation):
    a, e = math.radians(azimuth), math.radians(elevation)
    return [math.cos(e) * math.cos(a), math.cos(e) * math.sin(a), math.sin(e)]


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def cross(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def transform(matrix, vector):
    return [dot(row, vector) for row in matrix]


def compose(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def rotation(axis, degrees):
    norm = math.sqrt(dot(axis, axis))
    if not finite(degrees) or norm <= 0:
        raise ModelError("A rotation needs a finite angle and nonzero axis.")
    x, y, z = [v / norm for v in axis]
    c, s = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
    t = 1 - c
    return [[t*x*x+c, t*x*y-s*z, t*x*z+s*y], [t*x*y+s*z, t*y*y+c, t*y*z-s*x],
            [t*x*z-s*y, t*y*z+s*x, t*z*z+c]]


def align(principal, target):
    """Shortest rotation; deterministic roll, including opposite directions."""
    c = max(-1, min(1, dot(principal, target)))
    if c > 1 - 1e-14:
        return deepcopy(IDENTITY)
    axis = cross(principal, target)
    if c < -1 + 1e-14:
        axis = cross(principal, [0, 0, 1] if abs(principal[2]) < .9 else [0, 1, 0])
    return rotation(axis, math.degrees(math.acos(c)))


def principal_vector(value):
    return direction(value["principal"]["azimuth_deg"], value["principal"]["elevation_deg"])


def validate_orientation(value):
    if (not isinstance(value, list) or len(value) != 3 or
            any(not isinstance(row, list) or len(row) != 3 or not all(finite(v) for v in row) for row in value)):
        raise ModelError("Orientation must be a finite 3 × 3 rotation matrix.")
    for i in range(3):
        for j in range(3):
            if abs(dot(value[i], value[j]) - int(i == j)) > 1e-8:
                raise ModelError("Orientation axes must be perpendicular unit vectors.")
    if abs(dot(value[0], cross(value[1], value[2])) - 1) > 1e-8:
        raise ModelError("Orientation must preserve handedness.")
    return value


def read_orientation(raw):
    if raw is None or raw == "":
        return deepcopy(IDENTITY)
    try:
        return validate_orientation(json.loads(raw) if isinstance(raw, str) else raw)
    except (ValueError, TypeError) as error:
        raise ModelError(f"Invalid source orientation: {error}") from error


def definition(frequencies=None, rows=None, notes=""):
    return {"schema_version": 1, "frequencies_hz": list(OCTAVE_BANDS if frequencies is None else frequencies),
            "directions": deepcopy(rows or []), "principal": {"azimuth_deg": 0., "elevation_deg": 0.},
            "notes": notes, "level_reference": "relative_power", "interpolation": "nearest_direction",
            "generator": None, "attachment": None}


def validate_angles(a, e):
    if not finite(a) or not 0 <= a < 360 or not finite(e) or not -90 <= e <= 90:
        raise ModelError("Azimuth must be 0 ≤ angle < 360°; elevation must be −90° to +90°.")


def validate_definition(value):
    keys = {"schema_version", "frequencies_hz", "directions", "principal", "notes", "level_reference",
            "interpolation", "generator", "attachment"}
    if not isinstance(value, dict) or set(value) != keys or type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ModelError("Unsupported directionality definition.")
    frequencies = value["frequencies_hz"]
    if (not isinstance(frequencies, list) or not 1 <= len(frequencies) <= 64 or
            any(not finite(f) or f <= 0 for f in frequencies) or sorted(set(frequencies)) != frequencies):
        raise ModelError("Frequencies must be unique, positive, increasing numbers (up to 64 bands).")
    if not isinstance(value["notes"], str):
        raise ModelError("Directionality notes must be text.")
    if value["level_reference"] not in ("relative_power", "correction") or value["interpolation"] != "nearest_direction":
        raise ModelError("Unsupported level reference or angular sampling rule.")
    p = value["principal"]
    if not isinstance(p, dict) or set(p) != {"azimuth_deg", "elevation_deg"}:
        raise ModelError("A definition needs a principal direction.")
    validate_angles(p["azimuth_deg"], p["elevation_deg"])
    rows = value["directions"]
    if not isinstance(rows, list) or len(rows) * len(frequencies) > MAX_CELLS:
        raise ModelError(f"Direction tables may contain at most {MAX_CELLS:,} values.")
    seen = set()
    for i, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != {"azimuth_deg", "elevation_deg", "levels_db"}:
            raise ModelError(f"Direction {i + 1}: invalid row.")
        a, e, levels = row["azimuth_deg"], row["elevation_deg"], row["levels_db"]
        validate_angles(a, e)
        key = (0 if abs(e) == 90 else a, e)
        if key in seen:
            raise ModelError(f"Direction {i + 1}: duplicate direction (poles have only one azimuth).")
        seen.add(key)
        if not isinstance(levels, list) or len(levels) != len(frequencies) or any(v is not None and not finite(v) for v in levels):
            raise ModelError(f"Direction {i + 1}: enter one finite dB value per frequency, or leave unknowns blank.")
    generator = value["generator"]
    if generator is not None:
        if (not isinstance(generator, dict) or set(generator) != {"kind", "version", "parameters", "modified", "rules"}
                or generator["kind"] not in ("revolve", "sectors", "stack_iso_2024")
                or type(generator["version"]) is not int or generator["version"] != 1
                or type(generator["modified"]) is not bool or not isinstance(generator["parameters"], dict)
                or not isinstance(generator["rules"], dict)):
            raise ModelError("Invalid template provenance.")
        validate_generator(generator)
    attachment = value["attachment"]
    if attachment is not None:
        try:
            if (not isinstance(attachment, dict) or set(attachment) != {"filename", "format", "sha256", "data_base64", "decoded"}
                    or not isinstance(attachment["filename"], str) or attachment["format"] not in ("cf1", "cf2")
                    or attachment["decoded"] is not False or len(attachment["data_base64"]) > MAX_ATTACHMENT * 1.34):
                raise ValueError("Invalid CLF attachment metadata")
            data = base64.b64decode(attachment["data_base64"], validate=True)
            if not data or len(data) > MAX_ATTACHMENT or hashlib.sha256(data).hexdigest() != attachment["sha256"]:
                raise ValueError("CLF attachment checksum or size is invalid")
        except (ValueError, TypeError, KeyError) as error:
            raise ModelError(str(error)) from error
    try:
        json.dumps(value, allow_nan=False)
    except (ValueError, TypeError) as error:
        raise ModelError("Template metadata must be finite JSON data.") from error
    return value


def validate_generator(generator):
    """Retained input forms must also be safe to reopen from imported JSON."""
    kind, p, rules = generator["kind"], generator["parameters"], generator["rules"]
    spacing = p.get("spacing_deg")
    if not finite(spacing) or spacing not in (5, 10, 15, 30, 45, 90):
        raise ModelError("Invalid template angular spacing.")
    if kind == "revolve":
        if set(p) != {"profile", "spacing_deg", "axis", "interpolation"} or p["axis"] not in ("front", "up") or p["interpolation"] not in ("db", "energy"):
            raise ModelError("Invalid revolution template parameters.")
        points = p["profile"]
        if not isinstance(points, list) or not 2 <= len(points) <= 10000:
            raise ModelError("Invalid saved 2D profile.")
        for point in points:
            if (not isinstance(point, dict) or set(point) != {"angle_deg", "levels_db"}
                    or not finite(point["angle_deg"]) or not 0 <= point["angle_deg"] <= 180
                    or not isinstance(point["levels_db"], list) or len(point["levels_db"]) != 8
                    or not all(finite(v) for v in point["levels_db"])):
                raise ModelError("Invalid saved 2D profile point.")
        angles = [point["angle_deg"] for point in points]
        if sorted(set(angles)) != angles or angles[0] != 0 or angles[-1] != 180:
            raise ModelError("Saved profile angles must be unique and increase from 0° to 180°.")
    elif kind == "sectors":
        if set(p) != {"boundaries_deg", "levels_db", "spacing_deg"}:
            raise ModelError("Invalid sector template parameters.")
        boundaries, levels = p["boundaries_deg"], p["levels_db"]
        if (not isinstance(boundaries, list) or len(boundaries) != 2 or not all(finite(v) for v in boundaries)
                or not 0 < boundaries[0] < boundaries[1] < 180 or not isinstance(levels, list) or len(levels) != 3
                or any(not isinstance(row, list) or len(row) != 8 or not all(finite(v) for v in row) for row in levels)):
            raise ModelError("Invalid saved sector values.")
    else:
        if (set(p) != {"opening_diameter_m", "exhaust_temperature_c", "spacing_deg"}
                or not finite(p["opening_diameter_m"]) or p["opening_diameter_m"] <= 0
                or not finite(p["exhaust_temperature_c"]) or p["exhaust_temperature_c"] <= -273):
            raise ModelError("Invalid saved chimney parameters.")
        if rules != {"principal_up": True, "method": "ISO 9613-2:2024 Annex B",
                     "departure_angle": "downwind_curved_ray_5000m", "numerical_status": "not_implemented"}:
            raise ModelError("Unsupported chimney evaluation rules.")
    if kind != "stack_iso_2024" and rules:
        raise ModelError("Unsupported template evaluation rules.")


def status(value):
    if value["attachment"] and not value["attachment"]["decoded"]:
        return "CLF retained · decoding unavailable"
    if not value["directions"]:
        return "Draft · no directions"
    count = sum(v is None for row in value["directions"] for v in row["levels_db"])
    return f"Draft · {count} unknown values" if count else "Values entered · authoring only"


def upright(value):
    return bool(value.get("generator") and value["generator"]["rules"].get("principal_up"))


def grid(spacing, frequencies, calculate):
    if spacing not in (5, 10, 15, 30, 45, 90):
        raise ModelError("Choose a supported angular spacing.")
    rows = []
    # Start with front at the horizon, then progress towards each pole.
    elevations = sorted(range(-90, 91, spacing), key=lambda e: (abs(e), -e))
    for e in elevations:
        for a in ([0] if abs(e) == 90 else range(0, 360, spacing)):
            rows.append({"azimuth_deg": a, "elevation_deg": e,
                         "levels_db": [calculate(direction(a, e), f) for f in frequencies]})
    return rows


def generated(kind, parameters, rows, principal, reference="relative_power", rules=None):
    value = definition(rows=rows)
    value["principal"] = dict(principal)
    value["level_reference"] = reference
    value["generator"] = {"kind": kind, "version": 1, "parameters": deepcopy(parameters),
                          "modified": False, "rules": dict(rules or {})}
    return validate_definition(value)


def revolve(profile, spacing=15, axis="front", interpolation="db"):
    if axis not in ("front", "up") or interpolation not in ("db", "energy"):
        raise ModelError("Choose a revolution axis and interpolation rule.")
    points = sorted(deepcopy(profile), key=lambda p: p["angle_deg"])
    angles = [p["angle_deg"] for p in points]
    if (not angles or any(not finite(a) or not 0 <= a <= 180 for a in angles) or
            angles[0] != 0 or angles[-1] != 180 or len(set(angles)) != len(angles)):
        raise ModelError("The profile needs unique angles from 0° through 180°, including both endpoints.")
    if any(len(p["levels_db"]) != 8 or not all(finite(v) for v in p["levels_db"]) for p in points):
        raise ModelError("Complete all eight frequency values at each profile angle.")
    p = {"azimuth_deg": 0., "elevation_deg": 90. if axis == "up" else 0.}
    vector = direction(p["azimuth_deg"], p["elevation_deg"])

    def calculate(v, frequency):
        angle = math.degrees(math.acos(max(-1, min(1, dot(v, vector)))))
        index = OCTAVE_BANDS.index(frequency)
        for a, b in zip(points, points[1:]):
            if angle <= b["angle_deg"] + 1e-10:
                t = (angle - a["angle_deg"]) / (b["angle_deg"] - a["angle_deg"])
                t = max(0, min(1, t))
                x, y = a["levels_db"][index], b["levels_db"][index]
                if t == 0:
                    return x
                if t == 1:
                    return y
                if interpolation == "db":
                    return (1-t)*x + t*y
                high = max(x, y)
                return high + 10*math.log10((1-t)*10**((x-high)/10) + t*10**((y-high)/10))
        return points[-1]["levels_db"][index]

    rows = grid(spacing, OCTAVE_BANDS, calculate)
    for row in rows:
        row["levels_db"] = [rounded_db(level) for level in row["levels_db"]]
    value = generated("revolve", {"profile": points, "spacing_deg": spacing, "axis": axis,
                                  "interpolation": interpolation}, rows, p)
    value["notes"] = f"2D profile revolved 360° about {axis}; {interpolation} interpolation; {spacing}° output grid."
    return value


def sectors(boundaries, levels, spacing=15):
    if len(boundaries) != 2 or not all(finite(x) for x in boundaries) or not 0 < boundaries[0] < boundaries[1] < 180:
        raise ModelError("Sector boundaries must increase between 0° and 180°.")
    if len(levels) != 3 or any(len(row) != 8 or not all(finite(v) for v in row) for row in levels):
        raise ModelError("Enter eight finite dB values for each of the three sectors.")

    def calculate(vector, frequency):
        angle = math.degrees(math.acos(max(-1, min(1, vector[0]))))
        zone = 0 if angle < boundaries[0] else 1 if angle < boundaries[1] else 2
        return levels[zone][OCTAVE_BANDS.index(frequency)]

    value = generated("sectors", {"boundaries_deg": list(boundaries), "levels_db": deepcopy(levels),
                                  "spacing_deg": spacing}, grid(spacing, OCTAVE_BANDS, calculate),
                      {"azimuth_deg": 0., "elevation_deg": 0.})
    value["notes"] = f"User-defined angular sectors at {boundaries[0]:g}° and {boundaries[1]:g}° from the principal; constant levels in each sector. Boundaries belong to the next sector."
    return value


def stack_draft(diameter, temperature, spacing=15):
    if not finite(diameter) or diameter <= 0 or not finite(temperature) or temperature <= -273:
        raise ModelError("Enter a positive opening diameter and exhaust temperature above −273 °C.")
    value = generated("stack_iso_2024", {"opening_diameter_m": diameter, "exhaust_temperature_c": temperature,
                                        "spacing_deg": spacing}, grid(spacing, OCTAVE_BANDS, lambda v, f: None),
                      {"azimuth_deg": 0., "elevation_deg": 90.}, "correction",
                      {"principal_up": True, "method": "ISO 9613-2:2024 Annex B", "departure_angle": "downwind_curved_ray_5000m",
                       "numerical_status": "not_implemented"})
    value["notes"] = (f"Chimney outlet: diameter {diameter:g} m, exhaust {temperature:g} °C. "
                      "Vertical upward-facing open outlet. ISO 9613-2:2024 Annex B calculation pending; "
                      "blank cells are unknown. The receiver-dependent downwind departure-angle rule must be applied by the engine.")
    return value


def attach_clf(path):
    path = Path(path)
    if path.suffix.lower() not in (".cf1", ".cf2") or path.stat().st_size > MAX_ATTACHMENT:
        raise ModelError("Choose a CF1 or CF2 file no larger than 10 MiB.")
    data = path.read_bytes()
    value = definition(notes=f"Original CLF file: {path.name}. Binary decoding is unavailable; no directional values have been inferred.")
    value["attachment"] = {"filename": path.name, "format": path.suffix.lower()[1:],
                           "sha256": hashlib.sha256(data).hexdigest(), "data_base64": base64.b64encode(data).decode("ascii"),
                           "decoded": False}
    return validate_definition(value)


def import_table(text):
    """CSV/TSV: azimuth_deg,elevation_deg,<numeric Hz>...; blank means unknown."""
    delimiter = '\t' if '\t' in text.partition('\n')[0] else ','
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    try:
        if not rows or [c.strip().lower() for c in rows[0][:2]] != ["azimuth_deg", "elevation_deg"]:
            raise ModelError("Header must be azimuth_deg,elevation_deg followed by numerical frequencies in Hz.")
        value = definition([float(c.strip()) for c in rows[0][2:]])
        for row in rows[1:]:
            if not row or not any(c.strip() for c in row):
                continue
            if len(row) != len(rows[0]):
                raise ModelError("Every imported row must have the same number of columns as the header.")
            value["directions"].append({"azimuth_deg": float(row[0]), "elevation_deg": float(row[1]),
                                        "levels_db": [float(c) if c.strip() else None for c in row[2:]]})
        return validate_definition(value)
    except (ValueError, IndexError) as error:
        raise ModelError(f"Invalid direction table: {error}") from error
