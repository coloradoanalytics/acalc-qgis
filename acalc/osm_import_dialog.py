# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Select an OSM download area and review buildings before saving a model layer."""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)
from qgis.core import QgsCoordinateReferenceSystem
from qgis.gui import QgsExtentWidget

from .layer_import import ImportOptions
from .layer_import_dialog import ImportNotes
from .osm_download import OsmDownload
from .osm_import import build_osm_layer, validate_bbox
from .schema import ModelError
from .storage import require_name
from .units import LengthSpinBox, elevation_unit


class OsmImportDialog(QDialog):
    def __init__(self, parent, store, canvas):
        super().__init__(parent)
        self.setWindowTitle("Import OpenStreetMap buildings")
        self.resize(650, 720)
        self._payload = None
        self._source = None
        self.report = None
        self.store = store
        self.raster_record = None
        self.download = OsmDownload(self)
        self.download.succeeded.connect(self.downloaded)
        self.download.failed.connect(self.download_failed)
        layout = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        content = QWidget()
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 8, 0)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        intro = ImportNotes("Select the current map extent, draw on the map, or enter longitude / latitude bounds (WGS 84) using the area menu. Maximum area: 100 km².")
        intro.setWordWrap(True)
        body.addWidget(intro)
        self.inputs = QWidget()
        form = QFormLayout(self.inputs)
        form.setContentsMargins(0, 0, 0, 0)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self.extent = QgsExtentWidget(self, QgsExtentWidget.CondensedStyle)
        crs = canvas.mapSettings().destinationCrs()
        self.extent.setOriginalExtent(canvas.extent(), crs)
        self.extent.setCurrentExtent(canvas.extent(), crs)
        self.extent.setOutputCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
        self.extent.setMapCanvas(canvas)
        self.extent.setOutputExtentFromCurrent()
        self.extent.toggleDialogVisibility.connect(self.setVisible)
        form.addRow("Area", self.extent)
        self.name = QLineEdit("OpenStreetMap buildings")
        form.addRow("New layer name", self.name)
        self.use_osm_height = QCheckBox("Use OSM height tags when available")
        self.use_osm_height.setChecked(True)
        form.addRow("Building heights", self.use_osm_height)
        unit = elevation_unit(store)
        self.default_height = LengthSpinBox(unit)
        self.default_height.setRange(0.01, 10000)
        self.default_height.setValue(6)
        form.addRow("Default height above ground", self.default_height)
        self.ground_mode = QComboBox()
        self.ground_mode.addItem("Use a ground elevation value", "constant")
        self.ground_mode.addItem("Take elevations from raster", "raster")
        form.addRow("Ground elevation", self.ground_mode)
        self.ground_z = LengthSpinBox(unit)
        self.ground_z.setRange(-1000000, 1000000)
        self.ground_z.setValue(0)
        self.ground_z.setEnabled(False)
        form.addRow("Ground Z", self.ground_z)
        self.raster_controls = QWidget()
        raster_layout = QHBoxLayout(self.raster_controls)
        raster_layout.setContentsMargins(0, 0, 0, 0)
        self.raster_button = QPushButton("Choose raster…")
        self.raster_button.clicked.connect(self.choose_raster)
        raster_layout.addWidget(self.raster_button)
        self.raster_label = QLabel("No raster selected")
        self.raster_label.setTextFormat(Qt.PlainText)
        self.raster_label.setWordWrap(True)
        raster_layout.addWidget(self.raster_label, 1)
        form.addRow("Elevation raster", self.raster_controls)
        self.ground_mode.currentIndexChanged.connect(self.update_ground_controls)
        self.update_ground_controls()
        body.addWidget(self.inputs)
        self.download_button = QPushButton("Download buildings")
        self.download_button.clicked.connect(self.start_download)
        body.addWidget(self.download_button)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        body.addWidget(self.progress)
        self.summary = ImportNotes("Download to preview the import. Choose Import buildings to save the new layer.")
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.PlainText)
        body.addWidget(self.summary)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setPlaceholderText("Skipped outlines will appear here.")
        self.details.setMinimumHeight(90)
        self.details.setMaximumHeight(140)
        body.addWidget(self.details)
        note = ImportNotes("Missing or unusable heights use the default; floor counts are not converted. One height applies across each footprint. Review heights and ground elevations before calculating. Constant Ground Z and raster elevations must use the model's vertical datum.\n\n"
                           "Raster elevations are sampled at every building vertex when importing, using the same interpolation as the building editor. Every vertex must have valid coverage before the layer is saved.\n\n"
                           "Full outlines are kept at the boundary. Unsupported outlines, including courtyards, are skipped and listed above. Importing the same area again creates another copy.")
        note.setWordWrap(True)
        body.addWidget(note)
        attribution = ImportNotes('Data © <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a> · <a href="https://opendatacommons.org/licenses/odbl/1-0/">ODbL</a>. Downloads use the public Overpass API.')
        attribution.setWordWrap(True)
        attribution.setOpenExternalLinks(True)
        body.addWidget(attribution)
        body.addStretch()
        self.error = QLabel()
        self.error.setTextFormat(Qt.PlainText)
        self.error.setWordWrap(True)
        self.error.setStyleSheet("color: #b33;")
        layout.addWidget(self.error)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.import_button = self.buttons.button(QDialogButtonBox.Ok)
        self.import_button.setText("Import buildings")
        self.import_button.setEnabled(False)
        self.buttons.accepted.connect(self.validate)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.extent.extentChanged.connect(self.area_changed)
        self.default_height.valueChanged.connect(self.rebuild_preview)
        self.use_osm_height.toggled.connect(self.rebuild_preview)

    def bbox(self):
        if not self.extent.isValid():
            raise ModelError("Choose an area to download.")
        rect = self.extent.outputExtent()
        bbox = (rect.yMinimum(), rect.xMinimum(), rect.yMaximum(), rect.xMaximum())
        validate_bbox(bbox)
        return bbox

    def area_changed(self, *args):
        self.download.cancel()
        self._payload = self._source = self.report = None
        self.set_busy(False)
        self.summary.setText("Area changed. Download buildings to preview this area.")
        self.details.clear()
        self.error.clear()

    def set_busy(self, busy):
        self.inputs.setEnabled(not busy)
        self.download_button.setEnabled(not busy)
        self.progress.setVisible(busy)
        self.import_button.setEnabled(not busy and self._source is not None and self._source.featureCount() > 0)

    def start_download(self):
        try:
            require_name(self.name.text())
            bbox = self.bbox()
            self.area_changed()
            self.summary.setText("Downloading OpenStreetMap buildings… Cancel closes the dialog and stops the download.")
            self.set_busy(True)
            self.download.start(bbox)
        except (ModelError, ValueError, RuntimeError) as error:
            self.download_failed(str(error))

    def downloaded(self, payload):
        self._payload = payload
        self.set_busy(False)
        self.rebuild_preview()

    def download_failed(self, message):
        self._payload = self._source = self.report = None
        self.set_busy(False)
        self.summary.setText("No buildings imported.")
        self.error.setText(message)

    def rebuild_preview(self, *args):
        if self._payload is None:
            return
        self._source = self.report = None
        self.import_button.setEnabled(False)
        self.error.clear()
        try:
            self._source, self.report = build_osm_layer(
                self._payload, default_height=self.default_height.value(),
                use_osm_height=self.use_osm_height.isChecked())
        except (ModelError, ValueError, RuntimeError) as error:
            self.download_failed(str(error))
            return
        report = self.report
        self.summary.setText(f"{report.imported_count} buildings ready · {len(report.skipped)} outlines skipped.\n"
                             f"{report.tagged_height_count} use OSM heights; {report.default_height_count} use your default height.")
        self.details.setPlainText("\n".join(report.skipped))
        if not report.imported_count:
            self.error.setText("No supported building outlines were found. Choose another area or review the skipped outlines.")
        self.import_button.setEnabled(report.imported_count > 0)

    def source_layer(self):
        return self._source

    def update_ground_controls(self, *args):
        mode = self.ground_mode.currentData()
        self.ground_z.setEnabled(mode == "constant")
        self.raster_controls.setEnabled(mode == "raster")

    def choose_raster(self):
        from .terrain_ui import RasterSelectionDialog
        dialog = RasterSelectionDialog(self, self.store.elevation_rasters())
        try:
            if dialog.exec() != QDialog.Accepted:
                return
            self.raster_record = dict(dialog.selected_raster())
            self.raster_label.setText(f"{self.raster_record['name']} · band {self.raster_record['band']}")
            self.raster_label.setToolTip(self.raster_record["path"])
            self.error.clear()
        except ModelError as error:
            self.error.setText(str(error))
        finally:
            dialog.deleteLater()

    def options(self):
        mode = self.ground_mode.currentData()
        if mode == "raster" and self.raster_record is None:
            raise ModelError("Choose an elevation raster before importing.")
        return ImportOptions("building", self.name.text().strip(), name_field="name", notes_field="notes",
                             ground_mode=mode, ground_z=self.ground_z.value(), height_field="height_m",
                             building_height_mode="average",
                             raster_path=self.raster_record["path"] if mode == "raster" else "",
                             raster_band=self.raster_record["band"] if mode == "raster" else 1)

    def validate(self):
        try:
            require_name(self.name.text())
            if self._source is None or not self._source.featureCount():
                raise ModelError("Download an area containing supported buildings first.")
            self.options()
        except ModelError as error:
            self.error.setText(str(error))
            return
        self.accept()

    def done(self, result):
        self.download.cancel()
        super().done(result)
