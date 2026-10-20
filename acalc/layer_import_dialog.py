# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""External layer import options. The source remains owned by QGIS."""

from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QScrollArea, QVBoxLayout, QWidget,
)
from qgis.core import QgsProject

from .numeric_controls import DecimalInput
from .layer_import import DEFAULT_HEIGHTS, ImportOptions, compatible_kinds
from .schema import LAYER_TYPES, ModelError
from .spectra import compatible_power
from .storage import require_name
from .units import LengthSpinBox, input_unit, elevation_unit, length_factor, unit_combo
from .trees import DEFAULT_CANOPY_HEIGHT, TREES_GUIDANCE


class ImportNotes(QLabel):
    def resizeEvent(self, event):
        super().resizeEvent(event)
        # A scroll area's layout may use the unwrapped size hint. Reserve the
        # actual wrapped height so the final lines remain reachable by scrolling.
        self.setMinimumHeight(self.heightForWidth(self.width()))


class FieldOrValue(QWidget):
    def __init__(self, default, minimum=0, maximum=1000000, unit=None):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.field = QComboBox()
        self.value = LengthSpinBox(unit) if unit else DecimalInput()
        if not unit:
            self.value.setDecimals(4)
        self.value.setRange(minimum, maximum)
        self.value.setValue(default)
        layout.addWidget(self.field, 1)
        layout.addWidget(self.value)
        self.field.currentIndexChanged.connect(lambda: self.value.setEnabled(not self.field.currentData()))

    def set_fields(self, fields):
        self.field.clear()
        self.field.addItem("Use value", "")
        for field in fields:
            if field.isNumeric():
                self.field.addItem(field.name(), field.name())


class ImportLayerDialog(QDialog):
    def __init__(self, parent, store, project=None, active_layer=None, kind_key=None):
        super().__init__(parent)
        self.setWindowTitle("Import layer into Acalc")
        self.resize(610, 640)
        self.project = project or QgsProject.instance()
        self.spectra = store.spectra("sound_power")
        unit = elevation_unit(store)
        self.preferred_kind = kind_key
        layout = QVBoxLayout(self)
        intro = QLabel("Copy a vector layer already loaded in QGIS into this model. The original layer stays unchanged; current edits are included in the copy.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        content = QWidget()
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 8, 0)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        self.form = QFormLayout()
        body.addLayout(self.form)
        self.source = QComboBox()
        for layer in self.project.mapLayers().values():
            if compatible_kinds(layer):
                self.source.addItem(f"{layer.name()} · {layer.crs().authid() or 'custom/unknown CRS'}", layer.id())
        self.form.addRow("Source layer", self.source)
        self.selected_only = QCheckBox("Copy selected features only")
        self.form.addRow("", self.selected_only)
        self.kind = QComboBox()
        self.form.addRow("Acalc object type", self.kind)
        self.name = QLineEdit()
        self.form.addRow("New layer name", self.name)
        self.name_field = QComboBox()
        self.notes_field = QComboBox()
        self.form.addRow("Object names", self.name_field)
        self.form.addRow("Object notes", self.notes_field)
        self.ground_mode = QComboBox()
        for label, value in (("Use a ground elevation value", "constant"),
                             ("Use a ground elevation field", "field"),
                             ("Use geometry Z as ground elevation", "geometry_z")):
            self.ground_mode.addItem(label, value)
        self.form.addRow("Ground elevation", self.ground_mode)
        self.source_units = unit_combo(unit)
        self.form.addRow("Source Z / height field units", self.source_units)
        self.ground = FieldOrValue(0, -1000000, unit=unit)
        self.form.addRow("Ground Z", self.ground)
        self.height = FieldOrValue(1.5, unit=unit)
        self.form.addRow("Height above ground", self.height)
        self.ground_factor = FieldOrValue(0, 0, 1)
        self.form.addRow("Ground factor G", self.ground_factor)
        self.canopy_height = FieldOrValue(DEFAULT_CANOPY_HEIGHT, minimum=0.0001, unit=unit)
        self.canopy_height.setToolTip(TREES_GUIDANCE)
        self.form.addRow("Tree height", self.canopy_height)
        self.trees_guidance = ImportNotes(TREES_GUIDANCE)
        self.trees_guidance.setWordWrap(True)
        body.addWidget(self.trees_guidance)
        self.spacing = LengthSpinBox(input_unit(store))
        self.spacing.setRange(0.001, 1000000)
        self.spacing.setValue(10)
        self.form.addRow("Receiver spacing", self.spacing)
        self.spectrum = QComboBox()
        self.spectrum.setToolTip("Applied to every imported object, including each multipart part. Total power is not divided between parts.")
        self.form.addRow("Spectrum for each object", self.spectrum)
        note = ImportNotes(
            "X/Y are transformed to the model CRS. Select the units of source elevation/height fields and geometry Z above. Ground Z must use the model's vertical datum. Geometry M is ignored.\n\n"
            "Multipart features become separate objects. Curves, holes and invalid geometry are rejected. Original attributes are retained in the source_attributes JSON field.\n\n"
            "The copy is saved immediately. Add it to a variant and review its spectra, measurement levels and other acoustic properties before calculation.")
        note.setWordWrap(True)
        body.addWidget(note)
        body.addStretch()
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet("color: #b33;")
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Import copy")
        buttons.accepted.connect(self.validate)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.source.currentIndexChanged.connect(self.source_changed)
        self.kind.currentIndexChanged.connect(self.kind_changed)
        self.ground_mode.currentIndexChanged.connect(self.update_controls)
        if active_layer is not None:
            index = self.source.findData(active_layer.id())
            if index >= 0:
                self.source.setCurrentIndex(index)
        self.source_changed()

    def source_layer(self):
        return self.project.mapLayer(self.source.currentData())

    def source_changed(self, *args):
        layer = self.source_layer()
        preferred = self.kind.currentData() or self.preferred_kind
        self.kind.blockSignals(True)
        self.kind.clear()
        for kind in compatible_kinds(layer):
            self.kind.addItem(kind.title, kind.key)
        index = self.kind.findData(preferred)
        if index >= 0:
            self.kind.setCurrentIndex(index)
        self.kind.blockSignals(False)
        fields = layer.fields() if layer is not None else []
        self.name.setText(f"{layer.name()} (Acalc)" if layer is not None else "")
        for combo, empty, candidate in ((self.name_field, "Generate object names", "name"),
                                        (self.notes_field, "No notes", "notes")):
            combo.clear()
            combo.addItem(empty, "")
            for field in fields:
                combo.addItem(field.name(), field.name())
            index = combo.findData(candidate)
            if index >= 0:
                combo.setCurrentIndex(index)
        for control in (self.ground, self.height, self.ground_factor, self.canopy_height):
            control.set_fields(fields)
        count = layer.selectedFeatureCount() if layer is not None else 0
        self.selected_only.setChecked(False)
        self.selected_only.setEnabled(count > 0)
        self.selected_only.setText(f"Copy selected features only ({count} selected)")
        self.kind_changed()

    def kind_changed(self, *args):
        key = self.kind.currentData()
        self.height.value.setValue(DEFAULT_HEIGHTS.get(key, 0))
        self.spectrum.clear()
        self.spectrum.addItem("Assign later", "")
        for record in self.spectra:
            if key and LAYER_TYPES[key].spectrum and compatible_power(record["spectrum"], key):
                self.spectrum.addItem(record["name"], record["spectrum_id"])
        self.update_controls()

    def show_row(self, widget, visible):
        widget.setVisible(visible)
        self.form.labelForField(widget).setVisible(visible)

    def update_controls(self, *args):
        kind = LAYER_TYPES.get(self.kind.currentData())
        ground = bool(kind and kind.ground_points)
        self.show_row(self.ground_mode, ground)
        mode = self.ground_mode.currentData()
        self.show_row(self.ground, ground and mode in ("constant", "field"))
        self.ground.field.setVisible(mode == "field")
        self.ground.value.setVisible(mode == "constant")
        self.ground.value.setEnabled(True)
        self.show_row(self.height, bool(kind and kind.height))
        self.show_row(self.ground_factor, bool(kind and kind.key == "ground_absorption"))
        self.show_row(self.canopy_height, bool(kind and kind.key == "trees"))
        self.trees_guidance.setVisible(bool(kind and kind.key == "trees"))
        self.show_row(self.spacing, bool(kind and kind.key == "area_receiver"))
        self.show_row(self.spectrum, bool(kind and kind.spectrum))

    def options(self):
        return ImportOptions(
            kind_key=self.kind.currentData(), name=self.name.text().strip(),
            selected_only=self.selected_only.isChecked(),
            name_field=self.name_field.currentData() or "", notes_field=self.notes_field.currentData() or "",
            ground_mode=self.ground_mode.currentData(), ground_field=self.ground.field.currentData() or "",
            ground_z=self.ground.value.value(), height_field=self.height.field.currentData() or "",
            height=self.height.value.value(), ground_factor_field=self.ground_factor.field.currentData() or "",
            ground_factor=self.ground_factor.value.value(), spacing=self.spacing.value(),
            canopy_height_field=self.canopy_height.field.currentData() or "", canopy_height=self.canopy_height.value.value(),
            spectrum_id=self.spectrum.currentData() or "", vertical_to_m=length_factor(self.source_units.currentData()))

    def validate(self):
        try:
            require_name(self.name.text())
            if self.source_layer() is None or self.kind.currentData() is None:
                raise ModelError("Load a point, line or polygon vector layer into QGIS first.")
            if (LAYER_TYPES[self.kind.currentData()].ground_points and self.ground_mode.currentData() == "field"
                    and not self.ground.field.currentData()):
                raise ModelError("Choose the ground elevation field.")
        except ModelError as error:
            self.error.setText(str(error))
            return
        self.accept()
