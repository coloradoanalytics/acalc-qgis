# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Terrain object editors and bounded raster-to-terrain imports."""

from pathlib import Path

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout,
    QLabel, QListWidget, QListWidgetItem, QMenu, QPushButton, QTabWidget, QToolButton, QVBoxLayout, QWidget,
)
from qgis.core import QgsProject, QgsRasterLayer

from .schema import ModelError
from .layer_presentation import layer_label


def wrapped_label(text):
    label = QLabel(text)
    label.setWordWrap(True)
    return label


def available_elevation_rasters(records=()):
    """Saved references and local QGIS rasters, sharing one chooser contract."""
    result = [dict(record) for record in records]
    seen = {(record["path"], record["band"]) for record in records}
    layers = sorted(QgsProject.instance().mapLayers().values(), key=lambda layer: layer.name().casefold())
    for layer in layers:
        if not isinstance(layer, QgsRasterLayer) or not layer.isValid() or layer.providerType() != "gdal":
            continue
        path = str(Path(layer.source()).resolve())
        if not Path(path).is_file() or (path, 1) in seen:
            continue
        result.append({"name": layer.name(), "path": path, "band": 1})
        seen.add((path, 1))
    return result


class RasterSelectionDialog(QDialog):
    def __init__(self, parent, records=()):
        super().__init__(parent)
        self.setWindowTitle("Choose elevation raster")
        self.resize(440, 180)
        layout = QVBoxLayout(self)
        layout.addWidget(wrapped_label("Choose the raster to use for ground elevations. Values are interpolated from nearby pixels."))
        self.raster = QComboBox()
        for record in available_elevation_rasters(records):
            self.raster.addItem(record["name"], record)
        layout.addWidget(self.raster)
        browse = QPushButton("Choose raster file…")
        browse.clicked.connect(self.browse)
        layout.addWidget(browse)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setEnabled(self.raster.count() > 0)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.raster.currentIndexChanged.connect(
            lambda: buttons.button(QDialogButtonBox.Ok).setEnabled(self.raster.currentData() is not None))

    def browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose elevation raster", "", "Raster files (*.tif *.tiff *.img *.vrt *.asc);;All files (*)")
        if path:
            record = {"path": str(Path(path).resolve()), "name": Path(path).stem, "band": 1}
            self.raster.addItem(record["name"], record)
            self.raster.setCurrentIndex(self.raster.count() - 1)

    def selected_raster(self):
        record = self.raster.currentData()
        if record is None:
            raise ModelError("Choose an elevation raster.")
        return record


class RastersPage(QWidget):
    """Saved terrain imports; raster files are transient generation inputs."""

    def __init__(self, terrain):
        super().__init__(terrain)
        self.terrain = terrain
        self.controller = terrain.controller
        self.records = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        selector = QHBoxLayout()
        self.layer_combo = QComboBox()
        self.layer_combo.setAccessibleName("Generated terrain layer")
        self.layer_combo.currentIndexChanged.connect(self.layer_selected)
        selector.addWidget(self.layer_combo, 1)
        self.layer_menu_button = QToolButton()
        self.layer_menu_button.setText("☰")
        self.layer_menu_button.setToolTip("Layer commands")
        self.layer_menu_button.setAccessibleName("Layer commands")
        self.layer_menu_button.setPopupMode(QToolButton.InstantPopup)
        self.layer_menu_button.setMinimumSize(32, 32)
        self.layer_menu = QMenu(self.layer_menu_button)
        self.new_layer_action = self.layer_menu.addAction("New layer…", lambda: self.controller.run(self.generate_terrain))
        self.rename_layer_action = self.layer_menu.addAction("Rename layer…", lambda: self.controller.run(self.rename_terrain))
        self.remove_layer_action = self.layer_menu.addAction("Delete layer…", lambda: self.controller.run(self.remove_terrain))
        self.layer_menu_button.setMenu(self.layer_menu)
        selector.addWidget(self.layer_menu_button)
        layout.addLayout(selector)
        self.generate_button = QPushButton("Generate terrain from raster…")
        self.generate_button.clicked.connect(lambda: self.controller.run(self.generate_terrain))
        layout.addWidget(self.generate_button)
        layout.addWidget(QLabel("Terrain meshes"))
        self.terrains = QListWidget()
        self.terrains.setAccessibleName("Meshes in the selected terrain layer")
        self.terrains.itemDoubleClicked.connect(lambda: self.controller.run(self.zoom_terrain))
        layout.addWidget(self.terrains, 1)
        self.details = wrapped_label("")
        self.details.setTextFormat(Qt.PlainText)
        self.details.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.details)
        self.zoom_button = QPushButton("Zoom to mesh")
        self.zoom_button.clicked.connect(lambda: self.controller.run(self.zoom_terrain))
        layout.addWidget(self.zoom_button)
        layout.addWidget(wrapped_label("Each generation creates a new terrain layer saved in the model. Include the layer in the variants that should use it. The original raster is only needed during generation."))

    def selected_id(self):
        return self.layer_combo.currentData()

    def refresh(self, selected=None):
        selected = selected or self.selected_id()
        store = self.controller.store
        rows = store.generated_terrains() if store else []
        self.records = {row["layer_id"]: row for row in rows}
        previous = self.layer_combo.blockSignals(True)
        try:
            self.layer_combo.clear()
            for row in rows:
                self.layer_combo.addItem(layer_label(self.controller, row), row["layer_id"])
            self.layer_combo.setCurrentIndex(max(0, self.layer_combo.findData(selected)))
        finally:
            self.layer_combo.blockSignals(previous)
        self.selected()

    def refresh_labels(self):
        if not self.controller.store:
            return
        records = {row['layer_id']: row for row in self.controller.store.generated_terrains()}
        self.records = records
        for index in range(self.layer_combo.count()):
            record = records.get(self.layer_combo.itemData(index))
            if record:
                self.layer_combo.setItemText(index, layer_label(self.controller, record))
        self.selected()

    def layer_selected(self, *args):
        self.selected()
        self.open_terrain()

    def selected(self, *args):
        record = self.records.get(self.selected_id())
        self.terrains.clear()
        text = "No generated terrain yet. Choose Generate terrain from raster to create one."
        if record:
            item = QListWidgetItem(record['name'])
            item.setData(Qt.UserRole, record['layer_id'])
            self.terrains.addItem(item)
            self.terrains.setCurrentItem(item)
            data = record['generation']
            text = (f"{record['name']}"
                    + f"\n{data['vertex_count']:,} vertices · {data['triangle_count']:,} triangles"
                    + f"\nMaximum checked deviation: {data['max_error_m']:.3g} m (requested {data['tolerance_m']:.3g} m)"
                    + f"\nGenerated area: ({data['bounds'][0]:.3f}, {data['bounds'][1]:.3f})"
                    + f" to ({data['bounds'][2]:.3f}, {data['bounds'][3]:.3f})"
                    + f"\nImported from: {data['source_path']}")
        self.details.setText(text)
        self.generate_button.setEnabled(self.controller.store is not None)
        self.new_layer_action.setEnabled(self.controller.store is not None)
        for control in (self.zoom_button, self.rename_layer_action, self.remove_layer_action):
            control.setEnabled(record is not None)

    def generate_terrain(self):
        dialog = RasterSelectionDialog(self)
        try:
            if dialog.exec() == QDialog.Accepted:
                record = self.controller.generate_raster_terrain(dialog.selected_raster())
                self.refresh(record['layer_id'] if record else None)
        finally:
            dialog.deleteLater()

    def open_terrain(self):
        if self.selected_id():
            self.controller.open_layer_properties(self.selected_id())

    def zoom_terrain(self):
        if self.selected_id():
            self.controller.zoom_generated_terrain(self.selected_id())

    def rename_terrain(self):
        if self.selected_id():
            self.controller.rename_generated_terrain(self.selected_id())
            self.refresh()

    def remove_terrain(self):
        if self.selected_id():
            self.controller.remove_generated_terrain(self.selected_id())
            self.refresh()
            self.open_terrain()


class TerrainPage(QWidget):
    views = ("points", "lines", "rasters", "ground_absorption", "trees")
    object_views = {"terrain_point": "points", "terrain_line": "lines",
                    "ground_absorption": "ground_absorption", "trees": "trees"}

    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)
        self.point_host = QWidget()
        self.line_host = QWidget()
        for host, label in ((self.point_host, "Points"), (self.line_host, "Lines")):
            layout = QVBoxLayout(host)
            layout.setContentsMargins(0, 0, 0, 0)
            self.tabs.addTab(host, label)
        self.rasters_page = RastersPage(self)
        self.tabs.addTab(self.rasters_page, "Generated terrain")
        self.hosts = {"terrain_point": self.point_host, "terrain_line": self.line_host}
        for kind, label in (("ground_absorption", "Ground Absorption"), ("trees", "Trees")):
            host = QWidget()
            layout = QVBoxLayout(host)
            layout.setContentsMargins(0, 0, 0, 0)
            self.hosts[kind] = host
            self.tabs.addTab(host, label)
        self.tabs.currentChanged.connect(self.subpage_changed)

    def current_view(self):
        return self.views[self.tabs.currentIndex()]

    def current_kind(self):
        return next((kind for kind, view in self.object_views.items()
                     if view == self.current_view()), None)

    def set_view(self, key):
        old = self.tabs.blockSignals(True)
        self.tabs.setCurrentIndex(self.views.index(key))
        self.tabs.blockSignals(old)

    def subpage_changed(self, *args):
        dock = getattr(self.controller, "dock", None)
        if dock:
            dock.show_view(self.current_kind() or "terrain")
        if self.current_view() == "rasters":
            self.rasters_page.refresh()

    def refresh(self):
        busy = bool(getattr(getattr(self.controller, "engine_runner", None), "active", False))
        self.setEnabled(self.controller.store is not None and not busy)
        self.rasters_page.refresh()
