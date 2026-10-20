# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Whole-layer ground Z choices, with project elevation display units."""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QPushButton, QVBoxLayout

from .terrain_ui import RasterSelectionDialog, available_elevation_rasters
from .units import LengthSpinBox, elevation_unit


class DialogNote(QLabel):
    def __init__(self, text=""):
        super().__init__(text)
        self.setTextFormat(Qt.PlainText)
        self.setWordWrap(True)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.setMinimumHeight(self.heightForWidth(self.width()))


class SetLayerZDialog(QDialog):
    def __init__(self, parent, store, layer):
        super().__init__(parent)
        self.setWindowTitle("Set Z values")
        self.resize(520, 400)
        layout = QVBoxLayout(self)
        count = layer.featureCount()
        layout.addWidget(DialogNote(
            f'Set ground Z for {count} {"object" if count == 1 else "objects"} on “{layer.name()}”. All objects are included, regardless of selection or filter.'))
        form = QFormLayout()
        layout.addLayout(form)
        self.method = QComboBox()
        self.method.addItem("Set all Z values to a fixed value", "constant")
        self.method.addItem("Take Z values from a raster", "raster")
        form.addRow("Method", self.method)
        self.value = LengthSpinBox(elevation_unit(store))
        self.value.setRange(-1000000, 1000000)
        form.addRow("Ground Z", self.value)
        self.raster = QComboBox()
        self.raster.setMinimumContentsLength(20)
        self.raster.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        for record in available_elevation_rasters(store.elevation_rasters()):
            self.raster.addItem(record["name"], record)
        form.addRow("Elevation raster", self.raster)
        self.choose_raster = QPushButton("Choose elevation raster…")
        self.choose_raster.clicked.connect(self.select_raster)
        form.addRow("", self.choose_raster)
        self.raster_hint = DialogNote(
            "Ground Z is interpolated at every vertex. Choose a raster that covers every object and uses the model’s vertical datum.")
        layout.addWidget(self.raster_hint)
        layout.addWidget(DialogNote(
            "Object heights and building roof settings retain their meaning."))
        layout.addWidget(DialogNote(
            "Apply creates one undoable layer edit. Use Save layer edits to save the changes."))
        self.error = DialogNote()
        self.error.setStyleSheet("color: #c24f36;")
        layout.addWidget(self.error)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("Apply to layer")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.method.currentIndexChanged.connect(self.update_controls)
        self.update_controls()

    def select_raster(self):
        dialog = RasterSelectionDialog(self)
        try:
            if dialog.exec() == QDialog.Accepted:
                record = dialog.selected_raster()
                self.raster.addItem(record['name'], record)
                self.raster.setCurrentIndex(self.raster.count() - 1)
                self.update_controls()
        finally:
            dialog.deleteLater()

    def update_controls(self):
        raster = self.method.currentData() == "raster"
        self.choose_raster.setEnabled(raster)
        self.value.setEnabled(not raster)
        self.raster.setEnabled(raster and self.raster.count() > 0)
        self.raster_hint.setVisible(raster)
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(not raster or self.raster.count() > 0)
        self.error.clear()

    def options(self):
        if self.method.currentData() == "raster":
            return {"raster_record": self.raster.currentData()}
        return {"value": self.value.value()}
