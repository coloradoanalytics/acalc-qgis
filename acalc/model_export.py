# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Variant exports. QSI uses absolute metre coordinates, never stored ZM verbatim.

The CadnaA 2021 sample establishes the DIN45687-QSI:2010-7.1 dialect.
See docs/CADNAA_EXPORT.md for field evidence and verification limits.
"""

from collections import defaultdict
import json
import math
from pathlib import Path
import re
import tempfile

from osgeo import ogr, osr

from .building_faces import read_face_sources
from .object_geometry import building_heights
from .schema import LAYER_TYPES, ModelError
from .spectra import compatible_power, validate_spectrum


FORMATS = (("qsi", "CadnaA 2021 — QSI model"),
           ("shp", "ESRI shapefiles — authored GIS layers"))
COMMON = [("NAME", "s", 40, 0), ("ID", "s", 15, 0)]
SOURCE = COMMON + [(name, typ, width, precision) for name, typ, width, precision in (
    ("LW", "r", 5, 1), ("L_WEIGHT", "s", 1, 0), ("LREF", "i", 1, 0),
    ("KO_F", "r", 4, 1), ("KO_W", "r", 4, 1), ("K_T", "r", 4, 1), ("K_I", "r", 4, 1),
    ("THIST_ID", "s", 15, 0), ("MEANFREQ", "i", 4, 0), ("SPECT_ID", "s", 15, 0), ("EMI_TYPE", "i", 1, 0))]
FIELDS = {
    "srcp": SOURCE, "srcl": SOURCE, "srca": SOURCE,
    "recv": COMMON + [("IO_NO", "i", 2, 0), ("DELTA_H", "i", 2, 0)],
    "bldg": COMMON + [("RL", "r", 5, 1), ("BLDG_H", "r", 6, 1), ("BLDG_HREF", "i", 1, 0),
                      ("DWELL_N", "r", 6, 1), ("RES_N", "r", 6, 1), ("USAGE", "i", 1, 0)],
    "barr": COMMON + [("RL_L", "r", 5, 1), ("RL_R", "r", 5, 1), ("BARR_H", "r", 6, 1), ("BARR_HREF", "i", 1, 0)],
    "hlin": COMMON, "hgpt": COMMON,
    "gabs": COMMON + [("G", "r", 5, 3)],
    "spec": COMMON + [("LOWERFREQ", "i", 4, 0), ("UPPERFREQ", "i", 4, 0), ("L_WEIGHT", "s", 1, 0),
                      ("SPEC_TYPE", "i", 1, 0)] + [(f"VALUE_{i}", "r", 5, 1) for i in range(18, 40, 3)],
    "dipa": COMMON + [("TH_REF", "i", 1, 0)] + [(f"VALUE_{i}", "r", 7, 2) for i in range(1, 25)],
}
KINDS = dict(point_source="srcp", line_source="srcl", area_source="srca", point_receiver="recv", measurement="recv",
             building="bldg", source_building="bldg", barrier="barr", terrain_line="hlin", terrain_point="hgpt", ground_absorption="gabs")


def snapshot_variant(store, variant_id):
    """One read transaction; exports do not need a configured calculation API."""
    with store.connection(readonly=True) as db:
        db.execute("BEGIN")
        variant = db.execute("SELECT * FROM acalc_variants WHERE variant_id=?", (variant_id,)).fetchone()
        if variant is None:
            raise ModelError("The selected variant no longer exists.")
        variant = dict(variant)
        definition = json.loads(variant.pop("definition_json"))
        layers = {r["layer_id"]: dict(r) for r in db.execute("SELECT * FROM acalc_layers WHERE archived=0")}
        store.validate_definition(definition, set(layers))
        selected = []
        project = dict(db.execute("SELECT * FROM acalc_project").fetchone())
        project_srs = osr.SpatialReference()
        if project_srs.ImportFromWkt(project["crs_wkt"]) != 0:
            raise ModelError("Invalid project coordinate system.")
        for identity in definition["layer_ids"]:
            layer = layers[identity]
            table = layer["table_name"]
            if not re.fullmatch(r"model_[a-f0-9]{32}", table):
                raise ModelError("Invalid model layer table.")
            crs = db.execute("SELECT g.srs_id,s.definition FROM gpkg_geometry_columns g "
                             "JOIN gpkg_spatial_ref_sys s ON g.srs_id=s.srs_id WHERE g.table_name=?", (table,)).fetchone()
            layer_srs = osr.SpatialReference()
            if crs is None or layer_srs.ImportFromWkt(crs[1]) != 0 or not project_srs.IsSame(layer_srs):
                raise ModelError(f"{layer['name']}: layer CRS differs from the project.")
            rows = [dict(row) for row in db.execute(f'SELECT * FROM "{table}" ORDER BY object_id,fid')]
            kind = LAYER_TYPES.get(layer["feature_type"])
            if kind is None:
                raise ModelError(f"{layer['name']}: unknown model layer type.")
            expected = {"Point": "POINT", "LineString": "LINESTRING", "Polygon": "POLYGON"}[kind.geometry]
            for item in rows:
                blob = item["geom"]
                if (not isinstance(blob, bytes) or len(blob) < 8 or blob[:3] != b"GP\x00"
                        or int.from_bytes(blob[4:8], "little" if blob[3] & 1 else "big", signed=True) != crs[0]):
                    raise ModelError(f"{layer['name']}: invalid geometry coordinate reference.")
                if _geometry(blob).GetGeometryName() != expected:
                    raise ModelError(f"{layer['name']}: expected {expected} geometry.")
            selected.append(dict(layer, objects=rows))
        return dict(project=project,
                    variant=dict(variant, definition=definition), layers=selected,
                    settings=store._read_calculation(db)["settings"],
                    spectra=[dict(row) for row in db.execute("SELECT * FROM acalc_spectra")],
                    directivities=[dict(row) for row in db.execute("SELECT * FROM acalc_directivities")],
                    elevation_rasters=[dict(row) for row in db.execute("SELECT * FROM acalc_elevation_rasters")])


def _geometry(blob):
    if not isinstance(blob, bytes) or blob[:3] != b"GP\x00" or len(blob) < 8:
        raise ModelError("Invalid GeoPackage geometry.")
    envelope = (blob[3] >> 1) & 7
    if envelope > 4:
        raise ModelError("Invalid GeoPackage geometry envelope.")
    result = ogr.CreateGeometryFromWkb(blob[8 + (0, 32, 48, 48, 64)[envelope]:])
    if result is None or result.IsEmpty() or not result.IsValid():
        raise ModelError("Empty or invalid export geometry.")
    return result


def _points(geometry):
    if geometry.GetGeometryName() == "POLYGON":
        if geometry.GetGeometryCount() != 1:
            raise ModelError("QSI export does not support polygon holes.")
        ring = geometry.GetGeometryRef(0)
        return [ring.GetPointZM(i) for i in range(ring.GetPointCount() - 1)]
    return [geometry.GetPointZM(i) for i in range(geometry.GetPointCount())]


def _shape(points, name):
    geometry = ogr.Geometry({"POINT": ogr.wkbPoint25D, "LINESTRING": ogr.wkbLineString25D,
                             "POLYGON": ogr.wkbPolygon25D}[name])
    target = ogr.Geometry(ogr.wkbLinearRing) if name == "POLYGON" else geometry
    for p in points:
        target.AddPoint(*p)
    if name == "POLYGON":
        target.AddPoint(*points[0])
        geometry.AddGeometry(target)
    return geometry


def _finite(value, label, minimum=None, maximum=None):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or minimum is not None and value < minimum or maximum is not None and value > maximum):
        raise ModelError(f"{label}: invalid numeric value.")
    return value


def _reflection(item, kind, settings):
    if item.get("absorption_spectrum_id"):
        raise ModelError("Assigned absorption spectra need a verified spectral QSI mapping.")
    rho = item.get("reflection_coefficient")
    rho = settings[kind + "_reflection_coefficient"] if rho is None else rho
    _finite(rho, "Reflection coefficient", 0, 1)
    # Zero-reflection sentinel from the published QSI definition. The vendor
    # sample also contains +200; its import meaning still needs verification.
    return -200 if rho == 0 else -10 * math.log10(rho)


def prepare_qsi(snapshot):
    """Prepare everything before opening any destination files."""
    project, settings = snapshot["project"], snapshot["settings"]
    srs = osr.SpatialReference()
    scale = project["horizontal_to_m"]
    if (srs.ImportFromWkt(project["crs_wkt"]) != 0 or not srs.IsProjected()
            or not math.isclose(_finite(scale, "XY metre scale", 0), srs.GetLinearUnits(), rel_tol=1e-12)
            or scale <= 0 or project["vertical_unit"] != "m"):
        raise ModelError("QSI needs a projected CRS with verified XY units and metre elevations.")
    spectra = {row["spectrum_id"]: json.loads(row["spectrum_json"]) for row in snapshot["spectra"]}
    groups, mapping, used = defaultdict(list), [], {}
    ground = {}
    skipped = []
    for layer in snapshot["layers"]:
        kind = layer["feature_type"]
        for item in layer["objects"]:
            label = f"{layer['name']} / {item.get('name') or item['object_id']}"
            try:
                if kind not in KINDS:
                    raise ModelError(f"{kind} needs a verified QSI mapping; it cannot yet be exported.")
                if item.get("ground_mode") == "unresolved":
                    raise ModelError("Enter explicit ground Z values or take elevations from a raster before export.")
                if item.get("directivity_id"):
                    raise ModelError("Assigned directivity needs a verified QSI mapping.")
                if kind in ("building", "source_building") and read_face_sources(item.get("face_sources")):
                    raise ModelError("Building-attached sources need a verified QSI mapping.")
                geometry = _geometry(item["geom"])
                points = _points(geometry)
                for p in points:
                    for v in p:
                        _finite(v, "Coordinate")
                if kind == "ground_absorption":
                    identity = f"O{len(mapping) + 1:014d}"
                    mapping.append(dict(id=identity, layer_id=layer["layer_id"], object_id=item["object_id"], name=item.get("name")))
                    polygon = _shape([(p[0] * scale, p[1] * scale, 0) for p in points], "POLYGON")
                    polygon.FlattenTo2D()
                    groups["gabs"].append((polygon, dict(NAME=(item.get("name") or identity)[:40], ID=identity,
                                                        G=_finite(item["ground_factor"], "Ground factor", 0, 1))))
                    continue
                if not geometry.Is3D() or not kind.startswith("terrain") and not geometry.IsMeasured():
                    raise ModelError("Ground Z and height M must be present.")
                if kind in ("building", "source_building", "barrier"):
                    rows = [(0, *p) for p in points]
                    points = [r[1:] for r in building_heights(rows, item.get("height_mode") or "individual", item["height_value"])]
                for p in points:
                    xy = (p[0] * scale, p[1] * scale)
                    if xy in ground and not math.isclose(ground[xy], p[2], abs_tol=1e-8, rel_tol=0):
                        raise ModelError("Conflicting explicit ground heights at the same XY.")
                    ground[xy] = p[2]
                identity = f"O{len(mapping) + 1:014d}"
                attributes = dict(NAME=(item.get("name") or identity)[:40], ID=identity)
                mapping.append(dict(id=identity, layer_id=layer["layer_id"], object_id=item["object_id"], name=item.get("name")))
                if kind.endswith("source"):
                    spec_id = item.get("spectrum_id")
                    spectrum = spectra.get(spec_id)
                    if spectrum is None:
                        raise ModelError("Assign a known sound-power spectrum.")
                    validate_spectrum(spectrum)
                    if (spectrum["schema_version"] != 2 or spectrum["kind"] != "sound_power"
                            or not compatible_power(spectrum, kind) or any(v is None for v in spectrum["values"])):
                        raise ModelError("Use a complete schema-2 sound-power spectrum with the correct power basis.")
                    if spec_id not in used:
                        used[spec_id] = f"S{len(used) + 1:014d}"
                        values = [-200 if v == 0 else v for v in spectrum["values"]]
                        if any(v <= -200 for v in spectrum["values"] if v != 0):
                            raise ModelError("Sound levels at or below the QSI silence sentinel (-200 dB) cannot transfer.")
                        groups["spec"].append((None, dict(NAME=used[spec_id], ID=used[spec_id], LOWERFREQ=63,
                            UPPERFREQ=8000, L_WEIGHT="", SPEC_TYPE=2,
                            **{f"VALUE_{i}": v for i, v in zip(range(18, 40, 3), values)})))
                    weighted = [v + a for v, a in zip(spectrum["values"], (-26.2, -16.1, -8.6, -3.2, 0, 1.2, 1, -1.1)) if v != 0]
                    peak = max(weighted) if weighted else -200
                    lw = peak + 10 * math.log10(sum(10 ** ((v - peak) / 10) for v in weighted)) if weighted else -200
                    attributes.update(LW=lw, L_WEIGHT="A", LREF=1 if spectrum["power_basis"] == "total" else 2,
                                      KO_F=0, KO_W=0, K_T=0, K_I=0, THIST_ID="CONTINUOUS", SPECT_ID=used[spec_id], EMI_TYPE=2)
                if kind in ("point_receiver", "measurement"):
                    attributes.update(IO_NO=1, DELTA_H=0)
                if kind in ("building", "source_building", "barrier"):
                    loss = _reflection(item, "building" if kind == "source_building" else kind, settings)
                    height = points[0][3]
                    if kind in ("building", "source_building"):
                        attributes.update(RL=loss, BLDG_H=height, BLDG_HREF=0, DWELL_N=0, RES_N=0, USAGE=0)
                    else:
                        attributes.update(RL_L=loss, RL_R=loss, BARR_H=height, BARR_HREF=0)
                xyz = [(p[0] * scale, p[1] * scale, p[2] if kind.startswith("terrain") else p[2] + _finite(p[3], "Height", 0)) for p in points]
                if kind in ("area_source", "building", "source_building"):
                    # A QSI polygon has no authored facet indices. A nonplanar
                    # perimeter could be triangulated differently by the importer.
                    from .api_model import _mesh
                    _mesh(xyz, label)
                groups[KINDS[kind]].append((_shape(xyz, geometry.GetGeometryName()), attributes))
            except (ModelError, RuntimeError, ValueError) as error:
                raise ModelError(f"{label}: {error}") from error
    if not mapping:
        raise ModelError("The variant contains no exportable model objects.")
    # All explicit ground vertices contribute to Acalc's terrain, including
    # source/receiver/building vertices. Preserve them as QSI height points.
    explicit_points = {(geometry.GetX(), geometry.GetY()) for geometry, _ in groups.get("hgpt", [])}
    if ground:
        groups["hgpt"].extend((_shape([(x, y, z)], "POINT"), dict(NAME="Ground", ID=f"G{i:014d}"))
                              for i, ((x, y), z) in enumerate(sorted(ground.items()), 1) if (x, y) not in explicit_points)
    if used:
        groups["dipa"].append((None, dict(NAME="Continuous operation", ID="CONTINUOUS", TH_REF=2,
                                         **{f"VALUE_{i}": 60 for i in range(1, 25)})))
    return groups, mapping, skipped


def _write_table(folder, name, fields, rows, srs=None):
    # The vendor's N5.1 spectrum fields cannot hold the published -200 silence
    # sentinel with a decimal. Widen only when needed instead of overflowing.
    fields = [(field, typ, max([width] + [len(f"{values[field]:.{precision}f}") for _, values in rows
               if typ != "s" and values.get(field) is not None]), precision)
              for field, typ, width, precision in fields]
    geometry_type = rows[0][0].GetGeometryType() if rows and rows[0][0] else ogr.wkbNone
    path = folder / (name + (".dbf" if geometry_type == ogr.wkbNone else ".shp"))
    dataset = ogr.GetDriverByName("ESRI Shapefile").CreateDataSource(str(path))
    if dataset is None:
        raise ModelError(f"Could not create {path.name}.")
    try:
        layer = dataset.CreateLayer(name, srs=srs, geom_type=geometry_type, options=["ENCODING=CP1252"])
        if layer is None:
            raise ModelError(f"Could not create {name}.")
        for field, typ, width, precision in fields:
            definition = ogr.FieldDefn(field, {"s": ogr.OFTString, "i": ogr.OFTInteger, "r": ogr.OFTReal}[typ])
            definition.SetWidth(width)
            definition.SetPrecision(precision)
            if layer.CreateField(definition) != 0:
                raise ModelError(f"Could not create export field {field}.")
        for geometry, values in rows:
            feature = ogr.Feature(layer.GetLayerDefn())
            for field, typ, width, precision in fields:
                value = values.get(field)
                if value is None:
                    continue
                if typ == "s":
                    try:
                        encoded = str(value).encode("cp1252")
                    except UnicodeEncodeError as error:
                        raise ModelError(f"{field}: CadnaA 2021 export requires Windows-1252 text.") from error
                    if len(encoded) > width:
                        raise ModelError(f"{field}: text exceeds the export field width.")
                elif not math.isfinite(value) or len(f"{value:.{precision}f}") > width:
                    raise ModelError(f"{field}: numeric value exceeds the export field width.")
                feature.SetField(field, value)
            if geometry is not None:
                feature.SetGeometry(geometry)
            if layer.CreateFeature(feature) != 0:
                raise ModelError(f"Could not write {name} object.")
            feature = None
        layer = None
    finally:
        dataset = None


def export_variant(store, variant_id, destination, format_id):
    """Publish a new directory only after every output has been written.

    Refuse an existing target: never leave mismatched sidecars from a prior export.
    """
    if format_id not in dict(FORMATS):
        raise ModelError("Choose a supported export format.")
    destination = Path(destination).expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise ModelError("Choose a new export folder; the destination already exists.")
    if not destination.parent.is_dir():
        raise ModelError("The export parent folder does not exist.")
    snapshot = snapshot_variant(store, variant_id)
    groups, mapping, skipped = prepare_qsi(snapshot) if format_id == "qsi" else ({}, [], [])
    with tempfile.TemporaryDirectory(prefix=".acalc-export-", dir=destination.parent) as temp:
        folder = Path(temp) / "model"
        folder.mkdir()
        if format_id == "qsi":
            for key, rows in sorted(groups.items()):
                _write_table(folder, "model_" + key, FIELDS[key], rows)
            lines = ["[Files]"] + [f"{key}=model_{key}" for key in sorted(groups)]
            lines += ["[Meta]", "fmtqsi=DIN45687-QSI:2010-7.1", "dnorth=0.00", "proj=No global Coordinate System"]
            (folder / "model.qsi").write_bytes(("\r\n".join(lines) + "\r\n").encode("ascii"))
        else:
            srs = osr.SpatialReference()
            srs.ImportFromWkt(snapshot["project"]["crs_wkt"])
            for i, layer in enumerate(snapshot["layers"], 1):
                filename = f"layer_{i:03d}_{layer['feature_type']}"
                rows = [(_geometry(item["geom"]), dict(NAME=(item.get("name") or "")[:40], ID=f"O{n:014d}"))
                        for n, item in enumerate(layer["objects"], 1)]
                if rows:
                    _write_table(folder, filename, COMMON, rows, srs)
                    mapping.extend(dict(file=filename + ".shp", id=f"O{n:014d}", layer_id=layer["layer_id"],
                                        object_id=item["object_id"], name=item.get("name"))
                                   for n, item in enumerate(layer["objects"], 1))
        # Full authored attributes/spectra/geometry are retained in the companion
        # snapshot. Shapefiles are geometry interchange, not a QSI acoustic model.
        (folder / "model_snapshot.json").write_text(json.dumps(snapshot, indent=2, ensure_ascii=True,
            allow_nan=False, default=lambda value: value.hex() if isinstance(value, bytes) else str(value)), encoding="utf-8")
        report = dict(format=format_id, variant=snapshot["variant"]["name"], id_mapping=mapping,
                      omitted_noncalculation_objects=skipped,
                      verification="Written with GDAL; import into CadnaA 2021 is not yet verified.",
                      comparison="Acalc targets ISO 9613-2:2024; CadnaA 2021 uses the 1996 edition.",
                      settings=snapshot["settings"], crs_wkt=snapshot["project"]["crs_wkt"],
                      horizontal_to_m=snapshot["project"]["horizontal_to_m"], vertical_datum=snapshot["project"]["vertical_datum"])
        (folder / "export_report.json").write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
        (folder / "IMPORT.txt").write_text(
            ("In CadnaA 2021 use File > Import > QSI and select model.qsi. Keep all companion files together.\n"
             "Coordinates and all elevations are metres. XY is the project's projected XY multiplied by horizontal_to_m.\n"
             "Set the coordinate system manually; a feet-based CRS needs a suitable metre-based frame before overlaying other data.\n"
             "Rebuild/check the DTM and check roof/barrier heights, reflection losses and spectra after import.\n"
             if format_id == "qsi" else
             "Authored GIS geometries only: ground Z and height M retain Acalc's storage meaning.\n"
             "These files are not a calculation-ready CadnaA model. Use QSI for acoustic transfer.\n"
             "Layer order and full attributes/spectra are in model_snapshot.json. XY retains the project CRS units.\n")
            + "Match weather, default ground factor, meteorology, reflection order and numerical settings manually using export_report.json.\n"
            "Different triangulation and ISO editions may produce different results. No acoustic validation or conformity is asserted.\n"
            "Import into CadnaA 2021 has not yet been verified.\n", encoding="utf-8")
        # A concurrent creator must not be overwritten, even if it made an empty directory.
        destination.mkdir()
        try:
            for file in folder.iterdir():
                file.rename(destination / file.name)
        except Exception:
            import shutil
            shutil.rmtree(destination)
            raise
    return destination
