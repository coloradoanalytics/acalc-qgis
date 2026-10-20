# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""QGIS presentation and geometry rules for model layers."""

import math

from qgis.core import (
    QgsDefaultValue, QgsEditorWidgetSetup, QgsFieldConstraints, QgsFillSymbol,
    QgsFeature, QgsGeometry, QgsLineString, QgsLineSymbol, QgsMarkerSymbol, QgsPalLayerSettings, QgsPoint,
    QgsPolygon, QgsSingleSymbolRenderer, QgsSpatialIndex, QgsUnitTypes, QgsVectorLayer, QgsVectorLayerSimpleLabeling, QgsWkbTypes,
)

from .schema import LAYER_TYPES
from .storage import ModelError
from .spectra import missing_bands, compatible_power
from .measurement import ALL_MEASUREMENT_FIELDS, MEASUREMENT_BROADBAND_FIELDS, resolved_measurement, validate_measurement
from .object_geometry import feature_value, GROUND_MODES, HEIGHT_MODES, building_heights
from .trees import DEFAULT_CANOPY_HEIGHT, canopy_height


def required_layer_fields(kind):
    fields = {"fid", "object_id", "name", "notes"}
    if kind.ground_points:
        fields.add("ground_mode")
    if kind.key == "ground_absorption":
        fields.add("ground_factor")
    if kind.key == "trees":
        fields.add("canopy_height")
    if kind.spectrum:
        fields.add("spectrum_id")
    if kind.key == "point_source":
        fields.update(("directivity_id", "directivity_orientation"))
    if kind.height_controls:
        fields.update(("height_mode", "height_value"))
    if kind.is_building:
        fields.add("face_sources")
    if (kind.is_building or kind.key == "barrier"):
        fields.update(("reflection_coefficient", "absorption_spectrum_id"))
    if kind.key == "area_receiver":
        fields.update(("height_m", "spacing_m"))
    if kind.key == "measurement":
        fields.update(ALL_MEASUREMENT_FIELDS)
    return fields


def refresh_layer_schema(layer, kind):
    """Refresh appended migration fields without replacing the live edit buffer."""
    required = required_layer_fields(kind)
    layer.dataProvider().reloadData()
    if (required <= set(layer.fields().names())
            and layer.fields().names() == layer.dataProvider().fields().names()):
        return
    buffer = layer.editBuffer()
    if buffer and (buffer.addedAttributes() or buffer.deletedAttributeIds()):
        raise ModelError(f"{layer.name()}: required fields are missing. Undo pending field changes before reopening this layer.")
    # Reload the provider's field cache, then rebuild the layer's field mapping.
    # Do not stop editing: the buffer, pending features and undo stack must survive.
    layer.dataProvider().reloadData()
    layer.updateFields()
    missing = required - set(layer.fields().names())
    if missing:
        raise ModelError(f"{layer.name()}: missing model fields: {', '.join(sorted(missing))}.")


def initialize_added_object_fields(layer, kind, *, own_edit_command=True):
    """Give pre-migration pending additions the new fields' database defaults.

    Callers may include initialization in an existing edit command by passing
    own_edit_command=False; they then own rollback if initialization fails.
    """
    buffer = layer.editBuffer()
    if buffer is None:
        return {}
    defaults = {"ground_mode": "per_point"} if kind.ground_points else {}
    if kind.key == "ground_absorption":
        defaults["ground_factor"] = 0.0
    if kind.key == "trees":
        defaults["canopy_height"] = DEFAULT_CANOPY_HEIGHT
    if kind.height_controls:
        defaults.update(height_mode="individual", height_value=3.0 if kind.key == "barrier" else 6.0)
    if kind.key == "area_receiver":
        defaults.update(height_m=1.5, spacing_m=10.0)
    changes, replacements = [], []
    for fid, original in buffer.addedFeatures().items():
        feature = layer.getFeature(fid)
        # Updating the layer field cache does not resize features retained by
        # QGIS's old Add Feature undo commands. Attribute changes beyond their
        # original length appear to succeed but are not stored. Re-add a full
        # snapshot through the edit buffer, retaining the stable object_id.
        replace = len(original.attributes()) < len(layer.dataProvider().fields())
        feature_defaults = dict(defaults)
        for name, default in feature_defaults.items():
            index = layer.fields().indexFromName(name)
            if index >= 0 and feature_value(feature, name) is None:
                if replace:
                    feature[name] = default
                else:
                    changes.append((fid, index, default))
        if replace:
            replacements.append((fid, feature))
    if not changes and not replacements:
        return {}
    selected = set(layer.selectedFeatureIds())
    remapped = {}
    if own_edit_command:
        layer.beginEditCommand("Acalc: initialize new object fields")
    try:
        for fid, feature in replacements:
            if not layer.deleteFeature(fid) or not layer.addFeature(feature):
                raise ModelError(f"{layer.name()}: could not update a pending object to the new model fields.")
            remapped[fid] = feature.id()
        for fid, index, value in changes:
            if not layer.changeAttributeValue(fid, index, value):
                raise ModelError(f"{layer.name()}: could not initialize the new object fields.")
        if own_edit_command:
            layer.endEditCommand()
    except Exception:
        if own_edit_command:
            layer.destroyEditCommand()
        raise
    if selected & remapped.keys():
        layer.selectByIds([remapped.get(fid, fid) for fid in selected])
    return remapped


def load_layer(store, record):
    layer = QgsVectorLayer(f"{store.path}|layername={record['table_name']}", record["name"], "ogr")
    if not layer.isValid():
        raise ModelError(f"Could not load layer {record['name']}.")
    configure_layer(layer, store, record)
    style_model_layer(layer, LAYER_TYPES[record["feature_type"]])
    return layer


def style_model_layer(layer, kind):
    """Apply initial presentation only when creating a new model layer."""
    if kind.geometry == "Point":
        symbol = QgsMarkerSymbol.createSimple({"name": "diamond" if kind.key == "point_receiver" else "triangle" if kind.key == "measurement" else "circle",
            "color": kind.color, "outline_color": "white", "outline_width": "0.35",
            "size": "3.0" if kind.key == "terrain_point" else "3.8"})
    elif kind.geometry == "LineString":
        symbol = QgsLineSymbol.createSimple({"line_color": kind.color,
            "line_width": "0.85" if kind.key == "barrier" else "0.55",
            "line_style": "dash" if kind.key == "terrain_line" else "solid"})
    else:
        fill = {"area_receiver": "36,106,196,55", "ground_absorption": "87,148,92,65",
                "trees": "45,113,61,95", "source_building": "184,107,53,95"}.get(kind.key, "173,158,139,125")
        symbol = QgsFillSymbol.createSimple({"color": fill, "outline_color": kind.color,
                                            "outline_width": "0.45"})
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    layer.setDisplayExpression('coalesce("name", \'Unnamed\')')
    if kind.key == "measurement":
        labels = QgsPalLayerSettings()
        labels.isExpression = True
        labels.fieldName = '''CASE WHEN "broadband_a_db" IS NOT NULL THEN format_number("broadband_a_db", 1) || ' dBA' END'''
        layer.setLabeling(QgsVectorLayerSimpleLabeling(labels))
        layer.setLabelsEnabled(True)


def configure_layer(layer, store, record):
    """Refresh model fields and editing rules without changing user styling."""
    kind = LAYER_TYPES[record["feature_type"]]
    refresh_layer_schema(layer, kind)
    layer.setCustomProperty("acalc/project_path", store.path)
    layer.setCustomProperty("acalc/layer_id", record["layer_id"])
    layer.setCustomProperty("acalc/feature_type", kind.key)
    fields = layer.fields()
    # Native QGIS forms and attribute-table editors should also require typing
    # or pasting; their numeric default would otherwise introduce wheel spinners.
    for index, field in enumerate(fields):
        if field.isNumeric():
            layer.setEditorWidgetSetup(index, QgsEditorWidgetSetup("TextEdit", {"IsMultiline": False}))
    form = layer.editFormConfig()
    for name in ("fid", "object_id"):
        index = fields.indexFromName(name)
        form.setReadOnly(index, True)
    layer.setEditFormConfig(form)
    layer.setDefaultValueDefinition(fields.indexFromName("object_id"), QgsDefaultValue("uuid()"))
    if kind.key == "point_source":
        for name in ("directivity_id", "directivity_orientation"):
            index = fields.indexFromName(name)
            layer.setDefaultValueDefinition(index, QgsDefaultValue("NULL"))
            layer.setFieldAlias(index, "Directionality (edit in Acalc properties)" if name == "directivity_id" else "Directionality orientation (edit in Acalc properties)")
            form = layer.editFormConfig()
            form.setReadOnly(index, True)
            layer.setEditFormConfig(form)
            if name == "directivity_orientation":
                layer.setEditorWidgetSetup(index, QgsEditorWidgetSetup("Hidden", {}))
    layer.setDefaultValueDefinition(fields.indexFromName("name"), QgsDefaultValue("'" + kind.singular + "'"))
    if kind.height_controls:
        layer.setDefaultValueDefinition(fields.indexFromName("height_mode"), QgsDefaultValue("'individual'"))
        layer.setDefaultValueDefinition(fields.indexFromName("height_value"), QgsDefaultValue("3" if kind.key == "barrier" else "6"))
        if kind.key == "building":
            index = fields.indexFromName("face_sources")
            layer.setDefaultValueDefinition(index, QgsDefaultValue("NULL"))
            layer.setEditorWidgetSetup(index, QgsEditorWidgetSetup("Hidden", {}))
            form = layer.editFormConfig()
            form.setReadOnly(index, True)
            layer.setEditFormConfig(form)
    if kind.ground_points:
        layer.setDefaultValueDefinition(fields.indexFromName("ground_mode"), QgsDefaultValue("'per_point'"))
        layer.setFieldAlias(fields.indexFromName("ground_mode"), "Ground points (edit in Acalc properties)")
        form = layer.editFormConfig()
        form.setReadOnly(fields.indexFromName("ground_mode"), True)
        layer.setEditFormConfig(form)
    if kind.key == "ground_absorption":
        index = fields.indexFromName("ground_factor")
        layer.setDefaultValueDefinition(index, QgsDefaultValue("0"))
        layer.setFieldAlias(index, "G")
        layer.setFieldConstraint(index, QgsFieldConstraints.ConstraintNotNull)
        layer.setConstraintExpression(index, '"ground_factor" >= 0 AND "ground_factor" <= 1', "G must be between 0 and 1")
    if kind.key == "trees":
        index = fields.indexFromName("canopy_height")
        layer.setDefaultValueDefinition(index, QgsDefaultValue(str(DEFAULT_CANOPY_HEIGHT)))
        layer.setFieldAlias(index, "Tree height (m)")
        layer.setFieldConstraint(index, QgsFieldConstraints.ConstraintNotNull)
        layer.setConstraintExpression(index, '"canopy_height" > 0', "Tree height must be positive")
    if (kind.is_building or kind.key == "barrier"):
        index = fields.indexFromName("reflection_coefficient")
        layer.setDefaultValueDefinition(index, QgsDefaultValue("NULL"))
        layer.setFieldAlias(index, "Stored surface coefficient (edit absorption in Acalc properties)")
        layer.setConstraintExpression(index, '"reflection_coefficient" IS NULL OR ("reflection_coefficient" >= 0 AND "reflection_coefficient" <= 1)', "Reflection coefficient must be NULL or between 0 and 1")
        # Keep the legacy rho storage field out of user-facing forms; Acalc
        # properties expose alpha and handle conversion at the storage boundary.
        layer.setEditorWidgetSetup(index, QgsEditorWidgetSetup("Hidden", {}))
        form = layer.editFormConfig()
        form.setReadOnly(index, True)
        layer.setEditFormConfig(form)
        index = fields.indexFromName("absorption_spectrum_id")
        layer.setDefaultValueDefinition(index, QgsDefaultValue("NULL"))
        layer.setFieldAlias(index, "Surface absorption spectrum (edit in Acalc properties)")
        form = layer.editFormConfig()
        form.setReadOnly(index, True)
        layer.setEditFormConfig(form)
    if kind.key == "area_receiver":
        for name, default, label, rule in (("height_m", "1.5", "Height above ground (m)", '"height_m" >= 0'),
                                           ("spacing_m", "10", "Point spacing (m)", '"spacing_m" > 0')):
            index = fields.indexFromName(name)
            layer.setDefaultValueDefinition(index, QgsDefaultValue(default))
            layer.setFieldAlias(index, label)
            layer.setFieldConstraint(index, QgsFieldConstraints.ConstraintNotNull)
            layer.setConstraintExpression(index, rule, label + " must be " + ("positive" if name == "spacing_m" else "non-negative"))
    if kind.key == "measurement":
        for name in ALL_MEASUREMENT_FIELDS:
            index = fields.indexFromName(name)
            layer.setDefaultValueDefinition(index, QgsDefaultValue("NULL"))
            if name == "broadband_z_db":
                layer.setEditorWidgetSetup(index, QgsEditorWidgetSetup("Hidden", {}))
                form = layer.editFormConfig()
                form.setReadOnly(index, True)
                layer.setEditFormConfig(form)
            else:
                layer.setFieldAlias(index, "dBA" if name == "broadband_a_db" else name.replace("_", " ").replace("db", "dB"))
    # Retain the legacy storage column for file compatibility only. It has no
    # effect on inclusion, and is absent from forms and the attribute table.
    legacy = fields.indexFromName("enabled")
    if legacy >= 0:
        layer.setDefaultValueDefinition(legacy, QgsDefaultValue("1"))
        layer.setEditorWidgetSetup(legacy, QgsEditorWidgetSetup("Hidden", {}))
        form = layer.editFormConfig()
        form.setReadOnly(legacy, True)
        layer.setEditFormConfig(form)
        table = layer.attributeTableConfig()
        columns = table.columns()
        for column in columns:
            if (column.name in ("enabled", "reflection_coefficient", "broadband_z_db")
                    or column.name == "face_sources" and kind.key == "building"):
                column.hidden = True
        table.setColumns(columns)
        layer.setAttributeTableConfig(table)
    layer.setEditorWidgetSetup(fields.indexFromName("notes"), QgsEditorWidgetSetup("TextEdit", {"IsMultiline": True}))
    for name in ("object_id", "name"):
        layer.setFieldConstraint(fields.indexFromName(name), QgsFieldConstraints.ConstraintNotNull)
    layer.setFieldConstraint(fields.indexFromName("object_id"), QgsFieldConstraints.ConstraintUnique)
    layer.setConstraintExpression(fields.indexFromName("name"), 'length(trim("name")) > 0', "A feature needs a name")
    if kind.spectrum:
        layer.setFieldAlias(fields.indexFromName("spectrum_id"), "Sound power spectrum (edit in Acalc properties)")
        form = layer.editFormConfig()
        form.setReadOnly(fields.indexFromName("spectrum_id"), True)
        layer.setEditFormConfig(form)
    layer.triggerRepaint()


def vertex_rows(geometry, kind):
    """Return (ring, x, y, ground_z, height) without repeated closing vertices."""
    if geometry.isNull() or geometry.isEmpty():
        return []
    abstract = geometry.constGet()
    if kind.geometry == "Point":
        groups = [[abstract]]
    elif kind.geometry == "LineString":
        groups = [list(abstract.vertices())]
    else:
        groups = [list(abstract.exteriorRing().vertices())]
        groups += [list(abstract.interiorRing(i).vertices()) for i in range(abstract.numInteriorRings())]
        groups = [ring[:-1] for ring in groups]
    return [(ring, point.x(), point.y(), point.z() if kind.elevation else 0.0, point.m() if kind.height else 0.0)
            for ring, points in enumerate(groups) for point in points]


def geometry_from_rows(rows, kind):
    if not rows:
        raise ModelError("Enter coordinates for the feature.")
    grouped = {}
    for ring, x, y, z, height in rows:
        if not kind.elevation:
            z, height = 0.0, 0.0
        if not all(math.isfinite(value) for value in (x, y, z, height)):
            raise ModelError("Coordinates and height must be finite numbers.")
        if height < 0:
            raise ModelError("Height above ground cannot be negative.")
        if ring < 0 or int(ring) != ring:
            raise ModelError("Ring numbers must be whole numbers starting at 0.")
        point = QgsPoint(x, y, z, height) if kind.height else QgsPoint(x, y, z) if kind.elevation else QgsPoint(x, y)
        grouped.setdefault(ring, []).append(point)
    if kind.geometry == "Point":
        if len(rows) != 1:
            raise ModelError("A point feature must have exactly one vertex.")
        geometry = QgsGeometry(grouped[next(iter(grouped))][0])
    elif kind.geometry == "LineString":
        if len(rows) < 2:
            raise ModelError("A line must have at least two vertices.")
        geometry = QgsGeometry(QgsLineString([p for points in grouped.values() for p in points]))
    else:
        if set(grouped) != {0}:
            raise ModelError("A polygon must contain one perimeter.")
        polygon = QgsPolygon()
        for ring, points in sorted(grouped.items()):
            if len(points) < 3:
                raise ModelError("A perimeter needs at least three vertices.")
            line = QgsLineString(points + [points[0]])
            if ring == 0:
                polygon.setExteriorRing(line)
            else:
                polygon.addInteriorRing(line)
        geometry = QgsGeometry(polygon)
    validate_geometry(geometry, kind)
    return geometry


def validate_geometry(geometry, kind):
    expected = getattr(QgsWkbTypes, kind.geometry + kind.dimensions)
    if geometry.isNull() or geometry.isEmpty() or geometry.wkbType() != expected:
        raise ModelError(f"Expected a {kind.geometry}{kind.dimensions} geometry.")
    for vertex in geometry.vertices():
        coordinates = (vertex.x(), vertex.y(), vertex.z()) if kind.ground_points else (vertex.x(), vertex.y())
        if not all(math.isfinite(value) for value in coordinates):
            raise ModelError("Every vertex needs finite " + ("X, Y, and ground Z" if kind.ground_points else "X and Y") + " coordinates.")
        if kind.height and kind.key != "area_receiver" and (not math.isfinite(vertex.m()) or vertex.m() < 0):
            raise ModelError("Every vertex needs a finite, non-negative height (geometry M).")
    if kind.geometry == "LineString" and (geometry.constGet().numPoints() < 2 or geometry.length() <= 0):
        raise ModelError("A line needs at least two distinct XY positions.")
    if kind.geometry == "Polygon" and (geometry.area() <= 0 or not geometry.isGeosValid()):
        raise ModelError("The polygon perimeter is invalid. Check for crossing edges.")
    if kind.geometry == "Polygon":
        polygon = geometry.constGet()
        if kind.key == "trees" and polygon.numInteriorRings():
            raise ModelError("Trees areas need one perimeter without holes.")
        rings = [polygon.exteriorRing()] + [polygon.interiorRing(i) for i in range(polygon.numInteriorRings())]
        for ring in rings:
            first, last = ring.startPoint(), ring.endPoint()
            if (first.x(), first.y()) != (last.x(), last.y()) or (kind.ground_points and first.z() != last.z()) or (kind.height and kind.key != "area_receiver" and first.m() != last.m()):
                raise ModelError("Polygon rings must close with identical coordinates.")


def validate_layer(layer, kind, spectra=None, require_spectra=False, directivities=None):
    errors = []
    seen = set()
    required = required_layer_fields(kind)
    if not required <= {field.name() for field in layer.fields()}:
        return [f"{layer.name()}: required fields were removed. Undo the schema change."]
    for feature in layer.getFeatures():
        try:
            validate_geometry(feature.geometry(), kind)
            import uuid
            object_id = str(uuid.UUID(str(feature["object_id"])))
            if object_id in seen:
                raise ModelError("Object ID is duplicated.")
            seen.add(object_id)
            if feature["name"] is None or not str(feature["name"]).strip() or str(feature["name"]) == "NULL":
                raise ModelError("A feature needs a name.")
            if kind.ground_points and feature_value(feature, "ground_mode") == "unresolved":
                raise ModelError("Enter explicit ground Z values or take elevations from a raster before using this object.")
            if kind.key == "point_source":
                from .directivity import read_orientation, upright, principal_vector, transform, dot, AXES, CALCULATION_LIMIT
                pose = read_orientation(feature_value(feature, "directivity_orientation"))
                identity = feature_value(feature, "directivity_id")
                if identity and directivities is not None:
                    if identity not in directivities:
                        raise ModelError("Source directionality must reference a definition in this project.")
                    value = directivities[identity]["definition"]
                    if upright(value) and dot(transform(pose, principal_vector(value)), AXES["Up"]) < 1 - 1e-8:
                        raise ModelError("Stack directionality requires the principal to point Up.")
                if identity and require_spectra:
                    raise ModelError(CALCULATION_LIMIT)
            if kind.is_building:
                from .building_faces import read_face_sources
                sources = read_face_sources(feature_value(feature, "face_sources"))
                if sources and kind.key == "building":
                    raise ModelError("Buildings cannot contain attached sources. Use Source Buildings.")
                count = len(vertex_rows(feature.geometry(), kind))
                for source in sources:
                    if source["vertex_count"] != count:
                        raise ModelError("Attached sources need the original perimeter vertex count.")
                    identity = source["spectrum_id"]
                    if identity and spectra is not None:
                        record = spectra.get(identity)
                        source_type = source.get("type", "area")
                        if record is None or record["spectrum"]["kind"] != "sound_power" or not compatible_power(record["spectrum"], source_type + "_source"):
                            raise ModelError(f"{source['name']}: choose a project {source_type} sound power spectrum.")
                        if require_spectra and missing_bands(record["spectrum"]):
                            raise ModelError(f"{source['name']}: sound power bands are unknown.")
                    if require_spectra and not identity:
                        raise ModelError(f"{source['name']}: assign a sound power spectrum before calculation.")
            if kind.key == "ground_absorption":
                value = feature_value(feature, "ground_factor")
                if value is None or not math.isfinite(float(value)) or not 0 <= float(value) <= 1:
                    raise ModelError("G must be a finite number between 0 and 1.")
            if kind.key == "trees":
                canopy_height(feature_value(feature, "canopy_height"))
            if (kind.is_building or kind.key == "barrier"):
                value = feature_value(feature, "reflection_coefficient")
                if value is not None and (type(value) not in (float, int) or not math.isfinite(value) or not 0 <= value <= 1):
                    raise ModelError("Reflection coefficient must be a finite number between 0 and 1, or NULL to use the calculation default.")
                reference = feature_value(feature, "absorption_spectrum_id", "")
                if reference and spectra is not None:
                    record = spectra.get(reference)
                    if record is None or record["spectrum"]["kind"] != "absorption":
                        raise ModelError("Surface material must reference an absorption spectrum in this project.")
                    if require_spectra:
                        missing = missing_bands(record["spectrum"])
                        if missing:
                            raise ModelError("Surface absorption is unknown at " + ", ".join(map(str, missing)) + " Hz.")
            if kind.key == "area_receiver":
                for name, label in (("height_m", "Height above ground"), ("spacing_m", "Point spacing")):
                    value = feature_value(feature, name)
                    if value is None or not math.isfinite(float(value)) or float(value) < 0 or (name == "spacing_m" and float(value) == 0):
                        raise ModelError(label + " must be finite and " + ("positive." if name == "spacing_m" else "non-negative."))
            if kind.key == "measurement":
                validate_measurement({field: feature_value(feature, field) for field in ALL_MEASUREMENT_FIELDS})
            ground_mode = feature_value(feature, "ground_mode") if kind.ground_points else None
            if ground_mode is not None and ground_mode not in GROUND_MODES:
                raise ModelError("Choose a ground-point mode.")
            rows = vertex_rows(feature.geometry(), kind)
            if ground_mode == "uniform":
                if any(row[3] != rows[0][3] for row in rows[1:]):
                    raise ModelError("Uniform ground Z must match at every point. Reapply the object properties.")
            if kind.height_controls:
                mode = feature_value(feature, "height_mode", "individual")
                if mode not in HEIGHT_MODES or (kind.key == "barrier" and mode not in ("average", "individual", "absolute_individual")):
                    raise ModelError("Choose a building height mode.")
                resolved = building_heights(rows, mode, float(feature_value(feature, "height_value", 3 if kind.key == "barrier" else 6)))
                if kind.is_building and sources:
                    from .building_faces import faces_from_rows, source_polygon
                    scale = QgsUnitTypes.fromUnitToUnitFactor(layer.crs().mapUnits(), QgsUnitTypes.DistanceMeters)
                    faces = faces_from_rows(resolved, scale)
                    for source in sources:
                        source_polygon(source, faces, len(rows))
            if kind.spectrum and spectra is not None:
                reference = feature["spectrum_id"]
                reference = "" if reference is None or str(reference) == "NULL" else str(reference)
                record = spectra.get(reference)
                if reference and (record is None or record["spectrum"]["kind"] != "sound_power"):
                    raise ModelError("Source spectrum must reference a sound power spectrum in this project.")
                if record and not compatible_power(record["spectrum"], kind.key):
                    raise ModelError("This spectrum power basis is incompatible with this source type.")
                if require_spectra:
                    if not reference:
                        raise ModelError("No sound power spectrum assigned (required before calculation).")
                    missing = missing_bands(record["spectrum"])
                    if missing:
                        raise ModelError("Sound power is unknown at " + ", ".join(map(str, missing)) + " Hz.")
        except (ModelError, ValueError) as error:
            errors.append(f"{layer.name()} · feature {feature.id()}: {error}")
            if len(errors) >= 50:
                errors.append("Validation stopped after 50 errors in this layer.")
                break
    return errors


def synchronize_measurement_broadband(layer):
    """Derive persisted broadband fields from edited octave cells before save."""
    if not layer.isEditable():
        return
    updates = []
    for feature in layer.getFeatures():
        source = {field: feature_value(feature, field) for field in ALL_MEASUREMENT_FIELDS}
        resolved = resolved_measurement(source)
        for field in MEASUREMENT_BROADBAND_FIELDS:
            if source[field] != resolved[field]:
                updates.append((feature.id(), layer.fields().indexFromName(field), resolved[field]))
    if not updates:
        return
    layer.beginEditCommand("Acalc: calculate Measurement dBA")
    try:
        for fid, index, value in updates:
            if not layer.changeAttributeValue(fid, index, value):
                raise ModelError("Could not update a Measurement broadband level.")
        layer.endEditCommand()
    except Exception:
        layer.destroyEditCommand()
        raise


def ground_absorption_overlap_errors(layers):
    """Reject positive-area intersections, including containment and duplicates.

    The supplied layers define the scope: the whole project for model validation,
    or one variant for calculation. Their geometries share the model CRS. A
    spatial index avoids testing every pair, while GEOS area distinguishes real
    overlaps from allowed shared edges and vertices. Invalid polygons are already
    reported by normal layer validation and cannot be checked reliably here.
    """
    errors = []
    index = QgsSpatialIndex()
    previous = {}
    kind = LAYER_TYPES["ground_absorption"]
    for layer in layers:
        for feature in layer.getFeatures():
            geometry = feature.geometry()
            try:
                validate_geometry(geometry, kind)
            except ModelError:
                continue
            name = feature_value(feature, "name", "Unnamed")
            label = f'“{name}” (layer “{layer.name()}”, feature {feature.id()})'
            for candidate in sorted(index.intersects(geometry.boundingBox())):
                other_geometry, other_label = previous[candidate]
                intersection = geometry.intersection(other_geometry)
                if intersection.lastError():
                    errors.append(f"Could not check ground absorption areas {other_label} and {label} for overlap. Check their polygon geometry before calculating.")
                elif not intersection.isEmpty() and intersection.area() > 0:
                    errors.append(f"Ground absorption areas overlap: {other_label} and {label}. Remove the overlap before calculating.")
                if len(errors) >= 50:
                    errors.append("Ground absorption overlap validation stopped after 50 errors.")
                    return errors
            candidate = len(previous)
            indexed = QgsFeature(candidate)
            indexed.setGeometry(geometry)
            index.addFeature(indexed)
            previous[candidate] = (geometry, label)
    return errors
