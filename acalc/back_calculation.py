# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Nonnegative least-squares fit of sound power to authored octave measurements.

The propagation engine supplies one fixed source-to-receiver transfer matrix.
Each octave is fit independently in linear sound energy; no solver call occurs
inside the iterations. The returned spectra are proposals until applied.
"""

import math

from .schema import ModelError


def fit_spectra(relationships):
    spectra = {item["id"]: item for item in relationships["spectra"]}
    adjustable = sorted(item["id"] for item in spectra.values() if item["mode"] == "adjustable")
    if not adjustable:
        raise ModelError("No adjustable sound power spectra are assigned.")
    sources = relationships["sources"]
    measurements = relationships["measurements"]
    if not measurements:
        raise ModelError("Add Measurements with octave levels to this variant.")
    proposals = {identity: [] for identity in adjustable}
    residuals_before, residuals_after = [], []
    for band in range(8):
        observed = [item for item in measurements if item["observed"][band] is not None]
        if len(observed) < len(adjustable):
            raise ModelError(f"{(63, 125, 250, 500, 1000, 2000, 4000, 8000)[band]} Hz needs at least {len(adjustable)} octave Measurements to fit {len(adjustable)} adjustable spectra.")
        reference = max(item["observed"][band] for item in observed)
        y = [10 ** ((item["observed"][band] - reference) / 10) for item in observed]
        fixed = []
        columns = [[0.0] * len(observed) for _ in adjustable]
        for row, item in enumerate(observed):
            total = 0.0
            for source in sources:
                transfer = item["transfer_db"][source["id"]][band]
                if transfer is None:
                    continue
                coefficient = 10 ** (transfer / 10)
                spectrum = spectra[source["spectrum_id"]]
                if spectrum["mode"] == "adjustable":
                    columns[adjustable.index(source["spectrum_id"])][row] += coefficient
                else:
                    level = spectrum["current"][band]
                    if level is not None and level != 0:
                        total += coefficient * 10 ** ((level - reference) / 10)
            fixed.append(total)
        scales = [max(column) for column in columns]
        if any(scale <= 0 for scale in scales):
            raise ModelError("An adjustable spectrum has no audible path to the octave Measurements.")
        a = [[value / scale for value in column] for column, scale in zip(columns, scales)]
        independent = []
        for column in a:
            remainder = column[:]
            for basis in independent:
                projection = math.fsum(x * z for x, z in zip(remainder, basis))
                remainder = [value - projection * unit for value, unit in zip(remainder, basis)]
            norm = math.sqrt(math.fsum(value * value for value in remainder))
            original_norm = math.sqrt(math.fsum(value * value for value in column))
            if norm <= 1e-8 * original_norm:
                raise ModelError("The Measurements cannot distinguish the adjustable spectra in this octave. Add measurements at different source-to-receiver positions.")
            independent.append([value / norm for value in remainder])
        initial = []
        for identity, scale in zip(adjustable, scales):
            level = spectra[identity]["initial"][band]
            initial.append(0.0 if level == 0 else scale * 10 ** ((level - reference) / 10))
        x = initial[:]
        residual = [target - background - math.fsum(column[row] * value for column, value in zip(a, x))
                    for row, (target, background) in enumerate(zip(y, fixed))]
        for iteration in range(10000):
            change = 0.0
            for index, column in enumerate(a):
                denominator = math.fsum(value * value for value in column)
                step = math.fsum(value * error for value, error in zip(column, residual)) / denominator
                updated = max(0.0, x[index] + step)
                delta = updated - x[index]
                if delta:
                    x[index] = updated
                    residual = [error - delta * value for error, value in zip(residual, column)]
                    change = max(change, abs(delta))
            if change <= 1e-10 * max(1.0, max(x)):
                break
        else:
            raise ModelError("The sound-power fit did not converge. Try measurements with more distinct source contributions.")
        def errors(solution):
            for row, item in enumerate(observed):
                predicted = fixed[row] + math.fsum(column[row] * value for column, value in zip(a, solution))
                if predicted <= 0:
                    yield 300.0
                else:
                    yield 10 * math.log10(predicted / y[row])
        residuals_before.extend(errors(initial))
        for identity, value, scale in zip(adjustable, x, scales):
            level = 0.0 if value <= 0 else reference + 10 * math.log10(value / scale)
            if not math.isfinite(level):
                raise ModelError("The fitted sound power is outside the supported numeric range.")
            if value <= 0:
                proposals[identity].append(0.0)
            else:
                rounded = round(level, 1)
                # This catalog reserves numeric zero for silence. Keep a
                # finite fitted level audible even when it rounds to 0.0 dB.
                proposals[identity].append(rounded if rounded != 0 else (0.1 if level >= 0 else -0.1))
        applied = [0.0 if proposals[identity][-1] == 0 else scale * 10 ** ((proposals[identity][-1] - reference) / 10)
                   for identity, scale in zip(adjustable, scales)]
        residuals_after.extend(errors(applied))
    def rms(values):
        return math.sqrt(math.fsum(value * value for value in values) / len(values))
    return {"proposals": proposals, "initial_rms_db": rms(residuals_before),
            "fitted_rms_db": rms(residuals_after), "observations": len(residuals_after)}
