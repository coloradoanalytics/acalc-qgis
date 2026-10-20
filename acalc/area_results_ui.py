# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""A surface preview with one nearest-sample readout, never a point feature layer."""

from osgeo import gdal
from qgis.PyQt.QtCore import Qt, QTemporaryDir, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox
from qgis.core import QgsGeometry, QgsPointXY, QgsRasterLayer, QgsWkbTypes
from qgis.gui import QgsMapCanvas, QgsMapToolPan, QgsRubberBand, QgsVertexMarker

from .area_results import AreaSurface, BAND_NAMES, style_surface
from .schema import ModelError


class AreaResultView(QWidget):
    sample_changed = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.surface = self.layer = None
        self.directory = QTemporaryDir()
        self.revision = 0
        self.setMinimumHeight(340)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        controls = QHBoxLayout()
        controls.addWidget(QLabel("Display"))
        self.mode = QComboBox()
        self.mode.addItem("Filled areas · 5 dB bands", "areas")
        self.mode.addItem("Contour lines · 5 dB", "lines")
        self.mode.currentIndexChanged.connect(self.update_style)
        controls.addWidget(self.mode)
        self.band = QComboBox()
        for index, name in enumerate(BAND_NAMES, 1):
            self.band.addItem(name, index)
        self.band.currentIndexChanged.connect(self.update_style)
        controls.addWidget(self.band)
        controls.addStretch()
        layout.addLayout(controls)
        self.canvas = QgsMapCanvas()
        self.canvas.setCanvasColor(QColor("#f7f8fa"))
        self.canvas.setMinimumHeight(230)
        self.canvas.setMouseTracking(True)
        self.canvas.setMapTool(QgsMapToolPan(self.canvas))
        self.canvas.xyCoordinates.connect(self.queue_hover)
        self.canvas.viewport().installEventFilter(self)
        layout.addWidget(self.canvas, 1)
        self.outline = QgsRubberBand(self.canvas, QgsWkbTypes.PolygonGeometry)
        self.outline.setColor(QColor("#304258"))
        self.outline.setFillColor(QColor(0, 0, 0, 0))
        self.outline.setWidth(2)
        self.marker = QgsVertexMarker(self.canvas)
        self.marker.setIconType(QgsVertexMarker.ICON_CROSS)
        self.marker.setColor(QColor("#17263a"))
        self.marker.setPenWidth(2)
        self.marker.hide()
        self.caption = QLabel("Hover over the area to inspect the closest calculated point.")
        self.caption.setWordWrap(True)
        layout.addWidget(self.caption)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(40)
        self.timer.timeout.connect(self.show_hover)
        self.cursor = None

    def closeEvent(self, event):
        self.dispose()
        super().closeEvent(event)

    def dispose(self):
        self.timer.stop()
        self.canvas.stopRendering()
        self.canvas.setLayers([])
        if self.layer:
            self.layer.setParent(None)
            self.layer = None
        self.surface = None

    def set_area(self, store, area):
        self.canvas.stopRendering()
        self.timer.stop()
        self.cursor = None
        self.surface = None
        self.marker.hide()
        self.outline.reset(QgsWkbTypes.PolygonGeometry)
        self.canvas.setLayers([])
        if self.layer:
            self.layer.deleteLater()
            self.layer = None
        self.sample_changed.emit(None)
        if area is None:
            self.caption.setText("No saved surface for this area in the selected variant.")
            return
        try:
            self.surface = AreaSurface(area, store.metadata()["horizontal_to_m"])
            self.revision += 1
            path = self.directory.filePath(f"surface-{self.revision}.tif")
            dataset = gdal.Translate(path, self.surface.dataset(area.get("coordinate_reference", store.metadata())["crs_wkt"]), format="GTiff")
            if dataset is None:
                raise ModelError("Could not create the area preview.")
            dataset = None
            self.layer = QgsRasterLayer(path, "Area results", "gdal")
            self.layer.setParent(self)
            if not self.layer.isValid():
                raise ModelError("Could not load the area preview.")
            self.update_style()
            self.canvas.setDestinationCrs(self.layer.crs())
            self.canvas.setLayers([self.layer])
            self.canvas.setExtent(self.layer.extent())
            if self.surface.boundary:
                geometry = QgsGeometry()
                geometry.fromWkb(bytes(self.surface.boundary.ExportToWkb()))
                self.outline.setToGeometry(geometry, self.layer.crs())
            self.canvas.refresh()
            self.update_caption()
        except (ModelError, ValueError, RuntimeError) as error:
            self.surface = None
            self.caption.setText(str(error))

    def update_style(self, *args):
        if self.layer:
            self.canvas.stopRendering()
            style_surface(self.layer, self.mode.currentData(), band=self.band.currentData())
            self.canvas.refresh()
            self.update_caption()

    def update_caption(self):
        if self.surface:
            unit = "dBA" if self.band.currentData() == 1 else "dB"
            self.caption.setText(("Contour interval: 5 dB. " if self.mode.currentData() == "lines" else
                                  f"Colours: 5 dB bands, blue ≤30 → red >80 {unit}. ") +
                                 "Hover to inspect the closest calculated point."
                                 if self.surface.sample_count else "No calculated points remain in this area after building exclusions.")

    def queue_hover(self, point):
        self.cursor = (point.x(), point.y())
        if not self.timer.isActive():
            self.timer.start()

    def show_hover(self):
        found = self.surface.sample_at(*self.cursor) if self.surface and self.cursor else None
        if found is None:
            self.marker.hide()
            self.sample_changed.emit(None)
            return
        x, y, sample = found
        self.marker.setCenter(QgsPointXY(float(x), float(y)))
        self.marker.show()
        self.sample_changed.emit(sample)

    def eventFilter(self, obj, event):
        from qgis.PyQt.QtCore import QEvent
        if event.type() == QEvent.Leave:
            self.timer.stop()
            self.cursor = None
            self.marker.hide()
            self.sample_changed.emit(None)
        return super().eventFilter(obj, event)
