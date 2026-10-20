# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Independent placement checks for building-local attached sources."""
import math
import unittest

from acalc.building_faces import faces_from_rows, point_mounting, read_face_sources, source_polygon
from acalc.building_preview import BuildingPreview
from acalc.schema import ModelError, new_id
from tests.test_model import APP


def point(face='wall:0:1', **values):
    # Declared from the distance unless given; mounting=None builds an undeclared record.
    source = dict(id=new_id(), name='Fan', face=face, vertex_count=4, type='point',
                  x_m=2, y_m=1, distance_m=1.5, spectrum_id=None) | values
    source.setdefault('mounting', 'surface' if source['distance_m'] == 0 else 'standoff')
    if source['mounting'] is None:
        del source['mounting']
    return source


class AttachedSourceGeometryTests(unittest.TestCase):
    rows = [(0, x, y, 10, 6) for x, y in ((0, 0), (10, 0), (10, 8), (0, 8))]

    def assert_point(self, actual, expected):
        for a, b in zip(actual, expected):
            self.assertAlmostEqual(a, b, delta=1e-8)

    def test_point_outward_offsets_and_wall_left_vertex_height(self):
        faces = faces_from_rows(self.rows, 1)
        self.assertEqual(source_polygon(point(), faces, 4), ((2, -1.5, 5),))
        self.assertEqual(source_polygon(point(distance_m=0), faces, 4), ((2, 0, 5),))
        self.assertEqual(source_polygon(point('roof', frame='building_local'), faces, 4), ((2, 7, 7.5),))
        # Raising the left endpoint shifts every attachment on its right wall.
        raised = list(self.rows)
        raised[0] = (0, 0, 0, 10, 8)
        attached_area = dict(id=new_id(), name='Panel', face='wall:0:1', vertex_count=4,
                             x_m=2, y_m=3, width_m=1, height_m=1, spectrum_id=None)
        for source in (point(y_m=3), attached_area):
            before = source_polygon(source, faces, 4)
            after = source_polygon(source, faces_from_rows(raised, 1), 4)
            for a, b in zip(before, after):
                self.assert_point(b, (a[0], a[1], a[2]+2))
        # The opposite endpoint is not this wall's origin.
        raised[0] = self.rows[0]
        raised[1] = (0, 10, 0, 10, 8)
        self.assertEqual(source_polygon(point(), faces_from_rows(raised, 1), 4), ((2, -1.5, 5),))

    def test_rigid_rotation_translation_and_projected_units(self):
        sources = [point(), point('roof', frame='building_local'),
                   dict(id=new_id(), name='Roof panel', face='roof', vertex_count=4,
                        frame='building_local', x_m=2, y_m=2, width_m=3, height_m=1, spectrum_id=None)]
        for slope in (0, .5):
            original = [(r,x,y,z,h+slope*x) for r,x,y,z,h in self.rows]
            base = faces_from_rows(original, 1)
            for c, s in ((0, 1), (-1, 0), (.6, .8), (.8, -.6)):
                for scale in (1, .3048):
                    moved = [(r, (400000+c*x-s*y)/scale, (300000+s*x+c*y)/scale, z+30, h)
                             for r,x,y,z,h in original]
                    faces = faces_from_rows(moved, scale)
                    for source in sources:
                        before, after = source_polygon(source, base, 4), source_polygon(source, faces, 4)
                        for a, b in zip(before, after):
                            self.assert_point(b, (c*a[0]-s*a[1], s*a[0]+c*a[1], a[2]))

    def test_sloped_roof_distance_is_perpendicular_and_measured_in_metres(self):
        rows = [(r,x,y,z,h+.5*x) for r,x,y,z,h in self.rows]
        faces = faces_from_rows(rows, 1)
        root5 = math.sqrt(5)
        source = point('roof', frame='building_local', x_m=root5, y_m=2, distance_m=root5)
        # Roof origin (0,8,6), right (2,0,1)/sqrt(5), down (0,-1,0),
        # normal (-1,0,2)/sqrt(5): anchor (2,6,7), offset (-1,0,2).
        self.assert_point(source_polygon(source, faces, 4)[0], (1,6,9))

    def test_nonplanar_facets_and_sources_keep_identity_when_rotated(self):
        rows = [(r,x,y,z,h+(2 if x == 10 and y == 8 else 0)) for r,x,y,z,h in self.rows]
        original = faces_from_rows(rows, 1)
        for c,s in ((0,1),(-1,0),(.6,.8)):
            rotated = faces_from_rows([(r,c*x-s*y,s*x+c*y,z,h) for r,x,y,z,h in rows], 1)
            self.assertEqual(set(rotated), set(original))
            for key, face in original.items():
                if not key.startswith('roof:'):
                    continue
                x = sum(p[0] for p in face['uv'])/3
                y = sum(p[1] for p in face['uv'])/3
                source = point(key, frame='building_local', x_m=x, y_m=y)
                a = source_polygon(source, original, 4)[0]
                self.assert_point(source_polygon(source, rotated, 4)[0], (c*a[0]-s*a[1],s*a[0]+c*a[1],a[2]))

    def test_strict_record_and_anchor_validation(self):
        source = point()
        self.assertEqual(read_face_sources([source]), [source])
        for bad in (point(distance_m=-1), point(distance_m=float('nan')), point(distance_m=True),
                    point(width_m=1), point(type='unknown'), point(frame='building_local')):
            with self.subTest(bad=bad), self.assertRaises(ModelError):
                read_face_sources([bad])
        faces = faces_from_rows(self.rows, 1)
        for bad in (point(x_m=11), point(y_m=7), point(vertex_count=5)):
            with self.assertRaises(ModelError):
                source_polygon(bad, faces, 4)

    def test_point_mounting_must_be_declared(self):
        for good in (point(mounting='surface', distance_m=0), point(mounting='standoff', distance_m=.05)):
            self.assertEqual(read_face_sources([good]), [good])
        for bad in (point(mounting='surface', distance_m=.2), point(mounting='standoff', distance_m=0),
                    point(mounting='standoff', distance_m=.049), point(mounting='floating'),
                    dict(point(mounting='surface', distance_m=0), type=None)):
            with self.subTest(bad=bad), self.assertRaises(ModelError):
                read_face_sources([bad])
        self.assertEqual(point_mounting(point(distance_m=0)), 'surface')
        self.assertEqual(point_mounting(point(distance_m=1.5)), 'standoff')
        # Distance alone no longer selects the acoustic convention.
        for distance in (0, 1.5):
            with self.assertRaisesRegex(ModelError, 'declare this building-mounted point'):
                point_mounting(point(distance_m=distance, mounting=None))
        # A legacy displacement below the minimum is readable but must be reviewed.
        legacy = point(distance_m=.02, mounting=None)
        self.assertEqual(read_face_sources([legacy]), [legacy])
        with self.assertRaisesRegex(ModelError, 'surface point or a standoff point at least 0.05 m'):
            source_polygon(legacy, faces_from_rows(self.rows, 1), 4)

    def test_small_rotations_retain_concave_and_collinear_facet_sources(self):
        # The L's fourth vertex lies exactly on an ear diagonal; the rectangle
        # has collinear perimeter midpoints. Projected-coordinate rounding must
        # not change which side of either line those points occupy.
        shapes = [(((0,0),(8,0),(8,4),(4,4),(4,8),(0,8)),
                   {'roof:0:1:2','roof:0:2:3','roof:0:3:5','roof:3:4:5'}),
                  (((0,0),(4,0),(8,0),(8,8),(4,8),(0,8)),
                   {'roof:0:1:5','roof:1:2:5','roof:2:3:4','roof:2:4:5'})]
        for outline, expected in shapes:
            rows = [(0,x,y,0,6+i%3) for i,(x,y) in enumerate(outline)]
            original = faces_from_rows(rows, 1)
            self.assertEqual({key for key in original if key.startswith('roof:')}, expected)
            for degrees in (1,2,15,91,179):
                c,s = math.cos(math.radians(degrees)),math.sin(math.radians(degrees))
                moved = faces_from_rows([(r,400000+c*x-s*y,300000+s*x+c*y,z,h)
                                         for r,x,y,z,h in rows], 1)
                self.assertEqual({key for key in moved if key.startswith('roof:')}, expected)
                for key in expected:
                    uv = original[key]['uv']
                    source = point(key, frame='building_local', vertex_count=6,
                                   x_m=sum(p[0] for p in uv)/3, y_m=sum(p[1] for p in uv)/3)
                    a = source_polygon(source, original, 6)[0]
                    self.assert_point(source_polygon(source, moved, 6)[0],
                                      (c*a[0]-s*a[1],s*a[0]+c*a[1],a[2]))

    def test_point_markers_visible_in_front_occluded_behind_and_offset_shown(self):
        preview = BuildingPreview()
        self.addCleanup(preview.close)
        preview.set_building(self.rows)
        preview.resize(600, 500)
        preview.show()
        APP.processEvents()
        baseline = preview.grab().toImage()
        preview.set_sources([point(distance_m=0)])
        self.assertNotEqual(preview.grab().toImage(), baseline)
        preview.set_sources([point('wall:2:3', distance_m=0)])
        self.assertEqual(preview.grab().toImage(), baseline)
        preview.set_sources([point(distance_m=2)])
        self.assertEqual(preview.source_polygons[0][1], ((2,-2,5),))
        self.assertNotEqual(preview.grab().toImage(), baseline)
