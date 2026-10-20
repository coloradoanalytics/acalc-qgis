# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Project length display units; persisted acoustic quantities remain SI."""

from dataclasses import dataclass
import math

from qgis.PyQt.QtWidgets import QComboBox

from .numeric_controls import DecimalInput
from .schema import ModelError

LENGTH_UNITS = {"m": ("Meters", 1.0), "ft": ("Feet (international)", 0.3048)}


@dataclass(frozen=True)
class CRSLengthUnit:
    label: str
    factor: float

    def __str__(self):
        return self.label


def display_unit_name(name):
    return name.replace("metres", "meters").replace("metre", "meter").replace("Metres", "Meters").replace("Metre", "Meter")


def horizontal_unit(name, factor):
    if not math.isfinite(factor) or factor <= 0:
        raise ModelError("The CRS must have a positive linear unit conversion.")
    label = ("m" if factor == 1 else "ft" if factor == .3048 else
             "ftUS" if math.isclose(factor, 1200/3937, rel_tol=1e-12) else display_unit_name(name))
    return CRSLengthUnit(label, factor)


def length_factor(unit):
    if isinstance(unit, CRSLengthUnit):
        return unit.factor
    if not isinstance(unit, str) or unit not in LENGTH_UNITS:
        raise ModelError("Choose meters or feet for elevations.")
    return LENGTH_UNITS[unit][1]


def input_unit(store):
    metadata = store.metadata() if store else {"horizontal_unit": "meter", "horizontal_to_m": 1.0}
    return horizontal_unit(metadata["horizontal_unit"], metadata["horizontal_to_m"])


def elevation_unit(store):
    import json
    extra = json.loads(store.metadata()["metadata_json"]) if store else {}
    # Preserve the previous display preference as the elevation setting only.
    unit = extra.get("elevation_unit", extra.get("input_length_unit", "m"))
    if unit not in ("m", "ft"):
        raise ModelError("Choose meters or feet for elevations.")
    return unit


def unit_combo(unit="m"):
    combo = QComboBox()
    for key, (label, _) in LENGTH_UNITS.items():
        combo.addItem(label, key)
    combo.setCurrentIndex(combo.findData(unit))
    return combo


class LengthSpinBox(DecimalInput):
    """Two-place length display with precise editing and SI Python accessors."""

    def __init__(self, unit="m"):
        self.factor = length_factor(unit)
        self._editing = False
        super().__init__()
        self._minimum_m = self.minimum() * self.factor
        self._maximum_m = self.maximum() * self.factor
        self.setDecimals(6)
        self.setSuffix(" " + str(unit))
        self.lineEdit().textEdited.connect(self._edited)

    def _edited(self, text):
        self._original = None

    def textFromValue(self, value):
        if self._editing:
            text = super().textFromValue(value)
            whole, separator, fraction = text.partition(self.locale().decimalPoint())
            fraction = fraction.rstrip(self.locale().zeroDigit())
            return whole + separator + fraction.ljust(2, self.locale().zeroDigit())
        # Keep sub-centimeter values legible instead of presenting them as zero.
        decimals = self.decimals() if 0 < abs(value) < 0.005 else 2
        return self.locale().toString(value, 'f', decimals)

    def valueFromText(self, text):
        # Qt also parses programmatic text refreshes. Never feed a rounded
        # presentation back into the numeric value when leaving the field.
        value = super().value()
        if not self._editing and text == self.prefix() + self.textFromValue(value) + self.suffix():
            return value
        return super().valueFromText(text)

    def _refresh_text(self):
        value = super().value()
        text = (self.specialValueText() if value == self.minimum() and self.specialValueText()
                else self.prefix() + self.textFromValue(value) + self.suffix())
        self.lineEdit().setText(text)

    def focusInEvent(self, event):
        self._editing = True
        self._refresh_text()
        super().focusInEvent(event)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self._editing = False
        self._refresh_text()

    def setValue(self, value):
        self._original = None
        super().setValue(value / self.factor)
        if self._minimum_m <= value <= self._maximum_m:
            self._original = (super().value(), value)

    def value(self):
        original = getattr(self, "_original", None)
        if original and super().value() == original[0]:
            return original[1]
        return super().value() * self.factor

    def set_unit(self, unit):
        value = self.value()
        minimum, maximum = self._minimum_m, self._maximum_m
        step = self.singleStep() * self.factor
        self.factor = length_factor(unit)
        self.setDecimals(self.decimals())
        self.setRange(minimum, maximum)
        self.setSingleStep(step)
        self.setSuffix(" " + str(unit))
        self.setValue(value)

    def setRange(self, minimum, maximum):
        self._minimum_m, self._maximum_m = minimum, max(minimum, maximum)
        super().setRange(minimum / self.factor, maximum / self.factor)

    def setMinimum(self, value):
        self._minimum_m = value
        self._maximum_m = max(value, self._maximum_m)
        super().setMinimum(value / self.factor)

    def setMaximum(self, value):
        self._maximum_m = value
        self._minimum_m = min(value, self._minimum_m)
        super().setMaximum(value / self.factor)

    def setSingleStep(self, value):
        super().setSingleStep(value / self.factor)
