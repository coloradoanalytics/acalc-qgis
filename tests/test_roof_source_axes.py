# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Building-aligned roof frames, legacy placement, and real source editing."""
import math

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtTest import QTest
from qgis.PyQt.QtWidgets import QDialog

from acalc.building_faces import faces_from_rows, read_face_sources, source_polygon
from acalc.face_sources_ui import FaceSourcesDialog
from acalc.schema import ModelError
from tests.test_face_sources import rectangle
from tests.test_model import APP, ModelFixture


class RoofSourceAxesTests(ModelFixture):
    def rows(self, slope=False):
        # 3:4:5 rotation of a 10 x 8 m building, with a large map offset.
        return [(0, 500000 + .8*x - .6*y, 4000000 + .6*x + .8*y, 10,
                 6 + (.2*x if slope else 0)) for x, y in ((0, 0), (10, 0), (10, 8), (0, 8))]

    def assert_vector(self, actual, expected):
        for a, b in zip(actual, expected):
            self.assertAlmostEqual(a, b, delta=1e-8)

    def test_rotated_flat_and_sloped_roof_uses_actual_top_edge(self):
        for slope in (False, True):
            rows = self.rows(slope)
            faces = faces_from_rows(rows, 1)
            face = faces['roof']['top_edge']
            length = math.sqrt(1.04) if slope else 1
            self.assert_vector(face['right'], (.8/length, .6/length, .2/length if slope else 0))
            self.assert_vector(face['down'], (.6, -.8, 0))
            source = rectangle('roof', frame='roof_top_edge')
            polygon = source_polygon(source, faces, 4)
            # Origin (-4.8, 6.4, 6), then X=1, Y=2 along independent axes.
            self.assert_vector(polygon[0], (-3.6 + .8/length, 4.8 + .6/length,
                                            6 + (.2/length if slope else 0)))
            self.assertAlmostEqual(math.dist(polygon[0], polygon[1]), 3)
            self.assertAlmostEqual(math.dist(polygon[1], polygon[2]), 2)
            for winding in (rows, list(reversed(rows))):
                for start in range(4):
                    reordered = winding[start:] + winding[:start]
                    other = faces_from_rows(reordered, 1)['roof']['top_edge']
                    self.assert_vector(other['right'], face['right'])
                    self.assert_vector(other['down'], face['down'])

    def test_legacy_world_aligned_sources_keep_their_geometry(self):
        faces = faces_from_rows(self.rows(), 1)
        source = rectangle('roof', x_m=6, y_m=3, width_m=1, height_m=1)
        polygon = source_polygon(source, faces, 4)
        self.assert_vector(polygon[0], (1.2, 9.4, 6))
        self.assert_vector(polygon[1], (2.2, 9.4, 6))
        self.assert_vector(polygon[2], (2.2, 8.4, 6))
        self.assertNotIn('frame', read_face_sources([source])[0])

    def test_facets_and_concave_roofs_have_an_edge_parallel_to_x(self):
        rows = self.rows()
        r, x, y, z, h = rows[2]
        rows[2] = (r, x, y, z, h + 2)
        concave = [(0, .8*x-.6*y, .6*x+.8*y, 0, 6)
                   for x,y in ((0,0),(10,0),(10,4),(4,4),(4,8),(0,8))]
        for geometry in (rows, concave):
            for key, face in faces_from_rows(geometry, 1).items():
                if not key.startswith('roof'):
                    continue
                uv = face['uv']
                self.assertTrue(any(abs(a[1]-uv[(i+1)%len(uv)][1]) < 1e-8 for i,a in enumerate(uv)))
                self.assertAlmostEqual(sum(a*b for a,b in zip(face['right'], face['down'])), 0)

    def test_editor_preview_apply_reopen_and_cancel(self):
        faces = faces_from_rows(self.rows(), 1)
        source = rectangle('roof', x_m=2, y_m=2, width_m=1, height_m=1)
        editor = FaceSourcesDialog(None, 'roof', faces, 4, [source], [])
        self.addCleanup(editor.close)
        editor.show()
        APP.processEvents()
        self.assertAlmostEqual(editor.face_preview.face_bounds().width(), 10, delta=1e-8)
        self.assertAlmostEqual(editor.face_preview.face_bounds().height(), 8, delta=1e-8)
        self.assertEqual(editor.draft_source(0)['frame'], 'building_local')
        self.assertFalse(editor.face_preview.invalid_rows)
        editor.validate()
        self.assertEqual(editor.result(), QDialog.Accepted, editor.error.text())
        saved = read_face_sources(editor.sources)
        reopened = FaceSourcesDialog(None, 'roof', faces, 4, saved, [])
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.draft_source(0), saved[0])
        reopened.reject()
        self.assertNotIn('frame', source)

    def test_new_roof_sources_use_edge_frame_and_invalid_frame_is_rejected(self):
        faces = faces_from_rows(self.rows(), 1)
        editor = FaceSourcesDialog(None, 'roof', faces, 4, [], [])
        self.addCleanup(editor.close)
        QTest.mouseClick(editor.add_button, Qt.LeftButton)
        self.assertEqual(editor.draft_source(0)['frame'], 'building_local')
        for source in (rectangle(frame='roof_top_edge'), rectangle('roof', frame=None),
                       rectangle('roof', frame='unknown')):
            with self.assertRaises(ModelError):
                read_face_sources([source])
