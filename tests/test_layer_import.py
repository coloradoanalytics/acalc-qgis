# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Import real external QGIS layers, then inspect persisted Acalc copies."""

import json
from unittest.mock import patch

from osgeo import ogr
from qgis.PyQt.QtCore import QVariant
from qgis.PyQt.QtWidgets import QDialog
from qgis.core import QgsCoordinateReferenceSystem, QgsFeature, QgsField, QgsGeometry, QgsProject, QgsVectorLayer

from acalc.layer_import import ImportOptions, import_layer
from acalc.layer_import_dialog import ImportLayerDialog
from acalc.layers import load_layer, validate_layer, vertex_rows
from acalc.plugin import AcalcPlugin
from acalc.schema import LAYER_TYPES, ModelError
from acalc.spectra import spectrum_definition
from acalc.storage import ModelStore
from tests.test_model import APP, FakeIface, ModelFixture


class LayerImportTests(ModelFixture):
    def source(self, geometry_type, wkts, crs='EPSG:26913'):
        layer = QgsVectorLayer(f'{geometry_type}?crs={crs}', 'External survey', 'memory')
        provider = layer.dataProvider()
        provider.addAttributes([QgsField('name', QVariant.String), QgsField('notes', QVariant.String),
                                QgsField('elev', QVariant.Double), QgsField('height', QVariant.Double),
                                QgsField('object_id', QVariant.String), QgsField('enabled', QVariant.Int)])
        layer.updateFields()
        for index, wkt in enumerate(wkts):
            feature = QgsFeature(layer.fields())
            feature.setGeometry(QgsGeometry.fromWkt(wkt))
            feature.setAttributes([f'Object {index}', 'Original notes', 123.25, 8.5, 'foreign-id', 0])
            self.assertTrue(provider.addFeatures([feature])[0])
        QgsProject.instance().addMapLayer(layer)
        return layer

    def test_removed_terrain_inference_import_mode_is_rejected(self):
        source = self.source('Point', ['POINT (500000 4400000)'])
        before = self.store.layers()
        with self.assertRaisesRegex(ModelError, 'ground elevation'):
            import_layer(self.store, source, ImportOptions('point_receiver', 'Invalid', ground_mode='terrain'))
        self.assertEqual(self.store.layers(), before)

    def test_all_object_types_round_trip_and_source_is_unchanged(self):
        wkts = {'Point': 'POINT (500000 4400000)',
                'LineString': 'LINESTRING (500000 4400000, 500010 4400010)',
                'Polygon': 'POLYGON ((500000 4400000, 500010 4400000, 500010 4400010, 500000 4400000))'}
        for kind in LAYER_TYPES.values():
            with self.subTest(kind=kind.key):
                source = self.source(kind.geometry, [wkts[kind.geometry]])
                original = next(source.getFeatures())
                before = (bytes(original.geometry().asWkb()), original.attributes(), source.fields().names())
                options = ImportOptions(kind.key, kind.title, name_field='name', notes_field='notes',
                                        ground_mode='field', ground_field='elev', height_field='height',
                                        ground_factor=0.6, spacing=12)
                record, count, objects = import_layer(self.store, source, options)
                self.assertEqual((count, objects), (1, 1))
                layer = load_layer(self.store, record)
                self.assertEqual(validate_layer(layer, kind), [])
                copied = next(layer.getFeatures())
                self.assertEqual(copied['name'], 'Object 0')
                self.assertEqual(copied['notes'], 'Original notes')
                self.assertNotEqual(copied['object_id'], 'foreign-id')
                self.assertEqual(copied['enabled'], 1)
                self.assertEqual(json.loads(copied['source_attributes'])['object_id'], 'foreign-id')
                self.assertEqual(copied['source_fid'], str(original.id()))
                for row in vertex_rows(copied.geometry(), kind):
                    self.assertEqual(row[3], 123.25 if kind.ground_points else 0)
                    self.assertEqual(row[4], 8.5 if kind.height else 0)
                if kind.key == 'area_receiver':
                    self.assertEqual(copied['height_m'], 8.5)
                    self.assertEqual(copied['spacing_m'], 12)
                    self.assertNotIn('infer_z', copied.fields().names())
                after = next(source.getFeatures())
                self.assertEqual((bytes(after.geometry().asWkb()), after.attributes(), source.fields().names()), before)
                self.assertFalse(source.isEditable())
                self.assertEqual(self.store.variants()[0]['definition']['layer_ids'], [])

    def test_crs_transform_matches_known_utm_origin_and_z_m_stay_explicit(self):
        source = self.source('PointZM', ['POINT ZM (-105 0 987 999)'], 'EPSG:4269')
        record, _, _ = import_layer(self.store, source, ImportOptions('point_receiver', 'Transformed', ground_mode='geometry_z', height=2))
        feature = next(load_layer(self.store, record).getFeatures())
        row = vertex_rows(feature.geometry(), LAYER_TYPES['point_receiver'])[0]
        self.assertAlmostEqual(row[1], 500000, places=5)
        self.assertAlmostEqual(row[2], 0, places=5)
        self.assertEqual(row[3:], (987, 2))

    def test_selected_multipart_and_pending_edits_have_unique_identities(self):
        source = self.source('MultiPoint', ['MULTIPOINT ((1 2), (3 4))', 'MULTIPOINT ((5 6))'])
        selected = next(source.getFeatures())
        source.startEditing()
        source.changeAttributeValue(selected.id(), source.fields().indexFromName('name'), 'Pending name')
        source.selectByIds([selected.id()])
        record, count, objects = import_layer(self.store, source, ImportOptions('point_source', 'Selected', selected_only=True, name_field='name'))
        copied = list(load_layer(self.store, record).getFeatures())
        self.assertEqual((count, objects), (1, 2))
        self.assertEqual({f['name'] for f in copied}, {'Pending name (part 1)', 'Pending name (part 2)'})
        self.assertEqual(len({f['object_id'] for f in copied}), 2)
        self.assertTrue(source.isModified())
        self.assertEqual(source.selectedFeatureIds(), [selected.id()])
        source.rollBack()
        self.assertEqual(source.getFeature(selected.id())['name'], 'Object 0')
        self.assertEqual(copied[0]['name'], 'Pending name (part 1)')

    def test_foot_crs_keeps_vertical_values_in_metres(self):
        crs = QgsCoordinateReferenceSystem('EPSG:2232')
        store = ModelStore.create(self.path.with_name('feet.gpkg'), 'Feet', crs.toWkt(), crs.authid())
        source = self.source('Point', ['POINT (3000000 1000000)'], crs.authid())
        record, _, _ = import_layer(store, source, ImportOptions('point_source', 'Feet source',
                                   ground_mode='field', ground_field='elev', height_field='height'))
        row = vertex_rows(next(load_layer(store, record).getFeatures()).geometry(), LAYER_TYPES['point_source'])[0]
        self.assertEqual(row, (0, 3000000, 1000000, 123.25, 8.5))

    def test_spectrum_is_explicit_and_must_match_source_type(self):
        total = self.store.add_spectrum('Total', spectrum_definition('sound_power', [80] * 8, power_basis='total'))
        density = self.store.add_spectrum('Per metre', spectrum_definition('sound_power', [70] * 8, power_basis='per_metre'))
        source = self.source('MultiPoint', ['MULTIPOINT ((1 2), (3 4))'])
        with self.assertRaisesRegex(ModelError, 'compatible sound power'):
            import_layer(self.store, source, ImportOptions('point_source', 'Invalid spectrum', spectrum_id=density))
        self.assertEqual(self.store.layers(), [])
        record, _, _ = import_layer(self.store, source, ImportOptions('point_source', 'Sources', spectrum_id=total))
        self.assertEqual([f['spectrum_id'] for f in load_layer(self.store, record).getFeatures()], [total, total])

    def test_multipart_lines_and_polygons_preserve_parts(self):
        examples = [('MultiLineStringZ', 'MULTILINESTRING Z ((1 2 3, 4 5 6), (7 8 9, 10 11 12))', 'terrain_line'),
                    ('MultiPolygon', 'MULTIPOLYGON (((0 0, 10 0, 10 10, 0 0)), ((20 0, 30 0, 30 10, 20 0)))', 'building')]
        for geometry, wkt, key in examples:
            source = self.source(geometry, [wkt])
            options = ImportOptions(key, 'Parts', ground_mode='geometry_z' if key == 'terrain_line' else 'constant')
            record, _, count = import_layer(self.store, source, options)
            self.assertEqual(count, 2)
            layer = load_layer(self.store, record)
            self.assertEqual(validate_layer(layer, LAYER_TYPES[key]), [])
            if key == 'terrain_line':
                self.assertEqual([[r[3] for r in vertex_rows(f.geometry(), LAYER_TYPES[key])] for f in layer.getFeatures()], [[3, 6], [9, 12]])
            else:
                self.assertTrue(all('infer_z' not in f.fields().names() for f in layer.getFeatures()))

    def tables(self):
        with self.store.connection(readonly=True) as db:
            return list(db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"))

    def test_bad_feature_does_not_leave_partial_layer(self):
        cases = [
            ('Polygon', ['POLYGON ((0 0, 10 0, 10 10, 0 0))',
                         'POLYGON ((0 0, 10 0, 10 10, 0 10, 0 0), (1 1, 2 1, 2 2, 1 1))'], 'building', {}, 'holes'),
            ('Polygon', ['POLYGON ((0 0, 10 10, 0 10, 10 0, 0 0))'], 'building', {}, 'invalid'),
            ('Point', ['POINT (1 2)'], 'point_source', {'ground_mode': 'geometry_z'}, 'finite'),
            ('Point', ['POINT (1 2)'], 'point_source', {'height': -1}, 'range'),
            ('Point', ['POINT EMPTY'], 'point_receiver', {}, 'empty'),
            ('CircularString', ['CIRCULARSTRING (0 0, 1 1, 2 0)'], 'barrier', {}, 'Curved'),
            ('Point', ['POINT (1 2)'], 'point_receiver', {'selected_only': True}, 'No features'),
            ('Polygon', ['POLYGON ((0 0, 10 0, 10 10, 0 0))'], 'ground_absorption', {'ground_factor': 2}, 'range'),
        ]
        before = [tuple(r) for r in self.tables()]
        for geometry, wkts, key, extra, message in cases:
            with self.subTest(message=message):
                source = self.source(geometry, wkts)
                with self.assertRaisesRegex(ModelError, message):
                    import_layer(self.store, source, ImportOptions(key, 'Rejected', **extra))
                self.assertEqual(self.store.layers(), [])
                self.assertEqual([tuple(r) for r in self.tables()], before)

    def test_null_mapped_number_and_missing_crs_fail(self):
        source = self.source('Point', ['POINT (1 2)'])
        feature = next(source.getFeatures())
        source.dataProvider().changeAttributeValues({feature.id(): {source.fields().indexFromName('height'): None}})
        with self.assertRaisesRegex(ModelError, 'Height.*number'):
            import_layer(self.store, source, ImportOptions('point_source', 'Bad height', height_field='height'))
        source.setCrs(QgsCoordinateReferenceSystem())
        with self.assertRaisesRegex(ModelError, 'coordinate reference'):
            import_layer(self.store, source, ImportOptions('point_source', 'Bad CRS'))

    def test_population_failure_removes_created_table(self):
        def fail(layer):
            feature = ogr.Feature(layer.GetLayerDefn())
            feature.SetGeometry(ogr.CreateGeometryFromWkt('POINT ZM (1 2 0 1)'))
            self.assertEqual(layer.CreateFeature(feature), 0)
            raise ModelError('Simulated write failure')
        with self.assertRaisesRegex(ModelError, 'Simulated'):
            self.store.add_layer('point_source', 'Failed', populate=fail)
        self.assertEqual(self.store.layers(), [])
        self.assertFalse(any(r[0].startswith(('model_', 'rtree_model_')) for r in self.tables()))
        with self.store.connection(readonly=True) as db:
            for table in ('gpkg_contents', 'gpkg_geometry_columns', 'gpkg_extensions'):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table} WHERE table_name LIKE 'model_%'").fetchone()[0], 0)

    def test_import_write_failure_rolls_back_and_can_retry(self):
        source = self.source('Point', ['POINT (1 2)', 'POINT (3 4)'])
        original = ogr.Layer.CreateFeature
        calls = []
        def fail_second(layer, feature):
            calls.append(1)
            return 1 if len(calls) == 2 else original(layer, feature)
        with patch.object(ogr.Layer, 'CreateFeature', fail_second):
            with self.assertRaisesRegex(ModelError, 'write an imported object'):
                import_layer(self.store, source, ImportOptions('point_receiver', 'Failed'))
        self.assertEqual(self.store.layers(), [])
        self.assertFalse(any(r[0].startswith(('model_', 'rtree_model_')) for r in self.tables()))
        record, _, count = import_layer(self.store, source, ImportOptions('point_receiver', 'Retry'))
        self.assertEqual(count, 2)
        self.assertEqual(load_layer(self.store, record).featureCount(), 2)

    def test_dialog_and_controller_import_and_cancel(self):
        source = self.source('Polygon', ['POLYGON ((0 0, 10 0, 10 10, 0 0))'])
        iface = FakeIface()
        plugin = AcalcPlugin(iface)
        plugin.initGui()
        try:
            plugin.open_project(self.path)
            iface.setActiveLayer(source)
            plugin.dock.show_view('building')
            dialog = ImportLayerDialog(plugin.dock, self.store, active_layer=source, kind_key='building')
            self.assertEqual(dialog.kind.currentData(), 'building')
            self.assertEqual(dialog.height.value.value(), 6)
            self.assertEqual({dialog.kind.itemData(i) for i in range(dialog.kind.count())},
                             {'building', 'source_building', 'area_source', 'area_receiver', 'ground_absorption', 'trees'})
            dialog.validate()
            self.assertEqual(dialog.result(), QDialog.Accepted)
            with patch.object(ImportLayerDialog, 'exec', return_value=QDialog.Rejected):
                plugin.import_layer()
            self.assertEqual(self.store.layers(), [])
            with patch.object(ImportLayerDialog, 'exec', return_value=QDialog.Accepted):
                plugin.dock.import_layer_button.trigger()
            self.assertEqual(len(self.store.layers()), 1)
            record = self.store.layers()[0]
            self.assertEqual(record['feature_type'], 'building')
            self.assertEqual(plugin.selected_layer().featureCount(), 1)
            self.assertEqual(iface.activeLayer(), plugin.selected_layer())
            self.assertIn('Copy saved', plugin.dock.status.text())
            plugin.calculation_busy_changed(True)
            self.assertFalse(plugin.dock.import_button.isEnabled())
            plugin.calculation_busy_changed(False)
            self.assertTrue(plugin.dock.import_button.isEnabled())
        finally:
            plugin.unload()
            iface.window.close()
            APP.processEvents()
