# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Object options, clipboard workflows, edit state and receiver-result navigation."""
import json
from unittest.mock import patch

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtTest import QTest
from qgis.PyQt.QtWidgets import QApplication, QDialog, QLineEdit

from acalc.calculation_settings import default_settings, ISO_ENGINE
from acalc.dialogs import FeatureDialog
from acalc.layers import vertex_rows, load_layer, validate_layer
from acalc.plugin import AcalcPlugin
from acalc.schema import LAYER_TYPES, ModelError, new_id, SCHEMA_VERSION
from acalc.spectra import compatible_power, spectrum_definition
from acalc.spectra_ui import SpectrumDialog
from acalc.storage import ModelStore
from tests.test_calculation_storage import downgrade_to_legacy
from tests.test_model import APP, ModelFixture, FakeIface, add_feature


class AuthoringTests(ModelFixture):
    def setUp(self):
        super().setUp()
        self.iface = FakeIface()
        self.plugin = AcalcPlugin(self.iface)
        self.plugin.initGui()
        self.plugin.open_project(self.path)

    def tearDown(self):
        for layer in self.plugin.layers.values():
            if layer.isEditable():
                layer.rollBack()
        self.plugin.unload()
        self.iface.window.close()
        APP.processEvents()
        super().tearDown()

    def building(self):
        dialog = FeatureDialog(None, LAYER_TYPES['building'], 'm')
        for r, (x,y,z) in enumerate(((0,0,10),(10,0,12),(0,10,14))):
            for c, value in enumerate((x,y,z,3+r)):
                dialog.table.item(r,c).setText(str(value))
        return dialog

    def test_building_relative_roof_modes_require_explicit_ground(self):
        for mode, roof in (('lowest',18),('highest',22),('average',20)):
            dialog=self.building()
            self.assertEqual(dialog.columns, ['X','Y','Ground Z (m)','Height (m)'])
            self.assertEqual(dialog.height_mode.count(),6)
            dialog.height_value.setValue(8)
            dialog.height_mode.setCurrentIndex(dialog.height_mode.findData(mode))
            self.assertFalse(dialog.table.item(0,3).flags() & Qt.ItemIsEditable)
            self.assertEqual(float(dialog.table.item(0,3).text()),roof)
            dialog.validate()
            self.assertEqual(dialog.result(), QDialog.Accepted, dialog.error.text())
            self.assertEqual([r[3]+r[4] for r in vertex_rows(dialog.geometry,dialog.kind)],[roof]*3)
            self.assertEqual(dialog.geometry.constGet().numInteriorRings(),0)
            dialog.height_mode.setCurrentIndex(dialog.height_mode.findData('individual'))
            self.assertEqual([dialog.table.item(r,3).text() for r in range(3)],['3','4','5'])
            dialog.close()
        dialog=self.building()
        dialog.height_mode.setCurrentIndex(dialog.height_mode.findData('lowest'))
        dialog.height_value.setValue(1)
        dialog.validate()
        self.assertEqual(dialog.result(),QDialog.Rejected)
        self.assertIn('below',dialog.error.text())
        self.assertEqual(dialog.ground_mode.findData('terrain'), -1)
        self.assertTrue(dialog.table.item(0,2).flags() & Qt.ItemIsEnabled)
        dialog.height_value.setValue(8)
        dialog.validate()
        self.assertEqual(dialog.result(),QDialog.Accepted)
        self.assertEqual(dialog.object_attributes(),{'ground_mode':'per_point','height_mode':'lowest','height_value':8,'reflection_coefficient':None,'absorption_spectrum_id':None})

    def test_all_objects_have_no_inclusion_control_and_share_ground_modes(self):
        for kind in LAYER_TYPES.values():
            dialog=FeatureDialog(None,kind,'m')
            self.assertFalse(hasattr(dialog, "enabled"))
            if not kind.ground_points:
                self.assertFalse(hasattr(dialog, "ground_mode"))
                dialog.close()
                continue
            self.assertEqual(dialog.ground_mode.findData('terrain'), -1)
            dialog.ground_mode.setCurrentIndex(dialog.ground_mode.findData('per_point'))
            self.assertTrue(dialog.table.item(0,2).flags() & Qt.ItemIsEditable)
            dialog.close()

    def test_object_options_persist_with_undo_and_save_buttons_track_dirty_state(self):
        record=self.store.add_layer('building','Buildings')
        self.plugin.reload_layers()
        self.plugin.dock.show_view('building')
        layer=self.plugin.layers[record['layer_id']]
        dock=self.plugin.dock
        self.assertEqual(dock.features.columnCount(),1)
        self.assertFalse(dock.save_edits_button.isEnabled())
        layer.startEditing()
        APP.processEvents()
        self.assertFalse(dock.save_edits_button.isEnabled())
        def accept(dialog):
            for r,(x,y) in enumerate(((0,0),(10,0),(0,10))):
                dialog.table.item(r,0).setText(str(x))
                dialog.table.item(r,1).setText(str(y))
            dialog.ground_mode.setCurrentIndex(dialog.ground_mode.findData('per_point'))
            dialog.height_mode.setCurrentIndex(dialog.height_mode.findData('average'))
            dialog.height_value.setValue(12)
            dialog.validate()
            return dialog.result()
        with patch.object(FeatureDialog,'exec',accept):
            self.plugin.edit_feature(True)
        self.assertTrue(dock.save_edits_button.isEnabled())
        self.assertTrue(dock.discard_edits_button.isEnabled())
        layer.undoStack().undo(); APP.processEvents()
        self.assertFalse(dock.save_edits_button.isEnabled())
        self.assertFalse(dock.discard_edits_button.isEnabled())
        layer.undoStack().redo(); APP.processEvents()
        self.assertTrue(dock.save_edits_button.isEnabled())
        self.plugin.save_layer()
        self.assertFalse(dock.save_edits_button.isEnabled())
        loaded=load_layer(ModelStore.open(self.path),record)
        feature=next(loaded.getFeatures())
        self.assertEqual((feature['enabled'],feature['height_mode'],feature['height_value']),(1,'average',12))
        self.assertEqual(feature['ground_mode'], 'per_point')

    def test_horizontal_spectrum_clipboard_and_tab_while_editing(self):
        dialog=SpectrumDialog(None,'sound_power')
        dialog.name.setText('Clipboard')
        dialog.show(); APP.processEvents()
        table=dialog.bands
        self.assertEqual((table.rowCount(),table.columnCount()),(2,8))
        table.setCurrentCell(0,0)
        QApplication.clipboard().setText('1\t2\t3\t4\t5\t6\t7\t8')
        QTest.keyClick(table,Qt.Key_V,Qt.ControlModifier)
        self.assertEqual([table.item(0,c).text() for c in range(8)],[str(i) for i in range(1,9)])
        table.setCurrentCell(0,0); table.editItem(table.item(0,0)); APP.processEvents()
        editor=QApplication.focusWidget()
        editor.selectAll(); QTest.keyClicks(editor,'25'); QTest.keyClick(editor,Qt.Key_Tab); APP.processEvents()
        self.assertEqual(table.item(0,0).text(),'25')
        self.assertEqual(table.currentColumn(),1)
        editor=QApplication.focusWidget()
        QApplication.clipboard().setText('12\t13\t14\t15\t16\t17\t18')
        QTest.keyClick(editor,Qt.Key_V,Qt.ControlModifier); APP.processEvents()
        self.assertEqual(table.item(0,7).text(),'18')
        table.setRangeSelected(__import__('qgis.PyQt.QtWidgets',fromlist=['QTableWidgetSelectionRange']).QTableWidgetSelectionRange(0,0,0,7),True)
        QTest.keyClick(table,Qt.Key_C,Qt.ControlModifier)
        self.assertEqual(QApplication.clipboard().text(),'25\t12\t13\t14\t15\t16\t17\t18')
        dialog.validate()
        self.assertEqual(dialog.result(),QDialog.Accepted)
        dialog.close()

    def test_power_compatibility_in_picker_validation_and_catalog_edits(self):
        ids={basis:self.store.add_spectrum(basis,spectrum_definition('sound_power',[1]*8,power_basis=basis))
             for basis in ('total','per_metre','per_square_metre')}
        for key,bases in (('point_source',{'total'}),('line_source',{'total','per_metre'}),('area_source',{'total','per_square_metre'})):
            dialog=FeatureDialog(None,LAYER_TYPES[key],'m',spectra=self.store.spectra())
            self.assertEqual(set(dialog.spectrum_records),{ids[b] for b in bases})
            dialog.close()
        record=self.store.add_layer('point_source','Point')
        layer=load_layer(self.store,record)
        f=add_feature(layer,LAYER_TYPES['point_source'])
        layer.changeAttributeValue(f.id(),layer.fields().indexFromName('spectrum_id'),ids['per_metre'])
        spectra={s['spectrum_id']:s for s in self.store.spectra()}
        self.assertIn('incompatible',validate_layer(layer,LAYER_TYPES['point_source'],spectra)[0])
        layer.changeAttributeValue(f.id(),layer.fields().indexFromName('spectrum_id'),ids['total'])
        self.assertTrue(layer.commitChanges())
        with self.assertRaisesRegex(ModelError,'incompatible'):
            self.store.save_spectrum(ids['total'],'total',spectrum_definition('sound_power',[1]*8,power_basis='per_metre'))

    def test_results_double_click_reuses_receiver_dialog_and_switches_variants(self):
        record=self.store.add_layer('point_receiver','Receivers')
        self.plugin.reload_layers()
        layer=self.plugin.layers[record['layer_id']]
        add_feature(layer,LAYER_TYPES['point_receiver'],'Receiver A')
        self.plugin.commit_layer(layer)
        feature=next(layer.getFeatures())
        configs=[self.store.variants()[0]['variant_id'],self.store.add_variant('Alternative')]
        run_ids=[]
        for index,config in enumerate(configs):
            run_id=new_id(); run_ids.append(run_id)
            project=self.store.metadata()
            with self.store.connection() as db:
                db.execute("INSERT INTO acalc_calculation_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,'complete',?,?,?,?,'[]','{}')",
                    (run_id,project['project_id'],config,'Config',ISO_ENGINE,'test','ISO 9613-2:2024','2026-09-18','2026-09-18','f'*64,project['modified_at'],json.dumps(default_settings()),1,1,0.1))
                db.execute("INSERT INTO acalc_receiver_results VALUES (?,?,?,?,?,?,?,'{}')",
                    (run_id,record['layer_id'],feature['object_id'],'Receiver A',json.dumps([40+index]*8),50+index,45+index))
        self.plugin.dock.show_view('results')
        page=self.plugin.dock.results_page
        page.refresh(configs[0])
        opened=[]
        def inspect(dialog):
            opened.append(dialog.name.text())
            self.assertTrue(dialog.isModal())
            self.assertIs(dialog.tabs.currentWidget(),dialog.receiver_results)
            results=dialog.receiver_results
            self.assertEqual(results.table.item(0,1).text(),'45.0')
            results.variant.setCurrentIndex(results.variant.findData(configs[1]))
            self.assertEqual(results.table.item(0,1).text(),'46.0')
            return QDialog.Rejected
        with patch.object(FeatureDialog,'exec',inspect):
            page.table.cellDoubleClicked.emit(0,0)
        self.assertEqual(opened,['Receiver A'])
        self.assertFalse(layer.isModified())

    def test_schema3_migration_keeps_ids_geometry_and_calculation_settings(self):
        from pathlib import Path
        from acalc.schema import definition
        path=Path(self.directory.name)/'old-model.gpkg'
        legacy=ModelStore.create(path,'Old model',self.store.metadata()['crs_wkt'])
        record=legacy.add_layer('building','Existing buildings')
        layer=load_layer(legacy,record)
        add_feature(layer,LAYER_TYPES['building'])
        self.assertTrue(layer.commitChanges())
        feature=next(layer.getFeatures())
        original=(feature['object_id'],bytes(feature.geometry().asWkb()))
        del feature,layer
        config=legacy.variants()[0]
        value=definition([record['layer_id']]);value['schema_version']=3
        with legacy.connection() as db:
            for field in ('height_mode','height_value','ground_mode'):
                db.execute(f'ALTER TABLE "{record["table_name"]}" DROP COLUMN {field}')
            db.execute('UPDATE acalc_project SET schema_version=3')
            db.execute('UPDATE acalc_variants SET definition_json=?',(json.dumps(value),))
            downgrade_to_legacy(db, 3)
        migrated=ModelStore.open(path)
        self.assertTrue(Path(migrated.migration_backup).is_file())
        self.assertEqual(migrated.metadata()['schema_version'],SCHEMA_VERSION)
        self.assertEqual(migrated.calculation()['settings'],default_settings())
        layer=load_layer(migrated,record); feature=next(layer.getFeatures())
        self.assertEqual((feature['object_id'],bytes(feature.geometry().asWkb())),original)
        self.assertEqual((feature['height_mode'],feature['height_value']),('individual',6))

    def test_reload_upgrades_live_building_edit_session_and_preserves_pending_work(self):
        from pathlib import Path
        from qgis.core import QgsFeature, QgsGeometry, QgsVectorLayer
        path = Path(self.directory.name) / 'live-legacy.gpkg'
        legacy = ModelStore.create(path, 'Live legacy', self.store.metadata()['crs_wkt'])
        record = legacy.add_layer('building', 'Buildings')
        with legacy.connection() as db:
            for field in ('height_mode', 'height_value', 'ground_mode'):
                db.execute(f'ALTER TABLE "{record["table_name"]}" DROP COLUMN {field}')
            db.execute('UPDATE acalc_project SET schema_version=3')
            for config in db.execute('SELECT variant_id,definition_json FROM acalc_variants').fetchall():
                definition = json.loads(config['definition_json'])
                definition['schema_version'] = 3
                db.execute('UPDATE acalc_variants SET definition_json=? WHERE variant_id=?',
                           (json.dumps(definition), config['variant_id']))
            downgrade_to_legacy(db, 3)
        layer = QgsVectorLayer(f'{path}|layername={record["table_name"]}', 'Buildings', 'ogr')
        self.assertTrue(layer.isValid())
        feature = QgsFeature(layer.fields())
        feature['object_id'], feature['name'], feature['enabled'] = new_id(), 'Saved building', 1
        feature.setGeometry(QgsGeometry.fromWkt('POLYGON ZM ((0 0 0 6,10 0 0 6,0 10 0 6,0 0 0 6))'))
        self.assertTrue(layer.dataProvider().addFeatures([feature])[0])
        self.plugin.project.addMapLayer(layer)
        saved = next(layer.getFeatures())
        layer.startEditing()
        layer.beginEditCommand('Rename building')
        layer.changeAttributeValue(saved.id(), layer.fields().indexFromName('name'), 'Pending rename')
        layer.endEditCommand()
        pending = QgsFeature(feature)
        pending['object_id'], pending['name'] = new_id(), 'Pending building'
        layer.beginEditCommand('Add building before upgrade')
        self.assertTrue(layer.addFeature(pending))
        layer.endEditCommand()
        undo_count = layer.undoStack().count()
        try:
            self.assertTrue(self.plugin.open_project(path))
            self.assertIs(self.plugin.layers[record['layer_id']], layer)
            self.assertTrue(layer.isEditable())
            self.assertTrue({'height_mode', 'height_value', 'ground_mode'} <= set(layer.fields().names()))
            self.assertEqual(layer.undoStack().count(), undo_count)
            self.assertEqual(layer.getFeature(saved.id())['name'], 'Pending rename')
            layer.undoStack().undo()
            self.assertEqual(layer.featureCount(), 1)
            layer.undoStack().redo()
            self.assertEqual(layer.featureCount(), 2)
            def edit_pending(dialog):
                dialog.height_mode.setCurrentIndex(dialog.height_mode.findData('highest'))
                dialog.height_value.setValue(11)
                dialog.validate()
                return dialog.result()
            with patch.object(FeatureDialog, 'exec', edit_pending):
                self.assertTrue(self.plugin.edit_feature(layer=layer, feature_id=pending.id()))
            edited = next(f for f in layer.getFeatures() if f['object_id'] == pending['object_id'])
            self.assertEqual((edited['height_mode'], edited['height_value']), ('highest', 11))
            self.assertEqual(edited['name'], 'Pending building')
            # Undo the property edit and its field initialization. This restores
            # the old queued feature, which the native save must still handle.
            layer.undoStack().undo()
            layer.undoStack().undo()
            self.assertEqual(layer.undoStack().index(), undo_count)
            # Saving immediately after reload must initialize old queued
            # additions even when no Acalc properties dialog has been opened.
            self.assertTrue(layer.commitChanges(), layer.commitErrors())
            self.assertEqual({f['object_id'] for f in layer.getFeatures()},
                             {saved['object_id'], pending['object_id']})
            self.plugin.dock.show_view('building')
            def accept(dialog):
                for row, (x, y) in enumerate(((20, 0), (30, 0), (20, 10))):
                    dialog.table.item(row, 0).setText(str(x))
                    dialog.table.item(row, 1).setText(str(y))
                dialog.name.setText('Building after upgrade')
                dialog.height_mode.setCurrentIndex(dialog.height_mode.findData('highest'))
                dialog.height_value.setValue(9)
                dialog.validate()
                return dialog.result()
            with patch.object(FeatureDialog, 'exec', accept):
                self.assertTrue(self.plugin.edit_feature(True))
            self.plugin.commit_layer(layer)
            loaded = load_layer(ModelStore.open(path), record)
            features = {f['name']: f for f in loaded.getFeatures()}
            self.assertEqual(set(features), {'Pending rename', 'Pending building', 'Building after upgrade'})
            for name in ('Pending rename', 'Pending building'):
                self.assertEqual((features[name]['height_mode'], features[name]['height_value']),
                                 ('individual', 6))
            self.assertEqual(features['Building after upgrade']['height_value'], 9)
            self.assertEqual(features['Building after upgrade']['ground_mode'], 'per_point')
            self.assertEqual(features['Pending building']['ground_mode'], 'per_point')
        finally:
            if layer.isEditable():
                layer.rollBack()
