# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Recover missing legacy roof facets through the building draft interface."""

import unittest
from unittest.mock import patch

from qgis.PyQt.QtWidgets import QDialog

from acalc.building_faces import _triangulate, building_faces, source_polygon
from acalc.dialogs import FeatureDialog
from acalc.face_sources_ui import FaceSourcesDialog, UnattachedSourcesDialog
from acalc.schema import LAYER_TYPES
from tests.test_face_sources import rectangle
from tests.test_face_point_sources_ui import point_source
from tests.test_model import APP


class UnattachedSourceRecoveryTests(unittest.TestCase):
    rows = [(0,10,0,0,6), (0,10,8,0,9), (0,0,8,0,6), (0,0,0,0,6)]

    def editor(self, sources):
        dialog = FeatureDialog(None, LAYER_TYPES['source_building'], 'm')
        self.addCleanup(dialog.close)
        dialog.table.setRowCount(0)
        for row in self.rows:
            dialog.add_values(row)
        dialog.face_sources = list(sources)
        dialog.building_preview.set_sources(dialog.face_sources)
        dialog.update_building_preview()
        dialog.show()
        APP.processEvents()
        return dialog

    def legacy_source(self):
        base = tuple((x,y,z) for _,x,y,z,_ in self.rows)
        roof = tuple((x,y,z+h) for _,x,y,z,h in self.rows)
        perimeter, triangles = _triangulate(base)
        faces = building_faces(base+roof, perimeter, triangles)
        face = faces['roof:0:2:3']['top_edge']
        source = rectangle('roof:0:2:3', frame='roof_top_edge',
                           x_m=sum(p[0] for p in face['uv'])/3,
                           y_m=sum(p[1] for p in face['uv'])/3,
                           width_m=.1, height_m=.1)
        # This was valid under the previous canonical-start roof triangulation.
        self.assertEqual(len(source_polygon(source, faces, 4)), 4)
        return source

    def test_missing_legacy_facet_can_be_reassigned_then_edited(self):
        original = self.legacy_source()
        editor = self.editor([original])
        self.assertNotIn(original['face'], editor.building_preview.faces)
        self.assertTrue(editor.unattached_sources_button.isVisible())

        def reassign(dialog):
            target = dialog.table.cellWidget(0, 2)
            target.setCurrentIndex(target.findData('wall:0:1'))
            dialog.apply_changes()
            return dialog.result()

        with patch.object(UnattachedSourcesDialog, 'exec', reassign):
            editor.edit_unattached_sources()
        reassigned = editor.face_sources[0]
        self.assertEqual(reassigned['face'], 'wall:0:1')
        self.assertEqual(reassigned['vertex_count'], 4)
        self.assertNotIn('frame', reassigned)
        self.assertEqual(reassigned['id'], original['id'])
        self.assertEqual(reassigned['x_m'], original['x_m'])
        self.assertFalse(editor.unattached_sources_button.isVisible())
        self.assertEqual(editor.building_preview.selected_face, 'wall:0:1')
        self.assertIn('Wall 1–2', editor.preview_status.text())
        self.assertIn('must fit', editor.preview_status.text())

        def adjust(dialog):
            dialog.table.cellWidget(0, 1).setValue(1)
            dialog.table.cellWidget(0, 2).setValue(1)
            dialog.validate()
            return dialog.result()

        with patch.object(FaceSourcesDialog, 'exec', adjust):
            editor.edit_face_sources()
        editor.validate()
        self.assertEqual(editor.result(), QDialog.Accepted, editor.error.text())
        self.assertEqual(editor.face_sources[0]['x_m'], 1)
        self.assertEqual(original['face'], 'roof:0:2:3')
        self.assertEqual(original['frame'], 'roof_top_edge')

    def test_cancel_and_keep_unattached_preserve_draft_and_original(self):
        original = self.legacy_source()
        editor = self.editor([original])

        def cancel(dialog):
            self.assertIsNone(dialog.table.cellWidget(0, 2).currentData())
            target = dialog.table.cellWidget(0, 2)
            target.setCurrentIndex(target.findData('__remove__'))
            dialog.reject()
            return dialog.result()

        with patch.object(UnattachedSourcesDialog, 'exec', cancel):
            editor.edit_unattached_sources()
        self.assertEqual(editor.face_sources, [original])
        with patch.object(UnattachedSourcesDialog, 'exec', lambda dialog: dialog.apply_changes() or dialog.result()):
            editor.edit_unattached_sources()
        self.assertEqual(editor.face_sources, [original])
        editor.validate()
        self.assertNotEqual(editor.result(), QDialog.Accepted)
        self.assertIn('attached face changed', editor.error.text())

    def test_remove_orphan_preserves_other_sources_and_outer_cancel_is_draft_only(self):
        original = self.legacy_source()
        attached = point_source(distance_m=2)
        editor = self.editor([original, attached])

        def remove(dialog):
            self.assertEqual(dialog.table.rowCount(), 1)
            target = dialog.table.cellWidget(0, 2)
            target.setCurrentIndex(target.findData('__remove__'))
            dialog.apply_changes()
            return dialog.result()

        with patch.object(UnattachedSourcesDialog, 'exec', remove):
            editor.edit_unattached_sources()
        self.assertEqual(editor.face_sources, [attached])
        self.assertFalse(editor.unattached_sources_button.isVisible())
        editor.reject()
        self.assertEqual(editor.result(), QDialog.Rejected)
        self.assertIsNone(editor.geometry)
        reopened = self.editor([original, attached])
        self.assertEqual(reopened.face_sources, [original, attached])
        self.assertTrue(reopened.unattached_sources_button.isVisible())

    def test_count_mismatch_can_attach_to_roof_with_local_frame(self):
        original = point_source(vertex_count=5, distance_m=2)
        editor = self.editor([original])
        self.assertTrue(editor.unattached_sources_button.isVisible())
        destination = next(face for face in editor.building_preview.faces if face.startswith('roof'))

        def reassign(dialog):
            target = dialog.table.cellWidget(0, 2)
            target.setCurrentIndex(target.findData(destination))
            dialog.apply_changes()
            return dialog.result()

        with patch.object(UnattachedSourcesDialog, 'exec', reassign):
            editor.edit_unattached_sources()
        saved = editor.face_sources[0]
        self.assertEqual(saved['face'], destination)
        self.assertEqual(saved['frame'], 'building_local')
        self.assertEqual(saved['vertex_count'], 4)
        self.assertEqual(saved['distance_m'], 2)
        self.assertEqual(saved['type'], 'point')
        self.assertEqual(original['vertex_count'], 5)
