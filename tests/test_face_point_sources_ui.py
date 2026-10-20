# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Mixed surface-source authoring, spectra, and face-on point manipulation."""

import json

from qgis.PyQt.QtCore import QEvent, QPointF, Qt
from qgis.PyQt.QtGui import QMouseEvent
from qgis.PyQt.QtTest import QTest
from qgis.PyQt.QtWidgets import QApplication, QDialog

from acalc.building_faces import faces_from_rows, read_face_sources
from acalc.face_sources_ui import FaceSourcesDialog
from acalc.schema import new_id
from acalc.spectra import spectrum_definition
from tests.test_face_sources import ROWS, rectangle
from tests.test_model import APP, ModelFixture


def point_source(**kwargs):
    return dict(id=new_id(), name='Fan', type='point', face='wall:0:1', vertex_count=4,
                x_m=2, y_m=3, distance_m=.5, spectrum_id=None) | kwargs


class FacePointSourceEditorTests(ModelFixture):
    def editor(self, sources=(), input_unit='m', face_id='wall:0:1'):
        editor = FaceSourcesDialog(None, face_id, faces_from_rows(ROWS, 1), 4,
                                   list(sources), self.store.spectra(), input_unit=input_unit)
        self.addCleanup(editor.close)
        editor.show()
        APP.processEvents()
        return editor

    def test_mixed_sources_offer_compatible_spectra_and_round_trip(self):
        total = self.store.add_spectrum('Fan total', spectrum_definition('sound_power', [70] * 8))
        density = self.store.add_spectrum('Panel density', spectrum_definition('sound_power', [75] * 8, power_basis='per_square_metre'))
        self.store.add_spectrum('Line density', spectrum_definition('sound_power', [75] * 8, power_basis='per_metre'))
        editor = self.editor()
        QTest.mouseClick(editor.add_button, Qt.LeftButton)
        QTest.mouseClick(editor.add_point_button, Qt.LeftButton)
        area_combo, point_combo = (editor.table.cellWidget(row, 5) for row in (0, 1))
        self.assertGreater(area_combo.findData(density), 0)
        self.assertEqual(point_combo.findData(density), -1)
        self.assertEqual(point_combo.count(), 2)
        self.assertFalse(editor.table.cellWidget(1, 3).isEnabled())
        self.assertFalse(editor.table.cellWidget(1, 4).isEnabled())
        self.assertFalse(editor.table.cellWidget(0, 6).isEnabled())
        # New points sit on the surface; choosing standoff enables the distance.
        self.assertEqual(editor.table.cellWidget(1, 7).currentData(), 'surface')
        self.assertFalse(editor.table.cellWidget(1, 6).isEnabled())
        editor.table.cellWidget(1, 7).setCurrentIndex(1)
        self.assertTrue(editor.table.cellWidget(1, 6).isEnabled())
        self.assertEqual(editor.table.cellWidget(1, 6).value(), .05)
        area_combo.setCurrentIndex(area_combo.findData(density))
        point_combo.setCurrentIndex(point_combo.findData(total))
        editor.table.cellWidget(1, 1).setValue(2)
        editor.table.cellWidget(1, 2).setValue(3)
        editor.table.cellWidget(1, 6).setValue(.75)
        self.assertIn('Total source sound power', editor.spectrum_status.text())
        editor.validate()
        self.assertEqual(editor.result(), QDialog.Accepted, editor.error.text())
        self.assertEqual(read_face_sources(json.dumps(editor.sources)), editor.sources)
        area, point = editor.sources
        self.assertNotIn('type', area)
        self.assertNotIn('distance_m', area)
        self.assertEqual(point['type'], 'point')
        self.assertEqual(point['mounting'], 'standoff')
        self.assertEqual(point['distance_m'], .75)
        self.assertNotIn('width_m', point)
        self.assertNotIn('height_m', point)
        reopened = self.editor(editor.sources)
        self.assertEqual([reopened.draft_source(row) for row in (0, 1)], editor.sources)

    def test_point_rejects_an_assigned_area_density_and_outside_anchor(self):
        density = self.store.add_spectrum('Panel density', spectrum_definition('sound_power', [75] * 8, power_basis='per_square_metre'))
        original = point_source(spectrum_id=density)
        editor = self.editor([original])
        self.assertEqual(editor.table.cellWidget(0, 5).currentText(), 'Unavailable spectrum')
        editor.validate()
        self.assertNotEqual(editor.result(), QDialog.Accepted)
        self.assertIn('total sound power', editor.error.text())
        editor.table.cellWidget(0, 5).setCurrentIndex(0)
        editor.table.cellWidget(0, 1).setValue(11)
        self.assertEqual(editor.face_preview.invalid_rows, {0})
        editor.validate()
        self.assertNotEqual(editor.result(), QDialog.Accepted)
        editor.table.cellWidget(0, 1).setValue(10)
        self.assertFalse(editor.face_preview.invalid_rows)
        editor.validate()
        self.assertEqual(editor.result(), QDialog.Accepted, editor.error.text())
        self.assertEqual(original['spectrum_id'], density)
        self.assertEqual(original['x_m'], 2)

    def test_point_marker_drags_over_area_and_escape_restores_only_position(self):
        area, point = rectangle(), point_source()
        editor = self.editor([area, point])
        view = editor.face_preview
        editor.select_source(0)
        point_display = next(source for source in view.sources if source['row'] == 1)
        self.assertEqual(view.source_at(view.face_to_screen(2, 3)), 1)
        self.assertEqual(view.handle_rects(point_display), {})
        self.assertFalse(view.grab().isNull())

        def drag(dx, dy, cancel=False):
            source = editor.draft_source(1)
            start = view.face_to_screen(source['x_m'], source['y_m'])
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

        drag(1.234, -.567)
        changed = editor.draft_source(1)
        self.assertEqual(editor.table.currentRow(), 1)
        self.assertEqual((changed['x_m'], changed['y_m'], changed['distance_m']), (3.23, 2.43, .5))
        self.assertEqual(editor.draft_source(0), area)
        drag(2, 2, cancel=True)
        self.assertEqual(editor.draft_source(1), changed)
        editor.table.cellWidget(1, 6).setFocus()
        APP.processEvents()
        self.assertEqual(view.selected_row, 1)
        self.assertIn('Drag the marker', editor.preview_status.text())
        QTest.mouseClick(editor.remove_button, Qt.LeftButton)
        self.assertEqual(len(view.sources), 1)
        self.assertEqual(editor.draft_source(0), area)
        self.assertEqual(point['x_m'], 2)

    def test_point_distance_uses_input_units_and_new_roof_sources_use_local_frame(self):
        editor = self.editor([point_source(distance_m=.6096)], input_unit='ft')
        distance = editor.table.cellWidget(0, 6)
        self.assertEqual(float(distance.text()), 2)
        self.assertAlmostEqual(distance.singleStep(), .01)
        distance.lineEdit().setText('3.00')
        distance.interpretText()
        self.assertEqual(float(distance.text()), 3)
        self.assertAlmostEqual(editor.draft_source(0)['distance_m'], .9144)
        self.assertAlmostEqual(distance.minimum() * .3048, .05)
        mounting = editor.table.cellWidget(0, 7)
        self.assertEqual(mounting.currentData(), 'standoff')
        mounting.setCurrentIndex(mounting.findData('surface'))
        self.assertFalse(distance.isEnabled())
        self.assertEqual(editor.draft_source(0)['distance_m'], 0)
        mounting.setCurrentIndex(mounting.findData('standoff'))
        self.assertAlmostEqual(editor.draft_source(0)['distance_m'], .9144)
        roof = self.editor(face_id='roof')
        QTest.mouseClick(roof.add_point_button, Qt.LeftButton)
        QTest.mouseClick(roof.add_button, Qt.LeftButton)
        self.assertEqual([roof.draft_source(row)['frame'] for row in (0, 1)], ['building_local'] * 2)
        roof.validate()
        self.assertEqual(roof.result(), QDialog.Accepted, roof.error.text())
