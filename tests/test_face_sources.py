# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Face picking, rectangular geometry, editing and saved building attachments."""
import json
import math
from unittest.mock import patch

from qgis.PyQt.QtCore import QEvent, QPoint, QPointF, Qt
from qgis.PyQt.QtGui import QMouseEvent
from qgis.PyQt.QtTest import QTest
from qgis.PyQt.QtWidgets import QApplication, QDialog

from acalc.building_faces import read_face_sources, source_polygon
from acalc.building_preview import BuildingPreview
from acalc.dialogs import FeatureDialog
from acalc.face_sources_ui import FaceSourcesDialog
from acalc.layers import load_layer, validate_layer
from acalc.plugin import AcalcPlugin
from acalc.schema import LAYER_TYPES, ModelError, SCHEMA_VERSION, new_id
from acalc.spectra import spectrum_definition
from acalc.storage import ModelStore
from tests.test_model import APP, FakeIface, ModelFixture, add_feature

ROWS = [(0, 0, 0, 10, 6), (0, 10, 0, 10, 6), (0, 10, 8, 10, 6), (0, 0, 8, 10, 6)]


def rectangle(face='wall:0:1', **kwargs):
    return dict(id=new_id(), name='Vent', face=face, vertex_count=4, x_m=1, y_m=2,
                width_m=3, height_m=2, spectrum_id=None) | kwargs


class FaceSourceTests(ModelFixture):
    def preview(self, rows=ROWS):
        preview = BuildingPreview()
        self.addCleanup(preview.close)
        preview.set_building(rows)
        preview.resize(600, 500)
        preview.show()
        APP.processEvents()
        return preview

    def test_geometry_and_units_and_full_boundary_containment(self):
        p = self.preview()
        source = rectangle()
        self.assertEqual(source_polygon(source, p.faces, 4), ((1, 0, 4), (4, 0, 4), (4, 0, 2), (1, 0, 2)))
        self.assertEqual(source_polygon(rectangle('roof'), p.faces, 4), ((1, 6, 6), (4, 6, 6), (4, 4, 6), (1, 4, 6)))
        for bad in (rectangle(width_m=30), rectangle(height_m=8), rectangle(vertex_count=5)):
            with self.assertRaises(ModelError):
                source_polygon(bad, p.faces, 4)
        # Top-left corner on an inclined roof; true lengths remain 3 by 2 m.
        slope = [(r,x,y,z,6 + x/2) for r,x,y,z,h in ROWS]
        p.set_building(slope)
        polygon = source_polygon(rectangle('roof'), p.faces, 4)
        self.assertAlmostEqual(math.dist(polygon[0], polygon[1]), 3)
        self.assertAlmostEqual(math.dist(polygon[1], polygon[2]), 2)
        self.assertAlmostEqual(polygon[0][2], 6 + polygon[0][0]/2)
        # A U-shaped roof: all rectangle corners are on the roof but an edge
        # crosses the notch. Corner-only validation would silently accept it.
        u = [(0,x,y,0,6) for x,y in [(0,0),(6,0),(6,6),(4,6),(4,2),(2,2),(2,6),(0,6)]]
        p.set_building(u)
        with self.assertRaises(ModelError):
            source_polygon(rectangle('roof', vertex_count=8, x_m=1, y_m=1, width_m=4, height_m=4), p.faces, 8)

    def test_click_selects_visible_face_blank_deselects_and_drag_preserves(self):
        p = self.preview()
        camera, scale, origin = p._projection()
        # At the initial southeast camera the east wall is in front of west.
        east = p._project((10, 4, 3), camera, scale, origin)[0].toPoint()
        self.assertEqual(p.face_at(east), 'wall:1:2')
        original = p.grab().toImage()
        QTest.mouseClick(p, Qt.LeftButton, pos=east)
        APP.processEvents()
        self.assertEqual(p.selected_face, 'wall:1:2')
        self.assertNotEqual(p.grab().toImage(), original)
        QTest.mousePress(p, Qt.LeftButton, pos=east)
        QApplication.sendEvent(p, QMouseEvent(QEvent.MouseMove, east + QPoint(30, 20),
                                             Qt.NoButton, Qt.LeftButton, Qt.NoModifier))
        QTest.mouseRelease(p, Qt.LeftButton, pos=east + QPoint(30, 20))
        self.assertEqual(p.selected_face, 'wall:1:2')
        p.reset_view()
        QTest.mouseClick(p, Qt.LeftButton, pos=QPoint(5, 5))
        self.assertIsNone(p.selected_face)
        p.set_sources([rectangle()])
        with_sources = p.grab().toImage()
        p.set_sources([])
        self.assertNotEqual(p.grab().toImage(), with_sources)
        # Sources on a fully occluded back wall must not leak through.
        baseline = p.grab().toImage()
        p.set_sources([rectangle('wall:2:3')])
        self.assertEqual(p.grab().toImage(), baseline)

    def test_editor_creates_multiple_sources_assigns_power_and_rejects_overflow(self):
        p = self.preview()
        power = self.store.add_spectrum('Panel', spectrum_definition('sound_power', [75]*8, power_basis='per_square_metre'))
        self.store.add_spectrum('Line only', spectrum_definition('sound_power', [75]*8, power_basis='per_metre'))
        self.store.add_spectrum('Absorption', spectrum_definition('absorption', [0.5]*8))
        editor = FaceSourcesDialog(None, 'wall:0:1', p.faces, 4, [], self.store.spectra())
        self.addCleanup(editor.close)
        QTest.mouseClick(editor.add_button, Qt.LeftButton)
        self.assertEqual(editor.table.rowCount(), 1)
        self.assertEqual(editor.table.cellWidget(0, 5).count(), 2)
        editor.table.cellWidget(0, 5).setCurrentIndex(1)
        editor.table.cellWidget(0, 1).setValue(2)
        editor.table.cellWidget(0, 3).setValue(30)
        editor.validate()
        self.assertNotEqual(editor.result(), QDialog.Accepted)
        self.assertIn('fit entirely', editor.error.text())
        editor.table.cellWidget(0, 3).setValue(3)
        editor.add_source(rectangle(name='Grille', y_m=3))
        editor.validate()
        self.assertEqual(editor.result(), QDialog.Accepted)
        self.assertEqual(len(editor.sources), 2)
        self.assertEqual(editor.sources[0]['spectrum_id'], power)
        self.assertEqual(editor.sources[0]['x_m'], 2)
        self.assertEqual(editor.spectrum_preview.item(0, 0).text(), '')
        read_face_sources(json.dumps(editor.sources))

    def test_body_drag_and_resize_snap_and_cancel_without_changing_other_sources(self):
        p = self.preview()
        original = rectangle(x_m=1, y_m=2, width_m=3, height_m=2)
        other = rectangle(name='Other', x_m=7, y_m=1, width_m=1, height_m=1)
        editor = FaceSourcesDialog(None, 'wall:0:1', p.faces, 4, [original, other], [])
        self.addCleanup(editor.close)
        editor.show()
        editor.select_source(1)
        APP.processEvents()
        view = editor.face_preview

        def drag(mode, dx, dy, cancel=False):
            source = next(s for s in view.sources if s['row'] == 0)
            start = (view.source_rect(source).center() if mode == 'move'
                     else view.handle_rects(source)['resize'].center())
            scale = view._transform()[1]
            end = start + QPointF(dx * scale, dy * scale)
            QApplication.sendEvent(view, QMouseEvent(QEvent.MouseButtonPress, start,
                                                     Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
            QApplication.sendEvent(view, QMouseEvent(QEvent.MouseMove, end,
                                                     Qt.NoButton, Qt.LeftButton, Qt.NoModifier))
            if cancel:
                QTest.keyClick(view, Qt.Key_Escape)
            QApplication.sendEvent(view, QMouseEvent(QEvent.MouseButtonRelease, end,
                                                     Qt.LeftButton, Qt.NoButton, Qt.NoModifier))
            APP.processEvents()

        drag('move', 1.234, -0.567)
        self.assertEqual(editor.table.currentRow(), 0)
        source = editor.draft_source(0)
        self.assertEqual([source[k] for k in ('x_m','y_m','width_m','height_m')], [2.23,1.43,3,2])
        drag('resize', 0.456, 0.789)
        source = editor.draft_source(0)
        self.assertEqual([source[k] for k in ('x_m','y_m','width_m','height_m')], [2.23,1.43,3.46,2.79])
        self.assertEqual(editor.draft_source(1), other)
        before = editor.draft_source(0)
        drag('move', 1, 1, cancel=True)
        self.assertEqual(editor.draft_source(0), before)
        drag('resize', -10, -10)
        self.assertEqual((editor.draft_source(0)['width_m'], editor.draft_source(0)['height_m']), (0.01,0.01))
        # The resize hit target must not swallow a tiny source's entire body.
        drag('move', 0.12, 0.23)
        self.assertEqual((editor.draft_source(0)['x_m'], editor.draft_source(0)['y_m']), (2.35,1.66))
        self.assertEqual((editor.draft_source(0)['width_m'], editor.draft_source(0)['height_m']), (0.01,0.01))
        drag('resize', 0.99, 0.99)
        self.assertEqual((editor.draft_source(0)['width_m'], editor.draft_source(0)['height_m']), (1,1))
        drag('move', -20, -20)
        self.assertEqual((editor.draft_source(0)['x_m'], editor.draft_source(0)['y_m']), (0,0))
        editor.validate()
        self.assertEqual(editor.result(), QDialog.Accepted, editor.error.text())
        self.assertEqual(editor.sources[0]['width_m'], 1)
        self.assertEqual(original['x_m'], 1)

    def test_centimetre_fields_tab_select_all_and_blank_unassigned_octaves(self):
        p = self.preview()
        unknown = self.store.add_spectrum('Incomplete', spectrum_definition('sound_power', [None]*8))
        source = rectangle(x_m=1.234, y_m=2.346, width_m=3.456, height_m=1.237)
        editor = FaceSourcesDialog(None, 'wall:0:1', p.faces, 4, [source], self.store.spectra())
        self.addCleanup(editor.close)
        editor.show()
        APP.processEvents()
        fields = [editor.table.cellWidget(0, column) for column in range(1,5)]
        self.assertEqual([f.text() for f in fields], ['1.23','2.35','3.46','1.24'])
        self.assertTrue(all(f.singleStep() == 0.01 for f in fields))
        fields[0].setFocus()
        for previous, target in zip(fields, fields[1:]):
            QTest.keyClick(previous, Qt.Key_Tab)
            APP.processEvents()
            self.assertTrue(target.hasFocus())
            self.assertEqual(target.lineEdit().selectedText(), target.text())
        QTest.keyClick(fields[3], Qt.Key_Backtab)
        APP.processEvents()
        self.assertTrue(fields[2].hasFocus())
        self.assertEqual(fields[2].lineEdit().selectedText(), '3.456')
        QTest.keyClicks(fields[2], '2.50')
        QTest.keyClick(fields[2], Qt.Key_Tab)
        APP.processEvents()
        self.assertEqual(fields[2].value(), 2.5)
        self.assertEqual([editor.spectrum_preview.item(0,i).text() for i in range(8)], ['']*8)
        combo = editor.table.cellWidget(0,5)
        combo.setCurrentIndex(combo.findData(unknown))
        self.assertEqual([editor.spectrum_preview.item(0,i).text() for i in range(8)], ['Unknown']*8)
        combo.setCurrentIndex(0)
        self.assertEqual([editor.spectrum_preview.item(0,i).text() for i in range(8)], ['']*8)
        editor.reject()
        self.assertEqual(source['x_m'], 1.234)

    def test_graphical_editor_selection_coordinates_focus_and_removal_stay_in_sync(self):
        p = self.preview()
        first = rectangle(name='Vent A', x_m=1, y_m=1, width_m=2, height_m=2)
        second = rectangle(name='Vent B', x_m=6, y_m=2, width_m=2, height_m=2)
        editor = FaceSourcesDialog(None, 'wall:0:1', p.faces, 4, [first, second], [])
        self.addCleanup(editor.close)
        editor.show()
        APP.processEvents()
        view = editor.face_preview
        self.assertEqual(view.selected_row, 1)
        self.assertEqual(len(view.sources), 2)
        first_position = view.face_to_screen(2, 2).toPoint()
        image = view.grab().toImage()
        # Inactive objects remain visible, while the active one has a distinct fill.
        self.assertNotEqual(image.pixelColor(view.face_to_screen(1.3, 1.3).toPoint()),
                            image.pixelColor(view.face_to_screen(6.3, 2.3).toPoint()))
        QTest.mouseClick(view, Qt.LeftButton, pos=first_position)
        self.assertEqual(editor.table.currentRow(), 0)
        self.assertEqual(view.selected_row, 0)
        self.assertIn('Vent A', editor.preview_status.text())
        editor.table.cellWidget(0, 1).setValue(3)
        self.assertIsNone(view.source_at(first_position))
        self.assertEqual(view.source_at(view.face_to_screen(4, 2)), 0)
        editor.table.item(0, 0).setText('Renamed vent')
        self.assertIn('Renamed vent', editor.preview_status.text())
        # Clicking into a coordinate widget must activate that source too.
        editor.table.cellWidget(1, 2).setFocus()
        APP.processEvents()
        self.assertEqual(view.selected_row, 1)
        editor.table.cellWidget(1, 3).setValue(8)
        self.assertEqual(view.invalid_rows, {1})
        self.assertIn('fit entirely', editor.preview_status.text())
        editor.table.cellWidget(1, 3).setValue(2)
        self.assertFalse(view.invalid_rows)
        QTest.mouseClick(editor.remove_button, Qt.LeftButton)
        self.assertEqual(len(view.sources), 1)
        self.assertIsNone(view.source_at(view.face_to_screen(7, 3)))
        # Draft edits and selection must not mutate the caller's source records.
        editor.reject()
        self.assertEqual(first['x_m'], 1)
        self.assertEqual(first['name'], 'Vent A')
        self.assertEqual(second['width_m'], 2)

    def test_graphical_editor_uses_sloped_face_and_preserves_metre_aspect_ratio(self):
        p = self.preview([(0,0,0,10,6), (0,10,0,10,9), (0,10,8,10,6), (0,0,8,10,6)])
        editor = FaceSourcesDialog(None, 'wall:0:1', p.faces, 4, [], [])
        self.addCleanup(editor.close)
        editor.show()
        APP.processEvents()
        view = editor.face_preview
        self.assertEqual(view.face['uv'], ((0,6), (10,6), (10,-3), (0,0)))
        origin = view.face_to_screen(0, 0)
        x = view.face_to_screen(1, 0)
        y = view.face_to_screen(0, 1)
        self.assertAlmostEqual(x.x() - origin.x(), y.y() - origin.y())
        self.assertGreater(origin.y(), view.face_to_screen(10, -3).y())
        self.assertFalse(view.grab().isNull())
        self.assertEqual(view.sources, [])

    def test_parent_apply_undo_cancel_reopen_and_spectrum_reference_guards(self):
        record = self.store.add_layer('source_building', 'Building')
        power = self.store.add_spectrum('Vent power', spectrum_definition('sound_power', [70]*8))
        iface = FakeIface()
        plugin = AcalcPlugin(iface)
        plugin.initGui()
        self.addCleanup(iface.window.close)
        self.addCleanup(plugin.unload)
        plugin.open_project(self.path)
        layer = plugin.layers[record['layer_id']]
        source = rectangle(spectrum_id=power)

        def create(dialog):
            dialog.table.setRowCount(0)
            for row in ROWS:
                dialog.add_values(row)
            dialog.show()
            APP.processEvents()
            self.assertFalse(dialog.face_sources_button.isVisible())
            dialog.building_preview.set_selected_face(source['face'])
            self.assertTrue(dialog.face_sources_button.isVisible())
            def add(editor):
                editor.add_source(source)
                editor.validate()
                return editor.result()
            with patch.object(FaceSourcesDialog, 'exec', add):
                dialog.edit_face_sources()
            self.assertEqual(len(dialog.building_preview.source_polygons), 1)
            self.assertTrue(all(not b.isEnabled() for b in dialog.vertex_buttons))
            dialog.validate()
            self.assertEqual(dialog.result(), QDialog.Accepted, dialog.error.text())
            return dialog.result()

        with patch.object(FeatureDialog, 'exec', create):
            plugin.edit_feature(create=True, layer=layer)
        feature = next(layer.getFeatures())
        self.assertEqual(read_face_sources(feature['face_sources']), [source])
        self.assertEqual(plugin.validation_errors(require_spectra=True), [])
        layer.undoStack().undo()
        self.assertEqual(layer.featureCount(), 0)
        layer.undoStack().redo()
        plugin.commit_layer(layer)
        self.assertEqual(self.store.spectrum_usage(power)[0]['count'], 1)
        with self.assertRaises(ModelError):
            self.store.delete_spectrum(power)
        with self.assertRaises(ModelError):
            self.store.save_spectrum(power, 'Vent power', spectrum_definition('sound_power', [70]*8, power_basis='per_metre'))
        def cancel(dialog):
            dialog.face_sources = []
            dialog.reject()
            return dialog.result()
        with patch.object(FeatureDialog, 'exec', cancel):
            plugin.edit_feature(layer=layer, feature_id=next(layer.getFeatures()).id())
        reopened = load_layer(self.store, record)
        self.assertEqual(read_face_sources(next(reopened.getFeatures())['face_sources']), [source])
        reopened.startEditing()
        feature = next(reopened.getFeatures())
        bad = source | dict(width_m=100)
        reopened.changeAttributeValue(feature.id(), reopened.fields().indexFromName('face_sources'), json.dumps([bad]))
        self.assertIn('fit entirely', validate_layer(reopened, LAYER_TYPES['source_building'])[0])
        reopened.rollBack()

    def test_schema_10_migration_keeps_geometry_and_adds_empty_sources(self):
        record = self.store.add_layer('building', 'Legacy building')
        layer = load_layer(self.store, record)
        add_feature(layer, LAYER_TYPES['building'], 'Legacy')
        self.assertTrue(layer.commitChanges())
        del layer
        with self.store.connection() as db:
            geometry = db.execute(f'SELECT geom FROM "{record["table_name"]}"').fetchone()[0]
            db.execute(f'ALTER TABLE "{record["table_name"]}" DROP COLUMN face_sources')
            db.execute(f'ALTER TABLE "{record["table_name"]}" ADD COLUMN infer_z INTEGER NOT NULL DEFAULT 0')
            db.execute('UPDATE acalc_project SET schema_version=10')
            for row in db.execute('SELECT variant_id,definition_json FROM acalc_variants').fetchall():
                value = json.loads(row['definition_json']); value['schema_version'] = 10
                db.execute('UPDATE acalc_variants SET definition_json=? WHERE variant_id=?', (json.dumps(value), row['variant_id']))
        store = ModelStore.open(self.path)
        self.assertEqual(store.metadata()['schema_version'], SCHEMA_VERSION)
        self.assertTrue(store.migration_backup)
        with store.connection() as db:
            row = db.execute(f'SELECT geom,face_sources FROM "{record["table_name"]}"').fetchone()
            self.assertEqual(row[0], geometry)
            self.assertIsNone(row[1])
