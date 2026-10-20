# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Schema-11 GeoPackage boundary for the normalized calculation API v1.

This adapter supplies geometry and transport bookkeeping, not acoustic formulas.
The retained snapshot is sufficient to audit the exact submitted model. Unsupported
selected effects fail conversion rather than disappearing from a request.
"""

from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math
import re
import struct
import uuid

from osgeo import ogr, osr

from .building_faces import _normal, _triangulate
from .calculation_settings import ISO_ENGINE
from .schema import ModelError, SCHEMA_VERSION, SOURCE_TYPES, new_id
from .spectra import OCTAVE_BANDS, compatible_power, validate_spectrum
from .storage import now


from .api_diagnostics import named_diagnostic
from .api_parameters import NUMERICAL_DEFAULTS, validate_numerical_settings

API_DEFAULT_SETTINGS = NUMERICAL_DEFAULTS.copy()


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _revision(db, tables):
    """Detect input changes even if an external writer bypassed touch triggers."""
    rows = {}
    for table in tables:
        if not re.fullmatch(r"acalc_[a-z_]+|model_[a-f0-9]{32}", table):
            raise ModelError("Invalid input revision table.")
        rows[table] = sorted(_json({key: value.hex() if isinstance(value, bytes) else value
                                   for key, value in dict(row).items()})
                             for row in db.execute(f'SELECT * FROM "{table}"'))
    return hashlib.sha256(_json(rows).encode()).hexdigest()


def _number(value, label, minimum=None, maximum=None):
    try:
        valid = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid or minimum is not None and value < minimum or maximum is not None and value > maximum:
        raise ModelError(f"{label}: invalid finite numeric value.")
    return value


def _geometry(blob, kind, srs_id, label):
    if not isinstance(blob, bytes) or len(blob) < 8 or blob[:3] != b"GP\x00":
        raise ModelError(f"{label}: invalid GeoPackage geometry.")
    flags = blob[3]
    envelope = flags >> 1 & 7
    if envelope > 4 or flags & 0x30 or struct.unpack("<i" if flags & 1 else ">i", blob[4:8])[0] != srs_id:
        raise ModelError(f"{label}: invalid geometry flags or coordinate reference.")
    geometry = ogr.CreateGeometryFromWkb(blob[8 + (0, 32, 48, 48, 64)[envelope]:])
    expected = "POINT" if kind in ("point_source", "point_receiver", "terrain_point") else "LINESTRING" if kind in ("barrier", "terrain_line", "line_source") else "POLYGON"
    if geometry is None or geometry.IsEmpty() or geometry.GetGeometryName() != expected:
        raise ModelError(f"{label}: expected a nonempty {expected.lower()}.")
    if kind not in ("ground_absorption", "trees") and (not geometry.Is3D() or kind not in ("terrain_point", "terrain_line") and not geometry.IsMeasured()):
        raise ModelError(f"{label}: geometry must preserve ground Z and acoustic height M.")
    if expected == "POLYGON":
        if geometry.GetGeometryCount() != 1:
            raise ModelError(f"{label}: polygon holes are not supported by this frontend API adapter.")
        if not geometry.IsValid():
            raise ModelError(f"{label}: polygon crosses or touches itself.")
        points = [geometry.GetGeometryRef(0).GetPointZM(i) for i in range(geometry.GetGeometryRef(0).GetPointCount())]
        if points and points[0] == points[-1]:
            points.pop()
        if len(points) < 3:
            raise ModelError(f"{label}: polygon needs at least three vertices.")
    else:
        points = [geometry.GetPointZM(i) for i in range(geometry.GetPointCount())]
    # Perimeter Z/M of receiver areas are deliberately non-authoritative.
    for point in points:
        for coordinate in point[:2] if kind == "area_receiver" else point:
            _number(coordinate, label + " coordinate")
    return points


def _mesh(points, label):
    """Triangulate only an unambiguous planar, simple XY polygon."""
    try:
        _, indices = _triangulate(points)
    except ModelError as error:
        raise ModelError(f"{label}: {str(error).replace('building', 'area source')}") from error
    normal = _normal(points)
    for point in points:
        distance = abs(sum(normal[i] * (point[i] - points[0][i]) for i in range(3)))
        if distance > 1e-8:
            raise ModelError(f"{label}: nonplanar emitting polygon has ambiguous triangulation; use planar area sources.")
    return [[points[index] for index in triangle] for triangle in indices]


def _spectrum(spectra, spectrum_id, label, kind):
    value = spectra.get(spectrum_id)
    if value is None:
        raise ModelError(f"{label}: assign a known spectrum.")
    validate_spectrum(value)
    if value["schema_version"] != 2 or value["kind"] != kind:
        raise ModelError(f"{label}: expected a version-2 {kind} spectrum.")
    if any(number is None for number in value["values"]):
        raise ModelError(f"{label}: assigned spectrum has unknown bands; resolve all eight values.")
    return value


def _settings(model, capabilities):
    # All numerical settings belong to the model, independent of connection.
    values = validate_numerical_settings({key: model[key] for key in NUMERICAL_DEFAULTS})
    maximum = capabilities.get("max_reflect_order", 0)
    if type(maximum) is not int or maximum < 0:
        raise ModelError("The API advertised an invalid max_reflect_order capability.")
    if model["reflection_order"] > maximum:
        raise ModelError("reflection_order exceeds the selected API capability.")
    method = model["ground_method"]
    if method not in capabilities.get("ground_methods", ["general"]):
        raise ModelError(f"ground_method: {method} is unsupported by the selected API.")
    values.update(max_reflect_order=model["reflection_order"], ground_method=method,
                  meteorological_mode=model["meteorology"],
                  c0=model["c0_db"] if model["meteorology"] == "long_term" else 0,
                  minimum_contribution_db=None)
    strategy = model["pruning_strategy"]
    if strategy is not None:
        if strategy not in capabilities.get("pruning_strategies", []):
            raise ModelError("Pruning: the selected API does not support automatic pruning; switch pruning off or choose another API.")
        values["pruning_strategy"] = strategy
    return values


def prepare_calculation(store, variant_id, capabilities, *, relationships=False):
    """Read one consistent model snapshot and normalize its entire selected scene."""
    if (not isinstance(capabilities.get("api_version"), str) or not re.fullmatch(r"1\.\d+", capabilities["api_version"])
            or capabilities.get("method") != "ISO 9613-2:2024"
            or capabilities.get("atmospheric_absorption") != "ISO 9613-1:1993"
            or capabilities.get("frequencies_hz") != list(OCTAVE_BANDS)):
        raise ModelError("This frontend supports API version 1, ISO 9613-2:2024 and ISO 9613-1:1993 with eight octave bands.")
    if relationships and "source_receiver_transfers" not in capabilities.get("features", []):
        raise ModelError("The selected calculation API does not support source-power back-calculation. Refresh its capabilities or choose an updated API.")
    from .api_scene import supports_schema11, prepare_scene
    if not supports_schema11(capabilities):
        raise ModelError("The API must support the current schema-11 terrain construction profile.")
    with store.connection(readonly=True) as db:
        db.execute("BEGIN")
        project = dict(db.execute("SELECT * FROM acalc_project").fetchone())
        keep_integration_reports = json.loads(project["metadata_json"]).get("keep_integration_reports") is True
        if keep_integration_reports and "integration_reports" not in capabilities.get("features", []):
            raise ModelError("Project metadata requests full integration reports (keep_integration_reports=true), "
                             "but the selected API does not advertise integration_reports. "
                             "Choose an engine that supports it, or remove the flag or set it to false in Other metadata (JSON).")
        if project["schema_version"] != SCHEMA_VERSION:
            raise ModelError(f"The API adapter requires model schema {SCHEMA_VERSION}; reopen the model to migrate it.")
        srs = osr.SpatialReference()
        scale = project["horizontal_to_m"]
        if (srs.ImportFromWkt(project["crs_wkt"]) != 0 or not srs.IsProjected()
                or not math.isclose(_number(scale, "horizontal_to_m", 0), srs.GetLinearUnits(), rel_tol=1e-12)
                or scale <= 0 or project["vertical_unit"] != "m"):
            raise ModelError("Project CRS must have verified projected XY units and metre vertical units.")
        variant_row = db.execute("SELECT * FROM acalc_variants WHERE variant_id=?", (variant_id,)).fetchone()
        if variant_row is None:
            raise ModelError("The selected calculation variant no longer exists.")
        variant = dict(variant_row)
        definition = json.loads(variant["definition_json"])
        layers = {row["layer_id"]: dict(row) for row in db.execute("SELECT * FROM acalc_layers WHERE archived=0")}
        store.validate_definition(definition, set(layers))
        model_settings = store._read_calculation(db)["settings"]
        spectrum_rows = list(db.execute("SELECT spectrum_id,name,spectrum_json FROM acalc_spectra"))
        spectra = {row["spectrum_id"]: json.loads(row["spectrum_json"]) for row in spectrum_rows}
        spectrum_names = {row["spectrum_id"]: row["name"] for row in spectrum_rows}
        original_spectra = deepcopy(spectra)
        if relationships:
            if model_settings["ground_method"] != "general":
                raise ModelError("Back-calculation requires the general octave-band ground method.")
            # A finite reference is audible in every band, including unknown or
            # silent current values. The catalog and initial guesses are untouched.
            spectra = {identity: dict(value, values=[100.0] * 8)
                       if value["kind"] == "sound_power" else value
                       for identity, value in spectra.items()}
        selected, objects, provenance_rows, explicit_ground = [], [], {}, []
        measurements = []
        receiver_layers = []
        for layer_id in definition["layer_ids"]:
            layer = layers[layer_id]
            selected.append(layer)
            table, kind = layer["table_name"], layer["feature_type"]
            if not re.fullmatch(r"model_[a-f0-9]{32}", table):
                raise ModelError(f"{layer['name']}: unsupported feature table name.")
            crs = db.execute("SELECT g.srs_id,s.definition FROM gpkg_geometry_columns g JOIN gpkg_spatial_ref_sys s ON g.srs_id=s.srs_id WHERE g.table_name=?", (table,)).fetchone()
            layer_srs = osr.SpatialReference()
            if crs is None or layer_srs.ImportFromWkt(crs[1]) != 0 or not srs.IsSame(layer_srs):
                raise ModelError(f"{layer['name']}: layer CRS differs from project CRS.")
            if kind in ("point_receiver", "area_receiver", "measurement"):
                receiver_layers.append({key: layer[key] for key in ("layer_id", "name", "feature_type")})
            provenance_rows[layer_id] = []
            seen = set()
            for row in db.execute(f'SELECT * FROM "{table}" ORDER BY object_id,fid'):
                item = dict(row)
                provenance_rows[layer_id].append({key: value.hex() if isinstance(value, bytes) else value for key, value in item.items()})
                label = f"{layer['name']} / {item.get('name') or item.get('object_id')}"
                object_kind = "point_receiver" if kind == "measurement" else kind
                observed = None
                if relationships:
                    if kind == "measurement":
                        observed = [item.get(f"level_{band}_db") for band in OCTAVE_BANDS]
                        for value in observed:
                            if value is not None:
                                _number(value, label + " measured octave level")
                        object_kind = "point_receiver"
                    elif kind == "point_receiver":
                        # Their ground vertices still belong to the selected
                        # scene even though only Measurements are evaluated.
                        object_kind = "terrain_point"
                    elif kind == "area_receiver":
                        continue
                if object_kind not in ("point_source", "line_source", "area_source", "point_receiver", "area_receiver", "terrain_point", "terrain_line", "barrier", "building", "source_building", "ground_absorption", "trees"):
                    raise ModelError(f"{label}: unsupported feature type {kind}.")
                try:
                    object_id = str(uuid.UUID(item["object_id"]))
                except (ValueError, TypeError, AttributeError) as error:
                    raise ModelError(f"{label}: object ID must be a UUID.") from error
                if object_id in seen:
                    raise ModelError(f"{label}: duplicate object identity in layer.")
                seen.add(object_id)
                points = _geometry(item["geom"], "point_receiver" if kind == "measurement" else kind, crs[0], label)
                if kind == "barrier":
                    from .object_geometry import building_heights
                    mode = item.get("height_mode") or "individual"
                    if mode not in ("average", "individual", "absolute_individual"):
                        raise ModelError(f"{label}: invalid barrier height mode.")
                    points = [row[1:] for row in building_heights(
                        [(0, *point) for point in points], mode, item.get("height_value", 3))]
                if item.get("ground_mode") == "unresolved":
                    raise ModelError(f"{label}: enter explicit ground Z values or take elevations from a raster.")
                if kind not in ("area_receiver", "ground_absorption", "trees"):
                    explicit_ground.extend((point[2], label) for point in points)
                identity = f"{project['project_id']}/{layer_id}/{object_id}"
                if observed is not None:
                    if not any(value is not None for value in observed):
                        object_kind = "terrain_point"
                    else:
                        measurements.append(dict(id=identity, observed=observed))
                objects.append((object_kind, item, points, label, identity,
                                dict(layer_id=layer_id, layer_name=layer["name"], fid=item["fid"],
                                     object_id=object_id, name=item.get("name") or "", srs_id=crs[0],
                                     geometry_gpkg_hex=item["geom"].hex(), kind=kind)))
        raster_rows = [dict(row) for row in db.execute("SELECT * FROM acalc_elevation_rasters")]
        revision_tables = ["acalc_project", "acalc_calculation", "acalc_layers", "acalc_variants", "acalc_spectra"]
        revision_tables.extend(layer["table_name"] for layer in selected)
        model_revision = _revision(db, revision_tables)
    if not explicit_ground:
        raise ModelError("Cannot resolve ground: include at least one explicitly elevated selected object.")
    ground = explicit_ground[0][0]
    request = dict(sources=[], receivers=[], area_receivers=[], reflectors=[],
                   atmosphere={key: model_settings[key] for key in ("temperature_c", "relative_humidity_percent", "pressure_kpa")},
                   ground=dict(factor=model_settings["ground_factor"], elevation_m=ground))
    if relationships and not measurements:
        raise ModelError("Add Measurements with octave levels to this variant; a broadband-only value cannot fit octave sound power.")
    mapping, area_snapshots = prepare_scene(request, objects, scale, spectra, model_settings, capabilities)
    if relationships and not request["sources"]:
        raise ModelError("Include at least one source with an adjustable sound power spectrum in the selected variant.")
    if not request["sources"] or not (request["receivers"] or request["area_receivers"]):
        raise ModelError("Include at least one source and one point receiver or receiver area in the selected variant.")
    fit_inputs = None
    if relationships:
        assigned = {mapping[source["id"]]["spectrum_id"] for source in request["sources"]}
        fit_spectra = []
        for identity in sorted(assigned):
            value = original_spectra[identity]
            validate_spectrum(value)
            mode = value.get("back_calculation", "locked")
            if mode == "locked" and any(level is None for level in value["values"]):
                raise ModelError("Locked sound power spectra must have all eight values before back-calculation.")
            if mode == "adjustable" and any(level is None for level in value["initial_values"]):
                bands = ", ".join(str(band) for band, level in zip(OCTAVE_BANDS, value["initial_values"]) if level is None)
                raise ModelError(f"{spectrum_names[identity]}: enter initial sound power guesses for {bands} Hz before back-calculation.")
            fit_spectra.append(dict(id=identity, mode=mode, current=value["values"],
                                   initial=value.get("initial_values"), power_basis=value["power_basis"]))
        if not any(value["mode"] == "adjustable" for value in fit_spectra):
            raise ModelError("No adjustable sound power spectra are assigned.")
        fit_inputs = dict(spectra=fit_spectra, measurements=measurements,
                          sources=[dict(id=source["id"], spectrum_id=mapping[source["id"]]["spectrum_id"])
                                   for source in request["sources"]])
    request["settings"] = _settings(dict(model_settings, pruning_strategy=None) if relationships else model_settings, capabilities)
    if relationships:
        request["output"] = dict(source_power_transfers=True)
        request["settings"]["area_absolute_tolerance"] = 0
    if request["area_receivers"] and "area_receiver_totals" in capabilities.get("features", []):
        request["output"] = dict(area_source_contributions=False)
    if keep_integration_reports:
        request.setdefault("output", {})["integration_reports"] = "full"
    if request["settings"]["ground_method"] == "mean_path_height":
        request["applicability"] = dict(sound_character="not_pure_tone", ground_character="porous_or_predominantly_porous",
                                         assessment_quantity="a_weighted", evidence="User-selected mean path height ground method in Calculation Parameters")
    provenance = dict(project=project, variant=variant, layers=selected, objects=provenance_rows, spectra=original_spectra,
                      calculation_settings=model_settings, elevation_rasters=raster_rows,
                      terrain_preparation="Staged acalc_schema11 terrain from selected original ground vertices and breaklines; no horizontal reduction.")
    return dict(request=request, provenance=provenance, id_mapping=mapping, project_id=project["project_id"],
                project_modified_at=project["modified_at"], variant_id=variant_id, variant_name=variant["name"],
                horizontal_to_m=scale, receiver_layers=receiver_layers, area_receivers=area_snapshots,
                capabilities=deepcopy(capabilities), started_at=now(),
                model_revision=model_revision, revision_tables=revision_tables, relationship_inputs=fit_inputs,
                input_sha256=hashlib.sha256(_json(dict(request=request, provenance=provenance)).encode()).hexdigest())


def prepare_relationships(store, variant_id, capabilities):
    """Prepare the advertised homogeneous source-transfer operation."""
    return prepare_calculation(store, variant_id, capabilities, relationships=True)


def relationships_from_result(snapshot, result):
    """Validate a transfer result before exposing any coefficients to the fit.

    Reference spectra preserve each source's authored total/density power basis.
    Only complete, unpruned results can become a reusable linear transfer.
    """
    marker = {"reference_db": 100, "power_basis": "per_source_input", "pruning": "disabled"}
    if not isinstance(result, dict) or result.get("source_power_transfers") != marker:
        raise ModelError("The API did not return the requested source-power transfers.")
    request = snapshot["request"]
    if (not isinstance(result.get("method"), str) or "ISO 9613-2:2024" not in result["method"]
            or "ISO 9613-1:1993" not in result["method"]):
        raise ModelError("The API transfer result method does not match the submitted ISO editions.")
    for key in ("atmosphere", "ground", "settings"):
        if not isinstance(result.get(key), dict) or any(result[key].get(field) != value for field, value in request[key].items()):
            raise ModelError(f"The API transfer result {key} differs from the submitted settings.")
    if not isinstance(result.get("limitations"), list) or any(not isinstance(value, str) for value in result["limitations"]):
        raise ModelError("The API transfer limitations must be an array of text qualifications.")
    if result.get("area_receivers") != []:
        raise ModelError("Source-power transfers must contain only Measurement receivers.")
    receivers = result.get("receivers")
    expected = {receiver["id"]: receiver for receiver in request["receivers"]}
    if (not isinstance(receivers, list) or len(receivers) != len(expected)
            or any(not isinstance(receiver, dict) for receiver in receivers)
            or {receiver.get("receiver_id") for receiver in receivers} != set(expected)):
        raise ModelError("The API transfer result has missing or duplicate Measurement receivers.")
    source_ids = {source["id"] for source in request["sources"]}
    relationships = deepcopy(snapshot["relationship_inputs"])
    measurements = {item["id"]: item for item in relationships["measurements"]}

    def complete_levels(value, label):
        bands, _ = _levels(value, label)
        if (value.get("complete", True) is not True or value.get("warnings")
                or "cutoff_limited" in value["band_states"] or any(value["omitted_energy"])
                or any(increase != 0 for increase in value["omission_increase_db"])):
            raise ModelError(f"{label}: incomplete or omitted pathways cannot be used for back-calculation.")
        omission = value.get("omission")
        if omission is not None and (not isinstance(omission, dict)
                or omission.get("coverage_state") != ["none"] * 8
                or omission.get("upper_energy") != [0] * 8):
            raise ModelError(f"{label}: omitted or unbounded pathways cannot be used for back-calculation.")
        return bands

    for receiver in receivers:
        identity = receiver["receiver_id"]
        reference = snapshot["id_mapping"][identity]
        if receiver.get("position") != reference["position"] or receiver.get("area_receiver_id") is not None:
            raise ModelError("The API transfer receiver position differs from the Measurement.")
        if "terrain" in request:
            ground = _number(receiver.get("ground_elevation_m"), identity + " ground_elevation_m")
            height = _number(receiver.get("height_m"), identity + " height_m", 0)
            if (not math.isclose(receiver["position"][2], ground + height, abs_tol=1e-7, rel_tol=0)
                    or not math.isclose(height, reference["height_m"], abs_tol=1e-7, rel_tol=0)):
                raise ModelError("The API transfer receiver ground or height differs from the Measurement.")
        complete_levels(receiver, identity)
        _receiver_warnings(receiver, identity)
        contributions = receiver.get("sources")
        if (not isinstance(contributions, list) or len(contributions) != len(source_ids)
                or any(not isinstance(source, dict) for source in contributions)
                or {source.get("source_id") for source in contributions} != source_ids):
            raise ModelError("The API transfer source identities differ from the submitted model.")
        measurements[identity]["transfer_db"] = {
            source["source_id"]: [None if value is None else value - marker["reference_db"]
                                   for value in complete_levels(source, source["source_id"])]
            for source in contributions}
    relationships.update(project_id=snapshot["project_id"], project_modified_at=snapshot["project_modified_at"],
                         model_revision=snapshot["model_revision"], revision_tables=snapshot["revision_tables"],
                         variant_id=snapshot["variant_id"], standard=result["method"],
                         limitations=result["limitations"], input_sha256=snapshot["input_sha256"])
    return relationships


def _levels(value, label):
    bands = value.get("octave_db")
    states = value.get("band_states")
    if not isinstance(bands, list) or len(bands) != 8 or not isinstance(states, list) or len(states) != 8:
        raise ModelError(f"{label}: result needs eight octave levels and states.")
    for number, state in zip(bands, states):
        if state not in ("calculated", "cutoff_limited", "exact_zero") or (number is not None) != (state == "calculated"):
            raise ModelError(f"{label}: invalid level/state combination.")
        if number is not None:
            _number(number, label + " octave level")
    total = value.get("a_weighted_db")
    if (total is None) != all(number is None for number in bands):
        raise ModelError(f"{label}: broadband silence disagrees with octave levels.")
    if total is not None:
        _number(total, label + " broadband level")
    for field in ("omitted_energy", "omission_increase_db"):
        numbers = value.get(field)
        if not isinstance(numbers, list) or len(numbers) != 8:
            raise ModelError(f"{label}: missing {field}.")
        for number in numbers:
            if number is None and field == "omission_increase_db":
                continue
            _number(number, label + " " + field, 0)
    for state, omitted, increase in zip(states, value["omitted_energy"], value["omission_increase_db"]):
        if (state == "exact_zero" and (omitted != 0 or increase != 0)
                or state == "cutoff_limited" and (omitted <= 0 or increase is not None)
                or state == "calculated" and increase is None):
            raise ModelError(f"{label}: omission diagnostics disagree with the band state.")
    expected_state = "calculated" if any(number is not None for number in bands) else "cutoff_limited" if "cutoff_limited" in states else "exact_zero"
    if value.get("state") != expected_state:
        raise ModelError(f"{label}: inconsistent total state.")
    return bands, total


def _point_blob(srs_id, scale, position, ground):
    return (b"GP\x00\x01" + struct.pack("<iBI4d", srs_id, 1, 3001,
                                      position[0] / scale, position[1] / scale, ground, position[2] - ground)).hex()


def _broadband_z(bands):
    values = [value for value in bands if value is not None]
    if not values:
        return None
    peak = max(values)
    return _number(peak + 10 * math.log10(math.fsum(10 ** ((value - peak) / 10) for value in values)), "broadband Z result")


# Pathway problems the engine reports without stopping the run.
PATHWAY_WARNING_KINDS = {"failed_pathway", "unresolved_pathways_above_tolerance", "integration_tolerance_exceeded"}


def _receiver_warnings(receiver, identity):
    """Validate a receiver's pathway warnings and completeness flag."""
    warnings = receiver.get("warnings", [])
    if (not isinstance(warnings, list)
            or any(not isinstance(w, dict) or w.get("kind") not in PATHWAY_WARNING_KINDS
                   or not isinstance(w.get("message"), str) or not isinstance(w.get("bounded"), bool)
                   for w in warnings)):
        raise ModelError(f"{identity}: invalid pathway warnings.")
    complete = receiver.get("complete", True)
    if complete is not True and complete is not False:
        raise ModelError(f"{identity}: invalid receiver completeness.")
    if complete is False and all(w["bounded"] for w in warnings):
        raise ModelError(f"{identity}: an incomplete receiver must report its failed pathways.")
    return warnings


def pathway_warning_lines(receivers, snapshot, examples=3, areas=()):
    """Plain-language run warnings for pathway problems that did not stop it.

    Area grids report per-area digests (``sample_warnings``) instead of samples.
    """
    digests = [area["sample_warnings"] for area in areas if area.get("sample_warnings")]
    incomplete = sum(1 for r in receivers if r.get("complete") is False) + sum(d["incomplete_samples"] for d in digests)
    warnings = [w for r in receivers for w in r.get("warnings", [])]
    failed = sum(1 for w in warnings if w["kind"] == "failed_pathway") + sum(d["failed_pathways"] for d in digests)
    bounded = (sum(1 for r in receivers if any(w["bounded"] for w in r.get("warnings", [])))
               + sum(d["bounded_samples"] for d in digests))
    warnings += [w for d in digests for w in d["examples"]]
    lines = []
    if incomplete:
        lines.append(f"Completed with warnings: {incomplete} receiver(s) are incomplete. {failed} reflection pathway "
                     "calculation(s) failed and are excluded, so those levels may be understated.")
    if bounded:
        lines.append(f"{bounded} receiver(s) include unresolved pathways above the numerical tolerance; their "
                     "bound is included in the reported omitted energy (upper level).")
    seen = []
    for warning in warnings:
        text = named_diagnostic(warning["message"], snapshot)
        if text not in seen:
            seen.append(text)
    lines.extend("Example: " + text for text in seen[:examples])
    if len(seen) > examples:
        lines.append(f"{len(seen) - examples} further pathway warning(s) are kept with the receiver results.")
    return lines


AREA_RASTER_MEDIA_TYPE = "image/tiff"
AREA_RASTER_BANDS = ["dBA", *(f"{band} Hz" for band in OCTAVE_BANDS)]
AREA_RASTER_NODATA = -3.4028234663852886e38
MAX_AREA_RASTER_BYTES = 64 * 1024 * 1024


def _grid_axis_count(minimum, maximum, spacing):
    """Mirror the API grid rule, including its 1e-8 m upper-endpoint tolerance."""
    count = math.floor((maximum - minimum) / spacing) + 1
    following = minimum + count * spacing
    return count + 1 if maximum < following <= maximum + 1e-8 else count


def area_raster_downloads(snapshot, result):
    """Validate each area summary's grid and raster link before any download.

    Returns ``[(api_area_id, raster_descriptor)]`` in summary order.
    """
    if not isinstance(result, dict) or not isinstance(result.get("area_receivers"), list):
        raise ModelError("The API result must contain receiver-area summaries.")
    areas = {area["id"]: area for area in snapshot["request"].get("area_receivers", [])}
    summaries = result["area_receivers"]
    if (len(summaries) != len(areas) or any(not isinstance(area, dict) for area in summaries)
            or {area.get("area_receiver_id") for area in summaries} != set(areas)):
        raise ModelError("The API result has missing or duplicate receiver-area summaries.")
    downloads, names = [], set()
    for summary in summaries:
        identity = summary["area_receiver_id"]
        area = areas[identity]
        if (type(summary.get("receiver_count")) is not int or summary["receiver_count"] < 0
                or summary.get("spacing_m") != area["spacing_m"] or summary.get("height_m") != area["height_m"]):
            raise ModelError("The API receiver-area summary disagrees with its submitted definition.")
        raster = summary.get("raster")
        if not isinstance(raster, dict):
            raise ModelError("The API did not return an area raster. Update the calculation engine "
                             "to one that delivers receiver areas as rasters (API 1.2).")
        xs, ys = [point[0] for point in area["boundary"]], [point[1] for point in area["boundary"]]
        spacing = area["spacing_m"]
        columns, rows = _grid_axis_count(min(xs), max(xs), spacing), _grid_axis_count(min(ys), max(ys), spacing)
        transform = [min(xs) - spacing / 2, spacing, 0, min(ys) + (rows - 0.5) * spacing, 0, -spacing]
        if (raster.get("columns") != columns or raster.get("rows") != rows
                or raster.get("bands") != AREA_RASTER_BANDS or raster.get("data_type") != "Float32"
                or raster.get("media_type") != AREA_RASTER_MEDIA_TYPE or raster.get("nodata") != AREA_RASTER_NODATA
                or not isinstance(raster.get("geotransform"), list) or len(raster["geotransform"]) != 6
                or any(not math.isclose(_number(value, identity + " raster geotransform"), expected,
                                        rel_tol=1e-12, abs_tol=1e-9)
                       for value, expected in zip(raster["geotransform"], transform))):
            raise ModelError(f"{identity}: the API area raster disagrees with the submitted grid.")
        if (type(raster.get("bytes")) is not int or not 0 < raster["bytes"] <= MAX_AREA_RASTER_BYTES
                or not isinstance(raster.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", raster["sha256"])
                or not isinstance(raster.get("href"), str) or raster["href"] in names):
            raise ModelError(f"{identity}: the API area raster link is missing or invalid.")
        names.add(raster["href"])
        digest = summary.get("sample_warnings")
        if (not isinstance(digest, dict) or any(type(digest.get(key)) is not int or digest[key] < 0 for key in
                                                ("samples_with_warnings", "incomplete_samples", "bounded_samples", "failed_pathways"))
                or not isinstance(digest.get("examples"), list)
                or any(not isinstance(w, dict) or not isinstance(w.get("message"), str) for w in digest["examples"])):
            raise ModelError(f"{identity}: invalid area pathway-warning summary.")
        downloads.append((identity, raster))
    return downloads


def _finalize_area_raster(staged, final, raster, scale, crs_wkt):
    """Check the engine GeoTIFF, then georeference it in project units and CRS."""
    from osgeo import gdal
    with open(staged, "rb") as handle:
        digest = hashlib.sha256()
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != raster["sha256"]:
        raise ModelError("A downloaded area raster does not match the API checksum.")
    dataset = gdal.Open(str(staged))
    if (dataset is None or (dataset.RasterXSize, dataset.RasterYSize, dataset.RasterCount)
            != (raster["columns"], raster["rows"], 9)
            or any(dataset.GetRasterBand(i).DataType != gdal.GDT_Float32 for i in range(1, 10))):
        raise ModelError("A downloaded area raster has the wrong size, bands or data type.")
    x, size, _, y, _, _ = raster["geotransform"]
    width, height = raster["columns"] * size, raster["rows"] * size
    # API coordinates are metres; project XY are metres / horizontal_to_m.
    bounds = [x / scale, y / scale, (x + width) / scale, (y - height) / scale]
    output = gdal.Translate(str(final), dataset, format="GTiff", outputSRS=crs_wkt, outputBounds=bounds,
                            creationOptions=["COMPRESS=DEFLATE", "PREDICTOR=3", "TILED=YES"])
    dataset = None
    if output is None:
        raise ModelError("Could not save the area result raster.")
    output = None


def _scene_counts(request, mapping):
    """Count authored UI objects separately from their engine component sources."""
    source_objects = {(reference["layer_id"], reference["object_id"])
                      for reference in mapping.values()
                      if reference.get("kind") in SOURCE_TYPES}
    source_buildings = sum(mapping[building["id"]].get("kind") == "source_building"
                           for building in request.get("buildings", []))
    solids = len(request.get("buildings", []))
    return dict(source_count=len(source_objects), component_source_count=len(request["sources"]),
                source_building_count=source_buildings, solid_building_count=solids,
                building_count=solids - source_buildings,
                barrier_count=len(request["reflectors"]) + len(request.get("barriers", [])))


def save_completed_calculation(store, snapshot, result, backend, job, rasters=None):
    """Validate and atomically replace one variant's results after completion only.

    ``rasters`` maps each API area ID to its downloaded file ({"path", "bytes",
    "sha256"}). They are georeferenced into ``<model>_results/<run_id>/``,
    which is removed again unless the results transaction commits.
    """
    if job.get("state") != "completed":
        raise ModelError("Only a completed API calculation can replace stored results.")
    if not isinstance(result, dict) or not isinstance(result.get("receivers"), list) or not isinstance(result.get("area_receivers"), list):
        raise ModelError("The API result must contain receiver results and area summaries.")
    if not isinstance(result.get("profile"), str) or not result["profile"].strip():
        raise ModelError("The API result must identify its calculation profile.")
    warnings = result.get("limitations")
    if not isinstance(warnings, list) or any(not isinstance(warning, str) for warning in warnings):
        raise ModelError("The API result limitations must be an array of text qualifications.")
    request, mapping = snapshot["request"], snapshot["id_mapping"]
    if (not isinstance(result.get("method"), str) or "ISO 9613-2:2024" not in result["method"]
            or "ISO 9613-1:1993" not in result["method"]):
        raise ModelError("The API result method does not match the submitted ISO editions.")
    for key in ("atmosphere", "ground", "settings"):
        if not isinstance(result.get(key), dict) or any(result[key].get(field) != value for field, value in request[key].items()):
            raise ModelError(f"The API result {key} differs from the submitted settings.")
    source_ids = {source["id"] for source in request["sources"]}
    explicit = {receiver["id"]: receiver for receiver in request["receivers"]}
    downloads = area_raster_downloads(snapshot, result)
    rasters = rasters or {}
    for identity, raster in downloads:
        info = rasters.get(identity)
        if not isinstance(info, dict) or info.get("bytes") != raster["bytes"] or info.get("sha256") != raster["sha256"]:
            raise ModelError(f"{identity}: the downloaded area raster is missing or differs from the API result.")
    seen, rows = set(), []
    saved_receivers = []
    ground = request["ground"]["elevation_m"]
    for receiver in result["receivers"]:
        if not isinstance(receiver, dict):
            raise ModelError("The API receiver result must be an object.")
        identity = receiver.get("receiver_id")
        if not isinstance(identity, str) or identity in seen:
            raise ModelError("The API returned a missing or duplicate receiver identity.")
        seen.add(identity)
        bands, total = _levels(receiver, identity)
        position = receiver.get("position")
        if not isinstance(position, list) or len(position) != 3:
            raise ModelError(f"{identity}: invalid result position.")
        for number in position:
            _number(number, identity + " position")
        contributions = receiver.get("sources")
        if receiver.get("area_receiver_id") is not None:
            raise ModelError("The API returned per-sample area results. Update the calculation engine "
                             "to one that delivers receiver areas as rasters (API 1.2).")
        if (not isinstance(contributions, list) or len(contributions) != len(source_ids)
                or any(not isinstance(source, dict) for source in contributions)
                or {source.get("source_id") for source in contributions} != source_ids):
            raise ModelError(f"{identity}: source contribution identities differ from the submitted model.")
        for source in contributions or []:
            _levels(source, source["source_id"])
            if source.get("complete", True) not in (True, False):
                raise ModelError(f"{identity}: invalid source contribution completeness.")
        _receiver_warnings(receiver, identity)
        receiver_ground = ground
        if "terrain" in request:
            receiver_ground = _number(receiver.get("ground_elevation_m"), identity + " ground_elevation_m")
            height = _number(receiver.get("height_m"), identity + " height_m", 0)
            if not math.isclose(position[2], receiver_ground + height, abs_tol=1e-7, rel_tol=0):
                raise ModelError(f"{identity}: result elevation disagrees with its ground and height.")
        if identity not in explicit:
            raise ModelError(f"{identity}: explicit receiver identity/position differs from the submitted model.")
        reference = mapping[identity]
        expected = reference.get("position", explicit[identity].get("position"))
        if expected is None:
            expected = reference["position_xy"] + [receiver_ground + reference["height_m"]]
        if position != expected:
            raise ModelError(f"{identity}: explicit receiver identity/position differs from the submitted model.")
        if "terrain" in request and not math.isclose(receiver["height_m"], reference["height_m"], abs_tol=1e-7, rel_tol=0):
            raise ModelError(f"{identity}: receiver height differs from the submitted model.")
        object_id, name = reference["object_id"], reference["name"]
        # Only top-level metadata fields are added; nested contributions are
        # serialized immediately and never mutated.
        metadata = dict(receiver)
        saved_receivers.append(dict(metadata))
        metadata["geometry_gpkg_hex"] = _point_blob(reference["srs_id"], snapshot["horizontal_to_m"], position, receiver_ground)
        rows.append((reference["layer_id"], object_id, name, _json(bands), _broadband_z(bands), total, _json(metadata)))
    if not set(explicit) <= seen:
        raise ModelError("The API omitted an explicitly submitted receiver result.")
    # Run-specific pathway warnings precede the engine's standing limitations.
    pathway_warnings = pathway_warning_lines(result["receivers"], snapshot, areas=result["area_receivers"])
    warnings = pathway_warnings + warnings
    finished = now()
    elapsed = max(0, (datetime.fromisoformat(finished) - datetime.fromisoformat(snapshot["started_at"].replace("Z", "+00:00"))).total_seconds())
    implementation = backend.get("implementation") or snapshot["capabilities"].get("implementation", {})
    version = implementation.get("version", "unknown")
    counts = _scene_counts(request, mapping)
    metadata = dict(**counts, profile=result.get("profile"), backend=deepcopy(backend), api_job=deepcopy(job),
                    transport="http_json_v1", api_version=snapshot["capabilities"]["api_version"],
                    normalized_settings=deepcopy(result["settings"]),
                    api_snapshot=snapshot, api_result=dict(result, receivers=saved_receivers),
                    receiver_layers=snapshot["receiver_layers"], area_receivers=snapshot["area_receivers"],
                    coordinate_reference=snapshot["provenance"]["project"],
                    result_quantity="sound_pressure_level", octave_frequencies_hz=list(OCTAVE_BANDS),
                    octave_weighting="Z", unit="dB re 20 uPa", conformance="API transport; no acoustic conformity claim")
    run_id = new_id()
    from .area_results import raster_file_name, remove_run_rasters, results_directory
    area_rasters, area_samples = [], 0
    directory = results_directory(store) / run_id
    try:
        if downloads:
            directory.mkdir(parents=True)
        for identity, raster in downloads:
            reference = mapping[identity]
            relative = f"{run_id}/{raster_file_name(reference['name'], reference['object_id'])}"
            _finalize_area_raster(rasters[identity]["path"], results_directory(store) / relative, raster,
                                  snapshot["horizontal_to_m"], snapshot["provenance"]["project"]["crs_wkt"])
            summary = next(area for area in result["area_receivers"] if area["area_receiver_id"] == identity)
            area_samples += summary["receiver_count"]
            area_rasters.append(dict(api_area_receiver_id=identity, layer_id=reference["layer_id"],
                                     object_id=reference["object_id"], path=relative,
                                     receiver_count=summary["receiver_count"], columns=raster["columns"],
                                     rows=raster["rows"], engine_bytes=raster["bytes"], engine_sha256=raster["sha256"],
                                     sample_warnings=summary["sample_warnings"]))
        metadata["area_rasters"] = area_rasters
        previous = _commit_run(store, snapshot, result, backend, version, run_id, finished, rows,
                               area_samples, elapsed, warnings, metadata)
    except BaseException:
        remove_run_rasters(store, [run_id])
        raise
    remove_run_rasters(store, previous)
    return dict(run_id=run_id, engine=backend.get("engine_id", ISO_ENGINE), engine_version=version,
                standard=result.get("method"), profile=result.get("profile"), **counts,
                receiver_count=len(rows) + area_samples,
                ground_area_count=len(request["ground"].get("regions", [])), tree_area_count=len(request.get("misc_regions", [])),
                elapsed_seconds=elapsed, input_sha256=snapshot["input_sha256"], warnings=warnings,
                pathway_warnings=pathway_warnings)


def _commit_run(store, snapshot, result, backend, version, run_id, finished, rows,
                area_samples, elapsed, warnings, metadata):
    """Replace the variant's saved run; return the replaced run IDs."""
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute("SELECT project_id,modified_at FROM acalc_project").fetchone()
        if current["project_id"] != snapshot["project_id"] or current["modified_at"] != snapshot["project_modified_at"]:
            raise ModelError("Model inputs changed during calculation; previous results were preserved. Run again for the current revision.")
        if _revision(db, snapshot["revision_tables"]) != snapshot["model_revision"]:
            raise ModelError("Model inputs changed during calculation; previous results were preserved. Run again for the current revision.")
        previous = [row[0] for row in db.execute("SELECT run_id FROM acalc_calculation_runs WHERE variant_id=?", (snapshot["variant_id"],))]
        db.execute("DELETE FROM acalc_receiver_results WHERE run_id IN (SELECT run_id FROM acalc_calculation_runs WHERE variant_id=?)", (snapshot["variant_id"],))
        db.execute("DELETE FROM acalc_calculation_runs WHERE variant_id=?", (snapshot["variant_id"],))
        db.execute("""INSERT INTO acalc_calculation_runs
            (run_id,project_id,variant_id,variant_name,engine,engine_version,standard,started_at,finished_at,input_sha256,project_modified_at,status,settings_json,source_count,receiver_count,elapsed_seconds,warnings_json,metadata_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,'complete',?,?,?,?,?,?)""",
                   (run_id, snapshot["project_id"], snapshot["variant_id"], snapshot["variant_name"],
                    backend.get("engine_id", ISO_ENGINE), version, result.get("method", "ISO 9613-2:2024 / ISO 9613-1:1993"),
                    snapshot["started_at"], finished, snapshot["input_sha256"], snapshot["project_modified_at"],
                    _json(snapshot["provenance"]["calculation_settings"]), metadata["source_count"], len(rows) + area_samples, elapsed, _json(warnings), _json(metadata)))
        db.executemany("""INSERT INTO acalc_receiver_results
            (run_id,layer_id,object_id,receiver_name,octave_levels_json,broadband_z_db,broadband_a_db,metadata_json)
            VALUES (?,?,?,?,?,?,?,?)""", [(run_id,) + row for row in rows])
        db.execute("UPDATE gpkg_contents SET last_change=? WHERE table_name IN ('acalc_calculation_runs','acalc_receiver_results')", (finished,))
    return previous
