# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Convert complete Overpass building geometry to reviewable Acalc inputs.

This module performs no network access. It preserves full footprints and their
provenance, rejects unsupported boundaries, and never infers height from levels.
"""

import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

from qgis.PyQt.QtCore import QVariant
from qgis.core import QgsFeature, QgsField, QgsGeometry, QgsPointXY, QgsVectorLayer

from .schema import ModelError


MAX_AREA_KM2 = 100.0
ATTRIBUTION = "© OpenStreetMap contributors"
COPYRIGHT_URL = "https://www.openstreetmap.org/copyright"
LICENSE = "ODbL 1.0"
LICENSE_URL = "https://opendatacommons.org/licenses/odbl/1-0/"
_EARTH_RADIUS_M = 6371008.8
_NUMBER = r"(?:\d+(?:\.\d*)?|\.\d+)"
_HEIGHT = re.compile(rf"^({_NUMBER})\s*(m|metres?|meters?|ft|feet|foot|')?$", re.I)
_FEET_INCHES = re.compile(rf'''^({_NUMBER})\s*'\s*({_NUMBER})\s*"$''')


@dataclass
class OsmImportReport:
    imported_count: int = 0
    skipped: list[str] = field(default_factory=list)
    default_height_count: int = 0
    tagged_height_count: int = 0


def validate_bbox(bbox):
    """Return south/west/north/east after checking a non-crossing small area."""
    try:
        if len(bbox) != 4 or any(isinstance(value, bool) for value in bbox):
            raise ValueError
        south, west, north, east = (float(value) for value in bbox)
    except (TypeError, ValueError, OverflowError):
        raise ModelError("The area needs south, west, north and east coordinates.") from None
    if not all(math.isfinite(value) for value in (south, west, north, east)):
        raise ModelError("Area coordinates must be finite numbers.")
    if not (-90 <= south < north <= 90 and -180 <= west < east <= 180):
        raise ModelError("Use valid latitude/longitude bounds with south < north and west < east; areas crossing the antimeridian are unsupported.")
    area = (_EARTH_RADIUS_M ** 2 * math.radians(east - west)
            * (math.sin(math.radians(north)) - math.sin(math.radians(south)))) / 1e6
    if area > MAX_AREA_KM2:
        raise ModelError(f"Choose a smaller area (at most {MAX_AREA_KM2:g} km²).")
    return south, west, north, east


def overpass_query(bbox):
    bounds = ",".join(format(value, ".12g") for value in validate_bbox(bbox))
    # The bounding box selects objects. Do not add a bbox to `out geom`: that
    # clips member coordinates and can turn a complete footprint into a fragment.
    return (f'[out:json][timeout:60];'
            f'way["building"]["building"!="no"]({bounds})->.buildingways;'
            '(.buildingways;'
            f'relation["type"="multipolygon"]["building"]["building"!="no"]({bounds});'
            'relation(bw.buildingways)["type"="multipolygon"];'
            ');out body geom;')


def _tagged_height(value, allow_zero=False):
    if not isinstance(value, str):
        return None
    value = value.strip()
    feet_inches = _FEET_INCHES.fullmatch(value)
    if feet_inches:
        feet, inches = (float(part) for part in feet_inches.groups())
        if inches >= 12:
            return None
        height = feet * 0.3048 + inches * 0.0254
    else:
        match = _HEIGHT.fullmatch(value)
        if not match:
            return None
        number, unit = match.groups()
        height = float(number)
        if unit and unit.lower() in ("ft", "feet", "foot", "'"):
            height *= 0.3048
    return height if math.isfinite(height) and (height >= 0 if allow_zero else height > 0) else None


def _check_solid_building(tags):
    if tags.get("building", "").lower() == "roof":
        raise ModelError("roof-only structures cannot be imported as ground-anchored solid buildings")
    if tags.get("location", "").lower() == "underground" or tags.get("underground", "").lower() == "yes":
        raise ModelError("underground buildings are unsupported")
    if "min_height" in tags:
        clearance = _tagged_height(tags["min_height"], allow_zero=True)
        if clearance is None or clearance > 0:
            raise ModelError("elevated buildings or uncertain min_height clearance are unsupported")
    if "building:min_level" in tags:
        try:
            level = float(tags["building:min_level"])
        except (TypeError, ValueError, OverflowError):
            level = math.nan
        if not math.isfinite(level) or level != 0:
            raise ModelError("elevated buildings or uncertain building:min_level clearance are unsupported")


def _coordinates(geometry):
    if not isinstance(geometry, list) or len(geometry) < 2:
        raise ModelError("missing or incomplete way geometry")
    points = []
    for point in geometry:
        try:
            if not isinstance(point, dict) or any(isinstance(point[key], bool) for key in ("lon", "lat")):
                raise ValueError
            lon, lat = float(point["lon"]), float(point["lat"])
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ModelError("missing or invalid coordinates") from None
        if not (math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90):
            raise ModelError("coordinates are outside valid latitude/longitude bounds")
        points.append((lon, lat))
    # Geographic straight edges crossing the date line cannot be passed through
    # as ordinary planar footprints and transformed safely by this importer.
    if any(abs(a[0] - b[0]) > 180 for a, b in zip(points, points[1:])):
        raise ModelError("footprints crossing the antimeridian are unsupported")
    return points


def _polygon(ring):
    if ring[0] != ring[-1]:
        raise ModelError("the perimeter is unclosed")
    if len(ring) < 4 or len(set(ring[:-1])) != len(ring) - 1:
        raise ModelError("the perimeter has too few or repeated vertices")
    geometry = QgsGeometry.fromPolygonXY([[QgsPointXY(*point) for point in ring]])
    if geometry.isEmpty() or geometry.area() <= 0 or not geometry.isGeosValid():
        raise ModelError("the polygon perimeter is invalid")
    return geometry


def _relation_rings(element, ways):
    if element["tags"].get("type") != "multipolygon":
        raise ModelError("only building multipolygon relations are supported")
    members = element.get("members")
    if not isinstance(members, list) or not members:
        raise ModelError("missing relation members")
    segments, seen = [], set()
    for member in members:
        if not isinstance(member, dict):
            raise ModelError("invalid relation member")
        if member.get("role") == "inner":
            raise ModelError("courtyards / polygon holes are unsupported; the whole relation was skipped")
        if member.get("type") != "way" or member.get("role", "") not in ("", "outer"):
            raise ModelError("unsupported relation member type or role")
        reference = member.get("ref")
        if not isinstance(reference, int) or isinstance(reference, bool) or reference <= 0 or reference in seen:
            raise ModelError("missing or duplicated relation way reference")
        seen.add(reference)
        geometry = member.get("geometry")
        if geometry is None:
            geometry = ways.get(reference, {}).get("geometry")
        segments.append(_coordinates(geometry))

    rings = [segment for segment in segments if segment[0] == segment[-1]]
    open_segments = [segment for segment in segments if segment[0] != segment[-1]]
    degree = Counter(endpoint for segment in open_segments for endpoint in (segment[0], segment[-1]))
    if any(count != 2 for count in degree.values()):
        raise ModelError("outer members are unclosed or have ambiguous junctions")
    while open_segments:
        ring = list(open_segments.pop(0))
        while ring[0] != ring[-1]:
            for index, segment in enumerate(open_segments):
                if segment[0] == ring[-1]:
                    ring.extend(segment[1:])
                    break
                if segment[-1] == ring[-1]:
                    ring.extend(reversed(segment[:-1]))
                    break
            else:
                raise ModelError("outer members could not be joined into a closed perimeter")
            open_segments.pop(index)
        rings.append(ring)
    if not rings:
        raise ModelError("no outer perimeter")
    return rings


def _elements(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("elements"), list):
        raise ModelError("Overpass returned an invalid response: an elements list is required.")
    if "remark" in payload:
        raise ModelError(f"Overpass did not return a complete result: {str(payload['remark'])[:500]}")
    seen = set()
    for element in payload["elements"]:
        if not isinstance(element, dict) or element.get("type") not in ("node", "way", "relation"):
            raise ModelError("Overpass returned an invalid element.")
        identity = element.get("id")
        if not isinstance(identity, int) or isinstance(identity, bool) or identity <= 0:
            raise ModelError("Overpass returned an invalid object ID.")
        key = (element["type"], identity)
        if key in seen:
            raise ModelError("Overpass returned duplicated objects.")
        seen.add(key)
        tags = element.get("tags", {})
        if not isinstance(tags, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in tags.items()):
            raise ModelError("Overpass returned invalid object tags.")
    return payload["elements"]


def _is_building(element):
    value = element.get("tags", {}).get("building", "")
    return bool(value) and value.lower() != "no"


def build_osm_layer(payload, default_height=6.0, use_osm_height=True):
    """Return an EPSG:4326 memory layer and explicit counts / skipped reasons.

    A relation is accepted atomically. Multiple outer rings become independent
    building features; unsupported inner rings are never filled or discarded.
    Building relation members are suppressed even when their parent is skipped,
    preventing a courtyard or failed relation from reappearing as a solid way.
    """
    try:
        height_default = float(default_height)
    except (TypeError, ValueError, OverflowError):
        raise ModelError("Default building height must be a positive number in meters.") from None
    if isinstance(default_height, bool) or not math.isfinite(height_default) or height_default <= 0:
        raise ModelError("Default building height must be a positive finite number in meters.")
    elements = _elements(payload)
    ways = {element["id"]: element for element in elements if element["type"] == "way"}
    building_way_ids = {identity for identity, element in ways.items() if _is_building(element)}
    relations = []
    for element in elements:
        if element["type"] != "relation":
            continue
        members = element.get("members")
        parent_of_building = (element.get("tags", {}).get("type") == "multipolygon"
                              and isinstance(members, list)
                              and any(isinstance(member, dict) and member.get("type") == "way"
                                      and isinstance(member.get("ref"), int) and member["ref"] in building_way_ids
                                      for member in members))
        if _is_building(element) or parent_of_building:
            relations.append(element)
    relation_ids = {relation["id"] for relation in relations}
    member_parents = {}
    for relation in relations:
        members = relation.get("members")
        for member in members if isinstance(members, list) else []:
            if isinstance(member, dict) and member.get("type") == "way" and isinstance(member.get("ref"), int):
                member_parents.setdefault(member["ref"], []).append(relation["id"])

    layer = QgsVectorLayer("Polygon?crs=EPSG:4326", "OpenStreetMap buildings", "memory")
    provider = layer.dataProvider()
    names = ("name", "notes", "height_m", "osm_type", "osm_id", "osm_tags", "attribution", "source_url", "retrieved_at", "license", "license_url")
    if not provider.addAttributes([QgsField(name, QVariant.Double if name == "height_m" else QVariant.String) for name in names]):
        raise ModelError("Could not create the OpenStreetMap source fields.")
    layer.updateFields()
    report, features = OsmImportReport(), []
    retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for element in elements:
        if not _is_building(element):
            if element["type"] == "relation" and element["id"] in relation_ids:
                report.skipped.append(f"OSM relation {element['id']}: legacy or conflicting multipolygon has no valid building tag; "
                                      "the relation and its building member ways were skipped without inventing tags or filling holes.")
            continue
        kind, identity = element["type"], element["id"]
        label = f"OSM {kind} {identity}"
        if kind == "way" and identity in member_parents:
            parents = ", ".join(str(ref) for ref in member_parents[identity])
            report.skipped.append(f"{label}: member of relation {parents}; not imported separately.")
            continue
        try:
            _check_solid_building(element.get("tags", {}))
            if kind == "way":
                rings = [_coordinates(element.get("geometry"))]
            elif kind == "relation":
                rings = _relation_rings(element, ways)
            else:
                raise ModelError("building nodes have no footprint")
            geometries = [_polygon(ring) for ring in rings]
            if len(geometries) > 1:
                combined = QgsGeometry.fromMultiPolygonXY([geometry.asPolygon() for geometry in geometries])
                if not combined.isGeosValid():
                    raise ModelError("outer perimeters overlap or form an invalid multipolygon")
        except ModelError as error:
            report.skipped.append(f"{label}: {error}.")
            continue

        tags = element.get("tags", {})
        tagged_height = _tagged_height(tags.get("height")) if use_osm_height else None
        height = tagged_height if tagged_height is not None else height_default
        source_url = f"https://www.openstreetmap.org/{kind}/{identity}"
        if tagged_height is not None:
            height_note = f"Height {height:g} m from OSM height={tags['height']!r}; review before calculation."
        else:
            reason = "OSM heights disabled" if not use_osm_height else "no valid positive OSM height tag"
            height_note = f"Assumed height {height:g} m ({reason}); review before calculation. Building levels were not used."
        notes = (f"{label}. {ATTRIBUTION} ({COPYRIGHT_URL}). {LICENSE} ({LICENSE_URL}). "
                 f"Source: {source_url}. Retrieved: {retrieved_at}. {height_note} "
                 "One height is applied to the full footprint; roof form and ground elevations are not supplied by OSM.")
        for index, geometry in enumerate(geometries):
            name = tags.get("name", "").strip() or label
            if len(geometries) > 1:
                name += f" (part {index + 1})"
            feature = QgsFeature(layer.fields())
            feature.setGeometry(geometry)
            feature.setAttributes([name, notes, height, kind, str(identity), json.dumps(tags, ensure_ascii=False, sort_keys=True),
                                   f"{ATTRIBUTION} ({COPYRIGHT_URL})", source_url, retrieved_at, LICENSE, LICENSE_URL])
            features.append(feature)
            report.imported_count += 1
            if tagged_height is None:
                report.default_height_count += 1
            else:
                report.tagged_height_count += 1
    if features and not provider.addFeatures(features)[0]:
        raise ModelError("Could not prepare the OpenStreetMap building footprints.")
    layer.updateExtents()
    return layer, report
