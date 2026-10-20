# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Feature properties in the canvas menu and native QGIS feature action menus."""

from qgis.PyQt.QtCore import QObject
from qgis.gui import QgsGui, QgsMapLayerAction, QgsMapToolIdentify

from .dialogs import null_text


class FeaturePropertiesActions(QObject):
    def __init__(self, controller):
        super().__init__(controller.dock)
        self.controller = controller
        self.canvas = controller.iface.mapCanvas()
        # Use QGIS's normal search tolerance and map/layer CRS transformation.
        # This is a passive identifier, not a replacement for the active map tool.
        self.identifier = QgsMapToolIdentify(self.canvas)
        self.layer_actions = {}
        self.canvas.contextMenuAboutToShow.connect(self.populate_menu)

    def add_layer(self, layer):
        if layer.providerType() == "gdal":
            return  # Raster results use the canvas menu and have no feature IDs.
        if layer.id() in self.layer_actions:
            return
        action = QgsMapLayerAction("Acalc properties", self, layer, QgsMapLayerAction.SingleFeature)
        action.triggeredForFeatureV2.connect(self.feature_action)
        QgsGui.mapLayerActionRegistry().addMapLayerAction(action)
        self.layer_actions[layer.id()] = action

    def remove_layer(self, layer_id):
        action = self.layer_actions.pop(layer_id, None)
        if action is not None:
            QgsGui.mapLayerActionRegistry().removeMapLayerAction(action)
            action.deleteLater()

    def feature_action(self, layer, feature, context):
        layer_id, fid = layer.id(), feature.id()
        self.controller.run(lambda: self.controller.open_feature_properties(layer_id, fid))

    def populate_menu(self, menu, event):
        self.controller.run(lambda: self._populate_menu(menu, event))

    def _populate_menu(self, menu, event):
        if self.controller.store is None:
            return
        managed = {layer.id() for layer in [*self.controller.layers.values(), *self.controller.result_layers.values()]}
        layers = [layer for layer in self.canvas.layers()
                  if layer.id() in managed and layer.isInScaleRange(self.canvas.scale())]
        # An empty identify layer list means *all* map layers to QGIS.
        if not layers:
            return
        results = self.identifier.identify(event.pos().x(), event.pos().y(), layers, QgsMapToolIdentify.TopDownAll)
        hits = {}
        for result in results:
            layer, feature = result.mLayer, result.mFeature
            if layer.id() in managed and layer.customProperty("acalc/area_receiver_id", ""):
                hits[(layer.id(), None)] = (layer, None)
                continue
            if layer.id() in managed and feature.isValid():
                hits[(layer.id(), feature.id())] = (layer, feature)
        if not hits:
            return
        menu.addSeparator()
        target_menu = menu.addMenu("Acalc properties") if len(hits) > 1 else menu
        for (layer_id, fid), (layer, feature) in hits.items():
            label = "Acalc properties"
            area = layer.customProperty("acalc/area_receiver_id", "")
            name = layer.customProperty("acalc/area_name", layer.name()) if area else null_text(feature["receiver_name"] if layer in self.controller.result_layers.values() else feature["name"])
            layer_name = layer.name() + (" *" if layer.isModified() else "")
            if len(hits) > 1:
                label = f"{name or 'Unnamed feature'} · {layer_name}".replace("&", "&&")
            action = target_menu.addAction(label)
            action.setToolTip(f"Open receiver properties for {name}" if layer in self.controller.result_layers.values()
                              else f"Edit {name} in {layer_name}")
            # Resolve the IDs again when clicked: a menu never owns a feature
            # snapshot or a potentially deleted layer wrapper.
            action.triggered.connect(lambda checked=False, target=layer_id, feature_id=fid:
                self.controller.run(lambda: self.controller.open_feature_properties(target, feature_id)))
            if area:
                display = target_menu.addMenu("Area result display" if len(hits) == 1 else "Display · " + name)
                for mode, label in (("areas", "Filled areas"), ("lines", "Contour lines")):
                    action = display.addAction(label)
                    action.setCheckable(True)
                    action.setChecked(layer.customProperty("acalc/surface_mode", "areas") == mode)
                    action.triggered.connect(lambda checked=False, target=layer_id, value=mode: self.set_surface_mode(target, value))
                if layer.bandCount() == 9:
                    from .area_results import BAND_NAMES
                    bands = display.addMenu("Sound level")
                    for band, label in enumerate(BAND_NAMES, 1):
                        action = bands.addAction(label)
                        action.setCheckable(True)
                        action.setChecked(int(layer.customProperty("acalc/surface_band", 1)) == band)
                        action.triggered.connect(lambda checked=False, target=layer_id, value=band: self.set_surface_band(target, value))

    def set_surface_band(self, layer_id, band):
        from .area_results import style_surface
        layer = self.controller.project.mapLayer(layer_id)
        if layer is not None:
            style_surface(layer, layer.customProperty("acalc/surface_mode", "areas"), band=band)
            self.controller.project.setDirty(True)

    def set_surface_mode(self, layer_id, mode):
        from .area_results import style_surface
        layer = self.controller.project.mapLayer(layer_id)
        if layer is not None:
            style_surface(layer, mode)
            self.controller.project.setDirty(True)

    def shutdown(self):
        self.canvas.contextMenuAboutToShow.disconnect(self.populate_menu)
        for layer_id in list(self.layer_actions):
            self.remove_layer(layer_id)
        self.identifier.deleteLater()
        self.deleteLater()
