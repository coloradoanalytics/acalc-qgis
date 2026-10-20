# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Layer naming, navigation and grouping with real QGIS tree/Qt widgets."""

from unittest.mock import patch

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtTest import QTest
from qgis.PyQt.QtWidgets import QInputDialog, QMessageBox, QToolButton
from qgis.core import QgsLayerTreeModel, QgsProject, QgsRasterLayer
from qgis.gui import QgsLayerTreeView

from acalc.plugin import AcalcPlugin
from acalc.schema import LAYER_TYPES
from tests.test_model import APP, FakeIface, ModelFixture, add_feature


class LayerPresentationTests(ModelFixture):
    def setUp(self):
        super().setUp()
        self.iface = FakeIface()
        self.tree = QgsLayerTreeView(self.iface.window)
        self.tree.setModel(QgsLayerTreeModel(QgsProject.instance().layerTreeRoot(), self.tree))
        self.tree.resize(400, 600)
        self.iface.layerTreeView = lambda: self.tree
        self.plugin = AcalcPlugin(self.iface)
        self.plugin.initGui()
        self.records = [self.store.add_layer(key, name) for key, name in (
            ("point_source", "Plant"), ("point_source", "Roadside"), ("terrain_line", "Survey"))]
        self.plugin.open_project(self.path)
        self.iface.window.show()
        APP.processEvents()

    def tearDown(self):
        for layer in self.plugin.layers.values():
            if layer.isEditable():
                layer.rollBack()
        self.plugin.unload()
        self.iface.window.close()
        APP.processEvents()
        super().tearDown()

    def test_calculation_variant_tree_switches_membership_and_preserves_navigation(self):
        dock = self.plugin.dock
        dock.show_view("calculation")
        self.assertIs(dock.stack.currentWidget(), dock.calculation_page)
        self.assertTrue(dock.calculation_page.isAncestorOf(dock.variant_controls))
        self.assertFalse(dock.project_page.isAncestorOf(dock.variant_controls))
        self.assertIs(dock.calculation_page.variant, dock.variant_combo)
        self.assertNotIn("Variants", [a.text() for a in dock.findChild(QToolButton).menu().actions()])
        self.assertEqual([dock.members.topLevelItem(i).text(0)
                          for i in range(dock.members.topLevelItemCount())],
                         ['Point sources', 'Terrain lines'])
        group = dock.members.topLevelItem(0)
        self.assertFalse(group.flags() & Qt.ItemIsUserCheckable)
        first_id = self.records[0]['layer_id']
        dock.members.setCurrentItem(group.child(0))
        dock.members.topLevelItem(1).setExpanded(False)
        group.child(0).setCheckState(0, Qt.Checked)
        self.assertEqual(self.plugin.selected_variant()['definition']['layer_ids'], [first_id])
        self.assertEqual(dock.members.currentItem().data(0, Qt.UserRole), first_id)
        self.assertFalse(dock.members.topLevelItem(1).isExpanded())
        self.assertEqual(dock.variant_summary.text(), '1 of 3 object layers included')
        with patch.object(self.plugin, 'open_layer_properties') as open_layer:
            dock.open_member_layer(dock.members.topLevelItem(0), 0)
            open_layer.assert_not_called()
        original_id = dock.variant_combo.currentData()
        second_id = self.store.add_variant('Second')
        dock.refresh_variants(second_id)
        self.assertTrue(all(item.checkState(0) == Qt.Unchecked for item in dock.member_items()))
        dock.variant_combo.setCurrentIndex(dock.variant_combo.findData(original_id))
        self.assertEqual(dock.members.topLevelItem(0).child(0).checkState(0), Qt.Checked)
        self.assertFalse(dock.members.topLevelItem(1).isExpanded())

    def test_variant_menu_commands_keep_selection_membership_and_project_drafts(self):
        dock = self.plugin.dock
        dock.show_view('project')
        dock.project_name.setText('Unsaved project title')
        dock.show_view('calculation')
        next(dock.member_items()).setCheckState(0, Qt.Checked)
        self.assertFalse(dock.delete_variant_action.isEnabled())
        actions = [action.text() for action in dock.variant_menu.actions() if not action.isSeparator()]
        self.assertEqual(actions[:4], ['Create…', 'Rename…', 'Delete', 'Duplicate…'])
        with patch.object(QInputDialog, 'getText', return_value=('Copy', True)):
            dock.duplicate_variant_action.trigger()
        copied_id = dock.variant_combo.currentData()
        self.assertEqual(self.plugin.selected_variant()['definition']['layer_ids'], [self.records[0]['layer_id']])
        with patch.object(QInputDialog, 'getText', return_value=('Renamed copy', True)):
            dock.rename_variant_action.trigger()
        self.assertEqual(dock.variant_combo.currentData(), copied_id)
        self.assertEqual(dock.variant_combo.currentText(), 'Renamed copy')
        self.assertEqual(dock.calculation_page.variant.currentData(), copied_id)
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.Yes):
            dock.delete_variant_action.trigger()
        self.assertNotIn(copied_id, [row['variant_id'] for row in self.store.variants()])
        with patch.object(QInputDialog, 'getText', return_value=('Empty variant', True)):
            dock.create_variant_action.trigger()
        self.assertEqual(self.plugin.selected_variant()['name'], 'Empty variant')
        self.assertEqual(self.plugin.selected_variant()['definition']['layer_ids'], [])
        self.assertEqual(dock.project_name.text(), 'Unsaved project title')
        dock.set_calculating(True)
        dock.show_view('calculation')
        self.assertFalse(dock.variant_controls.isEnabled())
        self.assertTrue(all(not action.isEnabled() for action in dock.variant_actions))
        dock.set_calculating(False)
        self.assertTrue(dock.variant_controls.isEnabled())

    def test_dirty_names_update_in_every_view_without_renaming_or_membership_changes(self):
        record = self.records[0]
        layer = self.plugin.layers[record["layer_id"]]
        dock = self.plugin.dock
        self.plugin.open_layer_properties(record["layer_id"])
        node = QgsProject.instance().layerTreeRoot().findLayer(layer.id())
        membership = self.plugin.selected_variant()["definition"]

        def check(dirty):
            APP.processEvents()
            suffix = " *" if dirty else ""
            self.assertEqual(dock.layer_combo.currentText(), "Plant" + suffix)
            item = next(item for item in dock.member_items()
                        if item.data(0, Qt.UserRole) == record["layer_id"])
            self.assertEqual(item.text(0), "Plant" + suffix)
            self.assertEqual(bool(self.tree.indicators(node)), dirty)
            if dirty:
                self.assertFalse(self.tree.indicators(node)[0].icon().isNull())
            self.assertEqual(layer.name(), "Plant")
            self.assertEqual(self.store.layers()[0]["name"], "Plant")
            self.assertEqual(self.plugin.selected_variant()["definition"], membership)

        check(False)
        layer.startEditing()
        layer.beginEditCommand("Add object")
        add_feature(layer, LAYER_TYPES["point_source"])
        layer.endEditCommand()
        check(True)
        layer.undoStack().undo()
        check(False)
        layer.undoStack().redo()
        check(True)
        self.plugin.commit_layer(layer)
        check(False)
        layer.startEditing()
        feature = next(layer.getFeatures())
        layer.changeAttributeValue(feature.id(), layer.fields().indexFromName("notes"), "Pending")
        check(True)
        layer.rollBack()
        check(False)

    def test_double_click_navigates_from_variants_and_terrain_including_active_layer(self):
        dock = self.plugin.dock
        for record in self.records:
            layer = self.plugin.layers[record["layer_id"]]
            self.iface.setActiveLayer(layer)
            dock.show_view("calculation")
            item = next(item for item in dock.member_items()
                        if item.data(0, Qt.UserRole) == record["layer_id"])
            APP.processEvents()
            position = dock.members.visualItemRect(item).center()
            QTest.mouseClick(dock.members.viewport(), Qt.LeftButton, Qt.NoModifier, position)
            APP.processEvents()
            QTest.mouseDClick(dock.members.viewport(), Qt.LeftButton, Qt.NoModifier, position)
            self.assertIs(dock.current_layer, layer)
            self.assertIs(dock.stack.currentWidget(), dock.terrain_page if record["feature_type"] == "terrain_line" else dock.sources_page)

    def test_source_receiver_and_object_types_share_tabbed_pages(self):
        kinds = ("point_source", "line_source", "area_source", "source_building",
                 "point_receiver", "area_receiver", "measurement", "building", "barrier")
        records = {}
        for row in self.records:
            if row["feature_type"] in kinds:
                records.setdefault(row["feature_type"], row)
        for kind in kinds:
            if kind not in records:
                records[kind] = self.store.add_layer(kind, kind)
        self.plugin.reload_layers()
        dock = self.plugin.dock
        menu = dock.findChild(QToolButton).menu()
        self.assertEqual([action.text() for action in menu.actions()],
                         ['Project', 'Calculation', 'Spectra', 'Directivity',
                          'Sources', 'Receivers', 'Terrain', 'Objects', 'Results', 'Settings'])
        actions = {action.text() for action in menu.actions()}
        self.assertTrue({"Sources", "Receivers"} <= actions)
        self.assertFalse({LAYER_TYPES[kind].title for kind in kinds} & actions)
        for page, page_kinds, heading in (
            (dock.sources_page, kinds[:4], "Sources"),
            (dock.receivers_page, kinds[4:7], "Receivers"),
            (dock.objects_page, kinds[7:], "Objects"),
        ):
            self.assertEqual(page.tabs.count(), len(page_kinds))
            for index, kind in enumerate(page_kinds):
                if index == 0:
                    dock.show_view(kind)
                else:
                    page.tabs.setCurrentIndex(index)
                self.assertIs(dock.stack.currentWidget(), page)
                self.assertEqual(dock.heading.text(), heading)
                self.assertEqual(dock.current_kind, kind)
                self.assertIs(dock.feature_page.parent(), page.hosts[kind])
                self.assertEqual(dock.layer_combo.currentData(), records[kind]["layer_id"])
                self.assertIs(dock.current_layer, self.plugin.layers[records[kind]["layer_id"]])
            page.tabs.setCurrentIndex(0)
            self.assertEqual(dock.current_kind, page_kinds[0])
            self.assertIs(dock.stack.currentWidget(), page)
            dock.show_view(page_kinds[-1])
            dock.show_view(heading.lower())
            self.assertEqual(dock.current_kind, page_kinds[-1])
            self.plugin.open_layer_properties(records[page_kinds[1]]["layer_id"])
            self.assertIs(dock.stack.currentWidget(), page)
            self.assertEqual(page.current_kind(), page_kinds[1])
        next(action for action in menu.actions() if action.text() == 'Directivity').trigger()
        self.assertIs(dock.stack.currentWidget(), dock.directivity_page)
        self.assertEqual(dock.heading.text(), 'Directivity')

    def test_qgis_groups_and_double_click_survive_reload_and_keep_visibility(self):
        record = self.records[0]
        layer = self.plugin.layers[record["layer_id"]]
        root = QgsProject.instance().layerTreeRoot()
        node = root.findLayer(layer.id())
        self.assertEqual(node.parent().name(), "Sources")
        self.assertEqual(len(node.parent().findLayers()), 2)
        node.setItemVisibilityChecked(False)
        self.plugin.reload_layers()
        self.assertFalse(root.findLayer(layer.id()).itemVisibilityChecked())
        self.assertEqual(len([g for g in self.plugin.group.findGroups()
                              if g.customProperty("acalc/category") == "Sources"]), 1)
        self.tree.expandAll()
        self.tree.show()
        self.tree.raise_()
        self.plugin.dock.show_view("project")
        APP.processEvents()
        index = self.tree.node2index(root.findLayer(layer.id()))
        with patch.object(self.plugin, "open_layer_properties", wraps=self.plugin.open_layer_properties) as open_layer:
            QTest.mouseDClick(self.tree.viewport(), Qt.LeftButton, Qt.NoModifier,
                             self.tree.visualRect(index).center())
            open_layer.assert_called_once_with(record["layer_id"])
        self.assertIs(self.plugin.dock.current_layer, layer)

    def test_all_layer_types_share_category_groups_after_legacy_reload(self):
        for kind in LAYER_TYPES:
            self.store.add_layer(kind, kind)
        self.plugin.reload_layers()
        root = QgsProject.instance().layerTreeRoot()
        expected = {
            "Sources": {"point_source", "line_source", "area_source", "source_building"},
            "Receivers": {"point_receiver", "area_receiver", "measurement"},
            "Objects": {"building", "barrier", "ground_absorption", "trees"},
            "Terrain": {"terrain_point", "terrain_line"},
        }
        for record in self.store.layers():
            layer = self.plugin.layers[record["layer_id"]]
            category = next(name for name, kinds in expected.items() if record["feature_type"] in kinds)
            self.assertEqual(root.findLayer(layer.id()).parent().name(), category)
        layer = self.plugin.layers[self.records[0]["layer_id"]]
        legacy = self.plugin.group.addGroup("Point sources")
        legacy.setCustomProperty("acalc/layer_type", "point_source")
        self.plugin.place_layer(layer, legacy)
        legacy.setItemVisibilityChecked(False)
        self.plugin.reload_layers()
        self.assertEqual([g.name() for g in self.plugin.group.children()], list(expected))
        self.assertEqual(root.findLayer(layer.id()).parent().name(), "Sources")
        self.assertFalse(root.findLayer(layer.id()).itemVisibilityChecked())
        self.assertEqual(len(root.findLayers()), len(self.plugin.layers))

    def test_reload_reclassifies_loaded_building_without_losing_pending_edits_or_style(self):
        record = self.store.add_layer("building", "Factory")
        self.plugin.reload_layers()
        layer = self.plugin.layers[record["layer_id"]]
        feature = add_feature(layer, LAYER_TYPES["building"], "Pending name")
        geometry = feature.geometry().asWkb()
        symbol = layer.renderer().symbol()
        symbol.setOpacity(.4)
        with self.store.connection() as db:
            db.execute("UPDATE acalc_layers SET feature_type='source_building' WHERE layer_id=?",
                       (record["layer_id"],))
        self.plugin.reload_layers()
        self.assertIs(self.plugin.layers[record["layer_id"]], layer)
        self.assertEqual(layer.customProperty("acalc/feature_type"), "source_building")
        self.assertTrue(layer.isModified())
        self.assertEqual(layer.getFeature(feature.id())["name"], "Pending name")
        self.assertEqual(layer.getFeature(feature.id()).geometry().asWkb(), geometry)
        self.assertAlmostEqual(layer.renderer().symbol().opacity(), .4)
        self.assertEqual(QgsProject.instance().layerTreeRoot().findLayer(layer.id()).parent().name(), "Sources")
        self.plugin.dock.select_layer(layer)
        self.assertEqual(self.plugin.dock.current_kind, "source_building")
        self.assertIs(self.plugin.dock.stack.currentWidget(), self.plugin.dock.sources_page)

    def test_legacy_inclusion_column_is_hidden_in_native_forms_and_tables(self):
        layer = self.plugin.layers[self.records[0]["layer_id"]]
        field = layer.fields().indexFromName("enabled")
        self.assertEqual(layer.editorWidgetSetup(field).type(), "Hidden")
        self.assertTrue(next(c for c in layer.attributeTableConfig().columns() if c.name == "enabled").hidden)
        self.assertEqual(self.plugin.dock.features.columnCount(), 1)

    def test_saving_reloading_and_opening_properties_do_not_launch_engine(self):
        from qgis.PyQt.QtWidgets import QDialog
        with patch.object(self.plugin.engine_runner, "start") as start:
            layer = self.plugin.layers[self.records[2]["layer_id"]]
            add_feature(layer, LAYER_TYPES["terrain_line"])
            self.plugin.commit_layer(layer)
            self.plugin.reload_layers()
            with patch("acalc.plugin.FeatureDialog.exec", return_value=QDialog.Rejected):
                self.plugin.edit_feature(layer=layer, feature_id=next(layer.getFeatures()).id())
            QTest.qWait(400)
            start.assert_not_called()

    def test_legacy_raster_navigation_does_not_list_source_as_generated_terrain(self):
        from osgeo import gdal
        path = str(self.path.with_suffix(".tif"))
        ds = gdal.GetDriverByName("GTiff").Create(path, 2, 2, 1, gdal.GDT_Float64)
        ds.SetProjection(self.store.metadata()["crs_wkt"])
        ds.SetGeoTransform((0, 1, 0, 2, 0, -1))
        ds.GetRasterBand(1).Fill(5)
        ds = None
        raster = QgsRasterLayer(path, "Ground survey")
        self.assertTrue(raster.isValid())
        QgsProject.instance().addMapLayer(raster)
        ident = self.store.add_elevation_raster(path, raster.name())
        self.plugin.terrain_changed()
        node = QgsProject.instance().layerTreeRoot().findLayer(raster.id())
        self.assertEqual(node.parent().name(), "Terrain")
        dock = self.plugin.dock
        dock.show_view("terrain")
        self.plugin.open_raster_properties(ident)
        self.assertEqual(dock.terrain_page.current_view(), "rasters")
        self.assertIsNone(dock.terrain_page.rasters_page.selected_id())
        self.assertEqual(dock.terrain_page.rasters_page.terrains.count(), 0)
        self.tree.expandAll()
        self.tree.show()
        self.tree.raise_()
        dock.show_view("project")
        APP.processEvents()
        index = self.tree.node2index(node)
        QTest.mouseDClick(self.tree.viewport(), Qt.LeftButton, Qt.NoModifier,
                         self.tree.visualRect(index).center())
        self.assertIs(dock.stack.currentWidget(), dock.terrain_page)
        self.assertEqual(dock.terrain_page.current_view(), "rasters")
        self.assertIsNone(dock.terrain_page.rasters_page.selected_id())
        self.assertEqual(dock.terrain_page.rasters_page.terrains.count(), 0)
