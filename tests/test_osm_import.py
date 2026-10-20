# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Independently specified OSM geometries and persisted input assumptions."""

import json
import math

from acalc.layer_import import ImportOptions, import_layer
from acalc.layers import load_layer, validate_layer, vertex_rows
from acalc.osm_import import (
    ATTRIBUTION, COPYRIGHT_URL, build_osm_layer, overpass_query, validate_bbox,
)
from acalc.schema import LAYER_TYPES, ModelError
from acalc.storage import ModelStore
from tests.test_model import ModelFixture


def coords(points):
    return [{"lon": x, "lat": y} for x, y in points]


SQUARE = [(0, 0), (.001, 0), (.001, .001), (0, .001), (0, 0)]


def way(identity=1, points=None, **tags):
    return {"type": "way", "id": identity, "tags": {"building": "yes", **tags},
            "geometry": coords(SQUARE if points is None else points)}


def member(identity, points, role="outer"):
    return {"type": "way", "ref": identity, "role": role, "geometry": coords(points)}


def relation(members, identity=10, **tags):
    return {"type": "relation", "id": identity, "tags": {"building": "yes", "type": "multipolygon", **tags},
            "members": members}


class OsmImportTests(ModelFixture):
    def test_bbox_is_geographic_non_crossing_finite_and_limited(self):
        self.assertEqual(validate_bbox((39.99, -105.01, 40.01, -104.99)), (39.99, -105.01, 40.01, -104.99))
        for bbox in (None, (1, 2, 3), (0, 0, 0, 1), (0, 170, 1, -170), (-91, 0, -90, 1),
                     (0, 0, 91, 1), (0, -181, 1, 0), (0, 0, 1, math.inf), (math.nan, 0, 1, 1),
                     (False, 0, .01, .01), (0, 0, 1, 1)):
            with self.subTest(bbox=bbox), self.assertRaises(ModelError):
                validate_bbox(bbox)
        # One degree of longitude spans less ground at high latitude; the
        # spherical area limit accepts this independently chosen small strip.
        self.assertEqual(validate_bbox((89.9, 0, 89.91, 1)), (89.9, 0, 89.91, 1))

    def test_query_selects_buildings_without_clipping_returned_geometry(self):
        query = overpass_query((40, -105, 40.001, -104.999))
        self.assertIn('way["building"]["building"!="no"](40,-105,40.001,-104.999)', query)
        self.assertIn('relation["type"="multipolygon"]["building"]', query)
        self.assertIn('->.buildingways;', query)
        self.assertIn('relation(bw.buildingways)["type"="multipolygon"]', query)
        self.assertTrue(query.endswith("out body geom;"))
        self.assertNotIn("geom(", query)

    def test_complete_way_attributes_height_and_provenance(self):
        source, report = build_osm_layer({"elements": [way(name="Library", height="8.25 m", **{"building:levels": "99"})]})
        self.assertEqual(source.crs().authid(), "EPSG:4326")
        feature = next(source.getFeatures())
        self.assertEqual(feature["name"], "Library")
        self.assertEqual(feature["height_m"], 8.25)
        self.assertEqual(feature["osm_id"], "1")
        self.assertEqual(feature["osm_type"], "way")
        self.assertEqual(json.loads(feature["osm_tags"])["building:levels"], "99")
        self.assertIn(ATTRIBUTION, feature["notes"])
        self.assertIn(COPYRIGHT_URL, feature["attribution"])
        self.assertEqual(feature["license"], "ODbL 1.0")
        self.assertEqual(feature["license_url"], "https://opendatacommons.org/licenses/odbl/1-0/")
        self.assertEqual(feature["source_url"], "https://www.openstreetmap.org/way/1")
        self.assertTrue(feature["retrieved_at"].endswith("+00:00"))
        self.assertEqual([(p.x(), p.y()) for p in feature.geometry().vertices()], SQUARE)
        self.assertEqual((report.imported_count, report.tagged_height_count, report.default_height_count, report.skipped), (1, 1, 0, []))

    def test_height_units_defaults_and_no_levels_inference(self):
        cases = [("10", 10), ("4 metres", 4), ("10 ft", 3.048), ("10'", 3.048), ('10\'6"', 3.2004),
                 ("0", 7), ("-5", 7), ("NaN", 7), ("12;15", 7), ("4,5", 7), ("3 km", 7), (None, 7)]
        elements = [way(index + 1, **({"height": value, "building:levels": "30"} if value is not None else {"building:levels": "30"}))
                    for index, (value, _) in enumerate(cases)]
        source, report = build_osm_layer({"elements": elements}, default_height=7)
        for feature, (_, expected) in zip(source.getFeatures(), cases):
            self.assertAlmostEqual(feature["height_m"], expected, places=10)
        self.assertEqual((report.tagged_height_count, report.default_height_count), (5, 7))
        source, report = build_osm_layer({"elements": [way(height="20")]}, default_height=9, use_osm_height=False)
        self.assertEqual(next(source.getFeatures())["height_m"], 9)
        self.assertEqual(report.default_height_count, 1)
        for invalid in (0, -1, math.nan, math.inf, None, "bad", True):
            with self.subTest(height=invalid), self.assertRaises(ModelError):
                build_osm_layer({"elements": []}, invalid)

    def test_relation_stitches_reversed_segments_and_separate_outer_parts(self):
        members = [member(1, SQUARE[:3]), member(2, list(reversed(SQUARE[2:]))),
                   member(3, [(x + .01, y) for x, y in SQUARE])]
        source, report = build_osm_layer({"elements": [relation(members, name="Campus", height="12")]})
        features = list(source.getFeatures())
        self.assertEqual([f["name"] for f in features], ["Campus (part 1)", "Campus (part 2)"])
        perimeters = {tuple((p.x(), p.y()) for p in f.geometry().vertices()) for f in features}
        self.assertEqual(perimeters, {tuple(SQUARE), tuple((x + .01, y) for x, y in SQUARE)})
        self.assertEqual((report.imported_count, report.tagged_height_count, report.skipped), (2, 2, []))

    def test_relation_member_ways_are_not_duplicated_and_fallback_geometry_works(self):
        members = [{"type": "way", "ref": 1, "role": "outer"}]
        source, report = build_osm_layer({"elements": [way(), relation(members)]})
        self.assertEqual(source.featureCount(), 1)
        self.assertEqual(next(source.getFeatures())["osm_type"], "relation")
        self.assertEqual(len(report.skipped), 1)
        self.assertIn("member of relation 10", report.skipped[0])

    def test_legacy_multipolygon_parent_prevents_solid_courtyard_import(self):
        parent = relation([member(1, SQUARE), member(2, [(0, 0), (.001, 0), (.001, .001), (0, 0)], "inner")])
        parent["tags"] = {"type": "multipolygon"}
        source, report = build_osm_layer({"elements": [way(), parent]})
        self.assertEqual(source.featureCount(), 0)
        self.assertEqual(report.imported_count, 0)
        self.assertEqual(len(report.skipped), 2)
        self.assertIn("member of relation 10", report.skipped[0])
        self.assertIn("no valid building tag", report.skipped[1])
        self.assertIn("without inventing tags or filling holes", report.skipped[1])

    def test_courtyard_relation_is_skipped_atomically_including_member_way(self):
        inner = [(.0002, .0002), (.0008, .0002), (.0008, .0008), (.0002, .0002)]
        source, report = build_osm_layer({"elements": [relation([member(1, SQUARE), member(2, inner, "inner")]), way()]})
        self.assertEqual(source.featureCount(), 0)
        self.assertEqual(report.imported_count, 0)
        self.assertEqual(len(report.skipped), 2)
        self.assertIn("whole relation", report.skipped[0])
        self.assertIn("not imported separately", report.skipped[1])

    def test_invalid_unclosed_overlapping_and_missing_geometry_are_reported(self):
        cases = [way(points=SQUARE[:-1]), way(points=[(0, 0), (1, 1), (0, 1), (1, 0), (0, 0)]),
                 {"type": "way", "id": 1, "tags": {"building": "yes"}},
                 relation([member(1, SQUARE[:3])]), relation([member(1, SQUARE), member(2, SQUARE)]),
                 relation([member(1, SQUARE), {"type": "way", "ref": 2, "role": "outer"}]),
                 relation([member(1, SQUARE)], type="building"),
                 way(points=[(179, 0), (-179, 0), (-179, 1), (179, 0)]),
                 way(points=[(0, 0), (math.nan, 0), (1, 1), (0, 0)])]
        for element in cases:
            with self.subTest(element=element):
                source, report = build_osm_layer({"elements": [element]})
                self.assertEqual(source.featureCount(), 0)
                self.assertEqual(len(report.skipped), 1)

    def test_no_buildings_building_no_and_parts_only_do_not_import(self):
        partial = way()
        partial["tags"] = {"building:part": "yes"}
        source, report = build_osm_layer({"elements": [partial, way(2, building="no")]})
        self.assertEqual(source.featureCount(), 0)
        self.assertEqual(report.imported_count, 0)
        source, report = build_osm_layer({"elements": []})
        self.assertEqual(source.featureCount(), 0)

    def test_open_elevated_and_underground_buildings_are_explicitly_skipped(self):
        cases = [{"building": "roof"}, {"location": "underground"}, {"underground": "yes"},
                 {"min_height": "2 m"}, {"min_height": "uncertain"}, {"building:min_level": "1"}]
        source, report = build_osm_layer({"elements": [way(i + 1, **tags) for i, tags in enumerate(cases)]})
        self.assertEqual(source.featureCount(), 0)
        self.assertEqual(len(report.skipped), len(cases))
        source, report = build_osm_layer({"elements": [way(min_height="0 m", **{"building:min_level": "0"})]})
        self.assertEqual(source.featureCount(), 1)
        self.assertEqual(report.skipped, [])

    def test_incomplete_or_malformed_server_responses_are_fatal(self):
        cases = [None, [], {}, {"elements": {}}, {"elements": [None]}, {"elements": [way(), way()]},
                 {"elements": [way()], "remark": "runtime error: Query timed out"},
                 {"elements": [{"type": "way", "id": "1"}]},
                 {"elements": [{"type": "way", "id": 1, "tags": []}]}]
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(ModelError):
                build_osm_layer(payload)
        self.assertEqual(self.store.layers(), [])

    def test_persistence_transforms_xy_and_retains_height_provenance_and_assumptions(self):
        points = [(-105, 0), (-104.999, 0), (-104.999, .001), (-105, .001), (-105, 0)]
        source, report = build_osm_layer({"elements": [way(points=points, name="Known origin", **{"building:levels": "20"})]}, default_height=7.5)
        record, count, objects = import_layer(self.store, source, ImportOptions(
            "building", "OSM buildings", name_field="name", notes_field="notes", height_field="height_m", ground_mode="constant"))
        self.assertEqual((count, objects, report.default_height_count), (1, 1, 1))
        saved = load_layer(ModelStore.open(self.path), record)
        self.assertEqual(validate_layer(saved, LAYER_TYPES["building"]), [])
        feature = next(saved.getFeatures())
        rows = vertex_rows(feature.geometry(), LAYER_TYPES["building"])
        # UTM zone 13 central meridian at the equator is exactly 500000 m E, 0 N.
        self.assertAlmostEqual(rows[0][1], 500000, places=5)
        self.assertAlmostEqual(rows[0][2], 0, places=5)
        self.assertEqual([row[3:] for row in rows], [(0, 7.5)] * 4)
        self.assertNotIn("infer_z", feature.fields().names())
        self.assertEqual(feature["height_value"], 7.5)
        self.assertIn("Assumed height 7.5 m", feature["notes"])
        attributes = json.loads(feature["source_attributes"])
        self.assertEqual(attributes["source_url"], "https://www.openstreetmap.org/way/1")
        self.assertIn(ATTRIBUTION, attributes["attribution"])
        self.assertEqual(json.loads(attributes["osm_tags"])["building:levels"], "20")
        self.assertEqual(self.store.variants()[0]["definition"]["layer_ids"], [])
