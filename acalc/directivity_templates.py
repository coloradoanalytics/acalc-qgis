# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Template tools generate a reviewable direction table before applying it."""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QDialogButtonBox,
    QFormLayout, QHBoxLayout, QHeaderView, QLabel, QPushButton,
    QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from .numeric_controls import DecimalInput
from . import directivity as model
from .directivity_preview import DirectivityPreview, PolarProfile
from .schema import ModelError
from .spectra import OCTAVE_BANDS
from .spectra_ui import wrapped_label


def number(low, high, value, decimals=2):
    widget = DecimalInput()
    widget.setRange(low, high)
    widget.setDecimals(decimals)
    widget.setValue(value)
    return widget


class DirectivityTemplateDialog(QDialog):
    def __init__(self, parent=None, generator=None, input_unit="m"):
        super().__init__(parent)
        self.setWindowTitle("Directionality template")
        self.resize(1080, 840)
        self.value = None
        layout = QVBoxLayout(self)
        self.kind = QComboBox()
        self.kind.addItem("Revolve a 2D pattern", "revolve")
        self.kind.addItem("Angular sectors", "sectors")
        self.kind.addItem("Chimney / stack · ISO 9613-2:2024 draft", "stack_iso_2024")
        layout.addWidget(self.kind)
        body = QHBoxLayout()
        self.forms = QStackedWidget()
        body.addWidget(self.forms, 1)
        self.preview = DirectivityPreview()
        body.addWidget(self.preview, 1)
        layout.addLayout(body, 1)
        self.input_pages = QStackedWidget()
        layout.addWidget(self.input_pages, 1)
        self.axis = QComboBox()
        self.axis.addItem("Front (+X)", "front")
        self.axis.addItem("Top (+Z)", "up")
        self.interpolation = QComboBox()
        self.interpolation.addItem("Linear in dB", "db")
        self.interpolation.addItem("Linear in energy", "energy")
        page = QWidget()
        form = QFormLayout(page)
        form.addRow("Revolution axis / principal", self.axis)
        form.addRow("Between profile points", self.interpolation)
        form.addRow(wrapped_label("Full 360° revolution. Profile angles run from 0° along the principal to 180° behind it."))
        self.polar = PolarProfile()
        form.addRow(self.polar)
        input_page = QWidget()
        input_layout = QVBoxLayout(input_page)
        input_layout.setContentsMargins(0, 0, 0, 0)
        input_layout.addWidget(QLabel("2D profile · angle × frequency (dB)"))
        self.profile = QTableWidget(7, 9)
        self.profile.setHorizontalHeaderLabels(["Angle °"] + [f"{f} Hz" for f in OCTAVE_BANDS])
        self.profile.verticalHeader().hide()
        self.profile.verticalHeader().setMinimumSectionSize(24)
        self.profile.verticalHeader().setDefaultSectionSize(30)
        self.profile.horizontalHeader().setMinimumSectionSize(60)
        self.profile.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.profile.setMinimumHeight(175)
        for r, angle in enumerate(range(0, 181, 30)):
            for c, value in enumerate([angle] + [0] * 8):
                self.profile.setItem(r, c, QTableWidgetItem(str(value)))
        input_layout.addWidget(self.profile)
        actions = QHBoxLayout()
        add = QPushButton("Add profile angle")
        add.clicked.connect(self.add_profile)
        remove = QPushButton("Remove selected angle")
        remove.clicked.connect(self.remove_profile)
        actions.addWidget(add)
        actions.addWidget(remove)
        input_layout.addLayout(actions)
        self.input_pages.addWidget(input_page)
        self.forms.addWidget(page)
        page = QWidget()
        form = QFormLayout(page)
        self.front = number(.01, 179.98, 45)
        self.rear = number(.02, 179.99, 120)
        form.addRow("Front / side boundary (°)", self.front)
        form.addRow("Side / rear boundary (°)", self.rear)
        form.addRow(wrapped_label("Three cones about Front (+X). User-defined levels are constant within each sector; boundaries belong to the next sector."))
        self.levels = QTableWidget(3, 8)
        self.levels.setVerticalHeaderLabels(["Front", "Side", "Rear"])
        self.levels.verticalHeader().setMinimumSectionSize(24)
        self.levels.verticalHeader().setDefaultSectionSize(30)
        self.levels.setHorizontalHeaderLabels([f"{f} Hz" for f in OCTAVE_BANDS])
        self.levels.horizontalHeader().setMinimumSectionSize(60)
        self.levels.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        for r, level in enumerate((0, -6, -18)):
            for c in range(8):
                self.levels.setItem(r, c, QTableWidgetItem(str(level)))
        self.input_pages.addWidget(self.levels)
        self.forms.addWidget(page)
        self.input_pages.addWidget(QWidget())
        page = QWidget()
        form = QFormLayout(page)
        from .units import LengthSpinBox
        self.diameter = LengthSpinBox(input_unit)
        self.diameter.setRange(.001, 1000)
        self.diameter.setValue(.8)
        self.temperature = number(-272.99, 5000, 150)
        form.addRow("Opening diameter", self.diameter)
        form.addRow("Exhaust temperature (°C)", self.temperature)
        form.addRow(wrapped_label("Principal: Top (+Z) · Vertical upward-facing open outlet"))
        form.addRow(wrapped_label("Saves an input draft with unknown cells. Numerical Annex B generation is not yet available. "
                                 "The method edition and receiver-dependent downwind departure-angle rule remain attached."))
        self.forms.addWidget(page)
        footer = QHBoxLayout()
        footer.addWidget(QLabel("Output angular spacing"))
        self.spacing = QComboBox()
        for step in (5, 10, 15, 30, 45, 90):
            self.spacing.addItem(f"{step}°", step)
        self.spacing.setCurrentIndex(2)
        footer.addWidget(self.spacing)
        footer.addStretch()
        self.generate_button = QPushButton("Preview generated table")
        self.generate_button.clicked.connect(self.generate)
        footer.addWidget(self.generate_button)
        layout.addLayout(footer)
        self.review_label = wrapped_label("Generate to review the table before applying it.")
        layout.addWidget(self.review_label)
        self.review = QTableWidget()
        self.review.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.review.setFixedHeight(135)
        self.review.verticalHeader().setMinimumSectionSize(24)
        self.review.verticalHeader().setDefaultSectionSize(30)
        self.review.hide()
        layout.addWidget(self.review)
        self.error = wrapped_label("")
        self.error.hide()
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.use_button = buttons.button(QDialogButtonBox.Ok)
        self.use_button.setText("Use generated table")
        self.use_button.setEnabled(False)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.kind.currentIndexChanged.connect(self.change_kind)
        for widget in (self.axis, self.interpolation, self.spacing):
            widget.currentIndexChanged.connect(self.invalidate)
        for widget in (self.front, self.rear, self.diameter, self.temperature):
            widget.valueChanged.connect(self.invalidate)
        self.profile.itemChanged.connect(self.invalidate)
        self.levels.itemChanged.connect(self.invalidate)
        self.preview.band.currentIndexChanged.connect(self.update_polar)
        if generator:
            self.restore(generator)
        self.change_kind()

    def restore(self, generator):
        p = generator["parameters"]
        kind = generator["kind"]
        self.kind.setCurrentIndex(max(0, self.kind.findData(kind)))
        self.spacing.setCurrentIndex(max(0, self.spacing.findData(p.get("spacing_deg", 15))))
        if kind == "revolve" and "profile" in p:
            self.axis.setCurrentIndex(max(0, self.axis.findData(p.get("axis"))))
            self.interpolation.setCurrentIndex(max(0, self.interpolation.findData(p.get("interpolation"))))
            self.profile.setRowCount(len(p["profile"]))
            for r, row in enumerate(p["profile"]):
                for c, value in enumerate([row["angle_deg"]] + row["levels_db"]):
                    self.profile.setItem(r, c, QTableWidgetItem(str(value)))
        elif kind == "sectors" and "boundaries_deg" in p:
            self.front.setValue(p["boundaries_deg"][0])
            self.rear.setValue(p["boundaries_deg"][1])
            for r, row in enumerate(p["levels_db"]):
                for c, value in enumerate(row):
                    self.levels.setItem(r, c, QTableWidgetItem(str(value)))
        elif kind == "stack_iso_2024":
            self.diameter.setValue(p.get("opening_diameter_m", .8))
            self.temperature.setValue(p.get("exhaust_temperature_c", 150))

    def change_kind(self):
        self.forms.setCurrentIndex(self.kind.currentIndex())
        self.input_pages.setCurrentIndex(self.kind.currentIndex())
        stack = self.kind.currentData() == "stack_iso_2024"
        self.input_pages.setVisible(not stack)
        self.generate_button.setText("Preview draft table" if stack else "Preview generated table")
        self.use_button.setText("Use draft table" if stack else "Use generated table")
        self.invalidate()

    def invalidate(self, *args):
        self.value = None
        self.use_button.setEnabled(False)
        self.review.setRowCount(0)
        self.review.hide()
        self.review_label.setText("Inputs changed · generate to review the table.")
        self.preview.set_definition(model.definition())
        self.update_polar()
        self.error.clear()
        self.error.hide()

    def update_polar(self, *args):
        frequency = self.preview.band.currentData() or 1000
        column = OCTAVE_BANDS.index(frequency) + 1
        try:
            points = sorted((float(self.profile.item(r, 0).text()), float(self.profile.item(r, column).text()))
                            for r in range(self.profile.rowCount()))
            if any(not model.finite(a) or not 0 <= a <= 180 or not model.finite(v) for a, v in points):
                points = []
        except (ValueError, AttributeError):
            points = []
        self.polar.set_profile(points, frequency)

    def add_profile(self):
        row = self.profile.rowCount()
        self.profile.insertRow(row)
        for c in range(9):
            self.profile.setItem(row, c, QTableWidgetItem(""))
        self.profile.setCurrentCell(row, 0)

    def remove_profile(self):
        if self.profile.currentRow() >= 0:
            self.profile.removeRow(self.profile.currentRow())
            self.invalidate()

    def generate(self):
        try:
            kind, spacing = self.kind.currentData(), self.spacing.currentData()
            if kind == "revolve":
                profile = [{"angle_deg": float(self.profile.item(r, 0).text()),
                            "levels_db": [float(self.profile.item(r, c).text()) for c in range(1, 9)]}
                           for r in range(self.profile.rowCount())]
                self.value = model.revolve(profile, spacing, self.axis.currentData(), self.interpolation.currentData())
            elif kind == "sectors":
                levels = [[float(self.levels.item(r, c).text()) for c in range(8)] for r in range(3)]
                self.value = model.sectors([self.front.value(), self.rear.value()], levels, spacing)
            else:
                self.value = model.stack_draft(self.diameter.value(), self.temperature.value(), spacing)
            self.preview.set_definition(self.value)
            self.review.setColumnCount(10)
            self.review.setHorizontalHeaderLabels(["Azimuth °", "Elevation °"] + [f"{f} Hz" for f in OCTAVE_BANDS])
            self.review.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
            self.review.horizontalHeader().setMinimumSectionSize(65)
            self.review.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
            self.review.setRowCount(3)
            self.review.show()
            for r, row in enumerate(self.value["directions"][:3]):
                for c, value in enumerate([row["azimuth_deg"], row["elevation_deg"]] + row["levels_db"]):
                    level = kind == "revolve" and c >= 2
                    self.review.setItem(r, c, QTableWidgetItem("—" if value is None else f"{value:.1f}" if level else f"{value:g}"))
            self.review_label.setText(f'{len(self.value["directions"])} directions × 8 frequencies · '
                                      + model.status(self.value) + " · first three rows shown")
            self.use_button.setEnabled(True)
            self.error.clear()
            self.error.hide()
        except (ModelError, ValueError, AttributeError) as error:
            self.value = None
            self.use_button.setEnabled(False)
            self.error.setText(str(error) if isinstance(error, ModelError) else "Enter a number in each template cell.")
            self.error.show()
