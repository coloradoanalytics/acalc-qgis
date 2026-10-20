# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Capture geometry with QGIS, then author a feature in Acalc properties."""

from qgis.PyQt.QtCore import QObject, QTimer
from qgis.core import QgsGeometry, QgsVectorLayerUtils
from qgis.gui import QgsMapToolDigitizeFeature


class ModelDigitizer(QObject):
    def __init__(self, controller):
        super().__init__(controller.dock)
        self.controller = controller
        self.canvas = controller.iface.mapCanvas()
        self.tool = None
        self.layer_id = None
        self.previous_tool = None
        self.bindings = []
        self.pending_geometry = None
        self.showing_properties = False
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(lambda: self.controller.run(self.open_properties))
        controller.iface.currentLayerChanged.connect(self.layer_changed)

    def start(self, layer):
        if self.layer_id == layer.id() and self.canvas.mapTool() is self.tool:
            return
        self.stop()
        self.previous_tool = self.canvas.mapTool()
        self.layer_id = layer.id()
        self.tool = QgsMapToolDigitizeFeature(self.canvas, self.controller.iface.cadDockWidget())
        self.tool.setToolName("Acalc Draw on map")
        self.tool.setLayer(layer)
        self.bindings = [
            (self.tool.digitizingCompleted, self.geometry_captured),
            (self.tool.digitizingCanceled, self.cancel_sketch),
            (self.tool.deactivated, self.tool_deactivated),
            (layer.editingStopped, self.stop),
        ]
        for signal, slot in self.bindings:
            signal.connect(slot)
        self.canvas.setMapTool(self.tool)

    def geometry_captured(self, feature):
        if self.tool is None or self.showing_properties or self.pending_geometry is not None:
            return
        self.pending_geometry = QgsGeometry(feature.geometry())
        # Let QGIS finish its capture/rubber-band cleanup before opening a modal.
        # No featureAdded hook is involved, so undo/redo never reopens a dialog.
        self.timer.start(0)

    def open_properties(self):
        geometry = self.pending_geometry
        self.pending_geometry = None
        layer = self.controller.project.mapLayer(self.layer_id) if self.layer_id else None
        if (geometry is None or self.tool is None or self.canvas.mapTool() is not self.tool
                or layer is None or layer not in self.controller.layers.values() or not layer.isEditable()):
            return
        draft = QgsVectorLayerUtils.createFeature(layer, geometry)
        self.showing_properties = True
        try:
            accepted = self.controller.edit_feature(create=True, layer=layer, draft_feature=draft)
            if not accepted:
                self.controller.message("Drawing canceled. No feature was added; you can draw another.")
        finally:
            self.showing_properties = False

    def cancel_sketch(self):
        self.timer.stop()
        self.pending_geometry = None
        self.controller.message("Sketch canceled. You can draw another feature.")

    def layer_changed(self, layer):
        if self.tool is not None and (layer is None or layer.id() != self.layer_id):
            self.stop()

    def tool_deactivated(self):
        self.stop(restore=False)

    def stop(self, restore=True):
        self.timer.stop()
        self.pending_geometry = None
        tool, previous = self.tool, self.previous_tool
        self.tool = None
        self.previous_tool = None
        self.layer_id = None
        for signal, slot in self.bindings:
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        self.bindings.clear()
        if tool is not None:
            tool.stopCapturing()
            if restore and self.canvas.mapTool() is tool:
                # Another plugin may have deleted the previous map tool.
                try:
                    if previous is not None and previous.canvas() is self.canvas:
                        self.canvas.setMapTool(previous)
                    else:
                        self.canvas.unsetMapTool(tool)
                except RuntimeError:
                    self.canvas.unsetMapTool(tool)
            tool.deleteLater()

    def shutdown(self):
        self.stop()
        self.controller.iface.currentLayerChanged.disconnect(self.layer_changed)
        self.deleteLater()
