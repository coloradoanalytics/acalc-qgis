# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Small native dialogs for project creation and exact geometry entry."""

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QBrush, QColor, QPalette
from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
    QTextEdit, QVBoxLayout, QHeaderView, QAbstractItemView, QTabWidget, QWidget,
    QSplitter, QStyledItemDelegate, QStyle,
)
from qgis.core import QgsCoordinateReferenceSystem, QgsUnitTypes
from qgis.gui import QgsProjectionSelectionWidget

from .numeric_controls import DecimalInput
from .layers import geometry_from_rows, vertex_rows
from .storage import ModelError, require_name
from .spectra import OCTAVE_BANDS, POWER_BASES, compatible_power, missing_bands
from .spectrum_table import SpectrumTable
from .measurement import MEASUREMENT_FIELDS, broadband_from_octaves, resolved_measurement
from .decibels import rounded_db
from .spectra_ui import AbsorptionSpinBox
from .calculation_settings import REFLECTION_DEFAULTS
from .units import LengthSpinBox, length_factor, unit_combo, horizontal_unit, display_unit_name
from .object_geometry import (ABSOLUTE_HEIGHT_MODES, GROUND_MODES, HEIGHT_MODES,
                              PER_POINT_HEIGHT_MODES, feature_value, building_heights,
                              building_editor_rows)


def null_text(value):
    return "" if value is None or str(value) == "NULL" else str(value)


class InactiveCellDelegate(QStyledItemDelegate):
    """Keep calculated cells visibly grey, including within a selected row."""

    def __init__(self, parent, background, foreground):
        super().__init__(parent)
        self.background = QColor(background)
        self.foreground = QColor(foreground)

    def displayText(self, value, locale):
        try:
            return f"{float(value):.2f}"
        except (TypeError, ValueError):
            return str(value)

    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        if not (index.flags() & Qt.ItemIsEnabled and index.flags() & Qt.ItemIsEditable):
            option.backgroundBrush = QBrush(self.background)
            option.palette.setColor(QPalette.Text, self.foreground)
            option.state &= ~QStyle.State_Selected


class NewProjectDialog(QDialog):
    def __init__(self, parent, crs):
        super().__init__(parent)
        self.setWindowTitle("New Acalc project")
        self.resize(560, 340)
        layout = QVBoxLayout(self)
        intro = QLabel("Create a portable acoustical model. Choose a local projected CRS suited to the project site. Coordinates and distance inputs follow its units.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        form = QFormLayout()
        self.name = QLineEdit()
        self.author = QLineEdit()
        self.path = QLineEdit()
        browse = QPushButton("Browse…")
        browse.clicked.connect(self.browse)
        path_row = QHBoxLayout()
        path_row.addWidget(self.path)
        path_row.addWidget(browse)
        self.crs = QgsProjectionSelectionWidget()
        self.crs.setCrs(crs if crs.isValid() and not crs.isGeographic() else QgsCoordinateReferenceSystem())
        form.addRow("Project name", self.name)
        form.addRow("Author / organization", self.author)
        form.addRow("GeoPackage", path_row)
        form.addRow("Horizontal CRS", self.crs)
        self.horizontal_units = QLabel()
        self.crs.crsChanged.connect(lambda: self.horizontal_units.setText(display_unit_name(QgsUnitTypes.toString(self.crs.crs().mapUnits()))))
        self.horizontal_units.setText(display_unit_name(QgsUnitTypes.toString(self.crs.crs().mapUnits())))
        form.addRow("Input units (from CRS)", self.horizontal_units)
        self.elevation_units = unit_combo()
        form.addRow("Elevation / height units", self.elevation_units)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Create project")
        buttons.accepted.connect(self.validate)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def browse(self):
        path, _ = QFileDialog.getSaveFileName(self, "New model GeoPackage", self.path.text(), "GeoPackage (*.gpkg)")
        if path:
            self.path.setText(path if path.lower().endswith(".gpkg") else path + ".gpkg")

    def validate(self):
        try:
            require_name(self.name.text())
            if not self.path.text().strip():
                raise ModelError("Choose a filename for the GeoPackage.")
            if not self.path.text().lower().endswith(".gpkg"):
                self.path.setText(self.path.text() + ".gpkg")
            if not self.crs.crs().isValid() or self.crs.crs().isGeographic():
                raise ModelError("Choose a projected horizontal coordinate system.")
        except ModelError as error:
            QMessageBox.warning(self, "New project", str(error))
            return
        self.accept()


class ProjectCoordinatesDialog(QDialog):
    def __init__(self, parent, store):
        super().__init__(parent)
        from .units import elevation_unit
        self.setWindowTitle("Project CRS and units")
        self.resize(600, 300)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.crs = QgsProjectionSelectionWidget()
        self.crs.setCrs(QgsCoordinateReferenceSystem(store.metadata()["crs_wkt"]))
        self.elevation_units = unit_combo(elevation_unit(store))
        form.addRow("Horizontal CRS", self.crs)
        self.horizontal_units = QLabel()
        self.crs.crsChanged.connect(lambda: self.horizontal_units.setText(display_unit_name(QgsUnitTypes.toString(self.crs.crs().mapUnits()))))
        self.horizontal_units.setText(display_unit_name(QgsUnitTypes.toString(self.crs.crs().mapUnits())))
        form.addRow("Input units (from CRS)", self.horizontal_units)
        form.addRow("Elevation / height units", self.elevation_units)
        layout.addLayout(form)
        note = QLabel("Changing CRS reprojects all model objects, including archived layers. "
                      "Ground elevations and the vertical datum stay the same. "
                      "Coordinates and distance inputs use the CRS units. Elevations and object heights use the separate meters/feet setting. "
                      "Existing calculation results will be marked out of date.")
        note.setWordWrap(True)
        note.setMinimumHeight(note.heightForWidth(550))
        layout.addWidget(note)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.hide()
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.Apply | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Apply).clicked.connect(self.validate)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def validate(self):
        if not self.crs.crs().isValid() or self.crs.crs().isGeographic():
            self.error.setText("Choose a projected horizontal coordinate system.")
            self.error.show()
            return
        self.accept()


class FeatureDialog(QDialog):
    delete_requested = pyqtSignal()
    duplicate_requested = pyqtSignal()

    def __init__(self, parent, kind, crs_label, feature=None, spectra=(), results_context=None, object_actions=False,
                 horizontal_scale=1.0, model_wkt=None, elevation_rasters=(), reflection_default=None,
                 directivities=(), directivity_store=None, edit_directivity=None, elevation_unit="m", navigation_ids=(None, None)):
        super().__init__(parent)
        self.kind = kind
        self.input_unit = horizontal_unit(crs_label, horizontal_scale)
        self.elevation_unit = elevation_unit
        self.length_factor = length_factor(elevation_unit)
        self.geometry = None
        self.go_to_building = False
        self.go_to_object = False
        self.navigation_target = None
        self._needs_explicit_ground = bool(kind.ground_points and feature_value(feature, "ground_mode") == "unresolved")
        self.horizontal_scale = horizontal_scale
        self.model_wkt = model_wkt
        self.elevation_rasters = elevation_rasters
        self.vertex_buttons = []
        self.face_sources = []
        if kind.key == "source_building":
            from .building_faces import read_face_sources
            self.face_sources = read_face_sources(feature_value(feature, "face_sources"))
            self.face_power_spectra = spectra
        self._height_conversion_error = None
        self._updating_coordinates = False
        dark = self.palette().color(QPalette.Base).lightness() < 128
        inactive_background = "#424242" if dark else "#e2e2e2"
        inactive_foreground = "#b8b8b8" if dark else "#606060"
        self.setStyleSheet(
            "QAbstractSpinBox:disabled, QComboBox:disabled, QLineEdit:disabled, "
            "QTextEdit:disabled, QPushButton:disabled, "
            "QAbstractSpinBox[readOnly=\"true\"], QLineEdit[readOnly=\"true\"], "
            "QTextEdit[readOnly=\"true\"] { "
            f"background-color: {inactive_background}; color: {inactive_foreground}; }}")
        self.legacy_interiors = (feature is not None and kind.geometry == "Polygon"
                                 and feature.geometry().constGet().numInteriorRings() > 0)
        self.setWindowTitle(("Acalc properties · " if feature is not None else "Add ") + kind.singular)
        self.setModal(True)
        self.resize(1120 if kind.is_building else 800, 720 if kind.is_building else 680)
        if kind.key == "point_source":
            self.resize(1000, 840)
        if kind.key == "area_receiver":
            self.resize(1120, 840)
        if kind.key == "trees":
            self.resize(800, 800)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(null_text(feature["name"]) if feature is not None else kind.singular)
        self.notes = QTextEdit(null_text(feature["notes"]) if feature is not None else "")
        self.notes.setMaximumHeight(70)
        form.addRow("Name", self.name)
        if kind.is_building or kind.key == "trees":
            self.notes.setFixedHeight(52)
        if kind.key == "ground_absorption":
            self.notes.hide()
        else:
            form.addRow("Notes", self.notes)
        if kind.key == "ground_absorption":
            self.ground_factor = DecimalInput()
            self.ground_factor.setRange(0, 1)
            self.ground_factor.setDecimals(4)
            self.ground_factor.setSingleStep(0.1)
            self.ground_factor.setValue(float(feature_value(feature, "ground_factor", 0)))
            self.ground_factor.setToolTip("Ground factor: 0 = hard ground, 1 = porous ground.")
            form.addRow("G", self.ground_factor)
        if kind.key == "trees":
            from .trees import DEFAULT_CANOPY_HEIGHT, TREES_GUIDANCE, TREES_HINT, canopy_height
            self.canopy_height = LengthSpinBox(elevation_unit)
            self.canopy_height.setRange(0.0001, 1000000)
            self.canopy_height.setValue(canopy_height(feature_value(feature, "canopy_height", DEFAULT_CANOPY_HEIGHT)))
            self.canopy_height.setToolTip(TREES_GUIDANCE)
            form.addRow("Tree height", self.canopy_height)
            self.trees_guidance = QLabel(TREES_HINT)
            self.trees_guidance.setToolTip(TREES_GUIDANCE)
            self.trees_guidance.setWordWrap(True)
            form.addRow(self.trees_guidance)
        if kind.is_building or kind.key == "barrier":
            if reflection_default is None:
                reflection_default = REFLECTION_DEFAULTS["building_reflection_coefficient" if kind.is_building else "barrier_reflection_coefficient"]
            coefficient = feature_value(feature, "reflection_coefficient")
            if coefficient is not None:
                import math
                if type(coefficient) not in (int, float) or not math.isfinite(coefficient) or not 0 <= coefficient <= 1:
                    raise ModelError("Reflection coefficient must be a finite number between 0 and 1, or NULL to use the calculation default.")
            reflection = QHBoxLayout()
            self.use_default_reflection = QCheckBox(f"Use calculation default (α = {1 - reflection_default:g})")
            self.use_default_reflection.setChecked(coefficient is None)
            self.absorption_coefficient = AbsorptionSpinBox()
            self.absorption_coefficient.set_reflection(reflection_default if coefficient is None else coefficient)
            self.absorption_coefficient.setEnabled(coefficient is not None)
            self.absorption_coefficient.setToolTip("Absorption in every octave band: 0 = fully reflecting, 1 = fully absorbing. Clear ‘Use calculation default’ to set an object override. An assigned absorption spectrum overrides this setting.")
            self._reflection_override = self.absorption_coefficient.reflection()

            def reflection_default_changed(checked):
                if checked:
                    self._reflection_override = self.absorption_coefficient.reflection()
                self.absorption_coefficient.set_reflection(reflection_default if checked else self._reflection_override)
                self.absorption_coefficient.setEnabled(not checked)

            self.use_default_reflection.toggled.connect(reflection_default_changed)
            reflection.addWidget(self.use_default_reflection)
            reflection.addWidget(self.absorption_coefficient)
            if not kind.is_building:
                form.addRow("Absorption coefficient α", reflection)
        layout.addLayout(form)
        self.tabs = QTabWidget()
        if kind.is_building:
            from .building_preview import BuildingPreview
            self.building_splitter = QSplitter(Qt.Horizontal)
            self.building_splitter.setChildrenCollapsible(False)
            self.building_splitter.addWidget(self.tabs)
            preview_page = QWidget()
            preview_layout = QVBoxLayout(preview_page)
            preview_layout.setContentsMargins(8, 0, 0, 0)
            preview_header = QHBoxLayout()
            preview_title = QLabel("3D building preview")
            title_font = preview_title.font()
            title_font.setBold(True)
            preview_title.setFont(title_font)
            preview_header.addWidget(preview_title)
            preview_header.addStretch()
            if kind.key == "source_building":
                self.face_sources_button = QPushButton("Sources")
                self.face_sources_button.setVisible(False)
                self.face_sources_button.clicked.connect(self.edit_face_sources)
                preview_header.addWidget(self.face_sources_button)
                self.unattached_sources_button = QPushButton("Unattached sources…")
                self.unattached_sources_button.setVisible(False)
                self.unattached_sources_button.clicked.connect(self.edit_unattached_sources)
                preview_header.addWidget(self.unattached_sources_button)
            self.reset_preview_button = QPushButton("Reset view")
            preview_header.addWidget(self.reset_preview_button)
            preview_layout.addLayout(preview_header)
            self.building_preview = BuildingPreview(preview_page, face_selection_enabled=kind.key == "source_building")
            self.building_preview.setObjectName("buildingPreview")
            if kind.key == "source_building":
                self.building_preview.face_selected.connect(self.update_face_selection)
            self.building_preview.sources = self.face_sources
            self.building_preview.setAccessibleName("Interactive 3D building preview")
            self.building_preview.setAccessibleDescription(
                ("Click a face to select it and edit Sources. " if kind.key == "source_building" else "")
                + "Drag to rotate the building. Middle-drag or Shift-drag to pan. Scroll to zoom. Double-click to reset the view.")
            self.reset_preview_button.clicked.connect(self.building_preview.reset_view)
            preview_layout.addWidget(self.building_preview, 1)
            interaction_hint = QLabel(
                ("Click a face to select  ·  " if kind.key == "source_building" else "")
                + "Drag to rotate  ·  Scroll to zoom\nMiddle-drag or Shift-drag to pan"
                + ("\nBlue: selected face · Orange: sources · 0, 0: face origin"
                   if kind.key == "source_building" else ""))
            interaction_hint.setWordWrap(True)
            preview_layout.addWidget(interaction_hint)
            self.preview_status = QLabel()
            self.preview_status.setWordWrap(True)
            preview_layout.addWidget(self.preview_status)
            self.building_splitter.addWidget(preview_page)
            self.building_splitter.setStretchFactor(0, 1)
            self.building_splitter.setStretchFactor(1, 1)
            self.building_splitter.setSizes([510, 570])
            layout.addWidget(self.building_splitter, 1)
        else:
            layout.addWidget(self.tabs, 1)
        coordinate_page = QWidget()
        coordinate_layout = QVBoxLayout(coordinate_page)
        self.tabs.addTab(coordinate_page, "Coordinates")
        units = QLabel(f"X / Y and distances: {display_unit_name(crs_label)} · Elevations and heights: {elevation_unit}")
        units.setWordWrap(True)
        coordinate_layout.addWidget(units)
        ground_hint = QLabel("Z is ground elevation; height is the distance above that ground." if kind.height else "Z is ground elevation.")
        if kind.is_building:
            ground_hint.setText("Roof elevation = ground Z + height.")
        ground_hint.setWordWrap(True)
        if kind.is_building:
            units.setToolTip(ground_hint.text())
            ground_hint.deleteLater()
        elif kind.ground_points:
            coordinate_layout.addWidget(ground_hint)
        else:
            ground_hint.deleteLater()
        if kind.geometry == "Polygon" and not kind.is_building:
            hint = QLabel("Enter the perimeter vertices in order. The polygon closes automatically.")
            hint.setWordWrap(True)
            coordinate_layout.addWidget(hint)
        controls = QFormLayout()
        if kind.ground_points:
            self.ground_mode = QComboBox()
            modes = {"per_point": "Specify"} if kind.geometry == "Point" else GROUND_MODES
            for key, label in modes.items():
                self.ground_mode.addItem(label, key)
            ground_mode = feature_value(feature, "ground_mode", "per_point")
            if self._needs_explicit_ground:
                ground_mode = "per_point"
            self.ground_mode.setCurrentIndex(max(0, self.ground_mode.findData(ground_mode)))
            tips = {"per_point": "Edit ground Z for each point.",
                    "uniform": "Edit ground Z in the first row; all other points use that elevation."}
            for index in range(self.ground_mode.count()):
                self.ground_mode.setItemData(index, tips[self.ground_mode.itemData(index)], Qt.ToolTipRole)
            controls.addRow("Ground Z" if kind.geometry == "Point" else "Ground points", self.ground_mode)
            coordinate_layout.addLayout(controls)
            self.raster_button = QPushButton("Take elevations from raster…")
            self.raster_button.setToolTip("Interpolate ground Z from a raster layer, then use Apply to save the coordinates.")
            self.raster_button.setEnabled(bool(model_wkt))
            self.raster_button.clicked.connect(self.take_raster_elevations)
            if kind.is_building:
                geometry_actions = QHBoxLayout()
                self.square_corners_button = QPushButton("Square Corners")
                self.square_corners_button.setToolTip("Adjust X/Y to right angles when every corner is within 10° of 90°. Apply to save the coordinates.")
                self.square_corners_button.clicked.connect(self.square_corners)
                geometry_actions.addWidget(self.square_corners_button)
                geometry_actions.addWidget(self.raster_button)
                coordinate_layout.addLayout(geometry_actions)
            else:
                coordinate_layout.addWidget(self.raster_button)
        if kind.height_controls:
            self.height_mode = QComboBox()
            for key, label in HEIGHT_MODES.items():
                if kind.key == "barrier" and key not in ("average", "individual", "absolute_individual"):
                    continue
                self.height_mode.addItem(label, key)
            self.height_mode.setCurrentIndex(self.height_mode.findData(feature_value(feature, "height_mode", "individual")))
            self.height_value = LengthSpinBox(elevation_unit)
            self.height_value.setRange(-1000000 if self.height_mode.currentData() in ABSOLUTE_HEIGHT_MODES else 0,
                                       1000000)
            self.height_value.setKeyboardTracking(False)
            self.height_value.setValue(float(feature_value(feature, "height_value", 3 if kind.key == "barrier" else 6)))
            controls.addRow("Height mode", self.height_mode)
            controls.addRow("Nominal height" if kind.key == "barrier" else "H", self.height_value)
        if kind.key == "area_receiver":
            controls = QFormLayout()
            self.height_m = LengthSpinBox(elevation_unit)
            self.spacing_m = LengthSpinBox(self.input_unit)
            for widget, name, default, minimum, label in (
                    (self.height_m, "height_m", 1.5, 0, "Height above ground"),
                    (self.spacing_m, "spacing_m", 10, 0.0001, "Point spacing")):
                widget.setRange(minimum, 1000000)
                widget.setValue(float(feature_value(feature, name, default)))
                controls.addRow(label, widget)
            coordinate_layout.addLayout(controls)
            hint = QLabel("Calculation points follow the terrain at one uniform height above ground. The perimeter defines only the grid boundary; points inside or on buildings are excluded.")
            hint.setWordWrap(True)
            coordinate_layout.addWidget(hint)
        self.columns = ["X", "Y"] + ([f"Ground Z ({elevation_unit})"] if kind.ground_points else []) + ([f"Height ({elevation_unit})"] if kind.height and kind.key != "area_receiver" else [])
        self.table = QTableWidget(0, len(self.columns))
        self.table.setItemDelegate(InactiveCellDelegate(self.table, inactive_background, inactive_foreground))
        self.table.setHorizontalHeaderLabels(self.columns)
        self.table.horizontalHeader().setMinimumSectionSize(75)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setMinimumHeight(175)
        if kind.is_building:
            self.table.setMinimumHeight(220)
            self.table.verticalHeader().setDefaultSectionSize(max(28, self.table.fontMetrics().height() + 10))
            self.table.setToolTip("Enter perimeter vertices in order. The polygon closes automatically.")
            self.table.itemSelectionChanged.connect(self.update_preview_selection)
        coordinate_layout.addWidget(self.table, 1)
        if kind.geometry != "Point":
            row_actions = QHBoxLayout()
            for label, slot in (("Add vertex", self.add_row), ("Remove vertex", self.remove_row), ("Move up", lambda: self.move_row(-1)), ("Move down", lambda: self.move_row(1))):
                button = QPushButton(label)
                button.clicked.connect(slot)
                self.vertex_buttons.append(button)
                row_actions.addWidget(button)
            coordinate_layout.addLayout(row_actions)
        if kind.is_building or kind.key == "barrier":
            self.absorption_records = {row["spectrum_id"]: row for row in spectra
                                       if row["spectrum"]["kind"] == "absorption"}
            material_page = QWidget()
            material_layout = QVBoxLayout(material_page)
            if kind.is_building:
                material_form = QFormLayout()
                material_form.addRow("Absorption coefficient α", reflection)
                material_layout.addLayout(material_form)
            material_layout.addWidget(QLabel("Surface absorption spectrum α"))
            self.absorption_combo = QComboBox()
            self.absorption_combo.addItem("No spectrum — use single absorption value", None)
            for row in self.absorption_records.values():
                suffix = " (unknown bands)" if missing_bands(row["spectrum"]) else ""
                self.absorption_combo.addItem(row["name"] + suffix, row["spectrum_id"])
            reference = null_text(feature_value(feature, "absorption_spectrum_id"))
            if reference and reference not in self.absorption_records:
                self.absorption_combo.addItem("Unavailable or incompatible absorption spectrum", reference)
            self.absorption_combo.setCurrentIndex(max(0, self.absorption_combo.findData(reference or None)))
            material_layout.addWidget(self.absorption_combo)
            scope = "all building facades" if kind.is_building else "both sides of every wall segment"
            description = QLabel(f"Applies to {scope}. Clear the assignment to use the single absorption value above.")
            description.setWordWrap(True)
            self.absorption_combo.setToolTip("An assigned spectrum overrides the single absorption value and calculation default. Manage shared materials in the Spectra view.")
            material_layout.addWidget(description)
            self.absorption_preview = SpectrumTable(1, len(OCTAVE_BANDS))
            self.absorption_preview.setHorizontalHeaderLabels([f"{band} Hz" for band in OCTAVE_BANDS])
            self.absorption_preview.verticalHeader().hide()
            self.absorption_preview.horizontalHeader().setMinimumSectionSize(60)
            self.absorption_preview.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
            self.absorption_preview.setFixedHeight(95)
            self.absorption_preview.setEditTriggers(QAbstractItemView.NoEditTriggers)
            material_layout.addWidget(self.absorption_preview)
            self.absorption_status = QLabel()
            self.absorption_status.setTextFormat(Qt.PlainText)
            self.absorption_status.setWordWrap(True)
            material_layout.addWidget(self.absorption_status)
            material_layout.addStretch()
            self.absorption_combo.currentIndexChanged.connect(self.refresh_absorption)
            self.refresh_absorption()
            self.tabs.addTab(material_page, "Surface material")
            if kind.is_building:
                # A full-width material table keeps all eight band headings
                # visible; the 3D preview accompanies the coordinate editor.
                self.tabs.currentChanged.connect(lambda index: preview_page.setVisible(self.tabs.widget(index) is coordinate_page))
        if kind.spectrum:
            self.spectrum_records = {row["spectrum_id"]: row for row in spectra
                if row["spectrum"]["kind"] == "sound_power" and compatible_power(row["spectrum"], kind.key)}
            spectrum_page = QWidget()
            spectrum_layout = QVBoxLayout(spectrum_page)
            spectrum_layout.addWidget(QLabel("Sound power spectrum"))
            reference = null_text(feature["spectrum_id"]) if feature is not None else ""
            self.spectrum_combo = QComboBox()
            self.spectrum_combo.addItem("No spectrum assigned", None)
            for row in self.spectrum_records.values():
                suffix = " (unknown bands)" if missing_bands(row["spectrum"]) else ""
                self.spectrum_combo.addItem(row["name"] + suffix, row["spectrum_id"])
            if reference and reference not in self.spectrum_records:
                self.spectrum_combo.addItem("Unavailable or incompatible spectrum", reference)
            self.spectrum_combo.setCurrentIndex(max(0, self.spectrum_combo.findData(reference or None)))
            spectrum_layout.addWidget(self.spectrum_combo)
            description = QLabel("Choose a compatible sound power spectrum. Total power is distributed over the source’s length or area. Manage shared spectra in the Spectra view.")
            description.setWordWrap(True)
            spectrum_layout.addWidget(description)
            self.spectrum_preview = SpectrumTable(1, len(OCTAVE_BANDS))
            self.spectrum_preview.setHorizontalHeaderLabels([f"{band} Hz" for band in OCTAVE_BANDS])
            self.spectrum_preview.horizontalHeader().setMinimumSectionSize(60)
            self.spectrum_preview.horizontalHeader().setDefaultSectionSize(85)
            self.spectrum_preview.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
            self.spectrum_preview.verticalHeader().hide()
            self.spectrum_preview.verticalHeader().setMinimumSectionSize(28)
            self.spectrum_preview.verticalHeader().setSectionResizeMode(QHeaderView.Stretch)
            self.spectrum_preview.setMinimumHeight(90)
            self.spectrum_preview.setMaximumHeight(110)
            self.spectrum_preview.setEditTriggers(QAbstractItemView.NoEditTriggers)
            spectrum_layout.addWidget(self.spectrum_preview)
            self.spectrum_status = QLabel()
            self.spectrum_status.setWordWrap(True)
            spectrum_layout.addWidget(self.spectrum_status)
            spectrum_layout.addStretch()
            self.spectrum_combo.currentIndexChanged.connect(self.refresh_spectrum)
            self.refresh_spectrum()
            self.tabs.addTab(spectrum_page, "Spectrum")
        if kind.key == "point_source":
            from .directivity_ui import SourceDirectivityWidget
            from qgis.PyQt.QtWidgets import QScrollArea
            self.directivity = SourceDirectivityWidget(directivities,
                feature_value(feature, "directivity_id"), feature_value(feature, "directivity_orientation"),
                directivity_store, edit_directivity)
            self.directivity_page = QScrollArea()
            self.directivity_page.setWidgetResizable(True)
            self.directivity_page.setFrameShape(QScrollArea.NoFrame)
            self.directivity_page.setWidget(self.directivity)
            self.tabs.addTab(self.directivity_page, "Directionality")
        if kind.key == "measurement":
            self._legacy_broadband_z = feature_value(feature, "broadband_z_db") if feature is not None else None
            measurement_page = QWidget()
            measurement_layout = QVBoxLayout(measurement_page)
            description = QLabel("Enter observed sound-pressure levels in dB re 20 µPa. Blank means unknown; 0 is a measured 0 dB level. dBA updates as octave values are entered. Record measurement conditions and provenance in Notes.")
            description.setWordWrap(True)
            measurement_layout.addWidget(description)
            overall = QFormLayout()
            self.measurement_a = QLineEdit()
            value = feature_value(feature, "broadband_a_db")
            self.measurement_a.setText("" if value is None else str(value))
            overall.addRow("dBA", self.measurement_a)
            measurement_layout.addLayout(overall)
            measurement_layout.addWidget(QLabel("Unweighted octave-band levels (dB)"))
            self.measurement_bands = SpectrumTable(1, len(MEASUREMENT_FIELDS))
            self.measurement_bands.setHorizontalHeaderLabels([f"{band} Hz" for band in OCTAVE_BANDS])
            self.measurement_bands.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
            for column, field in enumerate(MEASUREMENT_FIELDS):
                value = feature_value(feature, field)
                self.measurement_bands.setItem(0, column, QTableWidgetItem("" if value is None else str(value)))
            measurement_layout.addWidget(self.measurement_bands)
            self.measurement_status = QLabel()
            self.measurement_status.setWordWrap(True)
            measurement_layout.addWidget(self.measurement_status)
            measurement_layout.addStretch()
            self._measurement_had_octaves = False
            self.measurement_bands.itemChanged.connect(self.update_measurement_broadband)
            self.update_measurement_broadband()
            self.tabs.addTab(measurement_page, "Measured levels")
        if feature is not None:
            for row in vertex_rows(feature.geometry(), kind):
                if row[0] == 0:
                    self.add_values(row)
        else:
            for _ in range({"Point": 1, "LineString": 2, "Polygon": 3}[kind.geometry]):
                self.add_row()
        if self._needs_explicit_ground:
            warning = QLabel("Ground Z values are required. Enter explicit elevations or take elevations from a raster before applying changes.")
            warning.setWordWrap(True)
            coordinate_layout.addWidget(warning)
            for row in range(self.table.rowCount()):
                self.table.item(row, 2).setText("")
        if kind.ground_points:
            self.ground_mode.currentIndexChanged.connect(self.update_coordinate_controls)
        self.table.itemChanged.connect(self.update_coordinate_controls)
        if kind.height_controls:
            self.height_mode.currentIndexChanged.connect(self.update_coordinate_controls)
            self.height_value.valueChanged.connect(self.apply_nominal_height)
            self.height_value.valueChanged.connect(self.update_coordinate_controls)
        if kind.key == "area_receiver":
            self.height_m.valueChanged.connect(self.update_coordinate_controls)
        self.update_coordinate_controls()
        if kind.is_building:
            self.building_preview.reset_view()
        if kind.key in ("point_receiver", "area_receiver", "measurement"):
            from .calculation_ui import ReceiverResultsPage
            self.receiver_results = ReceiverResultsPage(results_context, area=kind.key == "area_receiver",
                measurement_values=self.measurement_values if kind.key == "measurement" else None)
            if kind.key == "measurement":
                self.measurement_bands.itemChanged.connect(self.receiver_results.update_measurement_comparison)
                self.measurement_a.textChanged.connect(self.receiver_results.update_measurement_comparison)
            if self.receiver_results.area_view is not None:
                self.finished.connect(self.receiver_results.area_view.dispose)
            self.tabs.addTab(self.receiver_results, "Measured vs calculated" if kind.key == "measurement" else "Results")
        self.error = QLabel()
        self.error.setStyleSheet("color: #c24f36;")
        self.error.setWordWrap(True)
        self.error.hide()
        layout.addWidget(self.error)
        save_hint = QLabel("Apply updates the layer’s pending edits. Use Save layer edits in Acalc to save them to the GeoPackage.")
        save_hint.setWordWrap(True)
        save_hint.setStyleSheet("font-size: 12px;")
        layout.addWidget(save_hint)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Apply to layer")
        if object_actions:
            browsing = QHBoxLayout()
            self.previous_button = QPushButton("← Previous")
            self.next_button = QPushButton("Next →")
            for button, target, direction in ((self.previous_button, navigation_ids[0], "previous"),
                                               (self.next_button, navigation_ids[1], "next")):
                button.setEnabled(target is not None)
                button.setToolTip(f"Apply changes to the layer and open the {direction} object on this layer.")
                button.clicked.connect(lambda checked=False, identity=target: self.apply_and_navigate(identity))
                browsing.addWidget(button)
            browsing.addStretch()
            layout.addLayout(browsing)
            self.go_to_button = buttons.addButton("Go to " + kind.singular, QDialogButtonBox.ActionRole)
            self.go_to_button.setToolTip("Apply changes to the layer, close this window, and zoom to this object.")
            self.go_to_button.clicked.connect(self.apply_and_go_to_object)
            if kind.is_building:
                self.go_to_building_button = self.go_to_button
            self.delete_button = buttons.addButton("Delete", QDialogButtonBox.ActionRole)
            self.delete_button.clicked.connect(self.delete_requested.emit)
            self.duplicate_button = buttons.addButton("Duplicate", QDialogButtonBox.ActionRole)
            self.duplicate_button.setToolTip("Create a copy of the object as it currently exists in the layer, then open its properties.")
            self.duplicate_button.clicked.connect(self.duplicate_requested.emit)
        buttons.accepted.connect(self.validate)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def apply_and_go_to_object(self):
        self.validate()
        self.go_to_building = self.result() == QDialog.Accepted
        self.go_to_object = self.go_to_building

    def apply_and_navigate(self, target):
        if target is None:
            return
        self.validate()
        if self.result() == QDialog.Accepted:
            self.navigation_target = target

    def refresh_spectrum(self, *args):
        record = self.spectrum_records.get(self.spectrum_combo.currentData())
        value = record["spectrum"] if record else None
        for column, band in enumerate(OCTAVE_BANDS):
            text = "Unknown"
            if value:
                if value["values"][column] is not None:
                    text = str(value["values"][column])
            item = QTableWidgetItem(text)
            item.setTextAlignment(Qt.AlignCenter)
            item.setToolTip(f"{band} Hz: {text}" + (" " + value["unit"] if value and text not in ("Unknown", "Zero energy") else ""))
            self.spectrum_preview.setItem(0, column, item)
        if record:
            count = len(missing_bands(value))
            self.spectrum_status.setText(POWER_BASES[value["power_basis"]] + " · 0 = no energy\n" + (f"{count} band(s) still unknown. " if count else "All bands specified. ") + value["provenance"])
        else:
            self.spectrum_status.setText("Assign a project sound power spectrum before calculation. Geometry can be saved without one.")

    def refresh_absorption(self):
        reference = self.absorption_combo.currentData()
        record = self.absorption_records.get(reference)
        value = record["spectrum"] if record else None
        self.use_default_reflection.setEnabled(not reference)
        self.absorption_coefficient.setEnabled(not reference and not self.use_default_reflection.isChecked())
        for column, band in enumerate(OCTAVE_BANDS):
            alpha = value["values"][column] if value else None
            text = "Unknown" if alpha is None else f"{alpha:g}"
            item = QTableWidgetItem(text)
            item.setTextAlignment(Qt.AlignCenter)
            item.setToolTip(f"{band} Hz: α = {text}")
            self.absorption_preview.setItem(0, column, item)
        if record:
            missing = missing_bands(value)
            status = ("Calculation unavailable: absorption is unknown at " + ", ".join(map(str, missing)) + " Hz."
                      if missing else "All bands specified. α = 0 fully reflects; α = 1 fully absorbs.")
            self.absorption_status.setText(status + "\nReference: " + (value["provenance"] or "Not specified"))
        elif reference:
            self.absorption_status.setText("Calculation unavailable: the assigned absorption spectrum is missing or incompatible. Choose a project absorption spectrum or clear this assignment.")
        else:
            self.absorption_status.setText("No absorption spectrum assigned. The single absorption value above applies to every octave band.")

    def square_corners(self):
        from .building_geometry import square_footprint
        self.square_corners_button.setFocus()
        try:
            if self.legacy_interiors:
                raise ModelError("Buildings with interior boundaries are unsupported.")
            points = [(float(self.table.item(row, 0).text()), float(self.table.item(row, 1).text()))
                      for row in range(self.table.rowCount())]
            squared = square_footprint(points)
        except (ModelError, ValueError, AttributeError) as error:
            reason = str(error) if isinstance(error, ModelError) else "Enter a number in every X and Y cell."
            QMessageBox.information(self, "Square Corners", "Square corners isn't available. " + reason)
            return
        self._updating_coordinates = True
        try:
            for row, point in enumerate(squared):
                for column, value in enumerate(point):
                    self.table.item(row, column).setText(str(value))
        finally:
            self._updating_coordinates = False
        self.error.clear()
        self.update_coordinate_controls()

    def take_raster_elevations(self):
        from .terrain_ui import RasterSelectionDialog
        from .raster_elevation import RasterElevation
        # Moving focus commits any active coordinate editor before sampling.
        self.raster_button.setFocus()
        dialog = RasterSelectionDialog(self, self.elevation_rasters)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            record = dialog.selected_raster()
            points = [(float(self.table.item(row, 0).text()), float(self.table.item(row, 1).text()))
                      for row in range(self.table.rowCount())]
            if not points:
                raise ModelError("Add ground points before sampling a raster.")
            with RasterElevation(record["path"], self.model_wkt, record["band"]) as raster:
                elevations = raster.elevations(points)
            self.apply_raster_elevations(elevations)
            self.error.clear()
        except (ModelError, ValueError, AttributeError) as error:
            self.error.setText(str(error) if isinstance(error, ModelError) else "Enter a number in every X and Y cell before sampling.")

    def apply_raster_elevations(self, elevations):
        """Change Z atomically; heights and absolute roof entries retain their meaning."""
        import math
        if len(elevations) != self.table.rowCount() or not all(math.isfinite(z) for z in elevations):
            raise ModelError("The raster must provide a finite elevation for every ground point.")
        self._updating_coordinates = True
        try:
            self.ground_mode.setCurrentIndex(self.ground_mode.findData("per_point"))
            for row, z in enumerate(elevations):
                self.table.item(row, 2).setText(str(z if self.length_factor == 1 else z / self.length_factor))
        finally:
            self._updating_coordinates = False
        self.update_coordinate_controls()

    def add_values(self, row):
        ring, x, y, z, height = row
        if self.length_factor != 1:
            z, height = z / self.length_factor, height / self.length_factor
        absolute_point = self.kind.height_controls and self.height_mode.currentData() == "absolute_individual"
        if absolute_point:
            height = z + height
        values = [x, y] + ([z] if self.kind.ground_points else []) + ([height] if self.kind.height and self.kind.key != "area_receiver" else [])
        index = self.table.rowCount()
        self._updating_coordinates = True
        try:
            self.table.insertRow(index)
            for col, value in enumerate(values):
                # Python's float string is the shortest exact round trip, avoiding
                # distracting representations such as 1500.5999999999999.
                self.table.setItem(index, col, QTableWidgetItem(str(value)))
                if self.kind.height_controls and col == 3:
                    self.table.item(index, col).setData(Qt.UserRole + 1,
                                                      "absolute_individual" if absolute_point else "individual")
        finally:
            self._updating_coordinates = False
        self.update_coordinate_controls()

    def add_row(self):
        height = {"point_source": 1.0, "line_source": 0.5, "area_source": 0.5, "point_receiver": 1.5, "measurement": 1.5, "barrier": 3.0, "building": 6.0, "source_building": 6.0}.get(self.kind.key, 0.0)
        if self.kind.key == "barrier":
            height = self.height_value.value()
        self.add_values((0, "", "", 0.0, height))

    def remove_row(self):
        for row in sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True):
            self.table.removeRow(row)
        self.update_coordinate_controls()

    def move_row(self, direction):
        row = self.table.currentRow()
        target = row + direction
        if row < 0 or not 0 <= target < self.table.rowCount():
            return
        self._updating_coordinates = True
        try:
            for col in range(self.table.columnCount()):
                first = self.table.takeItem(row, col)
                second = self.table.takeItem(target, col)
                self.table.setItem(row, col, second)
                self.table.setItem(target, col, first)
        finally:
            self._updating_coordinates = False
        self.table.selectRow(target)
        self.update_coordinate_controls()

    def update_coordinate_controls(self, *args):
        if self._updating_coordinates:
            return
        self._updating_coordinates = True
        try:
            self._update_coordinate_controls()
        finally:
            self._updating_coordinates = False
        if self.kind.is_building:
            self.update_building_preview()

    def update_preview_selection(self):
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        self.building_preview.set_selected_vertex(rows[0] if len(rows) == 1 else None)

    def update_face_selection(self, face_id):
        if self.kind.key != "source_building":
            return
        self.face_sources_button.setVisible(face_id is not None)
        if face_id:
            self.face_sources_button.setToolTip("Edit point and area sources on " + self.building_preview.faces[face_id]["name"])

    def edit_face_sources(self):
        if self.kind.key != "source_building":
            return
        from .face_sources_ui import FaceSourcesDialog
        face_id = self.building_preview.selected_face
        if face_id is None:
            return
        dialog = FaceSourcesDialog(self, face_id, self.building_preview.faces,
                                   len(self.building_preview.vertices) // 2,
                                   [s for s in self.face_sources if s["face"] == face_id], self.face_power_spectra,
                                   input_unit=self.input_unit)
        if dialog.exec() == QDialog.Accepted:
            self.face_sources = [s for s in self.face_sources if s["face"] != face_id] + dialog.sources
            self.building_preview.set_sources(self.face_sources)
            self.update_building_preview()

    def unattached_sources(self):
        count = len(self.building_preview.vertices) // 2
        return [source for source in self.face_sources
                if source['face'] not in self.building_preview.faces or source['vertex_count'] != count]

    def edit_unattached_sources(self):
        if self.kind.key != "source_building":
            return
        from .face_sources_ui import UnattachedSourcesDialog
        sources = self.unattached_sources()
        if not sources:
            return
        dialog = UnattachedSourcesDialog(self, sources, self.building_preview.faces,
                                         len(self.building_preview.vertices) // 2)
        if dialog.exec() == QDialog.Accepted:
            previous = {source['id'] for source in sources}
            changed = {source['id']: source for source in dialog.sources}
            self.face_sources = [changed.get(source['id'], source) for source in self.face_sources
                                 if source['id'] not in previous or source['id'] in changed]
            self.building_preview.set_sources(self.face_sources)
            self.update_building_preview()
            originals = {source['id']: source for source in sources}
            targets = list(dict.fromkeys(source['face'] for source in dialog.sources
                                         if source != originals[source['id']]
                                         and source['face'] in self.building_preview.faces))
            if targets:
                names = ', '.join(self.building_preview.faces[identity]['name'] for identity in targets)
                guidance = f"Sources reassigned to {names}. Select that surface and use Sources to adjust position; each source must fit before applying the building."
                self.preview_status.setText(self.preview_status.text() + '\n' + guidance)
                if len(targets) == 1:
                    self.building_preview.set_selected_face(targets[0])

    def update_building_preview(self):
        """Draw committed cell values; an active editor does not alter the mesh."""
        for button in self.vertex_buttons:
            button.setEnabled(not self.face_sources)
            button.setToolTip("Remove attached sources before adding, removing, or reordering perimeter vertices." if self.face_sources else "")
        self.preview_status.setText(f"Ground Z and building height inputs use {self.elevation_unit}.")
        try:
            if self._height_conversion_error:
                raise ModelError("Waiting to convert per-point heights: " + self._height_conversion_error)
            if self.legacy_interiors:
                raise ModelError("Preview unavailable for a building with interior boundaries.")
            mode = self.height_mode.currentData()
            shared = mode not in PER_POINT_HEIGHT_MODES

            def cell(row, column):
                value = float(self.table.item(row, column).text())
                return value if column < 2 else value * self.length_factor

            rows = [(0, cell(row, 0), cell(row, 1),
                     cell(row, 2),
                     0.0 if shared else cell(row, 3)) for row in range(self.table.rowCount())]
            rows = building_editor_rows(rows, mode, self.height_value.value())
            self.building_preview.set_building(rows, self.horizontal_scale)
            if self.building_preview.source_errors:
                self.preview_status.setText("\n".join(self.building_preview.source_errors))
        except (ModelError, ValueError, AttributeError, OverflowError) as error:
            self.building_preview.clear(str(error) if isinstance(error, ModelError)
                                        else "Enter a number in every coordinate cell to preview the building.")
        if self.kind.key == "source_building":
            self.unattached_sources_button.setVisible(bool(self.unattached_sources()))

    def apply_nominal_height(self):
        if self.kind.key != "barrier" or self.height_mode.currentData() != "individual":
            return
        self._updating_coordinates = True
        try:
            for row in range(self.table.rowCount()):
                self.table.item(row, 3).setText(str(self.height_value.value() / self.length_factor))
                self.table.item(row, 3).setData(Qt.UserRole, None)
                self.table.item(row, 3).setData(Qt.UserRole + 1, "individual")
        finally:
            self._updating_coordinates = False

    def _update_coordinate_controls(self):
        self._height_conversion_error = None
        if not self.kind.ground_points:
            return
        shared = self.kind.height_controls and self.height_mode.currentData() not in PER_POINT_HEIGHT_MODES
        uniform_ground = self.ground_mode.currentData() == "uniform"
        if self.kind.height_controls:
            self.height_value.setEnabled(shared or (self.kind.key == "barrier" and self.height_mode.currentData() == "individual"))
            absolute = self.height_mode.currentData() in ABSOLUTE_HEIGHT_MODES
            self.height_value.setMinimum(-1000000 if absolute else 0)
            self.height_value.setToolTip(f"Absolute roof elevation in {self.elevation_unit}." if absolute else
                                         f"Height above the selected ground Z reference, in {self.elevation_unit}.")
        if self.kind.key == "barrier":
            self.height_value.setMinimum(0)
            self.height_value.setToolTip("Nominal height above ground. In Relative per point mode, changing this fills every point; individual heights can then be edited.")
        if uniform_ground and self.table.rowCount():
            first = self.table.item(0, 2)
            if first is not None:
                for row in range(1, self.table.rowCount()):
                    item = self.table.item(row, 2)
                    if item is not None:
                        item.setText(first.text())
        roofs = None
        if shared and self.height_mode.currentData() == "absolute":
            roofs = [self.height_value.value() / self.length_factor] * self.table.rowCount()
        elif shared and self.table.rowCount():
            try:
                rows = [(0, 0, 0, float(self.table.item(row, 2).text()), 0) for row in range(self.table.rowCount())]
                roofs = [row[3] + row[4] for row in building_heights(rows, self.height_mode.currentData(), self.height_value.value() / self.length_factor)]
            except (ValueError, AttributeError, ModelError):
                pass
        for row in range(self.table.rowCount()):
            for col in (2, 3) if self.kind.height else (2,):
                item = self.table.item(row, col)
                if item is not None:
                    conversion_pending = False
                    if col == 3 and self.kind.height_controls:
                        if shared:
                            if item.data(Qt.UserRole) is None:
                                item.setData(Qt.UserRole, item.text())
                            item.setText(str(roofs[row]) if roofs is not None else "Calculated")
                        else:
                            mode = self.height_mode.currentData()
                            value = item.data(Qt.UserRole)
                            value = item.text() if value is None else value
                            basis = item.data(Qt.UserRole + 1) or "individual"
                            if basis != mode:
                                try:
                                    z = float(self.table.item(row, 2).text())
                                    value = str(float(value) + z if mode == "absolute_individual" else float(value) - z)
                                except ModelError as error:
                                    self._height_conversion_error = str(error)
                                    conversion_pending = True
                                except (ValueError, AttributeError):
                                    value = ""
                            item.setText(value)
                            if not conversion_pending:
                                item.setData(Qt.UserRole, None)
                                item.setData(Qt.UserRole + 1, mode)
                    disabled = (uniform_ground and row > 0) if col == 2 else shared or conversion_pending
                    item.setFlags(Qt.ItemIsSelectable if disabled else
                                  Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemIsEditable)
                    item.setToolTip("Enter ground Z before converting this height; the original value is retained"
                                    if conversion_pending else
                                    "Uses the first point's ground Z" if col == 2 and disabled else
                                    ("Top elevation calculated from nominal height" if self.kind.key == "barrier" else "Roof elevation calculated from H") if disabled else
                                    f"Absolute {'top' if self.kind.key == 'barrier' else 'roof'} elevation in {self.elevation_unit}" if col == 3 and self.kind.height_controls and self.height_mode.currentData() == "absolute_individual" else "")
        if self.kind.height_controls:
            self.columns[3] = f"Height ({self.elevation_unit})" if self.height_mode.currentData() == "individual" else f"Roof Z ({self.elevation_unit})"
            if self.kind.key == "barrier":
                self.columns[3] = self.columns[3].replace("Roof Z", "Top Z")
            self.table.horizontalHeaderItem(3).setText(self.columns[3])

    def object_attributes(self):
        if self.kind.key == "ground_absorption":
            return {"ground_factor": self.ground_factor.value()}
        if self.kind.key == "trees":
            return {"canopy_height": self.canopy_height.value()}
        if self.kind.key == "area_receiver":
            return {"height_m": self.height_m.value(), "spacing_m": self.spacing_m.value()}
        values = {"ground_mode": self.ground_mode.currentData()}
        if self.kind.key == "point_source":
            values.update(self.directivity.attributes())
        if self.kind.height_controls:
            values.update(height_mode=self.height_mode.currentData(), height_value=self.height_value.value())
        if self.kind.key == "source_building":
            import json
            values["face_sources"] = json.dumps(self.face_sources, allow_nan=False)
        if self.kind.is_building or self.kind.key == "barrier":
            values["reflection_coefficient"] = None if self.use_default_reflection.isChecked() else self.absorption_coefficient.reflection()
            values["absorption_spectrum_id"] = self.absorption_combo.currentData()
        if self.kind.key == "measurement":
            values.update(self.measurement_values())
        return values

    @staticmethod
    def measurement_number(text, field):
        text = text.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError as error:
            raise ModelError(f"{field}: enter a number or leave it blank.") from error

    def measurement_octaves(self):
        return [self.measurement_number(self.measurement_bands.item(0, index).text(), f"{band} Hz")
                for index, band in enumerate(OCTAVE_BANDS)]

    def update_measurement_broadband(self, *args):
        try:
            levels = self.measurement_octaves()
            any_octave = any(level is not None for level in levels)
            if any_octave:
                dba = broadband_from_octaves(levels)
                self.measurement_a.setText(f"{rounded_db(dba):.1f}")
                self.measurement_status.setText("dBA is calculated from the entered octave bands.")
            else:
                if self._measurement_had_octaves:
                    self.measurement_a.clear()
                self.measurement_status.setText("With no octave bands, dBA can be entered manually.")
            self.measurement_a.setReadOnly(any_octave)
            self._measurement_had_octaves = any_octave
        except ModelError as error:
            self.measurement_a.clear()
            self.measurement_a.setReadOnly(True)
            self.measurement_status.setText(str(error))

    def measurement_values(self):
        levels = self.measurement_octaves()
        dba = self.measurement_number(self.measurement_a.text(), "dBA") if all(level is None for level in levels) else None
        values = {"broadband_a_db": dba, "broadband_z_db": self._legacy_broadband_z}
        values.update(zip(MEASUREMENT_FIELDS, levels))
        return resolved_measurement(values)

    def validate(self):
        try:
            if self._height_conversion_error:
                self.update_coordinate_controls()
                if self._height_conversion_error:
                    raise ModelError("Wait for terrain before applying the height conversion: " + self._height_conversion_error)
            require_name(self.name.text())
            if self.kind.key == "point_source":
                self.directivity.attributes()
            if self.legacy_interiors:
                raise ModelError("This legacy polygon contains interior boundaries. Its geometry has been preserved; replace it with a perimeter polygon to edit it here.")
            if self.kind.spectrum and self.spectrum_combo.currentData() and self.spectrum_combo.currentData() not in self.spectrum_records:
                raise ModelError("Choose an available sound power spectrum, or clear the unavailable assignment.")
            if self.kind.key == "measurement":
                self.measurement_values()
            rows = []
            for row in range(self.table.rowCount()):
                x, y = [float(self.table.item(row, col).text()) for col in (0, 1)]
                if not self.kind.ground_points:
                    z = 0.0
                else:
                    z = float(self.table.item(row, 2).text())
                shared = self.kind.height_controls and self.height_mode.currentData() not in PER_POINT_HEIGHT_MODES
                height = float(self.table.item(row, 3).text()) if self.kind.height and self.kind.ground_points and not shared else 0.0
                z, height = z * self.length_factor, height * self.length_factor
                if self.kind.key == "area_receiver":
                    height = self.height_m.value()
                ring = 0
                rows.append((ring, x, y, z, height))
            if self.kind.height_controls:
                rows = building_editor_rows(rows, self.height_mode.currentData(), self.height_value.value())
                if self.kind.is_building and self.face_sources:
                    self.update_building_preview()
                    if not self.building_preview.vertices:
                        raise ModelError("Resolve building preview geometry before applying attached sources.")
                    if self.building_preview.source_errors:
                        raise ModelError("\n".join(self.building_preview.source_errors))
            self.geometry = geometry_from_rows(rows, self.kind)
        except (ModelError, ValueError, AttributeError) as error:
            self.error.setText(str(error) if isinstance(error, ModelError) else "Enter a number in every coordinate cell.")
            self.error.show()
            return
        self.accept()
