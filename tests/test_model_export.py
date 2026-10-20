# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""GDAL round trips and vendor schema evidence, not acoustic acceptance cases."""

import json
from pathlib import Path
import unittest

from osgeo import ogr

from acalc.model_export import FIELDS, export_variant
from acalc.schema import ModelError, definition
from tests import test_api_model as api_fixtures


class ModelExportTests(unittest.TestCase):
    setUp = api_fixtures.ApiModelTests.setUp
    tearDown = api_fixtures.ApiModelTests.tearDown
    create_store = api_fixtures.ApiModelTests.create_store
    feature = api_fixtures.ApiModelTests.feature
    update_feature = api_fixtures.ApiModelTests.update_feature
    scene = api_fixtures.ApiModelTests.scene

    def export(self, format_id="qsi", name="export"):
        return export_variant(self.store, self.variant_id, Path(self.directory.name) / name, format_id)

    def records(self, folder, name):
        dataset = ogr.Open(str(folder / name))
        self.assertIsNotNone(dataset)
        return [(f.items(), f.GetGeometryRef().Clone() if f.GetGeometryRef() else None) for f in dataset.GetLayer()]

    def test_vendor_field_definitions_match_sample_layouts(self):
        samples = Path(__file__).resolve().parents[1] / "tests/fixtures/cadnaa"
        for key in ("srcp", "bldg", "barr", "hlin", "recv", "dipa", "spec"):
            with self.subTest(key=key):
                dataset = ogr.Open(str(samples / f"cadnaa_sample_{key.upper()}.DBF"))
                self.assertIsNotNone(dataset)
                definition = dataset.GetLayer().GetLayerDefn()
                actual = [(definition.GetFieldDefn(i).GetName(), definition.GetFieldDefn(i).GetWidth(),
                           definition.GetFieldDefn(i).GetPrecision()) for i in range(definition.GetFieldCount())]
                expected = [(name, width, precision) for name, _, width, precision in FIELDS[key]]
                # We intentionally omit the unused 31.5 Hz band from our eight-band export.
                if key == "spec":
                    actual = [f for f in actual if f[0] != "VALUE_15"]
                self.assertEqual(actual, expected)

    def test_point_spectra_silence_operating_times_and_identity_round_trip(self):
        self.scene()
        folder = self.export()
        self.assertIn("fmtqsi=DIN45687-QSI:2010-7.1", (folder / "model.qsi").read_text())
        source, geometry = self.records(folder, "model_srcp.shp")[0]
        self.assertEqual(geometry.GetPoint(), (0, 0, 14))
        self.assertEqual(source["EMI_TYPE"], 2)
        self.assertEqual(source["LREF"], 1)
        spec = self.records(folder, "model_spec.dbf")[0][0]
        self.assertEqual(source["SPECT_ID"], spec["ID"])
        self.assertEqual([spec[f"VALUE_{i}"] for i in range(18, 40, 3)], [90, -200, -5, 80, 70, 60, 50, 40])
        dipa = self.records(folder, "model_dipa.dbf")[0][0]
        self.assertEqual(dipa["TH_REF"], 2)
        self.assertEqual([dipa[f"VALUE_{i}"] for i in range(1, 25)], [60] * 24)
        self.assertEqual(self.records(folder, "model_recv.shp")[0][1].GetZ(), 13.5)
        self.assertEqual(len(self.records(folder, "model_hgpt.shp")), 2)
        report = json.loads((folder / "export_report.json").read_text())
        self.assertEqual(len(report["id_mapping"]), 2)
        self.assertIn("not yet verified", report["verification"])

    def test_only_selected_variant_and_disabled_legacy_objects_are_included(self):
        self.scene()
        other = self.store.add_layer("trees", "Not selected")
        self.store.add_variant("Other", definition([other["layer_id"]]))
        folder = self.export()
        snapshot = json.loads((folder / "model_snapshot.json").read_text())
        self.assertEqual(snapshot["variant"]["variant_id"], self.variant_id)
        self.assertNotIn(other["layer_id"], [r["layer_id"] for r in snapshot["layers"]])
        self.assertEqual(len(self.records(folder, "model_srcp.shp")), 1)

    def test_shared_building_roof_and_barrier_absolute_top(self):
        self.feature("building", [(0,0,10,1), (20,0,12,1), (20,20,14,1), (0,20,12,1), (0,0,10,1)],
                     height_mode="average", height_value=8, reflection_coefficient=0.5)
        self.feature("barrier", [(30,0,10,2), (30,20,12,3)], reflection_coefficient=0)
        folder = self.export()
        attributes, roof = self.records(folder, "model_bldg.shp")[0]
        self.assertEqual(attributes["RL"], 3)
        self.assertEqual(attributes["BLDG_H"], 10)
        self.assertEqual([roof.GetGeometryRef(0).GetZ(i) for i in range(5)], [20] * 5)
        attributes, wall = self.records(folder, "model_barr.shp")[0]
        self.assertEqual([wall.GetZ(i) for i in range(2)], [12, 15])
        self.assertEqual(attributes["RL_L"], -200)

    def test_source_building_qsi_retains_solid_and_refuses_to_drop_components(self):
        from acalc.schema import new_id
        layer, _ = self.feature("source_building", [(0,0,10,6), (20,0,10,6),
                                 (20,20,10,6), (0,20,10,6), (0,0,10,6)],
                                height_mode="average", height_value=8)
        folder = self.export(name="empty-source-building")
        attributes, roof = self.records(folder, "model_bldg.shp")[0]
        self.assertEqual(attributes["BLDG_H"], 8)
        self.assertEqual([roof.GetGeometryRef(0).GetZ(i) for i in range(5)], [18] * 5)
        source = dict(id=new_id(), name="Vent", face="wall:0:1", vertex_count=4,
                      x_m=1, y_m=1, width_m=2, height_m=2, spectrum_id=None)
        self.update_feature(layer, face_sources=json.dumps([source]))
        with self.assertRaisesRegex(ModelError, "Building-attached sources"):
            self.export(name="attached-qsi")
        destination = self.export("shp", name="attached-gis")
        snapshot = json.loads((destination / "model_snapshot.json").read_text())
        self.assertEqual(snapshot["layers"][0]["feature_type"], "source_building")
        self.assertEqual(json.loads(snapshot["layers"][0]["objects"][0]["face_sources"]), [source])

    def test_line_and_area_power_bases_and_3d_geometry(self):
        from acalc.spectra import spectrum_definition
        for kind, basis, points in (
            ("line_source", "per_metre", [(0,0,10,2), (10,0,11,2)]),
            ("area_source", "per_square_metre", [(20,0,10,2), (30,0,10,2), (30,10,10,2), (20,0,10,2)])):
            spectrum = self.store.add_spectrum(kind, spectrum_definition("sound_power", [80]*8, power_basis=basis))
            self.feature(kind, points, spectrum_id=spectrum)
        folder = self.export()
        self.assertEqual(self.records(folder, "model_srcl.shp")[0][0]["LREF"], 2)
        self.assertEqual(self.records(folder, "model_srca.shp")[0][0]["LREF"], 2)
        self.assertEqual(self.records(folder, "model_srcl.shp")[0][1].GetZ(1), 13)

    def test_feet_xy_converted_once_vertical_metres_unchanged(self):
        self.store = self.create_store("feet", 2232)
        self.variant_id = self.store.variants()[0]["variant_id"]
        self.layers = []
        self.feature("point_receiver", [(100, 200, 10, 2)])
        folder = self.export()
        p = self.records(folder, "model_recv.shp")[0][1].GetPoint()
        self.assertAlmostEqual(p[0], 30.48006096012192)
        self.assertEqual(p[2], 12)

    def test_unresolved_ground_fails_without_partial_folder(self):
        self.feature("point_receiver", [(0,0,0,1.5)], ground_mode="unresolved")
        with self.assertRaisesRegex(ModelError, "explicit ground Z"):
            self.export()
        self.assertFalse((Path(self.directory.name) / "export").exists())

    def test_nonplanar_source_is_rejected_before_triangulation_can_change(self):
        self.feature("area_source", [(0,0,10,2), (10,0,10,2), (10,10,10,3), (0,10,10,2), (0,0,10,2)])
        with self.assertRaisesRegex(ModelError, "nonplanar"):
            self.export()
        self.assertFalse((Path(self.directory.name) / "export").exists())

    def test_unsupported_selected_effect_fails_instead_of_disappearing(self):
        self.scene()
        self.feature("trees", [(0,0), (10,0), (10,10), (0,0)], canopy_height=10)
        with self.assertRaisesRegex(ModelError, "trees.*verified QSI mapping"):
            self.export()
        self.assertFalse((Path(self.directory.name) / "export").exists())

    def test_ground_material_is_2d_and_does_not_create_terrain(self):
        self.feature("ground_absorption", [(0,0), (10,0), (10,10), (0,0)], ground_factor=0.375)
        folder = self.export()
        attributes, geometry = self.records(folder, "model_gabs.shp")[0]
        self.assertEqual(attributes["G"], 0.375)
        self.assertFalse(geometry.Is3D())
        self.assertFalse((folder / "model_hgpt.shp").exists())
        self.assertNotIn("hgpt=", (folder / "model.qsi").read_text())

    def test_write_failure_leaves_no_partial_export(self):
        self.feature("point_receiver", [(0,0,10,2)], name="Not CP1252: 漢字")
        with self.assertRaisesRegex(ModelError, "Windows-1252"):
            self.export()
        self.assertFalse((Path(self.directory.name) / "export").exists())
        self.assertFalse(list(Path(self.directory.name).glob(".acalc-export-*")))

    def test_existing_directory_is_never_overwritten(self):
        self.scene()
        folder = self.export()
        before = (folder / "model.qsi").read_bytes()
        with self.assertRaisesRegex(ModelError, "already exists"):
            self.export()
        self.assertEqual((folder / "model.qsi").read_bytes(), before)

    def test_gis_export_retains_zm_and_all_attributes_in_snapshot(self):
        self.feature("point_receiver", [(5,6,10,2)], ground_mode="unresolved")
        folder = self.export("shp")
        attributes, geometry = self.records(folder, "layer_001_point_receiver.shp")[0]
        self.assertEqual(geometry.GetPointZM(), (5,6,10,2))
        snapshot = json.loads((folder / "model_snapshot.json").read_text())
        self.assertEqual(snapshot["layers"][0]["objects"][0]["ground_mode"], "unresolved")
        self.assertIn("not a calculation-ready", (folder / "IMPORT.txt").read_text())
        self.assertFalse((folder / "model.qsi").exists())

    def test_invalid_variant_and_format(self):
        with self.assertRaisesRegex(ModelError, "no longer exists"):
            export_variant(self.store, "missing", Path(self.directory.name) / "missing", "qsi")
        with self.assertRaisesRegex(ModelError, "supported export format"):
            self.export("cna")


if __name__ == "__main__":
    unittest.main()
