# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Layer labels and navigation shared by the persistent editor and QGIS tree."""

from qgis.PyQt.QtCore import QEvent, QObject, QTimer, Qt
from qgis.core import QgsApplication
from qgis.gui import QgsLayerTreeViewIndicator

from .schema import LAYER_TYPES


def layer_label(controller, record, with_type=False):
    layer = getattr(controller, "layers", {}).get(record.get("layer_id"))
    try:
        dirty = layer is not None and layer.isModified()
    except RuntimeError:
        dirty = False  # Providers can disappear before queued UI refreshes.
    name = record["name"] + (" *" if dirty else "")
    if with_type:
        name += "  ·  " + LAYER_TYPES[record["feature_type"]].title
    return name


class LayerPresentation(QObject):
    def __init__(self, controller):
        super().__init__(controller.dock)
        self.controller = controller
        self.indicators = {}
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.refresh)
        self.view = controller.iface.layerTreeView() if hasattr(controller.iface, "layerTreeView") else None
        if self.view is not None:
            self.view.viewport().installEventFilter(self)

    def queue(self, *args):
        self.timer.start(0)

    def refresh(self):
        controller = self.controller
        if controller.store is None:
            return
        controller.dock.refresh_layer_labels()
        if self.view is None:
            return
        root = controller.project.layerTreeRoot()
        for layer in controller.layers.values():
            try:
                layer_id, dirty = layer.id(), layer.isModified()
            except RuntimeError:
                continue
            node = root.findLayer(layer_id)
            self.remove_indicator(layer_id)
            if node is not None and dirty:
                indicator = QgsLayerTreeViewIndicator(self.view)
                indicator.setIcon(QgsApplication.getThemeIcon("/mActionSaveEdits.svg"))
                indicator.setToolTip("Unsaved layer edits")
                self.view.addIndicator(node, indicator)
                self.indicators[layer_id] = (node, indicator)

    def remove_indicator(self, layer_id):
        previous = self.indicators.pop(layer_id, None)
        if previous:
            node, indicator = previous
            try:
                self.view.removeIndicator(node, indicator)
            except RuntimeError:
                pass  # The node may already have been removed by QGIS.
            indicator.deleteLater()

    def eventFilter(self, watched, event):
        if event.type() == QEvent.MouseButtonDblClick and event.button() == Qt.LeftButton:
            layer = self.view.layerForIndex(self.view.indexAt(event.pos()))
            if layer is not None:
                controller = self.controller
                if layer in controller.layers.values():
                    controller.run(lambda: controller.open_layer_properties(layer.customProperty("acalc/layer_id")))
                    return True
                if layer in controller.result_layers.values():
                    controller.show_dock()
                    controller.dock.show_view("results")
                    controller.dock.results_page.refresh(layer.customProperty("acalc/variant_id"))
                    return True
                if controller.store:
                    from pathlib import Path
                    record = next((row for row in controller.store.elevation_rasters()
                                   if str(Path(layer.source()).resolve()) == row["path"]), None)
                    if record:
                        controller.open_raster_properties(record["raster_id"])
                        return True
        return super().eventFilter(watched, event)

    def shutdown(self):
        self.timer.stop()
        if self.view is not None:
            self.view.viewport().removeEventFilter(self)
        for layer_id in list(self.indicators):
            self.remove_indicator(layer_id)
