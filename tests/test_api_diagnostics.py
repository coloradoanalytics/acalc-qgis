# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Readable errors retain object identity and the original diagnostic detail."""

from copy import deepcopy
import unittest

from acalc.api_diagnostics import named_diagnostic


BUILDING_ID = "01a0dee3-6980-76e4-bec7-bf1accf0b2fd/01a0e980-9631-71d3-b5b2-89cbbf97d2ba/01a0e980-9628-7af1-acf0-9dd2e69e9f10"
REFERENCE = dict(kind="building", name="OSM way 1311885728",
                 layer_name="OpenStreetMap buildings", fid=27)


class ApiDiagnosticsTests(unittest.TestCase):
    def test_reported_wall_error_uses_saved_name_and_preserves_detail(self):
        snapshot = {"id_mapping": {BUILDING_ID: dict(REFERENCE)}}
        original = deepcopy(snapshot)
        detail = "prepared terrain 185.339747 m reaches or exceeds surface top 178.322667 m at (715276.657594, 346082.073450) [invalid_input]"
        raw = f"HTTP 422: buildings/barriers: building:{BUILDING_ID}:wall:3:4: {detail}"
        self.assertEqual(named_diagnostic(raw, snapshot),
                         'HTTP 422: buildings/barriers: Building “OSM way 1311885728” '
                         '(layer “OpenStreetMap buildings”, feature 27):wall:3:4: ' + detail)
        self.assertEqual(snapshot, original)

    def test_attached_sources_are_resolved_before_host_and_unknown_ids_retained(self):
        source = BUILDING_ID + "/source/01a0ee32-92b7-7d32-8376-21cd8fa98e5e"
        snapshot = {"id_mapping": {BUILDING_ID: REFERENCE,
                                  source: dict(REFERENCE, kind="point_source", name="Inlet 12")}}
        text = named_diagnostic(source + " on building:" + BUILDING_ID, snapshot)
        self.assertIn('Point source “Inlet 12”', text)
        self.assertIn('Building “OSM way 1311885728”', text)
        self.assertNotIn(BUILDING_ID, text)
        unknown = BUILDING_ID.replace("9628", "9999")
        self.assertEqual(named_diagnostic(unknown, snapshot), unknown)
        self.assertEqual(named_diagnostic(unknown, None), unknown)

    def test_duplicate_and_unnamed_objects_have_layer_and_feature_locations(self):
        second = BUILDING_ID.replace("9628", "9999")
        mapping = {BUILDING_ID: REFERENCE, second: dict(REFERENCE, layer_name="Site buildings", fid=8)}
        text = named_diagnostic(BUILDING_ID + " conflicts with " + second, {"id_mapping": mapping})
        self.assertIn('layer “OpenStreetMap buildings”, feature 27', text)
        self.assertIn('layer “Site buildings”, feature 8', text)
        mapping[second]["name"] = ""
        self.assertEqual(named_diagnostic(second, {"id_mapping": mapping}),
                         'Unnamed building (layer “Site buildings”, feature 8)')
