# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Choose a model variant and export format independently of calculation APIs."""

from pathlib import Path

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout,
)

from .model_export import FORMATS, export_variant
from .schema import ModelError


class ExportDialog(QDialog):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.store = controller.require_store()
        self.setWindowTitle("Export model")
        self.resize(640, 450)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.variant = QComboBox()
        for row in self.store.variants():
            self.variant.addItem(row["name"], row["variant_id"])
        current = controller.selected_variant()
        if current:
            self.variant.setCurrentIndex(self.variant.findData(current["variant_id"]))
        form.addRow("Variant", self.variant)
        self.format = QComboBox()
        for identity, name in FORMATS:
            self.format.addItem(name, identity)
        form.addRow("Format", self.format)
        self.destination = QLineEdit()
        self.destination.setPlaceholderText("Choose a new folder for the exported files")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self.browse)
        destination_row = QHBoxLayout()
        destination_row.addWidget(self.destination, 1)
        destination_row.addWidget(browse)
        form.addRow("Export folder", destination_row)
        layout.addLayout(form)
        self.description = QLabel()
        self.description.setWordWrap(True)
        self.description.setAlignment(Qt.AlignTop)
        self.description.setMinimumHeight(170)
        layout.addWidget(self.description)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Close)
        self.export_button = self.buttons.addButton("Export", QDialogButtonBox.ActionRole)
        self.buttons.rejected.connect(self.reject)
        self.export_button.clicked.connect(self.export)
        layout.addWidget(self.buttons)
        self.format.currentIndexChanged.connect(self.format_changed)
        self.destination.textChanged.connect(self.update_enabled)
        self.variant.currentIndexChanged.connect(self.update_enabled)
        self.format_changed()

    def format_changed(self, *args):
        self.description.setText(
            "Exports acoustic objects, spectra and terrain for CadnaA 2021. Unsupported features or "
            "unresolved elevations stop export with an explanation. The folder includes import instructions "
            "and settings to match. Import into CadnaA has not yet been verified."
            if self.format.currentData() == "qsi" else
            "Exports authored GIS geometries with their ground Z and height M. Full attributes and spectra "
            "are included in a JSON snapshot. These shapefiles require setup in CadnaA; they are not a calculation-ready acoustic model.")
        self.update_enabled()

    def update_enabled(self, *args):
        self.export_button.setEnabled(bool(self.variant.currentData() and self.destination.text().strip()))

    def browse(self):
        parent = QFileDialog.getExistingDirectory(self, "Choose parent folder", str(Path(self.store.path).parent))
        if parent:
            name = "cadnaa-export" if self.format.currentData() == "qsi" else "shapefile-export"
            self.destination.setText(str(Path(parent) / name))

    def export(self):
        self.export_button.setEnabled(False)
        try:
            if self.controller.store is not self.store:
                raise ModelError("The project changed. Reopen the export window.")
            # Reuse edit-buffer protection; saved disk contents must match what
            # the user sees. The operation never calls a calculation service.
            if self.controller.prepare_calculation_layers(self) is None:
                return
            result = export_variant(self.store, self.variant.currentData(), self.destination.text().strip(),
                                    self.format.currentData())
            self.status.setText(f"Exported to {result}. See IMPORT.txt for import steps and comparison settings.")
            self.controller.message(f"Exported variant “{self.variant.currentText()}” to {result}")
        except (ModelError, OSError, ValueError, RuntimeError) as error:
            self.status.setText(str(error))
        finally:
            self.update_enabled()
            self.adjustSize()
