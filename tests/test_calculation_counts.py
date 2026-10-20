# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Historical calculation accounting without changing authored object totals."""

from copy import deepcopy
import unittest

from acalc.run_info import calculation_scene_counts


class CalculationCountsTests(unittest.TestCase):
    def test_saved_snapshot_is_authoritative_even_without_component_metadata(self):
        run = {"source_count": 2, "metadata": {
            "building_count": 1, "api_snapshot": {"request": {
                "sources": [{}, {}, {}], "buildings": [{}, {}]}}}}
        before = deepcopy(run)
        self.assertEqual(calculation_scene_counts(run), {"source_count": 3, "building_count": 2})
        self.assertEqual(run, before)

    def test_completion_and_historical_component_totals_include_zero(self):
        for counts in ({"source_count": 2, "building_count": 1,
                        "component_source_count": 3, "solid_building_count": 2},
                       {"source_count": 1, "building_count": 0,
                        "component_source_count": 0, "solid_building_count": 1}):
            expected = {"source_count": counts["component_source_count"],
                        "building_count": counts["solid_building_count"]}
            self.assertEqual(calculation_scene_counts(counts), expected)
            self.assertEqual(calculation_scene_counts({"source_count": counts["source_count"],
                                                       "metadata": counts}), expected)

    def test_legacy_runs_without_snapshot_or_component_counts_remain_readable(self):
        self.assertEqual(calculation_scene_counts({"source_count": 5, "metadata": {"building_count": 4}}),
                         {"source_count": 5, "building_count": 4})
        self.assertEqual(calculation_scene_counts({"source_count": 0, "metadata": {}}),
                         {"source_count": 0, "building_count": 0})
        self.assertEqual(calculation_scene_counts({"source_count": 0, "metadata": {
            "api_snapshot": {"request": {"sources": [], "buildings": []}}}}),
                         {"source_count": 0, "building_count": 0})
