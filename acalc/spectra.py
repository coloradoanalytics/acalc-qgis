# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Versioned octave-band catalog values, independent of QGIS and SQLite."""

import math

from .schema import ModelError
from .decibels import rounded_db


OCTAVE_BANDS = (63, 125, 250, 500, 1000, 2000, 4000, 8000)
SPECTRUM_KINDS = {"sound_power": "Sound Power", "absorption": "Absorption"}


POWER_BASES = {"total": "Total sound power (dB re 1 pW)",
               "per_metre": "Sound power per meter (dB re 1 pW/m)",
               "per_square_metre": "Sound power per square meter (dB re 1 pW/m²)"}


def compatible_power(value, kind):
    return value.get("power_basis") in ({"total", "per_metre"} if kind == "line_source" else
                                       {"total", "per_square_metre"} if kind == "area_source" else {"total"})


def spectrum_definition(kind, values=None, provenance="", power_basis="total",
                        back_calculation="locked", initial_values=None):
    if not isinstance(kind, str) or kind not in SPECTRUM_KINDS:
        raise ModelError("Choose sound power or absorption for the spectrum type.")
    power = kind == "sound_power"
    value = {
        "schema_version": 2,
        "kind": kind,
        "bandwidth": "octave",
        "frequencies_hz": list(OCTAVE_BANDS),
        "quantity": "sound_power_level" if power else "absorption_coefficient",
        "unit": "dB re 1 pW" if power else "1",
        "weighting": "Z" if power else "none",
        "values": list(values) if values is not None else [None] * len(OCTAVE_BANDS),
        "provenance": provenance,
    }
    if power:
        value["power_basis"] = power_basis
        value["back_calculation"] = back_calculation
        if back_calculation == "adjustable":
            value["initial_values"] = list(initial_values) if initial_values is not None else list(value["values"])
    validate_spectrum(value)
    return value


def validate_spectrum(value):
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] not in (1, 2):
        raise ModelError("Unsupported spectrum definition version.")
    kind = value.get("kind")
    if not isinstance(kind, str) or kind not in SPECTRUM_KINDS:
        raise ModelError("Unsupported spectrum type.")
    power = kind == "sound_power"
    legacy = value["schema_version"] == 1
    expected = {"bandwidth": "octave", "frequencies_hz": list(OCTAVE_BANDS),
                "quantity": ("sound_power" if value.get("unit") == "W" else "sound_power_level") if power else "absorption_coefficient",
                "unit": value.get("unit") if power else "1", "weighting": "Z" if power else "none"}
    if (power and value.get("unit") not in (("W", "dB re 1 pW") if legacy else ("dB re 1 pW",))) or any(value.get(key) != item for key, item in expected.items()):
        raise ModelError("Use the eight unweighted octave bands from 63 to 8000 Hz with the specified quantity and units.")
    if not isinstance(value.get("provenance"), str):
        raise ModelError("Spectrum provenance must be text.")
    values = value.get("values")
    if not isinstance(values, list) or len(values) != len(OCTAVE_BANDS):
        raise ModelError("A spectrum must contain exactly eight band values; use null for unknown bands.")
    zero_bands = value.get("zero_energy_bands_hz") if power and legacy else []
    if not legacy and "zero_energy_bands_hz" in value:
        raise ModelError("Enter 0 for a sound-power band with no energy.")
    if power:
        if not isinstance(value.get("power_basis"), str) or value["power_basis"] not in POWER_BASES:
            raise ModelError("Choose total power, power per meter, or power per square meter.")
        if (not isinstance(zero_bands, list) or any(type(band) is not int or band not in OCTAVE_BANDS for band in zero_bands)
                or len(zero_bands) != len(set(zero_bands))):
            raise ModelError("Zero-energy bands must be distinct octave-band frequencies.")
        mode = value.get("back_calculation", "locked")
        if mode not in ("locked", "adjustable"):
            raise ModelError("Choose locked or adjustable sound power.")
        initial = value.get("initial_values")
        if mode == "adjustable":
            if not isinstance(initial, list) or len(initial) != len(OCTAVE_BANDS):
                raise ModelError("Adjustable sound power needs eight initial band values.")
        elif initial is not None:
            raise ModelError("Locked sound power cannot have initial band values.")
    elif "zero_energy_bands_hz" in value or "power_basis" in value:
        raise ModelError("Absorption spectra cannot contain sound power settings.")
    elif "back_calculation" in value or "initial_values" in value:
        raise ModelError("Absorption spectra cannot contain back-calculation settings.")
    for label, series in (("", values), ("initial ", initial if power and mode == "adjustable" else [])):
        for band, number in zip(OCTAVE_BANDS, series):
            if band in zero_bands and number is not None:
                raise ModelError(f"{band} Hz: zero energy cannot also have a sound power level.")
            if number is None:
                continue
            try:
                finite = type(number) in (float, int) and math.isfinite(number)
            except OverflowError:
                finite = False
            if not finite:
                raise ModelError(f"{band} Hz: enter a finite {label}number or leave the band unknown.")
            if power and value["unit"] == "W" and number < 0:
                raise ModelError(f"{band} Hz: power in watts cannot be negative.")
            if not power and not 0 <= number <= 1:
                raise ModelError(f"{band} Hz: absorption must be between 0 and 1.")


def missing_bands(value):
    return [band for band, number in zip(OCTAVE_BANDS, value["values"])
            if number is None and band not in value.get("zero_energy_bands_hz", [])]


def upgrade_spectrum(value):
    """Convert legacy watt values and silence markers to dB-only entry.

    Schema 2 reserves numeric zero for silence. Negative nonzero levels remain
    physical dB levels; null still means an unknown band.
    """
    validate_spectrum(value)
    if value["schema_version"] == 2:
        return value
    values = list(value["values"])
    if value["kind"] == "sound_power":
        for index, band in enumerate(OCTAVE_BANDS):
            number = values[index]
            if band in value["zero_energy_bands_hz"]:
                values[index] = 0.0
            elif number is not None and value["unit"] == "W":
                values[index] = 0.0 if number == 0 else rounded_db(10 * math.log10(number) + 120)
    return spectrum_definition(value["kind"], values, value["provenance"], value.get("power_basis", "total"))
