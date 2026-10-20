# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Schema-11 scene transport for capability-advertised API 1.1 services.

Terrain construction and propagation remain the service's responsibility. Authored
indices, roof rules and attached emitters are retained without flattening a scene.
"""

from .building_faces import _triangulate, point_mounting, read_face_sources
from .schema import ModelError
from .spectra import compatible_power


def _component(capabilities, category, identity):
    return any(value.get("category") == category and value.get("id") == identity
               for value in capabilities.get("component_profiles", []))


def supports_schema11(capabilities):
    return _component(capabilities, "terrain_construction", "acalc_schema11")


def prepare_scene(request, objects, scale, spectra, model_settings, capabilities):
    from .api_model import _mesh, _number, _spectrum

    def require(feature, label):
        if feature not in capabilities.get("features", []):
            raise ModelError(f"{label}: {feature} is unsupported by the selected API.")

    def material(item, kind, label):
        if item.get("absorption_spectrum_id"):
            return _spectrum(spectra, item["absorption_spectrum_id"], label, "absorption")["values"]
        value = item.get("reflection_coefficient")
        if value is None:
            value = model_settings[kind + "_reflection_coefficient"]
        return [1 - _number(value, label + " reflection_coefficient", 0, 1)] * 8

    def emission(identity, kind, spectrum_id, label):
        if kind not in capabilities.get("source_kinds", []):
            raise ModelError(f"{label}: {kind} sources are unsupported by the selected API.")
        spectrum = _spectrum(spectra, spectrum_id, label, "sound_power")
        if not compatible_power(spectrum, kind + "_source"):
            raise ModelError(f"{label}: incompatible sound-power basis.")
        return dict(id=identity, kind=kind,
                    spectrum=[None if value == 0 else value for value in spectrum["values"]],
                    power_basis={"total": "total_power", "per_metre": "power_per_m",
                                 "per_square_metre": "power_per_m2"}[spectrum["power_basis"]])

    terrain = dict(construction_profile="acalc_schema11", samples=[], breakline_definitions=[])
    request.update(terrain=terrain, buildings=[], barriers=[], misc_regions=[])
    mapping, area_snapshots = {}, []
    for kind, item, points, label, identity, reference in objects:
        mapping[identity] = reference
        xy = [[_number(p[0] * scale, label), _number(p[1] * scale, label)] for p in points]
        if kind in ("ground_absorption", "trees", "area_receiver"):
            if kind == "ground_absorption":
                require("mixed_ground", label)
                if "nonoverlapping_schema11" not in capabilities.get("ground_region_policies", []):
                    raise ModelError(f"{label}: nonoverlapping_schema11 ground regions are unsupported by the selected API.")
                request["ground"]["region_policy"] = "nonoverlapping_schema11"
                request["ground"].setdefault("regions", []).append(dict(
                    id=identity, boundary=xy, factor=_number(item["ground_factor"], label + " ground_factor", 0, 1)))
            elif kind == "trees":
                require("foliage_simple", label)
                profile = "iso9613_2024_foliage_simplified_acalc_v1"
                if not _component(capabilities, "miscellaneous_attenuation", profile):
                    raise ModelError(f"{label}: the API does not advertise the model's foliage profile.")
                height = _number(item["canopy_height"], label + " canopy_height", 0)
                if height == 0:
                    raise ModelError(f"{label}: canopy_height must be positive.")
                request["misc_regions"].append(dict(id=identity, kind="foliage", method_profile=profile,
                                                   boundary=xy, canopy_height_m=height))
            else:
                if "area" not in capabilities.get("receiver_kinds", []):
                    raise ModelError(f"{label}: area receivers are unsupported by the selected API.")
                height = _number(item["height_m"], label + " height_m", 0)
                spacing = _number(item["spacing_m"], label + " spacing_m", 0)
                if spacing == 0:
                    raise ModelError(f"{label}: spacing_m must be positive.")
                request["area_receivers"].append(dict(id=identity, boundary=xy, height_m=height, spacing_m=spacing))
                reference.update(height_m=height, spacing_m=spacing, boundary=xy)
                area_snapshots.append({key: reference[key] for key in
                                       ("layer_id", "object_id", "name", "geometry_gpkg_hex", "height_m", "spacing_m")})
            continue

        refs = [dict(position=location + [p[2]]) for p, location in zip(points, xy)]
        if kind.startswith("terrain"):
            if kind == "terrain_line":
                terrain["breakline_definitions"].append(dict(id=identity, vertices=refs))
            else:
                terrain["samples"].extend(ref["position"] for ref in refs if "position" in ref)
            continue

        if kind in ("building", "source_building"):
            require("building_solids", label)
            require("building_roof_mesh", label)
            # The model triangulates in local metres with its first authored
            # vertex as anchor. Return indices into the unchanged stored order.
            local = [[(p[0] - points[0][0]) * scale, (p[1] - points[0][1]) * scale] for p in points]
            perimeter, triangles = _triangulate(local, local_coordinates=True)
            mode = item.get("height_mode") or "individual"
            roof = dict(mode=mode)
            if mode in ("average", "highest", "lowest"):
                roof["height_m"] = _number(item["height_value"], label + " height_value", 0)
            elif mode == "absolute":
                roof["elevation_m"] = _number(item["height_value"], label + " height_value")
            elif mode == "individual":
                roof["heights_m"] = [_number(p[3], label + " height", 0) for p in points]
            elif mode == "absolute_individual":
                roof["elevations_m"] = [_number(p[2] + p[3], label + " roof elevation") for p in points]
            else:
                raise ModelError(f"{label}: invalid building height mode.")
            request["buildings"].append(dict(id=identity, ground_vertices=refs, roof=roof,
                                              roof_triangles=[list(t) for t in triangles],
                                              absorption=material(item, "building", label)))
            attached_sources = read_face_sources(item.get("face_sources"))
            if kind == "building" and attached_sources:
                raise ModelError(f"{label}: Buildings cannot have attached sources; use Source Buildings.")
            wall_ids = {f"wall:{a}:{perimeter[(i + 1) % len(perimeter)]}" for i, a in enumerate(perimeter)}
            roof_ids = {"roof"} | {"roof:" + ":".join(map(str, sorted(t))) for t in triangles}
            for attached in attached_sources:
                name = f"{label} / {attached['name']}"
                require("building_attached_sources", name)
                if not _component(capabilities, "attachment_placement", "acalc_schema11"):
                    raise ModelError(f"{name}: the API does not advertise schema-11 attachment placement.")
                if attached["vertex_count"] != len(points) or attached["face"] not in wall_ids | roof_ids:
                    raise ModelError(f"{name}: stale building face reference; review its attachment.")
                source_id = f"{identity}/source/{attached['id']}"
                source_kind = attached.get("type", "area")
                source = emission(source_id, source_kind, attached["spectrum_id"], name)
                placement = dict(placement_profile="acalc_schema11", x_m=attached["x_m"], y_m=attached["y_m"])
                if source_kind == "point":
                    placement["mounting"] = point_mounting(dict(attached, name=name))
                    if placement["mounting"] == "standoff":
                        placement["distance_m"] = attached["distance_m"]
                else:
                    placement.update(width_m=attached["width_m"], height_m=attached["height_m"])
                if attached["face"].startswith("roof"):
                    placement["frame"] = attached.get("frame", "legacy_world")
                source.update(host_building_id=identity, host_face_id=attached["face"], attachment=placement)
                request["sources"].append(source)
                mapping[source_id] = dict(reference, source_id=attached["id"], name=attached["name"],
                                          kind=source_kind + "_source", spectrum_id=attached["spectrum_id"],
                                          parent_source_id=identity, parent_source_name=reference["name"],
                                          parent_source_kind="source_building")
            continue
        if kind == "barrier":
            require("polyline_barriers", label)
            require("reflecting_barriers", label)
            request["barriers"].append(dict(id=identity, ground_vertices=refs,
                heights_m=[_number(p[3], label + " height", 0) for p in points], role="screen_and_reflector",
                opacity=dict(kind="declared_opaque", basis="Schema-11 closed opaque barrier assumption"),
                absorption=material(item, kind, label)))
            continue

        locations = [dict(ground_ref=ref, height_m=_number(p[3], label + " height", 0)) for ref, p in zip(refs, points)]
        if kind == "point_receiver":
            if "point" not in capabilities.get("receiver_kinds", []):
                raise ModelError(f"{label}: point receivers are unsupported by the selected API.")
            request["receivers"].append(dict(id=identity, **locations[0]))
            reference.update(position_xy=xy[0], height_m=points[0][3])
            reference["position"] = xy[0] + [points[0][2] + points[0][3]]
        else:
            source_kind = kind.removesuffix("_source")
            if item.get("directivity_id"):
                raise ModelError(f"{label}: assigned directivity is unsupported by this frontend; it cannot be discarded.")
            reference["spectrum_id"] = item.get("spectrum_id")
            source = emission(identity, source_kind, item.get("spectrum_id"), label)
            if source_kind == "point":
                source.update(locations[0])
            else:
                geometry = dict(vertices=locations)
                if source_kind == "area":
                    _mesh([position + [p[2] + p[3]] for position, p in zip(xy, points)], label)
                    _, triangles = _triangulate(xy)
                    geometry["triangles"] = [list(t) for t in triangles]
                source["geometry"] = geometry
            request["sources"].append(source)
    if any(s["kind"] in ("area", "line") for s in request["sources"]) and (request["buildings"] or request["barriers"]):
        require("screened_extended_sources", "Selected extended sources and obstacles")
        if model_settings["reflection_order"]:
            require("reflected_extended_sources", "Selected extended sources and reflections")
    return mapping, area_snapshots
