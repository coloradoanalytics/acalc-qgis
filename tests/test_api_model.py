# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Independent normalized transport fixtures, not acoustic reference cases."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
import uuid

import numpy as np
from osgeo import gdal, ogr, osr

from acalc.api_model import _finalize_area_raster, prepare_calculation, save_completed_calculation
from acalc.area_results import AreaSurface, NODATA, geometry_from_snapshot, grouped_results, results_directory
from acalc.schema import ModelError, definition, new_id
from acalc.spectra import spectrum_definition
from acalc.storage import ModelStore


CAPABILITIES = dict(api_version="1.0", method="ISO 9613-2:2024", atmospheric_absorption="ISO 9613-1:1993",
                    frequencies_hz=[63, 125, 250, 500, 1000, 2000, 4000, 8000],
                    source_kinds=["point", "area"], receiver_kinds=["point", "area"], max_reflect_order=3,
                    implementation=dict(name="independent-test-service", version="0.1.0"))
BACKEND = dict(connection_id="test", api_name="Test service", base_url="http://127.0.0.1:8343",
               engine_id="iso9613-2:2024", implementation=CAPABILITIES["implementation"])
JOB = dict(id="opaque-job", state="completed")


def extended_capabilities():
    return dict(CAPABILITIES, api_version="1.1", source_kinds=["point", "line", "area"],
                features=["terrain_inference", "building_solids", "building_roof_mesh", "building_attached_sources",
                          "mixed_ground", "foliage_simple", "polyline_barriers", "reflecting_barriers",
                          "screened_extended_sources", "reflected_extended_sources"],
                ground_region_policies=["nonoverlapping_schema11"],
                component_profiles=[dict(category=category, id=identity) for category, identity in (
                    ("terrain_construction", "acalc_schema11"), ("attachment_placement", "acalc_schema11"),
                    ("miscellaneous_attenuation", "iso9613_2024_foliage_simplified_acalc_v1"))])


CAPABILITIES = extended_capabilities()


def fields(level=42):
    return dict(octave_db=[level] * 8, a_weighted_db=48.99 if level is not None else None,
                band_states=["calculated" if level is not None else "exact_zero"] * 8,
                state="calculated" if level is not None else "exact_zero", omitted_energy=[0] * 8,
                omission_increase_db=[0] * 8)


def area_result(snapshot, directory, counts, levels=None):
    """Summaries plus engine-shaped GeoTIFF downloads for each submitted area.

    levels maps (area_id, column, row) to one level written in all nine bands.
    """
    from tests.test_api_runner import FakeCalculationAPI
    summaries = FakeCalculationAPI.area_summaries(snapshot["request"], "job")
    downloads = {}
    for summary in summaries:
        identity, raster = summary["area_receiver_id"], summary["raster"]
        summary["receiver_count"] = counts[identity]
        path = Path(directory) / f"download-{uuid.uuid4().hex}.tif"
        dataset = gdal.GetDriverByName("GTiff").Create(str(path), raster["columns"], raster["rows"], 9,
                                                       gdal.GDT_Float32, ["INTERLEAVE=BAND"])
        dataset.SetGeoTransform(raster["geotransform"])
        pixels = np.full((9, raster["rows"], raster["columns"]), NODATA, dtype=np.float32)
        for (area, column, row), level in (levels or {}).items():
            if area == identity:
                pixels[:, raster["rows"] - 1 - row, column] = level
        for band in range(9):
            dataset.GetRasterBand(band + 1).SetNoDataValue(NODATA)
            dataset.GetRasterBand(band + 1).WriteArray(pixels[band])
        dataset = None
        payload = path.read_bytes()
        raster.update(bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest())
        downloads[identity] = dict(path=str(path), bytes=len(payload), sha256=raster["sha256"])
    return summaries, downloads


class ApiModelTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = self.create_store("scene", 26913)
        self.variant_id = self.store.variants()[0]["variant_id"]
        self.layers = []
        self.spectrum_id = self.store.add_spectrum("Power", spectrum_definition("sound_power", [90, 0, -5, 80, 70, 60, 50, 40]))

    def tearDown(self):
        self.directory.cleanup()

    def create_store(self, name, epsg):
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(epsg)
        return ModelStore.create(Path(self.directory.name) / (name + ".gpkg"), name, srs.ExportToWkt())

    def feature(self, kind, points, **values):
        object_id = new_id()
        geometry_type = "POINT" if kind in ("point_source", "point_receiver", "terrain_point", "measurement") else "LINESTRING" if kind in ("barrier", "terrain_line", "line_source") else "POLYGON"
        dimensions = "" if kind in ("ground_absorption", "trees") else "Z" if kind.startswith("terrain") else "ZM"
        coordinates = ",".join(" ".join(map(str, p[:2] if not dimensions else p[:3] if dimensions == "Z" else p)) for p in points)
        wkt = f"{geometry_type} {dimensions} ({'(' if geometry_type == 'POLYGON' else ''}{coordinates}{')' if geometry_type == 'POLYGON' else ''})"
        def populate(layer):
            feature = ogr.Feature(layer.GetLayerDefn())
            feature.SetField("object_id", object_id)
            feature.SetField("name", values.get("name", kind + " fixture"))
            feature.SetGeometry(ogr.CreateGeometryFromWkt(wkt))
            if kind.endswith("source"):
                feature.SetField("spectrum_id", self.spectrum_id)
            for key, value in values.items():
                if value is not None:
                    feature.SetField(key, value)
            if layer.CreateFeature(feature):
                raise AssertionError("Fixture geometry could not be written")
        layer = self.store.add_layer(kind, kind + " layer", populate=populate)
        self.layers.append(layer["layer_id"])
        self.store.save_variant(self.variant_id, "Test variant", definition(self.layers))
        return layer, object_id

    def scene(self):
        self.feature("point_source", [(0, 0, 12, 2)], enabled=0)
        self.feature("point_receiver", [(100, 0, 12, 1.5)], enabled=0)

    def update_feature(self, layer, **values):
        dataset = ogr.Open(self.store.path, update=1)
        target = dataset.GetLayerByName(layer["table_name"])
        feature = target.GetNextFeature()
        for key, value in values.items():
            feature.SetFieldNull(key) if value is None else feature.SetField(key, value)
        self.assertEqual(target.SetFeature(feature), 0)
        feature = target = dataset = None

    def prepare(self, model_settings=None):
        if model_settings:
            model = self.store.calculation()["settings"]
            model.update({key: value for key, value in model_settings.items() if key in model})
            self.store.save_calculation(model)
        return prepare_calculation(self.store, self.variant_id, CAPABILITIES)

    def result(self, snapshot, silent=False):
        request = snapshot["request"]
        return dict(method="ISO 9613-2:2024 / ISO 9613-1:1993", profile="transport-fixture", limitations=["No acoustic reference claim"],
                    atmosphere=deepcopy(request["atmosphere"]), ground=deepcopy(request["ground"]), settings=deepcopy(request["settings"]),
                    receivers=[dict(receiver_id=r["id"], position=r["ground_ref"]["position"][:2] + [r["ground_ref"]["position"][2] + r["height_m"]],
                                    ground_elevation_m=r["ground_ref"]["position"][2], height_m=r["height_m"],
                                    sources=[dict(source_id=s["id"], **fields(None if silent else 42)) for s in request["sources"]],
                                    **fields(None if silent else 42)) for r in request["receivers"]], area_receivers=[])

    def test_selected_disabled_objects_metric_positions_silence_and_snapshot(self):
        self.scene()
        other = self.store.add_layer("building", "Unselected building layer")
        settings = self.store.calculation()["settings"]
        settings.update(c0_db=4)
        self.store.save_calculation(settings)
        snapshot = self.prepare()
        request = snapshot["request"]
        self.assertEqual(request["sources"][0]["ground_ref"]["position"], [0, 0, 12])
        self.assertEqual(request["receivers"][0]["ground_ref"]["position"], [100, 0, 12])
        self.assertEqual(request["ground"], dict(factor=0, elevation_m=12))
        self.assertEqual(request["sources"][0]["spectrum"], [90, None, -5, 80, 70, 60, 50, 40])
        self.assertEqual(request["settings"]["max_reflect_order"], 3)
        self.assertEqual(request["settings"]["c0"], 0)
        self.assertIsNone(request["settings"]["minimum_contribution_db"])
        self.assertNotIn("pruning_strategy", request["settings"])
        self.assertNotIn(other["layer_id"], snapshot["provenance"]["objects"])
        self.assertTrue(all(identity.startswith(snapshot["project_id"] + "/") for identity in snapshot["id_mapping"]))

    def test_projected_feet_convert_only_xy_and_roundtrip_saved_receiver(self):
        self.store = self.create_store("feet", 2232)
        self.variant_id = self.store.variants()[0]["variant_id"]
        self.spectrum_id = self.store.add_spectrum("Power", spectrum_definition("sound_power", [80] * 8))
        self.scene()
        snapshot = self.prepare()
        factor = 1200 / 3937
        self.assertAlmostEqual(snapshot["request"]["receivers"][0]["ground_ref"]["position"][0], 100 * factor)
        self.assertEqual(snapshot["request"]["receivers"][0]["height_m"], 1.5)
        save_completed_calculation(self.store, snapshot, self.result(snapshot), BACKEND, JOB)
        row = self.store.receiver_results(self.variant_id)[0]
        geometry = geometry_from_snapshot(row["metadata"]["geometry_gpkg_hex"])
        self.assertEqual(geometry.GetPointZM(), (100, 0, 12, 1.5))
        run = self.store.latest_calculation_run(self.variant_id)
        self.assertFalse(run["stale"])
        self.assertEqual(run["settings"]["meteorology"], "downwind")
        self.assertAlmostEqual(row["broadband_z_db"], 51.03089986991944)
        self.assertEqual(run["metadata"]["normalized_settings"], snapshot["request"]["settings"])
        self.assertEqual(run["metadata"]["api_snapshot"], snapshot)
        self.assertEqual(run["metadata"]["api_result"]["receivers"][0]["sources"], row["metadata"]["sources"])

    def test_unknown_spectrum_is_not_silence_or_adjustable_initial_guess(self):
        self.scene()
        self.store.save_spectrum(self.spectrum_id, "Power", spectrum_definition("sound_power", [None] * 8,
                                  back_calculation="adjustable", initial_values=[90] * 8))
        with self.assertRaisesRegex(ModelError, "unknown bands"):
            self.prepare()

    def test_explicit_scene_does_not_require_or_emit_terrain_inference(self):
        self.scene()
        capabilities = deepcopy(CAPABILITIES)
        capabilities['features'].remove('terrain_inference')
        request = prepare_calculation(self.store, self.variant_id, capabilities)['request']
        self.assertNotIn('inference_points', request['terrain'])
        self.assertTrue(all('position' in obj['ground_ref'] for obj in request['sources'] + request['receivers']))
        self.assertNotIn('inference_id', json.dumps(request))

    def test_nonflat_explicit_ground_is_sent_to_the_solver(self):
        self.feature("point_source", [(0, 0, 12, 2)])
        self.feature("point_receiver", [(100, 0, 500, 1.5)])
        request = self.prepare()["request"]
        self.assertEqual(request["receivers"][0]["ground_ref"], dict(position=[100, 0, 500]))
        self.feature("terrain_point", [(200, 200, 13, 0)])
        self.assertEqual(self.prepare()["request"]["terrain"]["samples"], [[200, 200, 13]])

    def test_no_explicit_terrain_does_not_fall_back_to_legacy_plane(self):
        self.feature("point_source", [(0, 0, 0, 2)], ground_mode="unresolved")
        self.feature("point_receiver", [(10, 0, 0, 2)], ground_mode="unresolved")
        with self.assertRaisesRegex(ModelError, "explicit ground Z"):
            self.prepare()

    def test_old_api_profile_is_not_supported(self):
        self.scene()
        with self.assertRaisesRegex(ModelError, "schema-11 terrain"):
            prepare_calculation(self.store, self.variant_id, dict(CAPABILITIES, component_profiles=[]))

    def prepare_extended(self, capabilities=None):
        return prepare_calculation(self.store, self.variant_id, capabilities or extended_capabilities())

    def test_extended_building_keeps_solid_material_and_roof_source(self):
        source_id = new_id()
        attached = dict(id=source_id, name="Exhaust", face="roof", vertex_count=4,
                        spectrum_id=self.spectrum_id, frame="building_local", x_m=1, y_m=1,
                        type="point", mounting="standoff", distance_m=0.2)
        layer, object_id = self.feature("source_building", [(0, 0, 12, 99), (4, 0, 13, 99), (4, 4, 14, 99),
                                                      (0, 4, 13, 99), (0, 0, 12, 99)],
                                        height_mode="average", height_value=6, reflection_coefficient=0.75,
                                        face_sources=json.dumps([attached]))
        self.feature("point_receiver", [(100, 0, 14, 1.5)])
        snapshot = self.prepare_extended()
        building = snapshot["request"]["buildings"][0]
        reference = snapshot["id_mapping"][building["id"]]
        self.assertEqual(reference["name"], "source_building fixture")
        self.assertEqual(reference["layer_name"], layer["name"])
        self.assertEqual(reference["fid"], 1)
        self.assertEqual(building["ground_vertices"], [dict(position=list(p)) for p in
                         [(0, 0, 12), (4, 0, 13), (4, 4, 14), (0, 4, 13)]])
        self.assertEqual(building["roof"], dict(mode="average", height_m=6))
        self.assertEqual(building["roof_triangles"], [[3, 0, 1], [1, 2, 3]])
        self.assertEqual(building["absorption"], [0.25] * 8)
        source = snapshot["request"]["sources"][0]
        self.assertEqual(source["host_building_id"], building["id"])
        self.assertEqual(source["host_face_id"], "roof")
        self.assertEqual(source["attachment"], dict(placement_profile="acalc_schema11", frame="building_local",
                                                    x_m=1, y_m=1, mounting="standoff", distance_m=0.2))
        self.assertEqual(snapshot["id_mapping"][source["id"]]["source_id"], source_id)
        self.assertEqual(source["spectrum"], [90, None, -5, 80, 70, 60, 50, 40])
        self.assertEqual(snapshot["request"]["reflectors"], [])
        attached["vertex_count"] = 5
        self.update_feature(layer, face_sources=json.dumps([attached]))
        with self.assertRaisesRegex(ModelError, "stale building face"):
            self.prepare_extended()

    def test_source_building_components_keep_identity_but_count_as_one_ui_source(self):
        self.scene()
        footprint = [(0, 10, 12, 5), (4, 10, 12, 5), (4, 14, 12, 5),
                     (0, 14, 12, 5), (0, 10, 12, 5)]
        self.feature("building", footprint)
        components = [dict(id=new_id(), name=name, face="roof", vertex_count=4,
                           spectrum_id=self.spectrum_id, frame="building_local", x_m=x, y_m=1,
                           type="point", mounting="surface", distance_m=0)
                      for name, x in (("Fan A", 1), ("Fan B", 2))]
        layer, object_id = self.feature("source_building", footprint, name="Plant",
                                        face_sources=json.dumps(components))
        snapshot = self.prepare_extended()
        request = snapshot["request"]
        self.assertEqual(len(request["buildings"]), 2)
        self.assertEqual(len(request["sources"]), 3)
        source_building = next(building for building in request["buildings"]
                               if snapshot["id_mapping"][building["id"]]["kind"] == "source_building")
        default_absorption = 1 - self.store.calculation()["settings"]["building_reflection_coefficient"]
        self.assertEqual(source_building["absorption"], [default_absorption] * 8)
        emitted = [source for source in request["sources"] if source.get("host_building_id")]
        self.assertEqual({source["id"].rsplit("/", 1)[1] for source in emitted},
                         {component["id"] for component in components})
        for source in emitted:
            reference = snapshot["id_mapping"][source["id"]]
            self.assertEqual(reference["parent_source_id"], source_building["id"])
            self.assertEqual(reference["parent_source_name"], "Plant")
            self.assertEqual(reference["parent_source_kind"], "source_building")
            self.assertEqual((reference["layer_id"], reference["object_id"]), (layer["layer_id"], object_id))
        summary = save_completed_calculation(self.store, snapshot, self.result(snapshot), BACKEND, JOB)
        run = self.store.calculation_runs()[0]
        self.assertEqual(summary["source_count"], 2)
        self.assertEqual(run["source_count"], 2)
        for key, expected in (("component_source_count", 3), ("building_count", 1),
                              ("source_building_count", 1), ("solid_building_count", 2)):
            self.assertEqual(summary[key], expected)
            self.assertEqual(run["metadata"][key], expected)
        contributions = self.store.receiver_results(self.variant_id)[0]["metadata"]["sources"]
        self.assertEqual({source["source_id"] for source in contributions},
                         {source["id"] for source in request["sources"]})

    def test_regular_building_rejects_attached_sources_at_calculation_boundary(self):
        from acalc.api_scene import prepare_scene
        attached = dict(id=new_id(), name="Fan", face="roof", vertex_count=4,
                        spectrum_id=self.spectrum_id, frame="building_local", x_m=1, y_m=1,
                        type="point", mounting="surface", distance_m=0)
        points = [(0, 0, 12, 5), (4, 0, 12, 5), (4, 4, 12, 5), (0, 4, 12, 5)]
        objects = [("building", {"face_sources": json.dumps([attached])}, points,
                    "Regular building", "building-id", {"name": "Regular building"})]
        with self.assertRaisesRegex(ModelError, "Buildings cannot have attached sources; use Source Buildings"):
            prepare_scene({}, objects, 1, {}, self.store.calculation()["settings"], extended_capabilities())

    def test_extended_roof_modes_preserve_explicit_ground_and_authored_roofs(self):
        self.scene()
        layer, _ = self.feature("building", [(0, 10, 100, 2), (4, 10, 101, 3),
                                             (4, 14, 102, 4), (0, 14, 103, 5), (0, 10, 100, 2)],
                                height_mode="absolute_individual")
        for mode, expected in (("average", dict(height_m=6)), ("highest", dict(height_m=6)),
                               ("lowest", dict(height_m=6)), ("absolute", dict(elevation_m=6)),
                               ("individual", dict(heights_m=[2, 3, 4, 5])),
                               ("absolute_individual", dict(elevations_m=[102, 104, 106, 108]))):
            with self.subTest(mode=mode):
                self.update_feature(layer, height_mode=mode)
                request = self.prepare_extended()["request"]
                building = request["buildings"][0]
                self.assertEqual(building["roof"], dict(mode=mode, **expected))
                self.assertTrue(all(set(ref) == {"position"} for ref in building["ground_vertices"]))
                self.assertNotIn("inference_points", request["terrain"])

    def test_extended_terrain_regions_and_foliage_are_not_flattened_or_ground_seeds(self):
        self.scene()
        self.feature("terrain_line", [(0, 20, 15, 0), (20, 20, 20, 0)])
        boundary = [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]
        self.feature("ground_absorption", boundary, ground_factor=0.6)
        self.feature("trees", boundary, canopy_height=8)
        request = self.prepare_extended()["request"]
        self.assertEqual(request["terrain"]["samples"], [])
        self.assertEqual(request["terrain"]["breakline_definitions"][0]["vertices"],
                         [dict(position=[0, 20, 15]), dict(position=[20, 20, 20])])
        self.assertEqual(request["ground"]["region_policy"], "nonoverlapping_schema11")
        self.assertEqual(request["ground"]["regions"][0]["factor"], 0.6)
        self.assertEqual(request["misc_regions"][0]["canopy_height_m"], 8)
        caps = extended_capabilities()
        caps["features"].remove("foliage_simple")
        with self.assertRaisesRegex(ModelError, "foliage_simple.*unsupported"):
            self.prepare_extended(caps)

    def test_extended_clockwise_building_faces_keep_authored_indices(self):
        self.scene()
        source = dict(id=new_id(), name="Wall source", face="wall:1:0", vertex_count=4,
                      spectrum_id=self.spectrum_id, x_m=1, y_m=1, type="point", distance_m=0)
        layer, _ = self.feature("source_building", [(0, 10, 12, 5), (0, 14, 12, 5), (4, 14, 12, 5),
                                             (4, 10, 12, 5), (0, 10, 12, 5)], face_sources=json.dumps([source]))
        # The engine needs a declared mounting; distance alone does not select one.
        with self.assertRaisesRegex(ModelError, "declare this building-mounted point"):
            self.prepare_extended()
        source["mounting"] = "surface"
        self.update_feature(layer, face_sources=json.dumps([source]))
        exported = self.prepare_extended()["request"]["sources"][-1]
        self.assertEqual(exported["host_face_id"], "wall:1:0")
        # A surface point carries no displacement.
        self.assertEqual(exported["attachment"]["mounting"], "surface")
        self.assertNotIn("distance_m", exported["attachment"])
        source.update(mounting="standoff", distance_m=0.02)
        self.update_feature(layer, face_sources=json.dumps([source]))
        with self.assertRaisesRegex(ModelError, "at least 0.05 m"):
            self.prepare_extended()
        source.update(mounting="surface", distance_m=0)
        source["face"] = "wall:0:1"
        self.update_feature(layer, face_sources=json.dumps([source]))
        with self.assertRaisesRegex(ModelError, "stale building face"):
            self.prepare_extended()

    def test_extended_material_spectrum_and_missing_building_capability(self):
        self.scene()
        material = self.store.add_spectrum("Facade", spectrum_definition("absorption", [0.2] * 8))
        self.feature("building", [(0, 10, 12, 5), (4, 10, 12, 5), (4, 14, 12, 5),
                                   (0, 14, 12, 5), (0, 10, 12, 5)],
                     reflection_coefficient=0, absorption_spectrum_id=material)
        self.assertEqual(self.prepare_extended()["request"]["buildings"][0]["absorption"], [0.2] * 8)
        caps = extended_capabilities()
        caps["features"].remove("building_solids")
        with self.assertRaisesRegex(ModelError, "building_solids.*unsupported"):
            self.prepare_extended(caps)

    def test_extended_receiver_result_retains_its_own_ground_z_and_height(self):
        self.feature("point_source", [(0, 0, 12, 2)])
        self.feature("point_receiver", [(100, 0, 14, 1.5)])
        snapshot = self.prepare_extended()
        result = dict(method="ISO 9613-2:2024 / ISO 9613-1:1993", profile="transport-fixture", limitations=[],
                      **{key: deepcopy(snapshot["request"][key]) for key in ("atmosphere", "ground", "settings")},
                      area_receivers=[], receivers=[dict(receiver_id=snapshot["request"]["receivers"][0]["id"],
                          position=[100, 0, 15.5], ground_elevation_m=14, height_m=1.5,
                          sources=[dict(source_id=snapshot["request"]["sources"][0]["id"], **fields())], **fields())])
        save_completed_calculation(self.store, snapshot, result, BACKEND, JOB)
        row = self.store.receiver_results(self.variant_id)[0]
        self.assertEqual(geometry_from_snapshot(row["metadata"]["geometry_gpkg_hex"]).GetPointZM(), (100, 0, 14, 1.5))
        result["receivers"][0].update(ground_elevation_m=13, height_m=2.5)
        with self.assertRaisesRegex(ModelError, "receiver height"):
            save_completed_calculation(self.store, snapshot, result, BACKEND, JOB)

    def test_extended_feet_do_not_scale_explicit_vertical_metres(self):
        self.store = self.create_store("extended-feet", 2232)
        self.variant_id = self.store.variants()[0]["variant_id"]
        self.spectrum_id = self.store.add_spectrum("Power", spectrum_definition("sound_power", [80] * 8))
        self.feature("point_source", [(0, 0, 12, 2)])
        self.feature("point_receiver", [(100, 0, 14, 1.524)])
        snapshot = self.prepare_extended()
        request = snapshot["request"]
        ref = request["receivers"][0]["ground_ref"]
        self.assertEqual(ref, dict(position=[100 * snapshot["horizontal_to_m"], 0, 14]))
        self.assertNotIn("inference_points", request["terrain"])
        self.assertEqual(request["sources"][0]["ground_ref"], dict(position=[0, 0, 12]))
        self.assertEqual(request["receivers"][0]["height_m"], 1.524)
        resolved_z = 14 + 1.524
        result = dict(method="ISO 9613-2:2024 / ISO 9613-1:1993", profile="transport-fixture", limitations=[],
                      **{key: deepcopy(request[key]) for key in ("atmosphere", "ground", "settings")}, area_receivers=[],
                      receivers=[dict(receiver_id=request["receivers"][0]["id"], position=[100 * snapshot["horizontal_to_m"], 0, resolved_z],
                          ground_elevation_m=14, height_m=resolved_z - 14,
                          sources=[dict(source_id=request["sources"][0]["id"], **fields())], **fields())])
        save_completed_calculation(self.store, snapshot, result, BACKEND, JOB)
        geometry = geometry_from_snapshot(self.store.receiver_results(self.variant_id)[0]["metadata"]["geometry_gpkg_hex"])
        self.assertEqual(geometry.GetZ(), 14)
        self.assertAlmostEqual(geometry.GetM(), 1.524)

    def test_barrier_average_height_is_resolved_before_transport(self):
        self.scene()
        self.feature("barrier", [(20, 10, 10, 99), (30, 10, 14, 99)],
                     height_mode="average", height_value=8)
        barrier = self.prepare()["request"]["barriers"][0]
        self.assertEqual(barrier["heights_m"], [10, 6])

    def test_extended_line_power_and_bent_reflecting_barrier_keep_physical_objects(self):
        self.store.save_spectrum(self.spectrum_id, "Line power", spectrum_definition("sound_power", [80] * 8, power_basis="per_metre"))
        self.feature("line_source", [(0, 0, 12, 2), (10, 0, 13, 3)])
        self.feature("point_receiver", [(100, 0, 12, 1.5)])
        self.feature("barrier", [(40, -10, 12, 3), (40, 10, 13, 4), (50, 15, 14, 5)], reflection_coefficient=0)
        request = self.prepare_extended()["request"]
        self.assertEqual(len(request["sources"]), 1)
        self.assertEqual(request["sources"][0]["power_basis"], "power_per_m")
        self.assertEqual(request["sources"][0]["geometry"]["vertices"],
                         [dict(ground_ref=dict(position=[0, 0, 12]), height_m=2),
                          dict(ground_ref=dict(position=[10, 0, 13]), height_m=3)])
        self.assertEqual(request["barriers"][0]["role"], "screen_and_reflector")
        self.assertEqual(request["barriers"][0]["heights_m"], [3, 4, 5])
        self.assertEqual(request["barriers"][0]["absorption"], [1] * 8)
        self.assertEqual(request["reflectors"], [])

    def test_directivity_and_fresnel_are_not_silently_discarded(self):
        self.feature("point_source", [(0, 0, 12, 2)], directivity_id=new_id())
        self.feature("point_receiver", [(100, 0, 12, 2)])
        with self.assertRaisesRegex(ModelError, "directivity"):
            self.prepare()
        self.update_feature(self.store.layers()[0], directivity_id=None)
        settings = self.store.calculation()["settings"]
        self.assertEqual(self.prepare()["request"]["settings"]["ground_method"], "general")

    def test_area_mesh_power_basis_planarity_and_explicit_model_settings(self):
        self.store.save_spectrum(self.spectrum_id, "Power", spectrum_definition("sound_power", [80] * 8, power_basis="per_square_metre"))
        self.feature("area_source", [(0, 0, 12, 2), (4, 0, 12, 3), (4, 4, 12, 3), (0, 4, 12, 2), (0, 0, 12, 2)])
        self.feature("point_receiver", [(100, 0, 12, 2)])
        self.assertEqual(self.prepare()["request"]["settings"]["area_relative_tolerance"], 0.001)
        snapshot = self.prepare(dict(area_relative_tolerance=0.002))
        source = snapshot["request"]["sources"][0]
        self.assertEqual(source["power_basis"], "power_per_m2")
        self.assertEqual(source["spectrum"], [80] * 8)
        self.assertEqual(len(source["geometry"]["triangles"]), 2)
        self.assertEqual({tuple(v["ground_ref"]["position"][:2] + [v["ground_ref"]["position"][2] + v["height_m"]]) for v in source["geometry"]["vertices"]}, {(0, 0, 14), (4, 0, 15), (4, 4, 15), (0, 4, 14)})
        with self.assertRaisesRegex(ModelError, "positive"):
            self.prepare(dict(area_relative_tolerance=0))

    def test_nonplanar_area_is_rejected_without_picking_a_diagonal(self):
        self.feature("area_source", [(0, 0, 12, 2), (4, 0, 12, 2), (4, 4, 12, 3), (0, 4, 12, 2), (0, 0, 12, 2)])
        self.feature("point_receiver", [(100, 0, 12, 2)])
        with self.assertRaisesRegex(ModelError, "nonplanar"):
            self.prepare({})

    def test_rectangle_material_precedence_explicit_zero_and_legacy_search_override(self):
        self.scene()
        self.feature("barrier", [(40, -10, 12, 3), (40, 10, 12, 3)], reflection_coefficient=0)
        self.assertEqual(self.prepare()["request"]["settings"]["max_reflect_order"], 3)
        self.assertEqual(self.prepare({})["request"]["barriers"][0]["absorption"], [1] * 8)
        material = self.store.add_spectrum("Unknown material", spectrum_definition("absorption", [None] * 8))
        layer = self.store.layers()[-1]
        self.update_feature(layer, absorption_spectrum_id=material)
        settings = self.store.calculation()["settings"]
        settings["reflection_order"] = 0
        self.store.save_calculation(settings)
        with self.assertRaisesRegex(ModelError, "unknown bands"):
            self.prepare()

    def test_area_receiver_ignores_legacy_perimeter_ground_and_saves_engine_raster(self):
        self.feature("point_source", [(0, 0, 12, 2)])
        layer, parent_id = self.feature("area_receiver", [(20, 0, -500, 90), (30, 0, 800, 30), (30, 10, 900, 40), (20, 0, -500, 90)],
                                        height_m=4, spacing_m=10)
        snapshot = self.prepare()
        area = snapshot["request"]["area_receivers"][0]
        self.assertEqual(area["height_m"], 4)
        self.assertEqual(area["boundary"], [[20, 0], [30, 0], [30, 10]])
        result = self.result(snapshot)
        result["area_receivers"], downloads = area_result(
            snapshot, self.directory.name, {area["id"]: 3},
            {(area["id"], 0, 0): 41.5, (area["id"], 1, 0): 42.5, (area["id"], 1, 1): 43.5})
        original = deepcopy(result)
        saved = save_completed_calculation(self.store, snapshot, result, BACKEND, JOB, downloads)
        self.assertEqual(result, original)
        self.assertEqual(saved["receiver_count"], 3)
        # Samples live in the raster, not in result rows.
        self.assertEqual(self.store.receiver_results(self.variant_id), [])
        run = self.store.latest_calculation_run(self.variant_id)
        raster, = run["metadata"]["area_rasters"]
        self.assertEqual((raster["layer_id"], raster["object_id"], raster["receiver_count"]), (layer["layer_id"], parent_id, 3))
        path = results_directory(self.store) / raster["path"]
        self.assertEqual(path.parent.name, run["run_id"])
        dataset = gdal.Open(str(path))
        reference = osr.SpatialReference()
        reference.ImportFromEPSG(26913)
        self.assertTrue(osr.SpatialReference(dataset.GetProjection()).IsSame(reference))
        self.assertEqual(dataset.GetGeoTransform(), (15, 10, 0, 15, 0, -10))
        self.assertEqual([dataset.GetRasterBand(i).GetNoDataValue() for i in (1, 9)], [NODATA, NODATA])
        dataset = None
        area_object, = grouped_results(self.store, run, [])
        surface = AreaSurface(area_object, 1)
        self.assertEqual(surface.sample_count, 3)
        x, y, sample = surface.sample_at(29, 9)
        self.assertEqual((x, y, sample["broadband_a_db"], sample["octave_levels"][7]), (30, 10, 43.5, 43.5))
        self.assertEqual(sample["metadata"], dict(grid_column=1, grid_row=1))
        self.assertIsNone(surface.sample_at(21, 9))  # Inside the grid, outside the triangle.
        # A service must advertise the opt-in before the adapter requests it.
        self.assertNotIn("output", snapshot["request"])
        capabilities = deepcopy(CAPABILITIES)
        capabilities["features"].append("area_receiver_totals")
        compact_snapshot = prepare_calculation(self.store, self.variant_id, capabilities)
        self.assertEqual(compact_snapshot["request"]["output"], {"area_source_contributions": False})
        # Recalculation replaces the previous run's folder.
        result["area_receivers"], downloads = area_result(snapshot, self.directory.name, {area["id"]: 0})
        save_completed_calculation(self.store, snapshot, result, BACKEND, JOB, downloads)
        self.assertFalse(path.parent.exists())
        self.assertEqual(len(list(results_directory(self.store).iterdir())), 1)

    def test_area_results_reject_samples_missing_or_altered_rasters_without_files(self):
        self.feature("point_source", [(0, 0, 12, 2)])
        self.feature("area_receiver", [(20, 0, 0, 0), (30, 0, 0, 0), (30, 10, 0, 0), (20, 0, 0, 0)], height_m=4, spacing_m=10)
        snapshot = self.prepare()
        area = snapshot["request"]["area_receivers"][0]
        result = self.result(snapshot)
        result["area_receivers"], downloads = area_result(snapshot, self.directory.name, {area["id"]: 3})
        legacy = deepcopy(result)
        legacy["receivers"].append(dict(receiver_id="area:x", position=[20, 0, 16], area_receiver_id=area["id"],
                                        grid_column=0, grid_row=0, **fields()))
        with self.assertRaisesRegex(ModelError, "per-sample area results"):
            save_completed_calculation(self.store, snapshot, legacy, BACKEND, JOB, downloads)
        legacy = deepcopy(result)
        del legacy["area_receivers"][0]["raster"]
        with self.assertRaisesRegex(ModelError, "did not return an area raster"):
            save_completed_calculation(self.store, snapshot, legacy, BACKEND, JOB, downloads)
        moved = deepcopy(result)
        moved["area_receivers"][0]["raster"]["geotransform"][0] += 1
        with self.assertRaisesRegex(ModelError, "disagrees with the submitted grid"):
            save_completed_calculation(self.store, snapshot, moved, BACKEND, JOB, downloads)
        with self.assertRaisesRegex(ModelError, "missing or differs"):
            save_completed_calculation(self.store, snapshot, result, BACKEND, JOB, {})
        Path(downloads[area["id"]]["path"]).write_bytes(b"tampered")
        with self.assertRaisesRegex(ModelError, "checksum"):
            save_completed_calculation(self.store, snapshot, result, BACKEND, JOB, downloads)
        self.assertEqual(self.store.calculation_runs(), [])
        self.assertEqual(list(results_directory(self.store).iterdir()), [])

    def test_area_raster_is_scaled_to_project_units(self):
        snapshot_raster = dict(columns=3, rows=2, geotransform=[0.3048 * 9.5, 0.3048, 0, 0.3048 * 21.5, 0, -0.3048])
        staged = Path(self.directory.name) / "staged.tif"
        dataset = gdal.GetDriverByName("GTiff").Create(str(staged), 3, 2, 9, gdal.GDT_Float32)
        dataset.SetGeoTransform(snapshot_raster["geotransform"])
        dataset = None
        snapshot_raster["sha256"] = hashlib.sha256(staged.read_bytes()).hexdigest()
        final = Path(self.directory.name) / "final.tif"
        _finalize_area_raster(staged, final, snapshot_raster, 0.3048, self.store.metadata()["crs_wkt"])
        transform = gdal.Open(str(final)).GetGeoTransform()
        for value, expected in zip(transform, (9.5, 1, 0, 21.5, 0, -1)):
            self.assertAlmostEqual(value, expected, places=9)

    def test_pathway_warnings_complete_the_run_and_are_reported_by_name(self):
        self.scene()
        snapshot = self.prepare()
        result = self.result(snapshot)
        receiver = result["receivers"][0]
        source_id = snapshot["request"]["sources"][0]["id"]
        message = f"Source {source_id}, receiver {receiver['receiver_id']}, reflection faces []: unresolved"
        receiver.update(complete=False, warnings=[dict(kind="failed_pathway", code="calculation_failed",
                                                       source_id=source_id, bounded=False, message=message)])
        summary = save_completed_calculation(self.store, snapshot, result, BACKEND, JOB)
        lines = summary["pathway_warnings"]
        self.assertIn("1 receiver(s) are incomplete", lines[0])
        self.assertIn("levels may be understated", lines[0])
        # Engine IDs become the authored object names in the example.
        self.assertNotIn(source_id, lines[1])
        self.assertIn("unresolved", lines[1])
        run = self.store.latest_calculation_run(self.variant_id)
        self.assertEqual(run["warnings"][:len(lines)], lines)
        self.assertEqual(run["warnings"][len(lines):], ["No acoustic reference claim"])
        saved = self.store.receiver_results(self.variant_id)[0]["metadata"]
        self.assertEqual((saved["complete"], saved["warnings"][0]["kind"]), (False, "failed_pathway"))
        for bad in (dict(complete=False, warnings=[]), dict(complete="no"),
                    dict(warnings=[dict(kind="other", bounded=True, message="x")]),
                    dict(warnings=[dict(kind="failed_pathway", bounded="no", message="x")])):
            invalid = self.result(snapshot)
            invalid["receivers"][0].update(bad)
            with self.subTest(bad=bad), self.assertRaises(ModelError):
                save_completed_calculation(self.store, snapshot, invalid, BACKEND, JOB)
        clean = save_completed_calculation(self.store, snapshot, self.result(snapshot), BACKEND, JOB)
        self.assertEqual(clean["pathway_warnings"], [])

    def test_area_totals_option_does_not_allow_missing_point_contributions(self):
        self.scene()
        snapshot = self.prepare()
        snapshot["request"]["output"] = {"area_source_contributions": False}
        result = self.result(snapshot)
        result["receivers"][0].pop("sources")
        with self.assertRaisesRegex(ModelError, "source contribution"):
            save_completed_calculation(self.store, snapshot, result, BACKEND, JOB)

    def test_completed_silence_and_failed_cancelled_bad_results_preserve_previous_run(self):
        self.scene()
        snapshot = self.prepare()
        summary = save_completed_calculation(self.store, snapshot, self.result(snapshot, silent=True), BACKEND, JOB)
        self.assertEqual(self.store.receiver_results(self.variant_id)[0]["octave_levels"], [None] * 8)
        for state in ("failed", "cancelled", "running"):
            with self.subTest(state=state), self.assertRaisesRegex(ModelError, "completed"):
                save_completed_calculation(self.store, snapshot, self.result(snapshot), BACKEND, dict(state=state))
        bad = self.result(snapshot)
        bad["receivers"][0]["sources"][0]["source_id"] = "unsubmitted-source"
        with self.assertRaisesRegex(ModelError, "identities"):
            save_completed_calculation(self.store, snapshot, bad, BACKEND, JOB)
        self.assertEqual(self.store.latest_calculation_run(self.variant_id)["run_id"], summary["run_id"])

    def test_changed_model_cannot_replace_prior_results(self):
        self.scene()
        snapshot = self.prepare()
        original = save_completed_calculation(self.store, snapshot, self.result(snapshot), BACKEND, JOB)
        self.store.touch()
        with self.assertRaisesRegex(ModelError, "changed during"):
            save_completed_calculation(self.store, snapshot, self.result(snapshot), BACKEND, JOB)
        self.assertEqual(self.store.latest_calculation_run(self.variant_id)["run_id"], original["run_id"])

    def test_result_omitting_explicit_receiver_cannot_replace_prior_results(self):
        self.scene()
        snapshot = self.prepare()
        result = self.result(snapshot)
        result["receivers"] = []
        with self.assertRaisesRegex(ModelError, "omitted"):
            save_completed_calculation(self.store, snapshot, result, BACKEND, JOB)
        self.assertEqual(self.store.calculation_runs(), [])

    def test_valid_empty_area_replaces_previous_results_and_retains_area_definition(self):
        self.scene()
        before = self.prepare()
        old = save_completed_calculation(self.store, before, self.result(before), BACKEND, JOB)
        layer, parent = self.feature("area_receiver", [(10, 0, 999, 100), (20, 10, 999, 100), (0, 10, 999, 100), (10, 0, 999, 100)],
                                     height_m=2, spacing_m=100)
        # Only bounding-box candidate (0,0) lies outside the triangle.
        self.store.save_variant(self.variant_id, "Empty area", definition([self.layers[0], layer["layer_id"]]))
        snapshot = self.prepare()
        result = self.result(snapshot)
        self.assertEqual(result["receivers"], [])
        result["area_receivers"], downloads = area_result(snapshot, self.directory.name,
                                                          {snapshot["request"]["area_receivers"][0]["id"]: 0})
        current = save_completed_calculation(self.store, snapshot, result, BACKEND, JOB, downloads)
        self.assertNotEqual(old["run_id"], current["run_id"])
        self.assertEqual(self.store.receiver_results(self.variant_id), [])
        areas = grouped_results(self.store, self.store.latest_calculation_run(self.variant_id), [])
        self.assertEqual(len(areas), 1)
        self.assertEqual(areas[0]["object_id"], parent)
        self.assertEqual(areas[0]["samples"], [])
        self.assertEqual(AreaSurface(areas[0], 1).sample_count, 0)

    def test_changed_input_detected_even_if_external_writer_removes_touch_trigger(self):
        self.scene()
        snapshot = self.prepare()
        with self.store.connection() as db:
            db.execute("DROP TRIGGER acalc_touch_acalc_calculation_update")
            settings = self.store.calculation()["settings"]
            settings["temperature_c"] = 20
            db.execute("UPDATE acalc_calculation SET settings_json=?", (json.dumps(settings),))
        self.assertEqual(self.store.metadata()["modified_at"], snapshot["project_modified_at"])
        with self.assertRaisesRegex(ModelError, "changed during"):
            save_completed_calculation(self.store, snapshot, self.result(snapshot), BACKEND, JOB)

    def test_invalid_band_states_levels_and_edition_echo_are_rejected(self):
        self.scene()
        snapshot = self.prepare()
        cases = (lambda result: result.update(method="ISO 9613-2:1996"),
                 lambda result: result.update(profile=None),
                 lambda result: result.update(limitations=[{"invalid": "not text"}]),
                 lambda result: result["ground"].update(elevation_m=0),
                 lambda result: result["receivers"][0]["octave_db"].__setitem__(0, None),
                 lambda result: result["receivers"][0]["octave_db"].__setitem__(0, float("nan")),
                 lambda result: result["receivers"].__setitem__(0, None))
        for mutation in cases:
            result = self.result(snapshot)
            mutation(result)
            with self.assertRaises(ModelError):
                save_completed_calculation(self.store, snapshot, result, BACKEND, JOB)
        self.assertEqual(self.store.calculation_runs(), [])

    def test_nonbreaking_api_minor_version_is_accepted(self):
        self.scene()
        capabilities = {**CAPABILITIES, "api_version": "1.1", "future_response_field": {"anything": True}}
        snapshot = prepare_calculation(self.store, self.variant_id, capabilities)
        save_completed_calculation(self.store, snapshot, self.result(snapshot), BACKEND, JOB)
        self.assertEqual(self.store.latest_calculation_run(self.variant_id)["metadata"]["api_version"], "1.1")

    def test_model_ground_method_pruning_and_integration_reach_api_and_persist(self):
        self.scene()
        settings = self.store.calculation()["settings"]
        settings.update(ground_method="mean_path_height", pruning_strategy="dbc",
                        area_relative_tolerance=0.002, area_max_extent_m=8, reflection_order=5)
        self.store.save_calculation(settings)
        reloaded = ModelStore(self.store.path).calculation()["settings"]
        self.assertEqual(reloaded, settings)
        caps = dict(extended_capabilities(), ground_methods=["general", "mean_path_height"], max_reflect_order=5,
                    pruning_strategies=["dba", "dbc"])
        snapshot = prepare_calculation(self.store, self.variant_id, caps)
        wire = snapshot["request"]["settings"]
        for key in ("ground_method", "pruning_strategy",
                    "area_relative_tolerance", "area_max_extent_m"):
            self.assertEqual(wire[key], settings[key])
        self.assertIsNone(wire["minimum_contribution_db"])
        # An API without automatic pruning is refused rather than run unpruned.
        with self.assertRaisesRegex(ModelError, "automatic pruning"):
            prepare_calculation(self.store, self.variant_id, dict(caps, pruning_strategies=["dba"]))
        self.assertEqual(wire["max_reflect_order"], 5)
        self.assertNotIn("reflection_search_distance_m", wire)
        self.assertNotIn("subdivision_tolerance_db", wire)
        self.assertNotIn("lateral_diffraction", wire)
        self.assertNotIn("pruning_values", wire)
        self.assertNotIn("receiver_margin_db", wire)
        self.assertEqual(snapshot["request"]["applicability"]["assessment_quantity"], "a_weighted")

    def test_remembered_pruning_basis_does_not_enable_api_pruning(self):
        self.scene()
        settings = self.store.calculation()["settings"]
        settings["pruning_values"].update(pruning_strategy="dbc")
        self.store.save_calculation(settings)
        self.assertEqual(ModelStore(self.store.path).calculation()["settings"], settings)
        wire = prepare_calculation(self.store, self.variant_id, CAPABILITIES)["request"]["settings"]
        self.assertNotIn("pruning_values", wire)
        self.assertNotIn("receiver_margin_db", wire)
        self.assertNotIn("pruning_strategy", wire)
        self.assertIsNone(wire["minimum_contribution_db"])


if __name__ == "__main__":
    unittest.main()
