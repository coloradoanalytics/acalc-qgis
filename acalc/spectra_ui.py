# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Project spectrum library and native octave-band editor."""

from pathlib import Path
from decimal import Decimal, ROUND_HALF_UP
import math

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHeaderView,
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QTabBar, QTableWidget,
    QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)

from .numeric_controls import DecimalInput
from .decibels import rounded_db
from .measurement import broadband_from_octaves
from .schema import ModelError
from .spectra import OCTAVE_BANDS, SPECTRUM_KINDS, POWER_BASES, spectrum_definition, upgrade_spectrum
from .spectrum_table import SpectrumTable
from .storage import require_name


def wrapped_label(text):
    label = QLabel(text)
    label.setTextFormat(Qt.PlainText)
    label.setWordWrap(True)
    return label


class AbsorptionSpinBox(DecimalInput):
    """Edit alpha while preserving the existing stored energy-reflection value."""

    def __init__(self):
        super().__init__()
        self.setRange(0, 1)
        self.setDecimals(4)
        self.setSingleStep(0.1)
        self.setToolTip("Absorption in every octave band: 0 = fully reflecting, 1 = fully absorbing.")
        self.set_reflection(0.9)

    def set_reflection(self, coefficient):
        self._stored_reflection = coefficient
        self.setValue(1 - coefficient)
        self._loaded_alpha = self.value()

    def reflection(self):
        # Opening/applying an unchanged editor must not round saved data or
        # change it through floating-point subtraction (e.g. 1 - 0.9).
        if self.value() == self._loaded_alpha:
            return self._stored_reflection
        return round(1 - self.value(), self.decimals())


class SpectrumDialog(QDialog):
    def __init__(self, parent, kind, record=None):
        super().__init__(parent)
        self.kind = kind
        self.value = None
        self.setWindowTitle(f"{SPECTRUM_KINDS[kind]} spectrum")
        self.setModal(True)
        self.resize(960, 560)
        self.setMinimumWidth(540)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(record["name"] if record else "")
        self.name.setPlaceholderText("Spectrum name")
        self.provenance = QTextEdit(record["spectrum"]["provenance"] if record else "")
        self.provenance.setAcceptRichText(False)
        self.provenance.setMaximumHeight(80)
        self.provenance.setPlaceholderText("Data source, measurement conditions, equipment state, or material reference")
        form.addRow("Name", self.name)
        form.addRow("Reference / notes", self.provenance)
        layout.addLayout(form)
        power = kind == "sound_power"
        value = upgrade_spectrum(record["spectrum"]) if record else spectrum_definition(kind)
        if power:
            self.power_basis = QComboBox()
            for key, label in POWER_BASES.items():
                self.power_basis.addItem(label, key)
            self.power_basis.setCurrentIndex(self.power_basis.findData(value["power_basis"]))
            form.addRow("Power basis", self.power_basis)
            self.back_calculation = QComboBox()
            self.back_calculation.addItem("Locked — keep entered sound power", "locked")
            self.back_calculation.addItem("Adjustable — fit to Measurements", "adjustable")
            self.back_calculation.setCurrentIndex(self.back_calculation.findData(value.get("back_calculation", "locked")))
            self.back_calculation.currentIndexChanged.connect(self.mode_changed)
            form.addRow("Back-calculation", self.back_calculation)
        layout.addWidget(wrapped_label(
            "Unweighted octave bands in dB. Enter 0 for no energy. Blank = unknown."
            if power else "Octave-band absorption coefficients (0–1). Blank = unknown."))
        layout.addWidget(wrapped_label("Copy / paste a row of values from a spreadsheet. Tab moves to the next band."))
        self.bands = SpectrumTable(2 if power else 1, len(OCTAVE_BANDS))
        self.bands.setHorizontalHeaderLabels([f"{band} Hz" for band in OCTAVE_BANDS])
        if power:
            self.bands.setVerticalHeaderLabels(["Sound Power", "Initial guess"])
            # Delimit the blank corner heading from the first octave heading.
            self.bands.setStyleSheet("QTableCornerButton::section {"
                                    "background: palette(button); border: 0;"
                                    "border-right: 1px solid palette(mid);"
                                    "border-bottom: 1px solid palette(mid); }")
        else:
            self.bands.verticalHeader().hide()
        self.bands.horizontalHeader().setMinimumSectionSize(65)
        self.bands.horizontalHeader().setDefaultSectionSize(90)
        self.bands.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.bands.verticalHeader().setDefaultSectionSize(36)
        self.bands.setMinimumHeight(160 if power else 140)
        for column, band in enumerate(OCTAVE_BANDS):
            number = value["values"][column]
            self.bands.setItem(0, column, QTableWidgetItem("" if number is None else str(number)))
            if power:
                initial = value.get("initial_values", value["values"])[column]
                self.bands.setItem(1, column, QTableWidgetItem("" if initial is None else str(initial)))
        self.bands.paste_band = self.paste_band
        self.bands.copy_rows = (0, 1) if power else (0,)
        self.bands.copy_value = self.copy_band
        layout.addWidget(self.bands, 1)
        self.overall = QLabel()
        self.overall.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.overall.setToolTip(
            "A-weighted sum of all eight Sound Power bands. 0 means no energy; blank means unknown."
            if power else
            "Average absorption at 250, 500, 1000 and 2000 Hz, rounded to the nearest 0.05.")
        layout.addWidget(self.overall)
        self.bands.itemChanged.connect(self.update_overall)
        if power:
            self.mode_changed()
            self.power_basis.currentIndexChanged.connect(self.update_overall)
        self.update_overall()
        layout.addWidget(wrapped_label(
            "Shared values update every assigned source. Save writes to the project."
            if power else "Shared values update every assigned building and wall, across all variants. Save writes to the project."))
        self.usage = wrapped_label("")
        layout.addWidget(self.usage)
        self.error = wrapped_label("")
        self.error.setStyleSheet("color: #c24f36;")
        self.error.hide()
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("Save spectrum")
        buttons.accepted.connect(self.validate)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def copy_band(self, row, column):
        return self.bands.item(row, column).text()

    def paste_band(self, row, column, text):
        zero = self.kind == "sound_power" and text.casefold() in ("zero energy", "-inf", "-infinity", "−∞")
        self.bands.item(row, column).setText("0" if zero else "" if text.casefold() == "unknown" else text)

    def mode_changed(self, *args):
        adjustable = self.back_calculation.currentData() == "adjustable"
        self.bands.setRowHidden(1, not adjustable)

    def update_overall(self, *args):
        power = self.kind == "sound_power"
        label = "Overall A-weighted level" if power else "NRC"
        columns = range(len(OCTAVE_BANDS)) if power else (2, 3, 4, 5)
        texts = [self.bands.item(0, column).text().strip() for column in columns]
        try:
            values = [float(text) if text else None for text in texts]
            if any(value is not None and (not math.isfinite(value) or
                   (not power and not 0 <= value <= 1)) for value in values):
                raise ValueError
            if None in values:
                result = "Unknown"
            elif power:
                # Catalog numeric zero denotes silence, unlike Measurement 0 dB.
                total = broadband_from_octaves([value if value != 0 else None for value in values])
                level = "−∞" if total is None else f"{rounded_db(total):.1f}"
                basis = {"total": "", "per_metre": " per meter",
                         "per_square_metre": " per square meter"}[self.power_basis.currentData()]
                result = f"{level} dBA{basis}"
            else:
                # Decimal arithmetic keeps exact midpoint inputs from drifting
                # below the rounding boundary through binary float conversion.
                steps = sum(Decimal(text) for text in texts) / Decimal("0.20")
                result = f"{steps.quantize(Decimal('1'), rounding=ROUND_HALF_UP) * Decimal('0.05'):.2f}"
        except (ValueError, ModelError):
            result = "Invalid values"
        self.overall.setText(f"{label}: {result}")

    def validate(self):
        try:
            require_name(self.name.text())
            def row_values(row):
                values = []
                for column, band in enumerate(OCTAVE_BANDS):
                    text = self.bands.item(row, column).text().strip()
                    try:
                        values.append(float(text) if text else None)
                    except ValueError as error:
                        raise ModelError(f"{band} Hz: enter a number or leave the band blank.") from error
                return values
            values = row_values(0)
            mode = self.back_calculation.currentData() if self.kind == "sound_power" else "locked"
            self.value = spectrum_definition(self.kind, values, self.provenance.toPlainText(),
                self.power_basis.currentData() if self.kind == "sound_power" else "total",
                mode, row_values(1) if mode == "adjustable" else None)
        except ModelError as error:
            self.error.setText(str(error))
            self.error.show()
            return
        self.accept()


class SpectraPage(QWidget):
    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        self.tabs = QTabBar()
        self.tabs.setExpanding(True)
        for kind, title in SPECTRUM_KINDS.items():
            self.tabs.addTab(title)
            self.tabs.setTabData(self.tabs.count() - 1, kind)
        layout.addWidget(self.tabs)
        self.help = wrapped_label("")
        layout.addWidget(self.help)
        self.adjustable_icon = QIcon(str(Path(__file__).with_name("adjustable.svg")))
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Name", "Fit"])
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self.selection_changed)
        self.table.cellDoubleClicked.connect(lambda *args: controller.run(controller.edit_spectrum))
        layout.addWidget(self.table, 1)
        self.summary = wrapped_label("")
        layout.addWidget(self.summary)
        self.reference = wrapped_label("")
        self.reference.setMaximumHeight(80)
        layout.addWidget(self.reference)
        actions = QHBoxLayout()
        self.new_button = QPushButton("New…")
        self.edit_button = QPushButton("Edit…")
        self.duplicate_button = QPushButton("Duplicate…")
        self.delete_button = QPushButton("Delete")
        for widget, callback in (
                (self.new_button, lambda: controller.edit_spectrum(create=True)),
                (self.edit_button, controller.edit_spectrum),
                (self.duplicate_button, lambda: controller.edit_spectrum(create=True, duplicate=True)),
                (self.delete_button, controller.delete_spectrum)):
            widget.clicked.connect(lambda checked=False, action=callback: controller.run(action))
            actions.addWidget(widget)
        layout.insertLayout(layout.indexOf(self.table), actions)
        layout.addWidget(wrapped_label("This library belongs to the open project and is shared by all its variants. Double-click a spectrum to edit it."))
        self.tabs.currentChanged.connect(lambda *args: self.refresh())
        self.records = {}

    @property
    def kind(self):
        return self.tabs.tabData(self.tabs.currentIndex())

    def selected_id(self):
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        return item.data(Qt.UserRole) if item is not None else None

    def refresh(self, preferred=None):
        selected = preferred or self.selected_id()
        store = self.controller.store
        self.setEnabled(store is not None)
        records = store.spectra(self.kind) if store else []
        self.records = {row["spectrum_id"]: row for row in records}
        self.table.setRowCount(0)
        self.table.setRowCount(len(records))
        selected_row = 0
        for index, row in enumerate(records):
            name = QTableWidgetItem(row["name"])
            name.setData(Qt.UserRole, row["spectrum_id"])
            self.table.setItem(index, 0, name)
            fit = QTableWidgetItem()
            fit.setTextAlignment(Qt.AlignCenter)
            if row["spectrum"].get("back_calculation") == "adjustable":
                fit.setIcon(self.adjustable_icon)
                fit.setToolTip("Adjustable: sound power can be fitted to Measurements.")
                fit.setData(Qt.AccessibleTextRole, "Adjustable for back-calculation")
            else:
                fit.setToolTip("Locked sound power")
            self.table.setItem(index, 1, fit)
            if row["spectrum_id"] == selected:
                selected_row = index
        if records:
            self.table.selectRow(selected_row)
        self.table.setColumnHidden(1, self.kind != "sound_power")
        self.summary.setText(f"{len(records)} {SPECTRUM_KINDS[self.kind].lower()} spectra" if records else "No spectra yet. Choose New to enter the first spectrum.")
        self.help.setText("63–8000 Hz octave bands · Sound power in dB · 0 = no energy · Total, per meter or per square meter" if self.kind == "sound_power"
                          else "63–8000 Hz octave bands · Absorption coefficients, 0–1 · Assign in building and wall properties · α = 0 fully reflects")
        self.selection_changed()

    def selection_changed(self):
        row = self.records.get(self.selected_id())
        for widget in (self.edit_button, self.duplicate_button, self.delete_button):
            widget.setEnabled(row is not None)
        self.reference.setText(row["spectrum"]["provenance"] if row else "")
