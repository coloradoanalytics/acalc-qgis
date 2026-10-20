# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Persistent, read-only map projections of the latest results for each receiver layer and variant.

Saved calculation results are authoritative. These projections never enter
the model layer registry or change its input revision. Current area results
are the saved engine GeoTIFFs themselves; their records use a ``file:`` key in
place of a GeoPackage table name.
"""

import json
import struct
import uuid

from osgeo import ogr, osr

from .decibels import rounded_db
from .schema import ModelError
from .area_results import (AreaSurface, BAND_NAMES, area_table_name, grouped_results, owned_band_tables,
                           prune_result_rasters, remove_run_rasters, write_projection)


BANDS = (63, 125, 250, 500, 1000, 2000, 4000, 8000)


def _point(blob):
    """Inspect a saved GeoPackage point without changing its coordinates."""
    if len(blob) < 8 or blob[:3] != b"GP\x00":
        raise ModelError("Invalid saved receiver geometry.")
    envelope = (blob[3] >> 1) & 7
    if envelope > 4:
        raise ModelError("Invalid saved receiver geometry envelope.")
    offset = 8 + (0, 32, 48, 48, 64)[envelope]
    geometry = ogr.CreateGeometryFromWkb(blob[offset:])
    if geometry is None or geometry.GetGeometryName() != "POINT" or geometry.IsEmpty():
        raise ModelError("Saved receiver geometry must be a point.")
    srs_id = struct.unpack("<i" if blob[3] & 1 else ">i", blob[4:8])[0]
    return srs_id, geometry.GetX(), geometry.GetY()


def result_table_name(variant_id, receiver_layer_id):
    return "acalc_results_" + uuid.UUID(variant_id).hex + "_" + uuid.UUID(receiver_layer_id).hex


def _result_tables(db):
    """Identify only owned projection tables, never arbitrary GeoPackage data."""
    records = {}
    for row in db.execute("SELECT table_name,description FROM gpkg_contents"):
        try:
            record = json.loads(row["description"])
            if not isinstance(record, dict):
                continue
            version = record.get("acalc_result_layer")
            if version in (1, 2):
                expected = "acalc_results_" + uuid.UUID(record["variant_id"]).hex
            elif version in (3, 4):
                expected = result_table_name(record["variant_id"], record["receiver_layer_id"])
            elif version == 5:
                expected = area_table_name(record["variant_id"], record["receiver_layer_id"], record["area_receiver_id"])
            else:
                continue
            # Read tables produced by the earlier snapshot implementation.
            if record.get("snapshot_id"):
                expected += "_" + uuid.UUID(record["snapshot_id"]).hex
            if row["table_name"] == expected:
                records[expected] = record
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
    return records


def _raster_records(run, variant_name, layers):
    """Records for a run's engine-delivered area GeoTIFFs (no GeoPackage tables)."""
    metadata = run["metadata"]
    areas = {(a["layer_id"], a["object_id"]): a for a in metadata.get("area_receivers") or []}
    saved_layers = {layer["layer_id"]: layer["name"] for layer in metadata.get("receiver_layers") or []}
    records = []
    for raster in metadata.get("area_rasters") or []:
        area_name = areas.get((raster["layer_id"], raster["object_id"]), {}).get("name") or "Area"
        layer_name = layers.get(raster["layer_id"], {}).get("name") or saved_layers.get(raster["layer_id"], "Receiver layer")
        records.append({"table_name": "file:" + raster["path"], "raster_file": raster["path"],
                        "variant_id": run["variant_id"], "receiver_layer_id": raster["layer_id"],
                        "area_receiver_id": raster["object_id"], "result_type": "area",
                        "raster_bands": list(BAND_NAMES), "area_name": area_name,
                        "name": "Results · " + variant_name + " · " + layer_name + " · " + area_name,
                        "variant_name": variant_name, "run_id": run["run_id"], "stale": run["stale"],
                        "finished_at": run["finished_at"]})
    return records


def _variant_name(run, variants):
    return variants.get(run["variant_id"], {}).get("name", run["variant_name"] + " (deleted variant)")


def result_layer_tables(store):
    with store.connection(readonly=True) as db:
        tables = _result_tables(db)
    variants = {v["variant_id"]: v for v in store.variants()}
    layers = {row["layer_id"]: row for row in store.layers()}
    for run in store.calculation_runs():
        for record in _raster_records(run, _variant_name(run, variants), layers):
            tables[record["table_name"]] = record
    return tables


def clear_variant_results(store, variant_id, *, projection_tables=None):
    """Atomically delete a variant's saved calculations and owned projections.

    The runs' area GeoTIFF folders are removed after the deletion commits.
    """
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        tables = [table for table, record in _result_tables(db).items()
                  if record["variant_id"] == variant_id
                  and (projection_tables is None or table in projection_tables)]
        runs = [row[0] for row in db.execute("SELECT run_id FROM acalc_calculation_runs WHERE variant_id=?", (variant_id,))]
        db.execute("DELETE FROM acalc_receiver_results WHERE run_id IN "
                   "(SELECT run_id FROM acalc_calculation_runs WHERE variant_id=?)", (variant_id,))
        db.execute("DELETE FROM acalc_calculation_runs WHERE variant_id=?", (variant_id,))
        for table in tables:
            _drop_projection(db, table)
    remove_run_rasters(store, runs)
    return tables


def _drop_projection(db, table):
    # These disposable tables contain derived results, never authored inputs.
    for child in owned_band_tables(db, table):
        _drop_projection(db, child)
    db.execute(f'DROP TABLE IF EXISTS "{table}"')
    db.execute(f'DROP TABLE IF EXISTS "rtree_{table}_geom"')
    for metadata, column in (("gpkg_2d_gridded_tile_ancillary", "tpudt_name"),
                             ("gpkg_2d_gridded_coverage_ancillary", "tile_matrix_set_name"),
                             ("gpkg_tile_matrix", "table_name"), ("gpkg_tile_matrix_set", "table_name")):
        if db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (metadata,)).fetchone():
            db.execute(f'DELETE FROM "{metadata}" WHERE "{column}"=?', (table,))
    for metadata in ("gpkg_geometry_columns", "gpkg_extensions", "gpkg_data_columns", "gpkg_metadata_reference", "gpkg_contents"):
        if db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (metadata,)).fetchone():
            db.execute(f'DELETE FROM "{metadata}" WHERE table_name=?', (table,))


def _receiver_layers(run, results, layers, variants):
    """Group by stable source-layer identity, independently of receiver type."""
    included = {}
    snapshot = run["metadata"].get("receiver_layers")
    if snapshot is not None:
        if not isinstance(snapshot, list):
            raise ModelError("Invalid saved receiver layer membership.")
        for layer in snapshot:
            try:
                layer_id = str(uuid.UUID(layer["layer_id"]))
                if not isinstance(layer["name"], str) or layer_id in included:
                    raise ValueError("invalid receiver layer")
                included[layer_id] = layer["name"]
            except (ValueError, KeyError, TypeError, AttributeError) as error:
                raise ModelError("Invalid saved receiver layer membership.") from error
    elif not run["stale"]:
        # Older runs did not snapshot empty layers. Only infer their membership
        # while the saved model still matches the calculated revision.
        for layer_id in variants.get(run["variant_id"], {}).get("definition", {}).get("layer_ids", []):
            layer = layers.get(layer_id)
            if layer and layer["feature_type"] in ("point_receiver", "area_receiver", "measurement"):
                included[layer_id] = layer["name"]
    grouped = {layer_id: [] for layer_id in included}
    for result in results:
        grouped.setdefault(result["layer_id"], []).append(result)
    return [(layer_id, layers.get(layer_id, {}).get("name", included.get(layer_id, "Receiver layer " + layer_id)), values)
            for layer_id, values in grouped.items()]


def sync_result_layers(store, *, prune=True):
    """Atomically project each receiver-layer/variant pair from its latest run.

    Split legacy combined projections, retaining any recovered positions. Drop
    obsolete derived projections only after all replacements have succeeded.
    Map publication disables pruning to keep other result layers usable.
    """
    records = []
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        variants = {v["variant_id"]: v for v in store.variants()}
        layers = {row["layer_id"]: dict(row) for row in db.execute("SELECT * FROM acalc_layers")}
        previous_tables = _result_tables(db)
        geometries = {}
        runs = store.calculation_runs()
        for run in runs:
            variant_id = run["variant_id"]
            variant_name = _variant_name(run, variants)
            legacy_table = "acalc_results_" + uuid.UUID(variant_id).hex
            groups = _receiver_layers(run, store.receiver_results(run_id=run["run_id"]), layers, variants)
            for receiver_layer_id, receiver_name, results in groups:
                kind = layers.get(receiver_layer_id, {}).get("feature_type")
                saved_layers = run["metadata"].get("receiver_layers") or []
                kind = next((r.get("feature_type") for r in saved_layers if r["layer_id"] == receiver_layer_id), kind)
                if kind == "area_receiver" and "area_rasters" in run["metadata"]:
                    # The engine's GeoTIFFs are the layers; nothing is projected.
                    records.extend(record for record in _raster_records(run, variant_name, layers)
                                   if record["receiver_layer_id"] == receiver_layer_id)
                    continue
                if kind == "area_receiver" or any(r["metadata"].get("area_receiver_id") for r in results):
                    for area in grouped_results(store, run, results):
                        if not area.get("is_area") or area["layer_id"] != receiver_layer_id:
                            continue
                        table = area_table_name(variant_id, receiver_layer_id, area["object_id"])
                        record = {"table_name": table, "variant_id": variant_id, "receiver_layer_id": receiver_layer_id,
                                  "area_receiver_id": area["object_id"], "result_type": "area",
                                  "raster_bands": list(BAND_NAMES),
                                  "area_name": area["receiver_name"],
                                  "name": "Results · " + variant_name + " · " + receiver_name + " · " + area["receiver_name"],
                                  "variant_name": variant_name, "run_id": run["run_id"], "stale": run["stale"],
                                  "finished_at": run["finished_at"]}
                        records.append(record)
                        previous = previous_tables.get(table)
                        if previous == {"acalc_result_layer": 5, **record}:
                            continue
                        if not previous and db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                            raise ModelError("The area result table is already used by another dataset.")
                        surface = AreaSurface(area, store.metadata()["horizontal_to_m"])
                        if previous:
                            _drop_projection(db, table)
                        write_projection(db, store, record, surface)
                    continue
                table = result_table_name(variant_id, receiver_layer_id)
                record = {"table_name": table, "variant_id": variant_id, "receiver_layer_id": receiver_layer_id,
                          "name": "Results · " + variant_name + " · " + receiver_name,
                          "variant_name": variant_name,
                          "run_id": run["run_id"], "stale": run["stale"], "level_digits": 1}
                records.append(record)
                description = json.dumps({"acalc_result_layer": 4, **record}, sort_keys=True)
                previous = db.execute("SELECT description FROM gpkg_contents WHERE table_name=?", (table,)).fetchone()
                exists = db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone()
                if exists and table not in previous_tables:
                    raise ModelError("The result layer table is already used by another dataset.")
                if exists and previous and previous[0] == description:
                    continue
                saved_geometry = {}
                for prior_table in (legacy_table, result_table_name(variant_id, receiver_layer_id), table):
                    prior = previous_tables.get(prior_table, {})
                    if prior.get("run_id") == run["run_id"]:
                        saved_geometry.update({(r[0], r[1]): (r[2], r[3]) for r in db.execute(
                            f'SELECT layer_id,object_id,geom,geometry_source FROM "{prior_table}" WHERE layer_id=? AND geom IS NOT NULL',
                            (receiver_layer_id,))})
                rows, positions, srs_ids = [], [], set()
                for result in results:
                    snapshot = result["metadata"].get("geometry_gpkg_hex")
                    blob, origin = None, "unavailable"
                    if snapshot:
                        try:
                            blob, origin = bytes.fromhex(snapshot), "calculation snapshot"
                        except (TypeError, ValueError) as error:
                            raise ModelError("Invalid saved receiver geometry.") from error
                    elif (result["layer_id"], result["object_id"]) in saved_geometry:
                        blob, origin = saved_geometry[result["layer_id"], result["object_id"]]
                    elif not run["stale"]:
                        layer_id = result["layer_id"]
                        if layer_id not in geometries:
                            source = db.execute("SELECT table_name FROM acalc_layers WHERE layer_id=?", (layer_id,)).fetchone()
                            geometries[layer_id] = {}
                            if source:
                                # Registry identifiers are validated by ModelStore.open.
                                source_table = source[0].replace('"', '""')
                                for feature in db.execute(f'SELECT object_id,geom FROM "{source_table}"'):
                                    geometries[layer_id][str(uuid.UUID(feature[0]))] = feature[1]
                        blob = geometries[layer_id].get(result["object_id"])
                        if blob:
                            origin = "matching model revision"
                    if blob:
                        srs_id, x, y = _point(blob)
                        srs_ids.add(srs_id)
                        positions.append((x, y))
                    rows.append((blob, run["run_id"], variant_id, result["layer_id"], result["object_id"],
                                 result["receiver_name"],
                                 *(rounded_db(level) if level is not None else None for level in
                                   (result["broadband_a_db"], *result["octave_levels"])),
                                 int(run["stale"]), origin))
                if len(srs_ids) > 1:
                    raise ModelError("Receiver result geometries use different coordinate systems.")
                # Models with only unlocated legacy results may not have an SRS row yet.
                if not srs_ids:
                    wkt = store.metadata()["crs_wkt"]
                    srs = db.execute("SELECT srs_id FROM gpkg_spatial_ref_sys WHERE definition=?", (wkt,)).fetchone()
                    if srs:
                        srs_ids.add(srs[0])
                    else:
                        reference = osr.SpatialReference(wkt)
                        srs_id = max(100000, db.execute("SELECT MAX(srs_id)+1 FROM gpkg_spatial_ref_sys").fetchone()[0])
                        db.execute("INSERT INTO gpkg_spatial_ref_sys(srs_name,srs_id,organization,organization_coordsys_id,definition) VALUES(?,?,'NONE',?,?)",
                                   (reference.GetName() or "Model CRS", srs_id, srs_id, wkt))
                        srs_ids.add(srs_id)
                srs_id = next(iter(srs_ids))
                if exists and previous and previous[0] != description and previous_tables[table].get("acalc_result_layer") != 4:
                    _drop_projection(db, table)
                octave_columns = ",".join(f"lz_{band}_db REAL" for band in BANDS)
                db.execute(f'''CREATE TABLE IF NOT EXISTS "{table}" (
                    fid INTEGER PRIMARY KEY, geom POINT, run_id TEXT NOT NULL, variant_id TEXT NOT NULL,
                    layer_id TEXT NOT NULL, object_id TEXT NOT NULL, receiver_name TEXT NOT NULL,
                    broadband_a_db REAL, {octave_columns},
                    out_of_date INTEGER NOT NULL, geometry_source TEXT NOT NULL,
                    UNIQUE(layer_id,object_id))''')
                db.execute(f'DELETE FROM "{table}"')
                placeholders = ",".join("?" for _ in range(17))
                db.executemany(f'INSERT INTO "{table}" VALUES(NULL,{placeholders})', rows)
                extent = (min(p[0] for p in positions), min(p[1] for p in positions),
                          max(p[0] for p in positions), max(p[1] for p in positions)) if positions else (None,) * 4
                db.execute("""INSERT INTO gpkg_contents(table_name,data_type,identifier,description,last_change,min_x,min_y,max_x,max_y,srs_id)
                    VALUES(?,'features',?,?,?,?,?,?,?,?) ON CONFLICT(table_name) DO UPDATE SET
                    description=excluded.description,last_change=excluded.last_change,min_x=excluded.min_x,min_y=excluded.min_y,
                    max_x=excluded.max_x,max_y=excluded.max_y,srs_id=excluded.srs_id""",
                           (table, table, description, run["finished_at"], *extent, srs_id))
                db.execute("""INSERT INTO gpkg_geometry_columns(table_name,column_name,geometry_type_name,srs_id,z,m)
                    VALUES(?,'geom','POINT',?,1,1) ON CONFLICT(table_name,column_name) DO UPDATE SET srs_id=excluded.srs_id""",
                           (table, srs_id))
        desired = {record["table_name"] for record in records}
        if prune:
            for table in previous_tables.keys() - desired:
                if not previous_tables[table].get("snapshot_id"):
                    _drop_projection(db, table)
    # Folders of replaced or cleared runs that could not be removed earlier.
    prune_result_rasters(store, {run["run_id"] for run in runs})
    return records


def configure_result_layer(layer):
    """Refresh field aliases without changing the layer's presentation."""
    layer.setFieldAlias(layer.fields().indexFromName("broadband_a_db"), "dBA")
    for band in BANDS:
        layer.setFieldAlias(layer.fields().indexFromName(f"lz_{band}_db"), f"{band} Hz (dB)")


def load_result_layer(store, record):
    if record.get("result_type") == "area":
        from .area_results import load_surface_layer
        return load_surface_layer(store, record)
    from qgis.core import (QgsMarkerSymbol, QgsPalLayerSettings, QgsSingleSymbolRenderer,
                           QgsVectorLayer, QgsVectorLayerSimpleLabeling)
    layer = QgsVectorLayer(f"{store.path}|layername={record['table_name']}", record["name"], "ogr")
    if not layer.isValid():
        raise ModelError(f"Could not load {record['name']}.")
    layer.setRenderer(QgsSingleSymbolRenderer(QgsMarkerSymbol.createSimple({
        "name": "circle", "color": "255,255,255,0", "outline_color": "#7b3294", "outline_width": "0.6", "size": "5"})))
    layer.setDisplayExpression('"receiver_name"')
    configure_result_layer(layer)
    labels = QgsPalLayerSettings()
    labels.isExpression = True
    labels.fieldName = '''CASE WHEN "broadband_a_db" IS NULL THEN '−∞' ELSE format_number("broadband_a_db", 1) END || ' dBA' '''
    layer.setLabeling(QgsVectorLayerSimpleLabeling(labels))
    layer.setLabelsEnabled(True)
    return layer


def result_layer_key(record):
    key = (record["variant_id"], record["receiver_layer_id"])
    return key + (record["area_receiver_id"],) if record.get("result_type") == "area" else key
