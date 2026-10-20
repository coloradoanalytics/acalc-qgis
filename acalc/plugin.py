# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""QGIS lifecycle and model editing controller."""

import json
from pathlib import Path

from qgis.PyQt.QtCore import QSettings, QTimer, Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QDialog, QFileDialog, QInputDialog, QMessageBox
from qgis.core import QgsFeature, QgsMessageLog, QgsProject, QgsProviderRegistry, Qgis

from .dialogs import FeatureDialog, NewProjectDialog, ProjectCoordinatesDialog
from .units import input_unit, elevation_unit
from .digitizing import ModelDigitizer
from .dock import ModelDock
from .layers import configure_layer, ground_absorption_overlap_errors, initialize_added_object_fields, load_layer, synchronize_measurement_broadband, validate_layer
from .map_actions import FeaturePropertiesActions
from .schema import LAYER_TYPES, SOURCE_TYPES, new_id
from .storage import ModelError, ModelStore
from .spectra_ui import SpectrumDialog
from .directivity_ui import DirectivityDialog
from .engine_runner import EngineRunner
from .layer_presentation import LayerPresentation
from .result_layers import clear_variant_results, load_result_layer, result_layer_tables, sync_result_layers, result_layer_key


class AcalcPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.project = QgsProject.instance()
        self.store = None
        self.layers = {}
        self.result_layers = {}
        self.group = None
        self.dock = None
        self.action = None
        self.metadata_snapshot = None
        self._connections = []
        self._layer_connections = {}
        self._changing = False
        self.properties_actions = None
        self.digitizer = None
        self.engine_runner = None
        self._calculation_layers = []
        self.calculation_dialog = None
        self.back_calculation_dialog = None
        self.layer_presentation = None

    def connect(self, signal, slot):
        signal.connect(slot)
        self._connections.append((signal, slot))

    def initGui(self):
        self.engine_runner = EngineRunner(self.iface.mainWindow())
        self.engine_runner.busyChanged.connect(self.calculation_busy_changed)
        self.engine_runner.succeeded.connect(self.calculation_succeeded)
        self.engine_runner.failed.connect(self.calculation_failed)
        self.engine_runner.stopped.connect(self.calculation_stopped)
        self.action = QAction(QIcon(str(Path(__file__).with_name("icon.svg"))), "Acalc", self.iface.mainWindow())
        self.action.setCheckable(True)
        self.action.triggered.connect(self.show_dock)
        self.iface.addPluginToMenu("&Acalc", self.action)
        self.iface.addToolBarIcon(self.action)
        self.dock = ModelDock(self)
        self.layer_presentation = LayerPresentation(self)
        settings = QSettings()
        area = int(settings.value("acalc/dock_area", int(Qt.RightDockWidgetArea)))
        if area not in (int(Qt.LeftDockWidgetArea), int(Qt.RightDockWidgetArea), int(Qt.TopDockWidgetArea), int(Qt.BottomDockWidgetArea)):
            area = int(Qt.RightDockWidgetArea)
        self.iface.addDockWidget(Qt.DockWidgetArea(area), self.dock)
        self.dock.visibilityChanged.connect(self.action.setChecked)
        geometry = settings.value("acalc/dock_geometry")
        if geometry:
            self.dock.restoreGeometry(geometry)
        self.dock.setVisible(settings.value("acalc/dock_visible", True, type=bool))
        self.properties_actions = FeaturePropertiesActions(self)
        self.digitizer = ModelDigitizer(self)
        self.connect(self.iface.currentLayerChanged, self.active_layer_changed)
        self.connect(self.project.readProject, self.restore_project)
        self.connect(self.project.cleared, self.project_cleared)
        self.connect(self.project.layersWillBeRemoved, self.layers_removing)
        self.connect(self.project.layersAdded, self.layers_added)
        self.restore_project()

    def unload(self):
        self.engine_runner.shutdown()
        self.dock.settings_page.shutdown()
        self.close_calculation_dialog()
        self.close_back_calculation_dialog()
        self.engine_runner.deleteLater()
        self.engine_runner = None
        self.digitizer.shutdown()
        self.digitizer = None
        settings = QSettings()
        settings.setValue("acalc/dock_visible", self.dock.isVisible())
        settings.setValue("acalc/dock_area", int(self.iface.mainWindow().dockWidgetArea(self.dock)))
        settings.setValue("acalc/dock_geometry", self.dock.saveGeometry())
        for signal, slot in self._connections:
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self._connections.clear()
        self.disconnect_layers()
        self.layer_presentation.shutdown()
        self.properties_actions.shutdown()
        self.properties_actions = None
        self.dock.shutdown()
        self.iface.removeDockWidget(self.dock)
        self.iface.removePluginMenu("&Acalc", self.action)
        self.iface.removeToolBarIcon(self.action)
        self.dock.deleteLater()
        self.action.deleteLater()
        self.dock = None

    def show_dock(self, visible=True):
        self.dock.setVisible(visible)
        if visible:
            self.dock.raise_()

    def run(self, operation):
        try:
            if self.engine_runner and self.engine_runner.active and operation != self.cancel_calculation:
                raise ModelError("A calculation is running. Wait for it to finish or cancel it before editing the model.")
            return operation()
        except (ModelError, OSError, ValueError, RuntimeError) as error:
            self.message(str(error), error=True)
            return False

    def message(self, text, error=False):
        if self.dock:
            self.dock.append_message(text)
            if (error or self.engine_runner.active
                    or self.dock.stack.currentWidget() is self.dock.calculation_page
                    or (self.calculation_dialog and self.calculation_dialog.isVisible())):
                self.dock.calculation_page.append_message(text)
        if error:
            self.iface.messageBar().pushMessage("Acalc", text, level=Qgis.Warning, duration=12)
            QgsMessageLog.logMessage(text, "Acalc", Qgis.Warning)

    def require_store(self):
        if self.store is None:
            raise ModelError("Create or open an Acalc project first.")
        return self.store

    def selected_record(self):
        if not self.store:
            return None
        if self.dock.stack.currentWidget() is self.dock.terrain_page and self.dock.terrain_page.current_view() == 'rasters':
            selected = self.dock.terrain_page.rasters_page.selected_id()
        else:
            selected = self.dock.layer_combo.currentData()
        return next((row for row in self.store.layers() if row["layer_id"] == selected), None)

    def selected_layer(self):
        record = self.selected_record()
        if record is None:
            raise ModelError("Select a model layer first.")
        return self.ensure_layer(record)

    def selected_variant(self):
        if self.store is None or self.dock is None:
            return None
        selected = self.dock.variant_combo.currentData()
        return next((row for row in self.store.variants() if row["variant_id"] == selected), None)

    def new_project(self):
        dialog = NewProjectDialog(self.dock, self.project.crs())
        if dialog.exec() != QDialog.Accepted:
            return
        if not self.prepare_close():
            return
        store = ModelStore.create(dialog.path.text(), dialog.name.text(), dialog.crs.crs().toWkt(),
                                  dialog.crs.crs().authid(), dialog.author.text())
        if dialog.elevation_units.currentData() != "m":
            from .project_coordinates import change_coordinates
            change_coordinates(store, dialog.crs.crs(), dialog.elevation_units.currentData(), self.project.transformContext())
        self.open_project(store.path, prepared=True)
        if not self.project.crs().isValid() or self.project.crs().isGeographic():
            self.project.setCrs(dialog.crs.crs())

    def choose_project(self):
        path, _ = QFileDialog.getOpenFileName(self.dock, "Open Acalc project", QSettings().value("acalc/last_directory", ""), "Acalc GeoPackage (*.gpkg)")
        if path:
            self.open_project(path)

    def open_project(self, path, prepared=False):
        # Removing legacy columns changes field indices. Never migrate beneath
        # a live edit buffer whose undo commands still refer to the old fields.
        target_path = str(Path(path).expanduser().resolve())
        for candidate in self.project.mapLayers().values():
            if candidate.providerType() != "ogr" or not candidate.isEditable():
                continue
            uri = QgsProviderRegistry.instance().decodeUri("ogr", candidate.source())
            if str(Path(uri.get("path", "")).resolve()) == target_path and "infer_z" in candidate.fields().names():
                raise ModelError(f"{candidate.name()}: save or discard layer edits and close its edit session before upgrading this project's elevation fields.")
        store = ModelStore.open(path)
        if self.store and self.store.path == store.path:
            self.reload_layers()
            self.message("This model is already open.")
            return True
        # Validate the next model and its providers before disturbing this one.
        candidates = {record["layer_id"]: load_layer(store, record) for record in store.layers()}
        if not prepared and not self.prepare_close():
            return False
        self.detach(remove=True)
        self.store = store
        self._changing = True
        try:
            self.ensure_group()
            for record in store.layers():
                existing = self.find_existing_layer(record)
                self.attach_layer(existing or candidates[record["layer_id"]], record)
            self.group_elevation_rasters()
            self.project.writeEntry("acalc", "model_path", self.project.pathResolver().writePath(store.path))
        finally:
            self._changing = False
        self.restore_result_layers()
        QSettings().setValue("acalc/last_directory", str(Path(store.path).parent))
        self.dock.refresh_project()
        self.dock.show_view("project")
        if store.migration_backup:
            self.message(f"Model upgraded to schema {store.metadata()['schema_version']}. Building layers with attached sources are now Source Buildings. Original backup: {store.migration_backup}")
        else:
            self.message("Model open. Choose a view to start editing.")
        return True

    def ensure_group(self):
        if not self.store:
            return None
        root = self.project.layerTreeRoot()
        project_id = self.store.metadata()["project_id"]
        self.group = next((group for group in root.findGroups() if group.customProperty("acalc/project_id") == project_id), None)
        if self.group is None:
            self.group = root.addGroup("Acalc · " + self.store.metadata()["name"])
            self.group.setCustomProperty("acalc/project_id", project_id)
        for child in list(self.group.children()):
            kind = child.customProperty("acalc/layer_type")
            if kind:
                target = self.category_group(self.group, self.layer_category(kind))
                self.merge_layer_group(child, target)
        return self.group

    @staticmethod
    def layer_category(kind):
        if kind in SOURCE_TYPES:
            return "Sources"
        if kind.endswith("_receiver") or kind == "measurement":
            return "Receivers"
        if kind.startswith("terrain_") or kind == "elevation_raster":
            return "Terrain"
        return "Objects"

    def category_group(self, parent, category):
        group = next((child for child in parent.children()
                      if child.customProperty("acalc/category") == category), None)
        if group is None:
            order = ("Results", "Sources", "Receivers", "Objects", "Terrain")
            index = next((i for i, child in enumerate(parent.children())
                          if child.customProperty("acalc/category") in order
                          and order.index(child.customProperty("acalc/category")) > order.index(category)),
                         len(parent.children()))
            group = parent.insertGroup(index, category)
            group.setCustomProperty("acalc/category", category)
        return group

    def merge_layer_group(self, old, target):
        # Retain node settings and contents from saved projects, including user layers.
        for child in list(old.children()):
            for node in child.findLayers() if hasattr(child, "findLayers") else [child]:
                if self.layer_presentation and hasattr(node, "layerId"):
                    self.layer_presentation.remove_indicator(node.layerId())
            clone = child.clone()
            if not old.itemVisibilityChecked():
                clone.setItemVisibilityChecked(False)
            target.addChildNode(clone)
        old.parent().removeChildNode(old)

    def layer_type_group(self, kind):
        parent = self.ensure_group()
        return self.category_group(parent, self.layer_category(kind))

    def group_elevation_rasters(self):
        if not self.store:
            return
        paths = {row["path"] for row in self.store.elevation_rasters()}
        for layer in self.project.mapLayers().values():
            if layer.providerType() == "gdal" and str(Path(layer.source()).resolve()) in paths:
                self.place_layer(layer, self.layer_type_group("elevation_raster"))

    def place_layer(self, layer, group):
        node = self.project.layerTreeRoot().findLayer(layer.id())
        if node is None:
            group.addLayer(layer)
        elif node.parent() is not group:
            if self.layer_presentation:
                self.layer_presentation.remove_indicator(layer.id())
            group.addChildNode(node.clone())
            node.parent().removeChildNode(node)

    def open_layer_properties(self, layer_id):
        record = next(row for row in self.require_store().layers() if row["layer_id"] == layer_id)
        layer = self.ensure_layer(record)
        self.show_dock()
        self.iface.setActiveLayer(layer)
        self.dock.select_layer(layer)

    def open_raster_properties(self, raster_id):
        self.show_dock()
        self.dock.terrain_page.set_view("rasters")
        self.dock.show_view("terrain")
        self.dock.terrain_page.rasters_page.refresh()

    def find_existing_layer(self, record):
        for layer in self.project.mapLayers().values():
            if self.matches(layer, record):
                return layer
        return None

    def matches(self, layer, record):
        if layer.providerType() == "gdal":
            from .area_results import matches_raster_source
            return matches_raster_source(layer.source(), self.store, record)
        if layer.providerType() != "ogr":
            return False
        uri = QgsProviderRegistry.instance().decodeUri("ogr", layer.source())
        return (uri.get("layerName") == record["table_name"] and
                str(Path(uri.get("path", "")).resolve()) == self.store.path)

    def is_area_result_view(self, layer, record):
        return (layer.customProperty("acalc/project_path") == self.store.path
                and all(layer.customProperty("acalc/" + name) == record[key] for name, key in (
                    ("variant_id", "variant_id"), ("receiver_layer_id", "receiver_layer_id"),
                    ("area_receiver_id", "area_receiver_id"))))

    def attach_layer(self, layer, record):
        self.ensure_group()
        configure_layer(layer, self.store, record)
        self.layers[record["layer_id"]] = layer
        if self.project.mapLayer(layer.id()) is None:
            self.project.addMapLayer(layer, False)
        self.place_layer(layer, self.layer_type_group(record["feature_type"]))
        if layer.id() not in self._layer_connections:
            rename_slot = lambda target=layer: self.layer_renamed(target)
            prepare_commit_slot = lambda *args, target=layer: self.run(lambda: initialize_added_object_fields(
                target, LAYER_TYPES[target.customProperty("acalc/feature_type")]))
            commit_slot = lambda: self.run(self.layer_committed) if self.store else None
            edit_slot = lambda: self.run(self.refresh_providers) if self.store else None
            selection_slot = lambda selected, deselected, clear, target=layer: self.model_selection_changed(target, selected)
            bindings = [(layer.nameChanged, rename_slot), (layer.beforeCommitChanges, prepare_commit_slot),
                        (layer.afterCommitChanges, commit_slot),
                        (layer.editingStarted, edit_slot), (layer.editingStopped, edit_slot),
                        (layer.selectionChanged, selection_slot)]
            bindings.extend((signal, self.layer_presentation.queue) for signal in
                            (layer.layerModified, layer.afterRollBack, layer.afterCommitChanges,
                             layer.editingStopped, layer.undoStack().indexChanged))
            for signal, slot in bindings:
                signal.connect(slot)
            self._layer_connections[layer.id()] = bindings
        self.properties_actions.add_layer(layer)
        self.layer_presentation.queue()

    def ensure_layer(self, record):
        layer = self.layers.get(record["layer_id"])
        if layer is None:
            layer = self.find_existing_layer(record) or load_layer(self.store, record)
            self._changing = True
            try:
                self.attach_layer(layer, record)
            finally:
                self._changing = False
        elif layer.customProperty("acalc/feature_type") != record["feature_type"]:
            # A schema migration can reclassify an already loaded building.
            # Refresh its editor rules without replacing its style or edit buffer.
            configure_layer(layer, self.store, record)
        return layer

    def activate_layer(self, layer_id):
        record = next(row for row in self.require_store().layers() if row["layer_id"] == layer_id)
        layer = self.ensure_layer(record)
        if layer_id in self.store.generated_terrain_ids():
            self.dock.select_layer(layer)
        else:
            self.dock.bind_layer(layer)
        if self.iface.activeLayer() is not layer:
            self.iface.setActiveLayer(layer)

    def active_layer_changed(self, layer):
        if self._changing or self.store is None or layer is None:
            return
        if layer in self.layers.values():
            self.dock.select_layer(layer)
        elif layer in self.result_layers.values():
            self.dock.show_view("results")
            self.dock.results_page.refresh(layer.customProperty("acalc/variant_id"))

    def restore_result_layers(self):
        """Load saved result sources without regenerating data or renaming layers."""
        runs = {run["run_id"] for run in self.store.calculation_run_summaries()}
        records = [record for record in result_layer_tables(self.store).values()
                   if record.get("run_id") in runs and record.get("name")]
        # Prefer the shared table over an older per-run snapshot of the same results.
        latest = {}
        for record in sorted(records, key=lambda record: not bool(record.get("snapshot_id"))):
            latest[result_layer_key(record)] = record
        self.attach_result_layers(list(latest.values()), restore=True)

    def refresh_result_layers(self):
        """Publish shared data and refresh every layer that references it."""
        if self.store:
            self.attach_result_layers(sync_result_layers(self.store, prune=False))

    def attach_result_layers(self, records, *, restore=False):
        changing = self._changing
        previous_layers = list(self.result_layers.values())
        self._changing = True
        try:
            self.ensure_group()
            group = next((g for g in self.group.findGroups() if g.customProperty("acalc/results")), None)
            if group is None and not records:
                return
            if group is None:
                group = self.category_group(self.group, "Results")
                group.setCustomProperty("acalc/results", True)
            variant_groups = {child.customProperty("acalc/result_variant_id"): child
                              for child in group.children()
                              if child.customProperty("acalc/result_variant_id")}
            owned = result_layer_tables(self.store)
            # Track the default layer separately from other views of the shared data.
            # Renamed and duplicate layers retain their own names and styling.
            self.result_layers = {key: layer for key, layer in self.result_layers.items()
                                  if layer.name() == layer.customProperty("acalc/default_result_name", "")}
            for record in records:
                key = result_layer_key(record)
                candidates = [node.layer() for node in group.findLayers() if node.layer() is not None]
                layer = next((item for item in candidates if item.name() == record["name"]
                              and self.matches(item, record)), None)
                references = [item for item in self.project.mapLayers().values() if self.matches(item, record)]
                if not restore:
                    # Migrate loaded per-run tables from the earlier implementation
                    # onto the shared source, including renamed layers and clones.
                    references.extend(item for item in self.project.mapLayers().values()
                                      if item not in references and any(prior.get("snapshot_id")
                                      and result_layer_key(prior) == key
                                      and self.matches(item, {"table_name": table})
                                      for table, prior in owned.items()))
                    if record.get("raster_file"):
                        # Each run saves its own GeoTIFF. Carry renamed, duplicated
                        # and legacy views of this area result over to the new file.
                        references.extend(item for item in self.project.mapLayers().values()
                                          if item not in references and item.providerType() == "gdal"
                                          and self.is_area_result_view(item, record))
                    for reference in references:
                        if record.get("result_type") == "area":
                            from .area_results import raster_source
                            # Reopen GDAL to refresh pixels AND cached extent/size.
                            reference.setDataSource(raster_source(self.store, record), reference.name(), "gdal", False)
                        elif not self.matches(reference, record):
                            reference.setDataSource(f"{self.store.path}|layername={record['table_name']}", reference.name(), "ogr", False)
                        else:
                            reference.dataProvider().reloadData()
                            reference.updateFields()
                        if not reference.isValid():
                            raise ModelError("Could not refresh results: " + reference.name())
                        if record.get("result_type") != "area":
                            reference.updateExtents(True)
                        reference.setCustomProperty("acalc/run_id", record["run_id"])
                        reference.triggerRepaint()
                    # A migrated default layer can be reused as well.
                    if layer is None:
                        layer = next((item for item in candidates if item.name() == record["name"]
                                      and self.matches(item, record)), None)
                if restore and layer is None and references:
                    continue  # Opening a map must not recreate a renamed layer.
                if layer is None:
                    layer = load_result_layer(self.store, record)
                    variant_group = variant_groups.get(record["variant_id"])
                    if variant_group is None:
                        variant_group = group.addGroup(record["variant_name"])
                        variant_group.setCustomProperty("acalc/result_variant_id", record["variant_id"])
                        variant_groups[record["variant_id"]] = variant_group
                    self.project.addMapLayer(layer, False)
                    self.place_layer(layer, variant_group)
                if record.get("result_type") != "area":
                    layer.setReadOnly(True)
                layer.setCustomProperty("acalc/default_result_name", record["name"])
                layer.setCustomProperty("acalc/project_path", self.store.path)
                layer.setCustomProperty("acalc/variant_id", record["variant_id"])
                layer.setCustomProperty("acalc/receiver_layer_id", record["receiver_layer_id"])
                layer.setCustomProperty("acalc/run_id", record["run_id"])
                layer.setCustomProperty("acalc/area_receiver_id", record.get("area_receiver_id", ""))
                layer.setCustomProperty("acalc/area_name", record.get("area_name", ""))
                self.result_layers[key] = layer
                self.properties_actions.add_layer(layer)
                layer.triggerRepaint()
        finally:
            for layer in previous_layers:
                if layer not in self.result_layers.values():
                    self.properties_actions.remove_layer(layer.id())
            self._changing = changing

    def model_selection_changed(self, layer, selected):
        if not selected or self._changing:
            return
        layer_id = layer.id()
        def show_selected():
            target = self.project.mapLayer(layer_id)
            if self.dock and self.store and target is not None and self.iface.activeLayer() is target:
                self.active_layer_changed(target)
        QTimer.singleShot(0, show_selected)

    def refresh_providers(self):
        # QGIS changes a GeoPackage's journal mode when editing starts/stops.
        # Refresh idle providers sharing the file so they do not retain a stale
        # read-only SQLite handle across that transition. Never reload buffers.
        for layer in [*self.layers.values(), *self.result_layers.values()]:
            if not layer.isEditable():
                layer.dataProvider().reloadData()

    def layer_committed(self):
        self.store.touch()
        self.refresh_providers()

    def layer_renamed(self, layer):
        if not self._changing and self.store:
            def rename():
                self.store.rename_layer(layer.customProperty("acalc/layer_id"), layer.name())
                self.dock.refresh_variants()
                self.dock.refresh_layers(layer.customProperty("acalc/layer_id"))
                self.dock.terrain_page.refresh()
                self.layer_presentation.queue()
            self.run(rename)

    def layers_added(self, layers):
        if self._changing or not self.store:
            return
        # The normal registry bridge adds legend nodes after layersAdded. Wait
        # until it finishes, and resolve IDs again rather than holding wrappers.
        ids = [layer.id() for layer in layers]
        QTimer.singleShot(0, lambda: self.run(lambda: self.adopt_layers(ids)) if self.store and self.dock else None)

    def adopt_layers(self, ids):
        if not self.store:
            return
        records = self.store.layers()
        self._changing = True
        try:
            for layer_id in ids:
                layer = self.project.mapLayer(layer_id)
                if layer is None:
                    continue
                record = next((r for r in records if self.matches(layer, r)), None)
                if record and record["layer_id"] not in self.layers:
                    self.attach_layer(layer, record)
            self.group_elevation_rasters()
        finally:
            self._changing = False

    def disconnect_layer(self, layer_id):
        if self.layer_presentation:
            self.layer_presentation.remove_indicator(layer_id)
        if self.digitizer is not None and self.digitizer.layer_id == layer_id:
            self.digitizer.stop()
        if self.properties_actions is not None:
            self.properties_actions.remove_layer(layer_id)
        for signal, slot in self._layer_connections.pop(layer_id, []):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass

    def disconnect_layers(self):
        for layer_id in list(self._layer_connections):
            self.disconnect_layer(layer_id)
        if self.properties_actions:
            for layer in self.result_layers.values():
                self.properties_actions.remove_layer(layer.id())

    def layers_removing(self, ids):
        if self._changing:
            return
        for key, layer in list(self.result_layers.items()):
            if layer.id() in ids:
                self.properties_actions.remove_layer(layer.id())
                del self.result_layers[key]
        for record_id, layer in list(self.layers.items()):
            if layer.id() in ids:
                if self.dock.current_layer is layer:
                    self.dock.bind_layer(None)
                self.disconnect_layer(layer.id())
                del self.layers[record_id]
        if self.store:
            self.message("Layer removed from QGIS. Its model data is retained; use Reload layers or select it in Acalc to load it again.")

    def reload_layers(self):
        for record in self.require_store().layers():
            layer = self.ensure_layer(record)
            self.place_layer(layer, self.layer_type_group(record["feature_type"]))
        self.layer_presentation.queue()
        self.group_elevation_rasters()
        self.dock.refresh_project(metadata=False)
        self.message("All model layers are loaded in the Acalc group.")

    def prepare_close(self):
        if self.engine_runner and self.engine_runner.active:
            raise ModelError("Cancel the active calculation before closing or switching models.")
        if not self.store:
            return True
        metadata_dirty = self.dock.metadata_values() != self.metadata_snapshot
        dirty = [layer for layer in self.layers.values() if layer.isModified()]
        calculation_dirty = self.dock.calculation_page.dirty()
        if not metadata_dirty and not dirty and not calculation_dirty:
            return True
        details = []
        if metadata_dirty:
            details.append("project details")
        if calculation_dirty:
            details.append("calculation parameters")
        details.extend(layer.name() for layer in dirty)
        result = QMessageBox.question(self.dock, "Unsaved Acalc changes", "Save changes to " + ", ".join(details) + "?",
                                      QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Save)
        if result == QMessageBox.Cancel:
            return False
        if result == QMessageBox.Save:
            if metadata_dirty:
                self.save_metadata()
            if calculation_dirty:
                self.configure_calculation()
            for layer in dirty:
                self.commit_layer(layer)
        else:
            for layer in dirty:
                if not layer.rollBack():
                    raise ModelError(f"Could not discard edits to {layer.name()}.")
        return True

    def detach(self, remove=False):
        if self.engine_runner:
            self.engine_runner.shutdown()
        self.close_calculation_dialog()
        self.close_back_calculation_dialog()
        self._changing = True
        try:
            self.dock.bind_layer(None)
            self.disconnect_layers()
            if remove:
                ids = [layer.id() for layer in self.layers.values()]
                ids.extend(layer.id() for layer in self.result_layers.values()
                           if layer.name() == layer.customProperty("acalc/default_result_name", ""))
                self.project.removeMapLayers(ids)
                if self.store:
                    root = self.project.layerTreeRoot()
                    project_id = self.store.metadata()["project_id"]
                    for group in list(root.findGroups()):
                        if group.customProperty("acalc/project_id") == project_id:
                            # Preserve any unrelated layers the user put in the group.
                            for child in list(group.children()):
                                if ((child.customProperty("acalc/layer_type") or child.customProperty("acalc/category") or child.customProperty("acalc/results"))
                                        and not child.findLayers()):
                                    continue
                                root.addChildNode(child.clone())
                            group.parent().removeChildNode(group)
            self.layers.clear()
            self.result_layers.clear()
            self.group = None
            self.store = None
            self.metadata_snapshot = None
        finally:
            self._changing = False

    def close_project(self):
        if not self.prepare_close():
            return False
        self.detach(remove=True)
        self.project.removeEntry("acalc", "model_path")
        self.dock.refresh_project()
        self.dock.show_view("project")
        self.message("Model closed.")
        return True

    def project_cleared(self):
        # QGIS handles its layer edit prompts before clearing the project.
        self.detach(remove=False)
        self.dock.refresh_project()
        self.message("Create or open an Acalc GeoPackage to begin.")

    def restore_project(self, *args):
        path, present = self.project.readEntry("acalc", "model_path", "")
        if present and path:
            self.run(lambda: self.open_project(self.project.pathResolver().readPath(path)))

    def save_metadata(self):
        name, author, description, datum, extra = self.dock.metadata_values()
        try:
            metadata = json.loads(extra or "{}")
        except json.JSONDecodeError as error:
            raise ModelError(f"Other metadata must be valid JSON: {error.msg}") from error
        self.require_store().update_metadata(name, author, description, datum, metadata)
        self.ensure_group().setName("Acalc · " + name.strip())
        self.dock.refresh_project()
        self.message("Project details saved.")

    def change_project_coordinates(self):
        from .project_coordinates import change_coordinates
        store = self.require_store()
        dialog = ProjectCoordinatesDialog(self.dock, store)
        if dialog.exec() != QDialog.Accepted or not self.prepare_close():
            return
        if self.digitizer:
            self.digitizer.stop()
        for layer in self.layers.values():
            if layer.isEditable() and not layer.rollBack():
                raise ModelError("Could not finish editing " + layer.name())
        changed = change_coordinates(store, dialog.crs.crs(), dialog.elevation_units.currentData(), self.project.transformContext())
        if not changed:
            return
        records = {record["layer_id"]: record for record in store.layers()}
        for record_id, layer in self.layers.items():
            layer.setDataSource(layer.source(), layer.name(), "ogr", False)
            configure_layer(layer, store, records[record_id])
            layer.updateExtents()
            layer.triggerRepaint()
        self.project.setCrs(dialog.crs.crs())
        self.project.setDistanceUnits(dialog.crs.crs().mapUnits())
        editors = list(self.calculation_dialog.panels.values()) if self.calculation_dialog else []
        if self.back_calculation_dialog:
            editors.append(self.back_calculation_dialog.settings_editor)
        for editor in editors:
            if hasattr(editor, "set_input_unit"):
                editor.set_input_unit(input_unit(store))
        self.dock.refresh_project()
        self.iface.mapCanvas().refresh()
        self.message("Project CRS and input units saved. Existing results are out of date.")

    def edit_spectrum(self, create=False, duplicate=False):
        store = self.require_store()
        page = self.dock.spectra_page
        record = next((row for row in store.spectra(page.kind) if row["spectrum_id"] == page.selected_id()), None)
        if (not create or duplicate) and record is None:
            raise ModelError("Select a spectrum first.")
        if duplicate:
            record = dict(record)
            names = {row["name"].casefold() for row in store.spectra(page.kind)}
            base = record["name"] + " copy"
            name, number = base, 2
            while name.casefold() in names:
                name = f"{base} {number}"
                number += 1
            record["name"] = name
        dialog = SpectrumDialog(self.dock, page.kind, record if not create or duplicate else None)
        if record and not create:
            saved = store.spectrum_usage(record["spectrum_id"])
            usage = [f'{row["name"]}: {row["count"]} saved' + (" (removed layer)" if row["archived"] else "")
                     for row in saved]
            for layer in self.layers.values():
                column = store._spectrum_reference_column(layer.customProperty("acalc/feature_type"))
                if column and layer.isEditable():
                    count = sum(str(feature[column]) == record["spectrum_id"] for feature in layer.getFeatures())
                    if layer.customProperty("acalc/feature_type") == "source_building":
                        from .building_faces import read_face_sources
                        from .object_geometry import feature_value
                        count += sum(s["spectrum_id"] == record["spectrum_id"] for feature in layer.getFeatures()
                                     for s in read_face_sources(feature_value(feature, "face_sources")))
                    if count or any(row["layer_id"] == layer.customProperty("acalc/layer_id") for row in saved):
                        usage.append(f"{layer.name()}: {count} in current edit buffer")
            dialog.usage.setText("Shared assignments: " + ("; ".join(usage) if usage else "None"))
        while dialog.exec() == QDialog.Accepted:
            try:
                if create:
                    selected = store.add_spectrum(dialog.name.text(), dialog.value)
                else:
                    selected = record["spectrum_id"]
                    store.save_spectrum(selected, dialog.name.text(), dialog.value)
            except ModelError as error:
                dialog.error.setText(str(error))
                dialog.error.show()
                continue
            self.dock.refresh_project(metadata=False)
            page.refresh(selected)
            self.message("Spectrum saved to the project. Shared assignments use the updated values.")
            return

    def delete_spectrum(self):
        store = self.require_store()
        page = self.dock.spectra_page
        record = next((row for row in store.spectra(page.kind) if row["spectrum_id"] == page.selected_id()), None)
        if record is None:
            raise ModelError("Select a spectrum first.")
        # Even an unmodified edit buffer can undo an unassignment/deletion.
        column = "spectrum_id" if record["spectrum"]["kind"] == "sound_power" else "absorption_spectrum_id"
        if any(layer.isEditable() and (store._spectrum_reference_column(layer.customProperty("acalc/feature_type")) == column
                                      or (column == "spectrum_id" and layer.customProperty("acalc/feature_type") == "source_building"))
               for layer in self.layers.values()):
            raise ModelError("Save or discard source layer edits before deleting a sound power spectrum, so undo cannot restore a deleted reference."
                             if column == "spectrum_id" else
                             "Save or discard building and wall layer edits before deleting an absorption spectrum, so undo cannot restore a deleted reference.")
        if QMessageBox.question(self.dock, "Delete spectrum", f"Delete “{record['name']}” from this project?",
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        store.delete_spectrum(record["spectrum_id"])
        self.dock.refresh_project(metadata=False)
        self.message("Spectrum deleted.")

    def edit_directivity(self, create=False, duplicate=False, identity=None):
        store = self.require_store()
        if self.engine_runner and self.engine_runner.active:
            raise ModelError("Wait for the calculation to finish before editing the library.")
        page = self.dock.directivity_page
        identity = identity or page.selected_id()
        record = next((r for r in store.directivities() if r["directivity_id"] == identity), None)
        if (not create or duplicate) and record is None:
            raise ModelError("Select a directionality first.")
        if duplicate:
            from copy import deepcopy
            record = deepcopy(record)
            existing = {r["name"].casefold() for r in store.directivities()}
            base, suffix = record["name"] + " copy", 1
            name = base
            while name.casefold() in existing:
                suffix += 1
                name = f"{base} {suffix}"
            record["name"] = name
        dialog = DirectivityDialog(self.dock, record if not create or duplicate else None, input_unit=input_unit(store))
        if record and not create:
            usage = [f'{r["name"]}: {r["count"]} saved' + (" (removed layer)" if r["archived"] else "")
                     for r in store.directivity_usage(identity)]
            for layer in self.layers.values():
                if layer.customProperty("acalc/feature_type") == "point_source" and layer.isEditable():
                    count = sum(str(f["directivity_id"]) == identity for f in layer.getFeatures())
                    usage.append(f"{layer.name()}: {count} in edit buffer")
            dialog.usage.setText("Shared assignments: " + ("; ".join(usage) if usage else "None") +
                                 ". Editing the principal also changes how existing assignments are aimed.")
        while dialog.exec() == QDialog.Accepted:
            try:
                if create:
                    identity = store.add_directivity(dialog.name.text(), dialog.value)
                else:
                    store.save_directivity(identity, dialog.name.text(), dialog.value, record["revision"])
            except ModelError as error:
                dialog.show_error(str(error))
                continue
            self.dock.refresh_project(metadata=False)
            page.refresh(identity)
            self.message("Directionality saved to the project local library.")
            return identity
        return None

    def delete_directivity(self):
        store = self.require_store()
        identity = self.dock.directivity_page.selected_id()
        record = next((r for r in store.directivities() if r["directivity_id"] == identity), None)
        if not record:
            raise ModelError("Select a directionality first.")
        if self.engine_runner and self.engine_runner.active:
            raise ModelError("Wait for the calculation to finish before editing the library.")
        sources = [r for r in store.layers() + store.layers(archived=True) if r["feature_type"] == "point_source"]
        if any(layer.isEditable() and any(self.matches(layer, record) for record in sources)
               for layer in self.project.mapLayers().values() if hasattr(layer, "isEditable")):
            raise ModelError("Save or discard point-source layer edits before deleting directionality, so undo cannot restore a deleted reference.")
        if QMessageBox.question(self.dock, "Delete directionality", f"Delete “{record['name']}” from this project?",
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        store.delete_directivity(identity)
        self.dock.refresh_project(metadata=False)
        self.message("Directionality deleted.")

    def add_layer(self):
        store = self.require_store()
        kind = LAYER_TYPES[self.dock.current_kind]
        name, accepted = QInputDialog.getText(self.dock, "New model layer", "Layer name", text=kind.title)
        if not accepted:
            return
        record = store.add_layer(kind.key, name)
        self.refresh_providers()
        self.ensure_layer(record)
        self.dock.refresh_project(metadata=False)
        self.dock.refresh_layers(record["layer_id"])
        self.message("Layer created. Add it to any variants that should use it.")

    def zoom_generated_terrain(self, layer_id):
        self.open_layer_properties(layer_id)
        layer = self.layers[layer_id]
        canvas = self.iface.mapCanvas()
        extent = canvas.mapSettings().layerExtentToOutputExtent(layer, layer.extent())
        canvas.zoomToFeatureExtent(extent)
        canvas.refresh()

    def rename_generated_terrain(self, layer_id):
        record = next(row for row in self.require_store().layers() if row["layer_id"] == layer_id)
        self._rename_layer(self.ensure_layer(record))

    def rename_layer(self):
        self._rename_layer(self.selected_layer())

    def _rename_layer(self, layer):
        name, accepted = QInputDialog.getText(self.dock, "Rename layer", "Layer name", text=layer.name())
        if accepted:
            self.require_store().rename_layer(layer.customProperty("acalc/layer_id"), name)
            layer.setName(name.strip())

    def set_layer_z_values(self):
        from .layer_elevation import set_layer_z_values
        from .layer_elevation_dialog import SetLayerZDialog
        layer = self.selected_layer()
        dialog = SetLayerZDialog(self.dock, self.require_store(), layer)
        while dialog.exec() == QDialog.Accepted:
            try:
                count = set_layer_z_values(layer, **dialog.options())
                break
            except (ModelError, OSError, RuntimeError) as error:
                dialog.error.setText(str(error))
        else:
            return
        self.dock.refresh_features()
        self.message(f"Z values applied to {count} objects. Save layer edits to write changes to the GeoPackage.")

    def import_layer(self):
        from .layer_import import import_layer
        from .layer_import_dialog import ImportLayerDialog
        store = self.require_store()
        dialog = ImportLayerDialog(self.dock, store, self.project,
                                   self.iface.activeLayer(), self.dock.current_kind)
        while True:
            if dialog.exec() != QDialog.Accepted:
                return
            try:
                record, source_count, object_count = import_layer(store, dialog.source_layer(), dialog.options(), self.project)
                break
            except (ModelError, OSError, RuntimeError) as error:
                dialog.error.setText(str(error))
        self.refresh_providers()
        self.ensure_layer(record)
        self.dock.refresh_project(metadata=False)
        self.dock.show_view(record["feature_type"])
        self.dock.refresh_layers(record["layer_id"])
        self.activate_layer(record["layer_id"])
        self.message(f"Imported {source_count} features as {object_count} objects in “{record['name']}”. "
                     "Copy saved. Add this layer to the variants that should use it.")

    def import_osm_buildings(self):
        from .layer_import import import_layer
        from .osm_import_dialog import OsmImportDialog
        store = self.require_store()
        dialog = OsmImportDialog(self.dock, store, self.iface.mapCanvas())
        try:
            while True:
                if dialog.exec() != QDialog.Accepted:
                    return
                # Drawing an extent temporarily hides the dialog and permits
                # interaction with the rest of QGIS, including model changes.
                if self.store is not store:
                    raise ModelError("The open Acalc model changed. Start the OpenStreetMap import again for the current model.")
                if self.engine_runner and self.engine_runner.active:
                    raise ModelError("Wait for the calculation to finish before importing buildings.")
                try:
                    record, _, object_count = import_layer(store, dialog.source_layer(), dialog.options(), self.project)
                    break
                except (ModelError, OSError, RuntimeError) as error:
                    dialog.error.setText(str(error))
            self.refresh_providers()
            self.ensure_layer(record)
            self.dock.refresh_project(metadata=False)
            self.dock.show_view("building")
            self.dock.refresh_layers(record["layer_id"])
            self.activate_layer(record["layer_id"])
            report = dialog.report
            self.message(f"Imported {object_count} OpenStreetMap buildings in “{record['name']}”; "
                         f"{len(report.skipped)} outlines skipped, {report.default_height_count} default heights. "
                         "Copy saved. Review heights and ground elevations, then add the layer to the variants that should use it.")
        finally:
            dialog.deleteLater()

    def remove_generated_terrain(self, layer_id):
        record = next(row for row in self.require_store().layers() if row["layer_id"] == layer_id)
        self._remove_layer(self.ensure_layer(record), record)

    def remove_layer(self):
        self._remove_layer(self.selected_layer(), self.selected_record())

    def _remove_layer(self, layer, record):
        answer = QMessageBox.question(self.dock, "Remove model layer", f"Remove “{layer.name()}” from this model and all variants?\n\nIts saved data will remain in the GeoPackage and can be restored from the Project view.", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        if layer.isModified():
            raise ModelError("Save or discard this layer’s edits before removing it.")
        self.store.archive_layer(record["layer_id"])
        self._changing = True
        try:
            self.dock.bind_layer(None)
            self.disconnect_layer(layer.id())
            self.layers.pop(record["layer_id"], None)
            self.project.removeMapLayer(layer.id())
        finally:
            self._changing = False
        self.dock.refresh_project(metadata=False)
        self.message("Layer removed. Use Project → Restore layer to bring it back.")

    def restore_layer(self):
        records = self.require_store().layers(archived=True)
        if not records:
            self.message("There are no removed layers to restore.")
            return
        labels = [f"{r['name']} · {LAYER_TYPES[r['feature_type']].title} · {r['layer_id'][-8:]}" for r in records]
        choice, accepted = QInputDialog.getItem(self.dock, "Restore layer", "Removed layer", labels, 0, False)
        if accepted:
            record = records[labels.index(choice)]
            self.store.restore_layer(record["layer_id"])
            self.ensure_layer(record)
            self.dock.refresh_project(metadata=False)
            self.message("Layer restored. Add it to the appropriate variants.")

    def start_editing(self, layer):
        if not layer.isEditable() and not layer.startEditing():
            raise ModelError("The layer could not be opened for editing. Check that the GeoPackage is writable.")

    def open_receiver_results(self, layer_id, object_id, variant_id):
        import uuid
        record = next((row for row in self.require_store().layers() if row["layer_id"] == layer_id), None)
        if record is None:
            raise ModelError("This receiver’s layer has been removed. Restore the layer to open its properties.")
        layer = self.ensure_layer(record)
        if record["feature_type"] == "area_receiver":
            results = self.store.receiver_results(variant_id, layer_id, object_id)
            if results:
                object_id = results[0]["metadata"].get("area_receiver_id", object_id)
        feature = next((f for f in layer.getFeatures() if str(uuid.UUID(str(f["object_id"]))) == object_id), None)
        if feature is None:
            raise ModelError("This receiver has been deleted. Its saved results remain available in the results list.")
        self.edit_feature(layer=layer, feature_id=feature.id(),
                          result_selection=variant_id)

    def feature_layer(self, layer_id, feature_id):
        layer = self.project.mapLayer(layer_id)
        if layer is None or layer not in self.layers.values():
            raise ModelError("This feature’s model layer is no longer open.")
        if not layer.getFeature(feature_id).isValid():
            raise ModelError("This feature no longer exists in the layer.")
        return layer

    def open_feature_properties(self, layer_id, feature_id):
        result_layer = self.project.mapLayer(layer_id)
        if result_layer is not None and result_layer in self.result_layers.values():
            area_id = result_layer.customProperty("acalc/area_receiver_id", "")
            if area_id:
                self.open_receiver_results(result_layer.customProperty("acalc/receiver_layer_id"), area_id,
                                           result_layer.customProperty("acalc/variant_id"))
                return
            result = result_layer.getFeature(feature_id)
            if not result.isValid():
                raise ModelError("This result no longer exists in the layer.")
            self.open_receiver_results(result["layer_id"], result["object_id"],
                                       result["variant_id"])
            return
        layer = self.feature_layer(layer_id, feature_id)
        self.iface.setActiveLayer(layer)
        self.dock.select_layer(layer)
        layer.selectByIds([feature_id])
        self.edit_feature(layer=layer, feature_id=feature_id)

    def edit_feature(self, create=False, layer=None, feature_id=None, draft_feature=None, result_selection=None):
        layer = layer if layer is not None else self.selected_layer()
        if layer.customProperty('acalc/layer_id') in self.require_store().generated_terrain_ids():
            self.open_layer_properties(layer.customProperty('acalc/layer_id'))
            return
        # Browsing is iterative so long sessions do not grow the Python stack.
        position, tab = None, None
        while True:
            result = self._edit_feature_once(create, layer, feature_id, draft_feature, result_selection, position, tab)
            if not isinstance(result, tuple):
                return result
            layer, feature_id, position, tab = result
            create, draft_feature = False, None

    def _edit_feature_once(self, create=False, layer=None, feature_id=None, draft_feature=None,
                           result_selection=None, position=None, tab=None):
        layer = layer if layer is not None else self.selected_layer()
        kind = LAYER_TYPES[layer.customProperty("acalc/feature_type")]
        feature = draft_feature if create else None
        if not create:
            if feature_id is None:
                selected = layer.selectedFeatureIds()
                if len(selected) != 1:
                    raise ModelError("Select exactly one feature to edit.")
                feature_id = selected[0]
            feature = layer.getFeature(feature_id)
            if not feature.isValid():
                raise ModelError("This feature no longer exists in the layer.")
        if create and feature is not None:
            feature["enabled"] = 1
        context = None
        if kind.key in ("point_receiver", "area_receiver", "measurement") and not create:
            context = (self.store, layer.customProperty("acalc/layer_id"), str(feature["object_id"]), result_selection)
        metadata = self.store.metadata()
        navigation = (None, None)
        if not create:
            ids = [item.id() for item in layer.getFeatures()]
            index = ids.index(feature.id())
            navigation = (ids[index - 1] if index else None, ids[index + 1] if index + 1 < len(ids) else None)
        dialog = FeatureDialog(self.dock, kind, metadata["horizontal_unit"], feature,
                               self.store.spectra() if kind.key == "source_building" else
                               self.store.spectra("sound_power") if kind.spectrum else
                               self.store.spectra("absorption") if (kind.is_building or kind.key == "barrier") else (), context,
                               object_actions=not create, horizontal_scale=metadata["horizontal_to_m"],
                               model_wkt=metadata["crs_wkt"], elevation_rasters=self.store.elevation_rasters(),
                               reflection_default=(self.store.calculation()["settings"]["building_reflection_coefficient" if kind.is_building else "barrier_reflection_coefficient"]
                                                   if (kind.is_building or kind.key == "barrier") else None),
                               directivities=self.store.directivities() if kind.key == "point_source" else (),
                               directivity_store=self.store, edit_directivity=self.edit_directivity, elevation_unit=elevation_unit(self.store),
                               navigation_ids=navigation)
        if position is not None:
            dialog.restoreGeometry(position)
        if tab is not None:
            dialog.tabs.setCurrentIndex(tab)
        duplicate = None
        if not create:
            def delete_object():
                if self.delete_feature(layer.id(), feature_id, parent=dialog):
                    dialog.reject()

            def duplicate_object():
                nonlocal duplicate
                duplicate = self.copy_feature(layer.id(), feature_id)
                dialog.reject()

            dialog.delete_requested.connect(lambda: self.run(delete_object))
            dialog.duplicate_requested.connect(lambda: self.run(duplicate_object))
        if result_selection is not None and tab is None:
            dialog.tabs.setCurrentWidget(dialog.receiver_results)
        try:
            result = dialog.exec()
        finally:
            if getattr(dialog, "receiver_results", None) and dialog.receiver_results.area_view:
                dialog.receiver_results.area_view.dispose()
        if duplicate is not None:
            self.open_feature_properties(layer.id(), duplicate.id())
            return True
        if result != QDialog.Accepted:
            return False
        self.start_editing(layer)
        remapped = initialize_added_object_fields(layer, kind)
        if not create and feature.id() in remapped:
            feature = layer.getFeature(remapped[feature.id()])
        layer.beginEditCommand("Acalc: add feature" if create else "Acalc: edit feature")
        try:
            if create:
                feature = QgsFeature(layer.fields())
                feature["object_id"] = new_id()
                feature["name"] = dialog.name.text().strip()
                feature["notes"] = dialog.notes.toPlainText()
                feature["enabled"] = 1
                for name, value in dialog.object_attributes().items():
                    feature[name] = value
                if kind.spectrum:
                    feature["spectrum_id"] = dialog.spectrum_combo.currentData()
                feature.setGeometry(dialog.geometry)
                if not layer.addFeature(feature):
                    raise ModelError("Could not add the feature.")
            else:
                if not layer.changeGeometry(feature.id(), dialog.geometry):
                    raise ModelError("Could not update the geometry.")
                for name, value in (("name", dialog.name.text().strip()), ("notes", dialog.notes.toPlainText())):
                    if not layer.changeAttributeValue(feature.id(), layer.fields().indexFromName(name), value):
                        raise ModelError(f"Could not update {name}.")
                for name, value in dialog.object_attributes().items():
                    if not layer.changeAttributeValue(feature.id(), layer.fields().indexFromName(name), value):
                        raise ModelError(f"Could not update {name}.")
                if kind.spectrum and not layer.changeAttributeValue(feature.id(), layer.fields().indexFromName("spectrum_id"), dialog.spectrum_combo.currentData()):
                    raise ModelError("Could not update the spectrum assignment.")
            layer.endEditCommand()
        except Exception:
            layer.destroyEditCommand()
            raise
        layer.selectByIds([feature.id()])
        layer.updateExtents()
        layer.triggerRepaint()
        self.dock.refresh_features()
        self.message("Feature applied. Save layer edits to write changes to the GeoPackage.")
        if dialog.go_to_object:
            self.iface.setActiveLayer(layer)
            self.dock.select_layer(layer)
            self.iface.mapCanvas().zoomToSelected(layer)
            self.iface.mapCanvas().refresh()
        if dialog.navigation_target is not None:
            target = layer.getFeature(remapped.get(dialog.navigation_target, dialog.navigation_target))
            if target.isValid():
                self.iface.setActiveLayer(layer)
                self.dock.select_layer(layer)
                layer.selectByIds([target.id()])
                return layer, target.id(), dialog.saveGeometry(), dialog.tabs.currentIndex()
            raise ModelError("The next object no longer exists in the layer.")
        return True

    def draw_feature(self):
        layer = self.selected_layer()
        self.iface.setActiveLayer(layer)
        self.start_editing(layer)
        self.digitizer.start(layer)
        self.message("Draw a point, or finish a line/polygon with a right-click. Acalc properties opens automatically; Apply adds the feature and Cancel discards the sketch.")

    def copy_feature(self, layer_id, feature_id):
        """Copy the current layer object, preserving geometry and shared references."""
        layer = self.feature_layer(layer_id, feature_id)
        self.start_editing(layer)
        kind = LAYER_TYPES[layer.customProperty("acalc/feature_type")]
        remapped = initialize_added_object_fields(layer, kind)
        source = layer.getFeature(remapped.get(feature_id, feature_id))
        duplicate = QgsFeature(layer.fields())
        duplicate.setAttributes(source.attributes())
        duplicate["fid"] = None
        duplicate["object_id"] = new_id()
        duplicate["name"] = str(source["name"]) + " copy"
        duplicate.setGeometry(source.geometry())
        layer.beginEditCommand("Acalc: duplicate feature")
        try:
            if not layer.addFeature(duplicate):
                raise ModelError("Could not duplicate the feature.")
            layer.endEditCommand()
        except Exception:
            layer.destroyEditCommand()
            raise
        layer.updateExtents()
        layer.triggerRepaint()
        self.dock.refresh_features()
        self.message("Feature duplicated. Save layer edits to write changes to the GeoPackage.")
        return duplicate

    def duplicate_feature(self, layer_id, feature_id):
        duplicate = self.copy_feature(layer_id, feature_id)
        self.open_feature_properties(layer_id, duplicate.id())

    def feature_move_destinations(self, layer):
        store = self.require_store()
        generated = store.generated_terrain_ids()
        if layer.customProperty("acalc/layer_id") in generated:
            return []
        return [record for record in store.layers()
                if record["feature_type"] == layer.customProperty("acalc/feature_type")
                and record["layer_id"] != layer.customProperty("acalc/layer_id")
                and record["layer_id"] not in generated]

    def move_feature(self, layer_id, feature_id, destination_id):
        """Transfer one object between same-type model layers' edit buffers."""
        source_layer = self.feature_layer(layer_id, feature_id)
        record = next((record for record in self.feature_move_destinations(source_layer)
                       if record["layer_id"] == destination_id), None)
        if record is None:
            raise ModelError("Choose another model layer of the same type.")
        destination = self.ensure_layer(record)
        missing = set(source_layer.fields().names()) - set(destination.fields().names())
        if missing:
            raise ModelError("The destination layer is missing fields: " + ", ".join(sorted(missing)))
        source = source_layer.getFeature(feature_id)
        if any(feature["object_id"] == source["object_id"] for feature in destination.getFeatures()):
            raise ModelError("The destination layer already contains this object.")
        self.start_editing(destination)
        self.start_editing(source_layer)
        kind = LAYER_TYPES[record["feature_type"]]
        remapped = initialize_added_object_fields(source_layer, kind)
        source = source_layer.getFeature(remapped.get(feature_id, feature_id))
        moved = QgsFeature(destination.fields())
        for field in source_layer.fields():
            if field.name() != "fid":
                moved[field.name()] = source[field.name()]
        moved.setGeometry(source.geometry())
        destination.beginEditCommand("Acalc: move feature into layer")
        source_layer.beginEditCommand("Acalc: move feature out of layer")
        try:
            if not destination.addFeature(moved):
                raise ModelError("Could not add the object to the destination layer.")
            if not source_layer.deleteFeature(source.id()):
                raise ModelError("Could not remove the object from its original layer.")
        except Exception:
            source_layer.destroyEditCommand()
            destination.destroyEditCommand()
            raise
        source_layer.endEditCommand()
        destination.endEditCommand()
        for layer in (source_layer, destination):
            layer.updateExtents()
            layer.triggerRepaint()
        self.iface.setActiveLayer(destination)
        self.dock.select_layer(destination)
        destination.selectByIds([moved.id()])
        self.dock.refresh_features()
        self.message(f"Object moved to “{destination.name()}”. Save edits to both layers to keep the move. "
                     "To undo the move, undo once in each layer.")
        return moved

    def delete_feature(self, layer_id, feature_id, parent=None):
        layer = self.feature_layer(layer_id, feature_id)
        name = layer.getFeature(feature_id)["name"]
        return self.delete_features(layer, [feature_id], parent, f"Delete “{name}”?")

    def delete_features(self, layer=None, ids=None, parent=None, prompt=None):
        layer = layer if layer is not None else self.selected_layer()
        ids = layer.selectedFeatureIds() if ids is None else ids
        if not ids:
            return False
        prompt = prompt or f"Delete {len(ids)} selected features?"
        answer = QMessageBox.question(parent if parent is not None else self.dock, "Delete features", prompt + " This can be undone while the layer is in edit mode.", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return False
        self.start_editing(layer)
        layer.beginEditCommand("Acalc: delete features")
        if not layer.deleteFeatures(ids):
            layer.destroyEditCommand()
            raise ModelError("Could not delete the selected features.")
        layer.endEditCommand()
        layer.updateExtents()
        layer.triggerRepaint()
        self.dock.refresh_features()
        return True

    def commit_layer(self, layer, feature_type=None):
        feature_type = feature_type or layer.customProperty("acalc/feature_type")
        if feature_type == "measurement":
            synchronize_measurement_broadband(layer)
        spectra = {row["spectrum_id"]: row for row in self.require_store().spectra()}
        directivities = {row["directivity_id"]: row for row in self.require_store().directivities()}
        errors = validate_layer(layer, LAYER_TYPES[feature_type], spectra, directivities=directivities)
        if errors:
            raise ModelError("Fix model validation errors before saving:\n" + "\n".join(errors[:8]))
        if layer.isEditable() and not layer.commitChanges():
            raise ModelError("Could not save edits. The edit buffer is retained.\n" + "\n".join(layer.commitErrors()))

    def save_layer(self):
        self.commit_layer(self.selected_layer())
        self.dock.refresh_features()
        self.message("Layer edits saved to the GeoPackage.")

    def discard_layer(self):
        layer = self.selected_layer()
        if layer.isModified():
            answer = QMessageBox.question(self.dock, "Discard edits", f"Discard unsaved edits to “{layer.name()}”?", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return
        if layer.isEditable() and not layer.rollBack():
            raise ModelError("Could not discard the edits.")
        self.dock.refresh_features()
        self.message("Unsaved layer edits discarded.")

    def zoom_selection(self):
        layer = self.selected_layer()
        self.iface.mapCanvas().zoomToSelected(layer)
        self.iface.mapCanvas().refresh()

    def validation_errors(self, layer_ids=None, require_spectra=False):
        errors = []
        ground_layers = []
        spectra = {row["spectrum_id"]: row for row in self.require_store().spectra()}
        directivities = {row["directivity_id"]: row for row in self.require_store().directivities()}
        for record in self.require_store().layers():
            if layer_ids is None or record["layer_id"] in layer_ids:
                layer = self.ensure_layer(record)
                errors.extend(validate_layer(layer, LAYER_TYPES[record["feature_type"]], spectra, require_spectra, directivities))
                if record["feature_type"] == "ground_absorption":
                    ground_layers.append(layer)
        errors.extend(ground_absorption_overlap_errors(ground_layers))
        return errors

    def show_validation(self):
        errors = self.validation_errors(require_spectra=True)
        if errors:
            box = QMessageBox(QMessageBox.Warning, "Model validation", f"Found {len(errors)} issue(s).", parent=self.dock)
            box.setDetailedText("\n".join(errors))
            box.exec()
            self.message(errors[0], error=True)
        else:
            self.message("Model validation passed: geometry, identities, attributes, source spectra, and surface materials. Calculation readiness also needs the remaining engine inputs.")

    def configure_calculation(self):
        store = self.require_store()
        store.save_calculation(self.dock.calculation_page.settings())
        self.dock.calculation_page.refresh(force=True)
        self.dock.results_page.refresh()
        self.message("Calculation parameters saved for all variants.")

    def open_calculation_dialog(self):
        self.require_store()
        from .calculation_engines import available_engines
        if not available_engines():
            self.dock.show_view("settings")
            raise ModelError("Add an engine connection in Settings and check its capabilities before calculating.")
        if self.calculation_dialog and self.calculation_dialog.isVisible():
            self.calculation_dialog.raise_()
            self.calculation_dialog.activateWindow()
            return
        self.close_calculation_dialog()
        from .calculation_dialog import CalculationDialog
        self.calculation_dialog = CalculationDialog(self)
        self.calculation_dialog.show()

    def close_calculation_dialog(self):
        if self.calculation_dialog:
            self.calculation_dialog.dispose()
            self.calculation_dialog = None

    def open_back_calculation_dialog(self):
        self.require_store()
        from .calculation_engines import available_engines
        if not any("source_receiver_transfers" in engine.capabilities.get("features", [])
                   for engine in available_engines()):
            self.dock.show_view("settings")
            raise ModelError("The available calculation engines do not provide source-power back-calculation. "
                             "Update the engine and refresh its capabilities in Settings.")
        if self.back_calculation_dialog and self.back_calculation_dialog.isVisible():
            self.back_calculation_dialog.raise_()
            self.back_calculation_dialog.activateWindow()
            return
        self.close_back_calculation_dialog()
        from .back_calculation_dialog import BackCalculationDialog
        self.back_calculation_dialog = BackCalculationDialog(self)
        self.back_calculation_dialog.show()

    def close_back_calculation_dialog(self):
        if self.back_calculation_dialog:
            self.back_calculation_dialog.dispose()
            self.back_calculation_dialog = None

    def model_provider_layers(self):
        """Include untagged and duplicate QGIS providers of this GeoPackage."""
        store = self.require_store()
        layers = []
        for layer in self.project.mapLayers().values():
            if layer.providerType() != "ogr":
                continue
            uri = QgsProviderRegistry.instance().decodeUri("ogr", layer.source())
            if str(Path(uri.get("path", "")).resolve()) == store.path:
                layers.append((layer, uri.get("layerName")))
        return layers

    def prepare_calculation_layers(self, parent=None):
        """Save model edit sessions with consent before the engine reads the file."""
        store = self.require_store()
        providers = self.model_provider_layers()
        model_layers = [layer for layer, _ in providers]
        tables = {layer.id(): table for layer, table in providers}
        pending = [layer for layer in model_layers if layer.isEditable() or layer.isModified()]
        if not pending:
            return model_layers
        answer = QMessageBox.question(
            parent if parent is not None else self.dock, "Save layers before calculating",
            "The following layers in this Acalc model are in edit mode or have unsaved changes:\n\n"
            + "\n".join(layer.name() for layer in pending)
            + "\n\nSave all these layers, close their edit sessions, and continue?",
            QMessageBox.Save | QMessageBox.Cancel, QMessageBox.Save)
        if answer != QMessageBox.Save:
            return None
        records = {record["table_name"]: record for record in [*store.layers(), *store.layers(archived=True)]}
        self.digitizer.stop()
        for layer in pending:
            try:
                record = records.get(tables[layer.id()])
                if layer.isModified() and record is not None:
                    # Raw QGIS copies may not carry Acalc's custom properties.
                    self.commit_layer(layer, feature_type=record["feature_type"])
                elif layer.isEditable() and not layer.commitChanges():
                    raise ModelError("The edit buffer is retained.\n" + "\n".join(layer.commitErrors()))
                if layer.isEditable() or layer.isModified():
                    raise ModelError("The layer still has an open edit session or unsaved changes.")
            except (ModelError, OSError, ValueError, RuntimeError) as error:
                raise ModelError(f"Could not save layer “{layer.name()}”. Calculation was not started.\n{error}") from error
        self.dock.refresh_features()
        return model_layers

    def start_back_calculation(self, variant_id, settings, engine_id=None):
        store = self.require_store()
        from .calculation_engines import calculation_engine
        engine_id = engine_id or self.dock.calculation_page.engine.currentData()
        engine = calculation_engine(engine_id)
        if "source_receiver_transfers" not in engine.capabilities.get("features", []):
            raise ModelError("The selected calculation engine does not provide source-power back-calculation. "
                             "Update the engine and refresh its capabilities in Settings.")
        existing = {row["variant_id"]: row for row in store.variants()}
        if variant_id not in existing:
            raise ModelError("Choose an existing variant to back-calculate.")
        if self.engine_runner.active:
            raise ModelError("A calculation is already running.")
        if self.dock.metadata_values() != self.metadata_snapshot:
            raise ModelError("Save or discard project detail changes before calculating.")
        model_layers = self.prepare_calculation_layers(self.back_calculation_dialog)
        if model_layers is None:
            return False
        included = set(existing[variant_id]["definition"]["layer_ids"])
        overlaps = ground_absorption_overlap_errors([
            self.ensure_layer(record) for record in store.layers()
            if record["feature_type"] == "ground_absorption" and record["layer_id"] in included])
        if overlaps:
            raise ModelError("Cannot back-calculate this variant:\n" + "\n".join(overlaps[:8]))
        settings = dict(settings, pruning_strategy=None)
        engine.save_settings(store, settings)
        self.dock.calculation_page.refresh(force=True)
        self.dock.calculation_page.select_engine(engine_id)
        self.digitizer.stop()
        self._calculation_layers = [(layer, layer.readOnly()) for layer in model_layers]
        try:
            for layer, _ in self._calculation_layers:
                layer.setReadOnly(True)
            self.engine_runner.start_relationships(store, variant_id, engine_id=engine_id)
        except Exception:
            self.calculation_busy_changed(False)
            raise
        self.message("Evaluating source-to-Measurement relationships…")
        return True

    def apply_back_calculation(self, relationships, proposals):
        store = self.require_store()
        if self.engine_runner.active:
            raise ModelError("Wait for the active calculation before applying fitted spectra.")
        if self.dock.metadata_values() != self.metadata_snapshot:
            raise ModelError("Save or discard project detail changes and evaluate the fit again.")
        if any(layer.isModified() for layer, _ in self.model_provider_layers()):
            raise ModelError("The model has unsaved layer edits. Save or discard them and evaluate the fit again.")
        store.apply_back_calculation(relationships, proposals)

    def calculate(self, validate=False, variant_ids=None, settings=None, engine_id=None):
        store = self.require_store()
        from .calculation_engines import calculation_engine
        engine_id = engine_id or self.dock.calculation_page.engine.currentData()
        engine = calculation_engine(engine_id)
        variant = self.selected_variant()
        if variant_ids is None:
            variant_ids = [variant["variant_id"]] if variant else []
        existing = {row["variant_id"]: row for row in store.variants()}
        if not variant_ids or any(value not in existing for value in variant_ids):
            raise ModelError("Choose at least one existing variant to calculate.")
        if self.engine_runner.active:
            raise ModelError("A calculation is already running.")
        if self.dock.metadata_values() != self.metadata_snapshot:
            raise ModelError("Save or discard project detail changes before calculating.")
        model_layers = self.prepare_calculation_layers(self.calculation_dialog)
        if model_layers is None:
            return False
        ground_records = [record for record in store.layers() if record["feature_type"] == "ground_absorption"]
        for variant_id in variant_ids:
            selected = existing[variant_id]
            included = set(selected["definition"]["layer_ids"])
            overlaps = ground_absorption_overlap_errors([
                self.ensure_layer(record) for record in ground_records if record["layer_id"] in included])
            if overlaps:
                raise ModelError(f'Variant “{selected["name"]}” cannot run:\n' + "\n".join(overlaps[:8]))
        if settings is not None:
            engine.save_settings(store, settings)
            self.dock.calculation_page.refresh(force=True)
        self.dock.calculation_page.select_engine(engine_id)
        self.digitizer.stop()
        self._calculation_layers = [(layer, layer.readOnly()) for layer in model_layers]
        try:
            for layer, _ in self._calculation_layers:
                layer.setReadOnly(True)
            self.engine_runner.start_batch(store, variant_ids, validate=validate, engine_id=engine_id)
        except Exception:
            self.calculation_busy_changed(False)
            raise
        self.message("Checking calculation inputs…" if validate else "Calculating receiver levels…")
        return True

    def cancel_calculation(self):
        if self.engine_runner and self.engine_runner.active:
            self.engine_runner.cancel()
            self.message("Stopping the calculation…")

    def calculation_busy_changed(self, busy):
        if not busy:
            for layer, readonly in self._calculation_layers:
                try:
                    layer.setReadOnly(readonly)
                except RuntimeError:
                    pass
            self._calculation_layers = []
        if self.dock:
            self.dock.set_calculating(busy)

    def _calculation_context_current(self, context):
        return self.dock is not None and self.store is context["store"] and self.store.path == context["model_path"]

    def calculation_succeeded(self, context, result):
        if not self._calculation_context_current(context):
            return
        if context.get("relationships"):
            return
        if self.engine_runner.active:
            # Keep model layers locked across the entire batch. Refresh result
            # map layers after the last process releases the GeoPackage.
            return
        self.refresh_providers()
        if context["validate"]:
            self.message(f"engine input compatibility checks passed: {result.get('source_count', 0)} sources, "
                         f"{result.get('receiver_count', 0)} point receivers and "
                         f"{result.get('area_receiver_count', 0)} receiver areas. "
                         "The engine checks propagation paths and resource limits when calculating.")
            return
        self.dock.show_view("results")
        self.dock.results_page.publish_saved_results(context["variant_id"])
        if not any(run["run_id"] == result["run_id"] for run in self.store.calculation_runs(context["variant_id"])):
            self.message("The engine reported completion, but its run was not found in the model. Check the saved results.", error=True)
            return
        if self.dock.results_page.layer_error:
            self.message(self.dock.results_page.layer_error, error=True)
            return
        tree_count = result.get("tree_area_count", 0)
        trees_note = f" Included {tree_count} wooded areas." if tree_count else ""
        self.message(f"Calculated {result.get('receiver_count', 0)} receivers.{trees_note} Octave and broadband results saved to the model.")
        if result.get("pathway_warnings"):
            # The run completed; surface its pathway problems prominently.
            self.message(f"{context['variant_name']}: " + "\n".join(result["pathway_warnings"]), error=True)

    def calculation_failed(self, context, message):
        if context.get("relationships"):
            return
        if self._calculation_context_current(context):
            self.dock.results_page.refresh(context["variant_id"])
            self.message(message, error=True)

    def calculation_stopped(self, context):
        if context.get("relationships"):
            return
        if self._calculation_context_current(context):
            self.dock.results_page.refresh(context["variant_id"])
            self.message("Calculation stopped. Check saved results; a run that completed before cancellation may have been stored.")

    def new_variant(self, duplicate=False):
        store = self.require_store()
        current = self.selected_variant()
        default = current["name"] + " copy" if duplicate and current else "New variant"
        name, accepted = QInputDialog.getText(self.dock, "New variant", "Variant name", text=default)
        if accepted:
            value = current["definition"] if duplicate and current else None
            selected = store.add_variant(name, value)
            self.dock.refresh_project(metadata=False)
            self.dock.refresh_variants(selected)
            self.message("Variant saved.")

    def rename_variant(self):
        config = self.selected_variant()
        if not config:
            return
        name, accepted = QInputDialog.getText(self.dock, "Rename variant", "Variant name", text=config["name"])
        if accepted:
            self.store.save_variant(config["variant_id"], name, config["definition"])
            self.dock.refresh_variants(config["variant_id"])

    def clear_results(self):
        store = self.require_store()
        page = self.dock.results_page
        variant_id = page.variant.currentData()
        if not variant_id or not store.latest_calculation_run(variant_id):
            return
        name = page.variant.currentText()
        if QMessageBox.question(
                self.dock, "Clear results",
                f"Delete all saved calculation results and result layers for “{name}”?\n\n"
                "Renamed result layers, model layers and measurements will be kept. This cannot be undone.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        owned = result_layer_tables(store)
        targets = [layer for node in self.group.findLayers() if (layer := node.layer()) is not None
                   and any(record.get("variant_id") == variant_id and layer.name() == record.get("name")
                           and self.matches(layer, {"table_name": table}) for table, record in owned.items())]
        ids = {layer.id() for layer in targets}
        tables = {table for table, record in owned.items() if record.get("variant_id") == variant_id
                  and any(self.matches(layer, {"table_name": table}) for layer in targets)
                  and not any(layer.id() not in ids and self.matches(layer, {"table_name": table})
                              for layer in self.project.mapLayers().values())}
        clear_variant_results(store, variant_id, projection_tables=tables)
        changing = self._changing
        self._changing = True
        try:
            for layer in list(self.project.mapLayers().values()):
                if layer.id() in ids:
                    self.properties_actions.remove_layer(layer.id())
                    for key, current in list(self.result_layers.items()):
                        if current is layer:
                            del self.result_layers[key]
                    self.project.removeMapLayer(layer.id())
        finally:
            self._changing = changing
        page.refresh(variant_id)
        if not page.layer_error:
            self.message(f"Results cleared for “{name}”.")

    def delete_variant(self):
        config = self.selected_variant()
        if not config:
            return
        if QMessageBox.question(self.dock, "Delete variant", f"Delete “{config['name']}”? Model layers will be kept.", QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes:
            self.store.delete_variant(config["variant_id"])
            self.dock.refresh_project(metadata=False)

    def save_membership(self):
        config = self.selected_variant()
        if not config:
            return
        ids = [item.data(0, Qt.UserRole) for item in self.dock.member_items()
               if item.checkState(0) == Qt.Checked]
        config["definition"]["layer_ids"] = ids
        try:
            self.store.save_variant(config["variant_id"], config["name"], config["definition"])
        except Exception:
            # Deleting tree items inside their itemChanged signal can crash Qt.
            QTimer.singleShot(0, self.dock.variant_selected)
            raise
        self.dock.variant_summary.setText(f"{len(ids)} of {len(self.store.layers())} object layers included")
        self.message("Variant membership saved.")

    def generate_raster_terrain(self, raster):
        from .raster_terrain import save_terrain
        from .raster_terrain_dialog import RasterTerrainDialog
        store = self.require_store()
        dialog = RasterTerrainDialog(self.dock, store, self.iface.mapCanvas(), raster)
        try:
            if dialog.exec() != QDialog.Accepted:
                return
            if self.store is not store or store.metadata()['crs_wkt'] != dialog.wkt:
                raise ModelError("The model or its coordinate system changed. Generate terrain again for the current model.")
            if self.engine_runner and self.engine_runner.active:
                raise ModelError("Wait for the calculation to finish before importing terrain.")
            if dialog.mesh is None:
                raise ModelError("Generate terrain before saving it.")
            record = save_terrain(store, dialog.name.text(), dialog.mesh)
            self.refresh_providers()
            self.ensure_layer(record)
            self.dock.refresh_project(metadata=False)
            self.terrain_changed()
            self.open_layer_properties(record['layer_id'])
            self.message(f"Saved terrain in “{record['name']}” with {len(dialog.mesh.vertices):,} vertices. "
                         "Add this terrain layer to the variants that should use it.")
            return record
        finally:
            dialog.deleteLater()

    def terrain_changed(self):
        """Refresh raster sources, generated terrain and variant membership."""
        self.group_elevation_rasters()
        self.dock.terrain_page.refresh()
        self.dock.refresh_variants()

    def export_variant(self):
        config = self.selected_variant()
        if not config:
            return
        records = config["definition"]["layer_ids"]
        if any(layer.isModified() for record_id, layer in self.layers.items() if record_id in records):
            raise ModelError("Save the included layers’ edits before exporting a variant.")
        errors = self.validation_errors(records)
        if errors:
            raise ModelError("Fix validation issues before exporting:\n" + "\n".join(errors[:8]))
        path, _ = QFileDialog.getSaveFileName(self.dock, "Export variant definition", str(Path(self.store.path).with_suffix(".variant.json")), "JSON (*.json)")
        if path:
            if not path.lower().endswith(".json"):
                path += ".json"
            if Path(path).resolve() == Path(self.store.path):
                raise ModelError("Choose a separate JSON filename.")
            value = self.store.export_variant(config["variant_id"])
            import os
            value["geopackage"] = os.path.relpath(self.store.path, Path(path).resolve().parent)
            # Atomic replacement avoids leaving a truncated definition on interruption.
            import tempfile
            fd, temp = tempfile.mkstemp(prefix=".acalc-", suffix=".json", dir=Path(path).resolve().parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
                    stream.write("\n")
                os.replace(temp, path)
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)
            self.message(f"Variant definition exported to {path}.")
