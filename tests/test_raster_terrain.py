# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Raster authoring tests; synthetic terrain is not an acoustic reference case."""

import json
from pathlib import Path
import unittest
import os
import select
import subprocess
import time
from unittest.mock import patch

import numpy as np
from osgeo import gdal, ogr, osr
from qgis.PyQt.QtCore import QEvent, QPoint, Qt, QTimer
from qgis.PyQt.QtTest import QTest
from qgis.PyQt.QtWidgets import QDialog, QInputDialog, QMessageBox
from qgis.core import QgsCoordinateReferenceSystem, QgsPointXY, QgsProject, QgsRectangle
from qgis.gui import QgsMapToolPan

from acalc.api_model import prepare_calculation
from acalc.layers import load_layer, validate_layer
from acalc.plugin import AcalcPlugin
from acalc.raster_terrain import GenerationCancelled, generate, raster_candidates, save_terrain, simplify
from acalc.raster_terrain_dialog import RasterTerrainDialog
from acalc.schema import LAYER_TYPES, ModelError, definition, new_id
from acalc.spectra import spectrum_definition
from acalc.storage import ModelStore
from tests.test_api_model import CAPABILITIES
from tests.test_api_runner import wait_until
from tests.test_model import APP, FakeIface, ModelFixture


class RasterTerrainTests(ModelFixture):
    def raster(self, values=None, affine=(0, 1, 0, 21, 0, -1), epsg=26913, unit='m', scale=1, offset=0):
        values = np.zeros((21, 21)) if values is None else np.asarray(values, dtype=float)
        path = str(self.path.with_suffix('.tif'))
        ds = gdal.GetDriverByName('GTiff').Create(path, values.shape[1], values.shape[0], 1, gdal.GDT_Float64)
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(epsg)
        ds.SetProjection(srs.ExportToWkt())
        ds.SetGeoTransform(affine)
        band = ds.GetRasterBand(1)
        band.SetNoDataValue(-9999)
        band.SetUnitType(unit)
        band.SetScale(scale)
        band.SetOffset(offset)
        band.WriteArray(values)
        band = ds = None
        return {'path': path, 'name': 'Survey', 'band': 1}

    def generated(self, record, bounds=(.5, .5, 20.5, 20.5), tolerance=.1):
        return generate(record, self.store.metadata()['crs_wkt'], bounds, tolerance)

    def assert_surface(self, mesh, points, tolerance):
        # Independently evaluate the saved triangle planes at input samples.
        for x, y, z in points:
            found = False
            for ids in mesh.triangles:
                triangle = mesh.vertices[list(ids)]
                weights = np.linalg.solve(np.vstack((triangle[:, :2].T, np.ones(3))), [x, y, 1])
                if weights.min() >= -1e-9:
                    self.assertLessEqual(abs(weights @ triangle[:, 2] - z), tolerance + 1e-8)
                    found = True
                    break
            self.assertTrue(found, (x, y))

    def test_plane_needs_only_corners_and_units_are_applied(self):
        rows, cols = np.mgrid[:21, :21]
        record = self.raster(50 + cols * 3 + rows * 2, unit='ft', scale=2, offset=10)
        mesh = self.generated(record)
        self.assertEqual(len(mesh.vertices), 4)
        self.assertEqual(len(mesh.triangles), 2)
        self.assertAlmostEqual(mesh.max_error, 0)
        self.assertAlmostEqual(mesh.vertices[:, 2].min(), (50 * 2 + 10) * .3048)
        self.assertGreater(mesh.candidate_count, 400)

    def test_narrow_berm_is_retained_and_all_samples_meet_tolerance(self):
        values = np.zeros((21, 21))
        values[:, 10] = 4
        record = self.raster(values)
        mesh = self.generated(record)
        self.assertGreater(len(mesh.vertices), 4)
        self.assertEqual(mesh.vertices[:, 2].max(), 4)
        points, _ = raster_candidates(record, self.store.metadata()['crs_wkt'], (.5, .5, 20.5, 20.5))
        self.assert_surface(mesh, points, .1)
        second = self.generated(record)
        np.testing.assert_array_equal(second.vertices, mesh.vertices)
        self.assertEqual(second.triangles, mesh.triangles)
        self.assertLessEqual(mesh.max_error, .1)

    def test_curved_surface_and_subpixel_box(self):
        rows, cols = np.mgrid[:21, :21]
        record = self.raster(np.sin(cols / 2) * np.cos(rows / 3))
        bounds = (3.2, 5.7, 15.1, 18.6)
        mesh = self.generated(record, bounds, .15)
        points, _ = raster_candidates(record, self.store.metadata()['crs_wkt'], bounds)
        self.assert_surface(mesh, points, .15)
        self.assertEqual(tuple(mesh.vertices[:, :2].min(axis=0)), bounds[:2])
        self.assertEqual(tuple(mesh.vertices[:, :2].max(axis=0)), bounds[2:])
        tiny = self.generated(record, (10.1, 10.1, 10.2, 10.2), .1)
        self.assertGreaterEqual(len(tiny.vertices), 4)

    def test_large_integer_raster_does_not_create_collinear_sliver_triangles(self):
        rows, cols = np.mgrid[:501, :501]
        values = 1500 + 8 * np.sin(cols / 60) * np.cos(rows / 80)
        values[:, 250] += 3
        raster = self.raster(values, affine=(0, 1, 0, 501, 0, -1))
        mesh = self.generated(raster, (.5, .5, 500.5, 500.5), .25)
        self.assertGreater(mesh.candidate_count, 250000)
        self.assertLess(len(mesh.vertices), 5000)
        self.assertLessEqual(mesh.max_error, .25)
        triangles = mesh.vertices[np.array(mesh.triangles), :2]
        u, v = triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0]
        self.assertTrue(np.all(np.abs(u[:, 0] * v[:, 1] - u[:, 1] * v[:, 0]) > 0))

    def test_mesh_lines_preserve_exactly_the_triangle_edges(self):
        values = np.zeros((21, 21)); values[10, 10] = 5
        mesh = self.generated(self.raster(values))
        expected = {tuple(sorted((tuple(mesh.vertices[t[i]]), tuple(mesh.vertices[t[(i + 1) % 3]]))))
                    for t in mesh.triangles for i in range(3)}
        actual = set()
        for line in mesh.lines():
            self.assertLessEqual(len(line), 1000)
            for a, b in zip(line, line[1:]):
                self.assertFalse(np.array_equal(a[:2], b[:2]))
                actual.add(tuple(sorted((tuple(a), tuple(b)))))
        self.assertEqual(actual, expected)

    def test_missing_interior_boundary_and_outside_data_fail(self):
        values = np.zeros((21, 21)); values[10, 10] = -9999
        record = self.raster(values)
        with self.assertRaisesRegex(ModelError, 'NoData'):
            self.generated(record)
        with self.assertRaisesRegex(ModelError, 'NoData'):
            self.generated(record, (10.2, 10.2, 12, 12))
        with self.assertRaisesRegex(ModelError, 'outside'):
            self.generated(record, (-2, 2, 12, 12))
        self.assertEqual(self.store.layers(), [])

    def test_rotated_raster_and_geographic_raster(self):
        record = self.raster(np.full((21, 21), 123), affine=(0, 1, .2, 21, .15, -1))
        self.assert_surface(self.generated(record, (5, 6, 16, 17)), [(10, 10, 123)], .1)
        record = self.raster(np.full((21, 21), 456), affine=(-105.01, .001, 0, 40.01, 0, -.001), epsg=4326)
        self.assert_surface(self.generated(record, (499800, 4427200, 500200, 4427600)), [(500000, 4427400, 456)], .1)

    def test_cancellation_invalid_tolerance_and_resource_limit_do_not_publish(self):
        record = self.raster()
        with self.assertRaises(GenerationCancelled):
            generate(record, self.store.metadata()['crs_wkt'], (.5, .5, 20.5, 20.5), .1, lambda _: False)
        with self.assertRaises(ModelError):
            self.generated(record, tolerance=0)
        with self.assertRaisesRegex(ModelError, 'more than 4'):
            simplify(np.array([[0, 0, 0], [2, 0, 0], [2, 2, 0], [0, 2, 0], [1, 1, 5]]), (0, 0, 2, 2), .1, max_vertices=4)
        self.assertEqual(self.store.layers(), [])

    def test_saved_mesh_survives_missing_raster_and_variant_controls_transport(self):
        record = self.raster()
        mesh = self.generated(record)
        layer = save_terrain(self.store, 'Generated terrain', mesh)
        qlayer = load_layer(self.store, layer)
        self.assertEqual(validate_layer(qlayer, LAYER_TYPES['terrain_line']), [])
        saved = ModelStore.open(self.path).generated_terrains()
        self.assertEqual(saved[0]['generation'], mesh.metadata)
        self.assertEqual(saved[0]['layer_id'], layer['layer_id'])
        power = self.store.add_spectrum('Power', spectrum_definition('sound_power', [90] * 8))
        def feature(kind, xyz):
            def populate(target):
                f = ogr.Feature(target.GetLayerDefn())
                f.SetField('object_id', new_id()); f.SetField('name', kind)
                if kind == 'point_source':
                    f.SetField('spectrum_id', power)
                f.SetGeometry(ogr.CreateGeometryFromWkt(f'POINT ZM ({xyz[0]} {xyz[1]} 0 1.5)'))
                self.assertEqual(target.CreateFeature(f), 0)
            return self.store.add_layer(kind, kind, populate=populate)['layer_id']
        ids = [feature('point_source', (2, 2)), feature('point_receiver', (19, 19))]
        variant = self.store.variants()[0]
        self.store.save_variant(variant['variant_id'], variant['name'], definition(ids))
        empty = prepare_calculation(self.store, variant['variant_id'], CAPABILITIES)
        self.assertFalse(empty['request']['terrain']['breakline_definitions'])
        self.store.save_variant(variant['variant_id'], variant['name'], definition(ids + [layer['layer_id']]))
        before = prepare_calculation(self.store, variant['variant_id'], CAPABILITIES)
        self.assertTrue(before['request']['terrain']['breakline_definitions'])
        Path(record['path']).unlink()
        self.assertEqual(self.store.elevation_rasters(), [])
        after = prepare_calculation(ModelStore.open(self.path), variant['variant_id'], CAPABILITIES)
        self.assertEqual(before['request'], after['request'])
        with self.store.connection(readonly=True) as db:
            row = db.execute('SELECT * FROM acalc_raster_terrain').fetchone()
            self.assertIsNone(row['raster_id'])
            self.assertEqual(json.loads(row['generation_json']), mesh.metadata)

    def test_registration_failure_rolls_back_layer_and_provenance(self):
        record = self.raster()
        mesh = self.generated(record)
        def fail(*args, **kwargs):
            raise ModelError('registration failed')
        with patch.object(self.store, '_create_input_triggers', fail):
            with self.assertRaisesRegex(ModelError, 'registration failed'):
                save_terrain(self.store, 'Failed', mesh)
        self.assertEqual(self.store.layers(), [])
        self.assertEqual(self.store.generated_terrains(), [])
        with self.store.connection(readonly=True) as db:
            self.assertFalse(db.execute("SELECT 1 FROM gpkg_contents WHERE table_name LIKE 'model_%'").fetchall())

    def dialog(self, record):
        iface = FakeIface()
        self.addCleanup(iface.window.close)
        iface.canvas.setDestinationCrs(QgsCoordinateReferenceSystem('EPSG:26913'))
        iface.canvas.setExtent(QgsRectangle(0, 0, 21, 21))
        dialog = RasterTerrainDialog(None, self.store, iface.canvas, record)
        self.addCleanup(dialog.close)
        return dialog, iface

    def test_dialog_background_generation_and_new_import_starts_fresh(self):
        record = self.raster()
        dialog, _ = self.dialog(record)
        self.assertFalse(dialog.extent.isValid())
        with self.assertRaisesRegex(ModelError, 'Draw a terrain box'):
            dialog.bounds()
        dialog.extent.setOutputExtentFromUser(QgsRectangle(.5, .5, 20.5, 20.5), QgsCoordinateReferenceSystem('EPSG:26913'))
        dialog.start_generation()
        self.assertFalse(dialog.inputs.isEnabled())
        wait_until(lambda: dialog.worker is None)
        self.assertEqual(dialog.result(), QDialog.Accepted, dialog.status.text())
        save_terrain(self.store, dialog.name.text(), dialog.mesh)
        reopened, _ = self.dialog(record)
        self.assertFalse(reopened.extent.isValid())
        self.assertEqual(reopened.tolerance.value(), .5)
        self.assertEqual(reopened.name.text(), 'Survey terrain 2')

    def test_dialog_cancel_during_generation_writes_nothing(self):
        record = self.raster(np.zeros((501, 501)), affine=(0, 1, 0, 501, 0, -1))
        dialog, _ = self.dialog(record)
        dialog.extent.setOutputExtentFromUser(QgsRectangle(1, 1, 500, 500), QgsCoordinateReferenceSystem('EPSG:26913'))
        dialog.start_generation()
        dialog.reject()
        wait_until(lambda: dialog.worker is None)
        self.assertEqual(dialog.result(), QDialog.Rejected)
        self.assertIsNone(dialog.mesh)
        self.assertEqual(self.store.layers(), [])

    def test_draw_box_uses_real_map_tool_and_restores_pan(self):
        dialog, iface = self.dialog(self.raster())
        canvas = iface.canvas
        iface.window.show()
        pan = QgsMapToolPan(canvas)
        canvas.setMapTool(pan)
        dialog.show()
        APP.processEvents()
        dialog.draw_button.click()
        APP.processEvents()
        self.assertFalse(dialog.isVisible())
        transform = canvas.getCoordinateTransform()
        a, b = transform.transform(QgsPointXY(3, 18)), transform.transform(QgsPointXY(18, 3))
        start, end = QPoint(round(a.x()), round(a.y())), QPoint(round(b.x()), round(b.y()))
        QTest.mousePress(canvas.viewport(), Qt.LeftButton, Qt.NoModifier, start)
        QTest.mouseMove(canvas.viewport(), end)
        QTest.mouseRelease(canvas.viewport(), Qt.LeftButton, Qt.NoModifier, end)
        APP.processEvents()
        self.assertTrue(dialog.isVisible())
        self.assertIs(canvas.mapTool(), pan)
        for actual, expected in zip(dialog.bounds(), (3, 3, 18, 18)):
            self.assertAlmostEqual(actual, expected, delta=.1)
        canvas.unsetMapTool(pan)
        pan.deleteLater()

    def test_modal_controller_draws_box_then_generates_saved_terrain(self):
        raster = self.raster()
        iface = FakeIface()
        plugin = AcalcPlugin(iface)
        plugin.initGui()
        self.addCleanup(plugin.unload)
        plugin.open_project(self.path)
        iface.window.show()
        canvas = iface.canvas
        canvas.setDestinationCrs(QgsCoordinateReferenceSystem('EPSG:26913'))
        canvas.setExtent(QgsRectangle(0, 0, 21, 21))
        pan = QgsMapToolPan(canvas)
        canvas.setMapTool(pan)
        APP.processEvents()
        timer, timeout = QTimer(), QTimer()
        timeout.setSingleShot(True)
        errors, dialogs, stages = [], [], []

        def fail_timeout():
            errors.append('Modal drawing workflow timed out')
            if dialogs:
                dialogs[0].reject()

        def drive():
            try:
                if not stages:
                    dialog = APP.activeModalWidget()
                    self.assertIsInstance(dialog, RasterTerrainDialog)
                    dialogs.append(dialog)
                    dialog.draw_button.click()
                    stages.append('drawing')
                elif stages[-1] == 'drawing':
                    dialog = dialogs[0]
                    self.assertFalse(dialog.isVisible())
                    self.assertIsNone(APP.activeModalWidget())
                    transform = canvas.getCoordinateTransform()
                    a = transform.transform(QgsPointXY(3, 18))
                    b = transform.transform(QgsPointXY(18, 3))
                    start, end = QPoint(round(a.x()), round(a.y())), QPoint(round(b.x()), round(b.y()))
                    QTest.mousePress(canvas.viewport(), Qt.LeftButton, Qt.NoModifier, start)
                    QTest.mouseMove(canvas.viewport(), end)
                    QTest.mouseRelease(canvas.viewport(), Qt.LeftButton, Qt.NoModifier, end)
                    stages.append('drawn')
                elif stages[-1] == 'drawn':
                    dialog = dialogs[0]
                    self.assertTrue(dialog.isVisible())
                    self.assertIs(APP.activeModalWidget(), dialog)
                    self.assertIs(canvas.mapTool(), pan)
                    for actual, expected in zip(dialog.bounds(), (3, 3, 18, 18)):
                        self.assertAlmostEqual(actual, expected, delta=.1)
                    stages.append('generating')
                    dialog.start_generation()
            except Exception as error:
                errors.append(error)
                timer.stop()
                if dialogs:
                    dialogs[0].reject()

        timer.timeout.connect(drive)
        timeout.timeout.connect(fail_timeout)
        timer.start(20)
        timeout.start(5000)
        try:
            plugin.generate_raster_terrain(raster)
        finally:
            timer.stop()
            timeout.stop()
            if dialogs and not dialogs[0]._closing:
                dialogs[0].reject()
        self.assertEqual(errors, [])
        self.assertEqual(stages, ['drawing', 'drawn', 'generating'])
        self.assertEqual(len(self.store.layers()), 1)
        self.assertIs(canvas.mapTool(), pan)
        APP.sendPostedEvents(None, QEvent.DeferredDelete)
        canvas.unsetMapTool(pan)
        pan.deleteLater()

    def test_modal_dialog_remains_alive_until_cancelled_during_drawing(self):
        dialog, iface = self.dialog(self.raster())
        iface.window.show()
        pan = QgsMapToolPan(iface.canvas)
        iface.canvas.setMapTool(pan)
        timer = QTimer()
        stages = []

        def drive():
            if not stages:
                # Exercise the extent menu's entry point as well as our button.
                dialog.extent.setOutputExtentFromDrawOnCanvas()
                stages.append('drawing')
            else:
                stages.append('cancelled')
                dialog.reject()
                timer.stop()

        timer.timeout.connect(drive)
        timer.start(20)
        try:
            result = dialog.exec()
        finally:
            timer.stop()
        self.assertEqual(result, QDialog.Rejected)
        self.assertEqual(stages, ['drawing', 'cancelled'])
        self.assertIs(iface.canvas.mapTool(), pan)
        self.assertIsNone(APP.activeModalWidget())
        iface.canvas.unsetMapTool(pan)
        pan.deleteLater()

    @unittest.skipUnless(os.environ.get('ACALC_TERRAIN_ENGINE_BIN'), 'Set ACALC_TERRAIN_ENGINE_BIN for a real engine calculation')
    def test_generated_berm_runs_in_real_engine(self):
        binary = os.environ['ACALC_TERRAIN_ENGINE_BIN']
        capabilities = json.loads(subprocess.check_output([binary, '--capabilities']))
        values = np.zeros((21, 21)); values[:, 10] = 4
        raster = self.raster(values)
        mesh = self.generated(raster)
        layer = save_terrain(self.store, 'Berm', mesh)
        power = self.store.add_spectrum('Power', spectrum_definition('sound_power', [90] * 8))
        ids = [layer['layer_id']]
        for kind, x in [('point_source', 2), ('point_receiver', 19)]:
            def populate(target):
                f = ogr.Feature(target.GetLayerDefn())
                f.SetField('object_id', new_id()); f.SetField('name', kind)
                if kind == 'point_source':
                    f.SetField('spectrum_id', power)
                f.SetGeometry(ogr.CreateGeometryFromWkt(f'POINT ZM ({x} 10 0 1.5)'))
                self.assertEqual(target.CreateFeature(f), 0)
            ids.append(self.store.add_layer(kind, kind, populate=populate)['layer_id'])
        variant = self.store.variants()[0]
        self.store.save_variant(variant['variant_id'], variant['name'], definition(ids))
        request = prepare_calculation(self.store, variant['variant_id'], capabilities)['request']
        payload = json.dumps(request).encode()
        process = subprocess.Popen([binary, '--threads', '1'], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            process.stdin.write(json.dumps(dict(id=1, op='calculate', size=len(payload))).encode() + b'\n' + payload)
            process.stdin.flush()
            deadline = time.monotonic() + 20
            while True:
                remaining = deadline - time.monotonic()
                self.assertGreater(remaining, 0, 'Engine calculation timed out')
                self.assertTrue(select.select([process.stdout], [], [], remaining)[0], 'Engine calculation timed out')
                header = json.loads(process.stdout.readline())
                result = process.stdout.read(header.get('size', 0))
                self.assertNotIn('error', header, header)
                if 'ok' in header:
                    result = json.loads(result)
                    self.assertEqual(len(result['receivers']), 1)
                    self.assertTrue(all(np.isfinite(value) for value in result['receivers'][0]['octave_db']))
                    break
        finally:
            process.terminate()
            process.wait(timeout=5)
            process.stdin.close(); process.stdout.close()

    def test_plugin_registers_generated_layer_for_variants_and_rejects_model_change(self):
        raster = self.raster()
        mesh = self.generated(raster)
        iface = FakeIface()
        plugin = AcalcPlugin(iface)
        plugin.initGui()
        self.addCleanup(plugin.unload)
        plugin.open_project(self.path)
        def complete(dialog):
            dialog.mesh = mesh
            return QDialog.Accepted
        with patch.object(RasterTerrainDialog, 'exec', complete):
            plugin.generate_raster_terrain(raster)
        record = self.store.layers()[0]
        self.assertIn(record['layer_id'], plugin.layers)
        self.assertEqual(len(list(plugin.dock.member_items())), 1)
        self.assertEqual(plugin.dock.terrain_page.current_view(), 'rasters')
        self.assertTrue(plugin.dock.terrain_page.rasters_page.zoom_button.isEnabled())
        self.assertIs(iface.activeLayer(), plugin.layers[record['layer_id']])
        self.assertEqual(plugin.selected_record()['layer_id'], record['layer_id'])
        self.assertIsNone(plugin.dock.current_layer)  # Mesh edges never enter the line editor.
        original_store = plugin.store
        def changed(dialog):
            dialog.mesh = mesh
            plugin.store = None
            return QDialog.Accepted
        try:
            with patch.object(RasterTerrainDialog, 'exec', changed):
                with self.assertRaisesRegex(ModelError, 'model'):
                    plugin.generate_raster_terrain(raster)
        finally:
            plugin.store = original_store
        self.assertEqual(len(self.store.layers()), 1)

    def test_generated_entries_have_independent_controls_and_survive_source_deletion(self):
        raster = self.raster()
        first = save_terrain(self.store, 'West', self.generated(raster, (.5, .5, 10.5, 20.5)))
        second = save_terrain(self.store, 'East', self.generated(raster, (10.5, .5, 20.5, 20.5)))
        Path(raster['path']).unlink()
        iface = FakeIface()
        plugin = AcalcPlugin(iface)
        plugin.initGui()
        self.addCleanup(plugin.unload)
        plugin.open_project(self.path)
        page = plugin.dock.terrain_page.rasters_page
        page.refresh(second['layer_id'])
        self.assertEqual(page.layer_combo.count(), 2)
        self.assertEqual(page.terrains.count(), 1)
        self.assertEqual(page.selected_id(), second['layer_id'])
        self.assertTrue(page.zoom_button.isEnabled())
        self.assertTrue(page.rename_layer_action.isEnabled())
        self.assertTrue(page.remove_layer_action.isEnabled())
        with patch.object(QInputDialog, 'getText', return_value=('East slope', True)):
            page.rename_terrain()
        self.assertEqual(page.terrains.currentItem().text(), 'East slope')
        page.open_terrain()
        self.assertEqual(plugin.selected_record()['layer_id'], second['layer_id'])
        variant = self.store.variants()[0]
        self.store.save_variant(variant['variant_id'], variant['name'], definition([first['layer_id'], second['layer_id']]))
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.No):
            page.remove_terrain()
        self.assertEqual(page.layer_combo.count(), 2)
        self.assertEqual(page.terrains.count(), 1)
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.Yes):
            page.remove_terrain()
        self.assertEqual(page.terrains.count(), 1)
        self.assertEqual(page.selected_id(), first['layer_id'])
        self.assertEqual(self.store.variants()[0]['definition']['layer_ids'], [first['layer_id']])
        reopened = ModelStore.open(self.path)
        self.assertEqual(reopened.generated_terrains()[0]['layer_id'], first['layer_id'])
        self.assertEqual(reopened.generated_terrains(archived=True)[0]['name'], 'East slope')
        reopened.restore_layer(second['layer_id'])
        page.refresh(second['layer_id'])
        self.assertEqual(page.layer_combo.count(), 2)
        self.assertEqual(page.terrains.count(), 1)
        self.assertEqual(reopened.elevation_rasters(), [])

    def test_legacy_generation_is_listed_without_a_live_source(self):
        raster = self.raster()
        source_id = self.store.add_elevation_raster(raster['path'], raster['name'])
        record = save_terrain(self.store, 'Legacy terrain', self.generated(raster))
        with self.store.connection() as db:
            db.execute('UPDATE acalc_raster_terrain SET raster_id=? WHERE layer_id=?', (source_id, record['layer_id']))
        self.assertEqual(self.store.generated_terrains()[0]['layer_id'], record['layer_id'])
        self.store.remove_elevation_raster(source_id)
        self.assertEqual(self.store.generated_terrains()[0]['layer_id'], record['layer_id'])

    def test_mesh_navigation_and_layer_controls_are_separate_from_lines(self):
        from acalc.terrain_ui import RasterSelectionDialog
        raster = self.raster()
        mesh = self.generated(raster)
        line = self.store.add_layer('terrain_line', 'Authored contours')
        iface = FakeIface()
        plugin = AcalcPlugin(iface)
        plugin.initGui()
        self.addCleanup(plugin.unload)
        plugin.open_project(self.path)
        dock = plugin.dock
        page = dock.terrain_page.rasters_page
        plugin.open_layer_properties(line['layer_id'])
        self.assertEqual([action.text() for action in page.layer_menu.actions()],
                         ['New layer…', 'Rename layer…', 'Delete layer…'])
        def complete(dialog):
            dialog.mesh = mesh
            return QDialog.Accepted
        with patch.object(RasterSelectionDialog, 'exec', return_value=QDialog.Accepted), \
                patch.object(RasterSelectionDialog, 'selected_raster', return_value=raster), \
                patch.object(RasterTerrainDialog, 'exec', complete):
            page.new_layer_action.trigger()
            first_id = page.selected_id()
            page.new_layer_action.trigger()
            second_id = page.selected_id()
        self.assertNotEqual(first_id, second_id)
        self.assertEqual(plugin.feature_move_destinations(plugin.layers[line['layer_id']]), [])
        self.assertEqual(plugin.feature_move_destinations(plugin.layers[first_id]), [])
        self.assertEqual(page.layer_combo.count(), 2)
        self.assertEqual(page.terrains.count(), 1)
        self.assertEqual(page.terrains.item(0).data(Qt.UserRole), second_id)
        self.assertIs(iface.activeLayer(), plugin.layers[second_id])
        self.assertEqual(dock.terrain_page.current_view(), 'rasters')
        self.assertIsNone(dock.current_layer)
        dock.show_view('terrain_line')
        self.assertEqual([dock.layer_combo.itemData(i) for i in range(dock.layer_combo.count())], [line['layer_id']])
        self.assertEqual(dock.terrain_page.current_view(), 'lines')
        page.layer_combo.setCurrentIndex(page.layer_combo.findData(first_id))
        self.assertEqual(dock.terrain_page.current_view(), 'rasters')
        self.assertIs(iface.activeLayer(), plugin.layers[first_id])
        self.assertEqual(page.terrains.item(0).data(Qt.UserRole), first_id)
        plugin.open_layer_properties(line['layer_id'])
        plugin.open_layer_properties(second_id)
        self.assertEqual(dock.terrain_page.current_view(), 'rasters')
        with patch('acalc.plugin.FeatureDialog.exec') as editor:
            plugin.edit_feature(layer=plugin.layers[first_id], feature_id=next(plugin.layers[first_id].getFeatures()).id())
        editor.assert_not_called()
        self.assertEqual(page.selected_id(), first_id)
        dock.show_view('variants')
        item = next(item for item in dock.member_items() if item.data(0, Qt.UserRole) == second_id)
        self.assertEqual(item.parent().text(0), 'Generated terrain')
        dock.open_member_layer(item, 0)
        self.assertEqual(dock.terrain_page.current_view(), 'rasters')
        self.assertEqual(page.selected_id(), second_id)
        with patch.object(QInputDialog, 'getText', return_value=('Renamed mesh', True)):
            page.rename_layer_action.trigger()
        self.assertEqual(page.layer_combo.currentText(), 'Renamed mesh')
        self.assertEqual(page.terrains.item(0).text(), 'Renamed mesh')
        with patch.object(iface.canvas, 'zoomToFeatureExtent') as zoom:
            page.zoom_button.click()
        zoom.assert_called_once()
        self.assertEqual(dock.terrain_page.current_view(), 'rasters')
        plugin.reload_layers()
        iface.setActiveLayer(plugin.layers[first_id])
        self.assertEqual(dock.terrain_page.current_view(), 'rasters')
        self.assertEqual(page.selected_id(), first_id)
        dock.show_view('terrain_line')
        self.assertEqual([dock.layer_combo.itemData(i) for i in range(dock.layer_combo.count())], [line['layer_id']])
