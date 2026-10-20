# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Calculation engines discovered through the user's named HTTP services."""

from copy import deepcopy
from dataclasses import dataclass
from functools import partial

from .api_connections import ApiConnectionStore, capabilities_problem, engine_identity
from .schema import ModelError


@dataclass(frozen=True)
class CalculationEngine:
    id: str
    name: str
    create_editor: object
    save_settings: object
    connection: dict
    capabilities: dict
    capabilities_path: str = "v1/capabilities"
    calculations_path: str = "v1/calculations"


def available_engines(connection_store=None):
    from .api_settings_ui import ApiISOSettingsEditor

    connection_store = connection_store or ApiConnectionStore()
    engines = []
    from .api_connection_check import engine_summary

    for connection in connection_store.usable_connections():
        profiles = connection.get("engine_profiles")
        if profiles is None:
            profiles = [{"default": True, "state": "ready", "capabilities": connection["capabilities"],
                         "links": {"capabilities": "v1/capabilities", "calculations": "v1/calculations"}}]
        for profile in profiles:
            caps = profile.get("capabilities")
            if profile.get("state") != "ready" or profile.get("error") or capabilities_problem(caps):
                continue
            selected = deepcopy(connection)
            selected["capabilities"] = deepcopy(caps)
            # Preserve existing default-engine model selections across discovery.
            identity = engine_identity(selected)
            if not profile.get("default"):
                identity += ":engine:" + profile["name"]
            engines.append(CalculationEngine(
                id=identity,
                name=f"{connection['name']} — {engine_summary(profile)}",
                create_editor=partial(ApiISOSettingsEditor, selected),
                save_settings=lambda store, settings: store.save_calculation(settings),
                connection=selected,
                capabilities=deepcopy(caps),
                capabilities_path=profile["links"]["capabilities"],
                calculations_path=profile["links"]["calculations"],
            ))
    return tuple(engines)


def calculation_engine(engine_id):
    for engine in available_engines():
        if engine.id == engine_id:
            return engine
    raise ModelError("The selected calculation engine is unavailable. Configure its connection and check its capabilities on the Settings page.")
