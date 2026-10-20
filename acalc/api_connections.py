# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Persistent named HTTP calculation services and their discovered profiles."""

from copy import deepcopy
from datetime import datetime, timezone
import json
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from qgis.PyQt.QtCore import QSettings
from qgis.core import QgsApplication, QgsAuthMethodConfig

from .schema import ModelError

SETTINGS_KEY = "acalc/api_connections_v1"
LOCAL_BASE_URL = "http://127.0.0.1:8343"
FREQUENCIES = [63, 125, 250, 500, 1000, 2000, 4000, 8000]


def normalize_base_url(value):
    value = str(value).strip()
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ModelError("Enter a valid engine URL, including http:// or https://.") from error
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or any(c.isspace() for c in value)):
        raise ModelError("Use an HTTP or HTTPS engine URL without credentials, a query, or a fragment.")
    if port is not None and not 1 <= port <= 65535:
        raise ModelError("The engine port must be between 1 and 65535.")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def connection_mode(connection):
    return connection.get("mode", "local" if connection.get("base_url") == LOCAL_BASE_URL else "remote")


def health_problem(data, connection):
    if not isinstance(data, dict) or data.get("status") != "ok":
        return "The calculation server is not ready."
    if data.get("role") == "node":
        return "This URL points to a coordinator's internal node. Enter the coordinator's URL instead."
    if connection_mode(connection) == "local" and data.get("key_required") is True:
        return ("The local engine was configured with a client key. Choose Remote with "
                + LOCAL_BASE_URL + " and enter the client key.")
    return ""


def validate_client_key(key):
    if not isinstance(key, str) or not key or any(not 33 <= ord(c) <= 126 for c in key):
        raise ModelError("Enter a client key without spaces or control characters.")
    return key


class QgisCredentialStore:
    """Keep bearer secrets exclusively in QGIS's encrypted authentication DB.

    Basic is used as an encrypted password container, never as wire Basic auth.
    QGIS manages its own master-password prompt; failures have no plaintext fallback.
    """

    def __init__(self, manager=None):
        self.manager = manager if manager is not None else QgsApplication.authManager()

    @staticmethod
    def _result(result, config):
        # SIP versions expose the in/out config either as a tuple or in place.
        return result if isinstance(result, tuple) else (result, config)

    def save(self, key, name, authcfg=None):
        validate_client_key(key)
        config = QgsAuthMethodConfig()
        config.setName("Acalc engine: " + name)
        config.setMethod("Basic")
        config.setConfig("username", "acalc-client")
        config.setConfig("password", key)
        if authcfg:
            config.setId(authcfg)
            ok = self.manager.updateAuthenticationConfig(config)
        else:
            ok, config = self._result(self.manager.storeAuthenticationConfig(config), config)
        if not ok or not config.id():
            raise ModelError("The client key could not be saved in QGIS's encrypted credential store. Unlock QGIS authentication and try again.")
        return config.id()

    def load(self, authcfg):
        config = QgsAuthMethodConfig()
        ok, config = self._result(self.manager.loadAuthenticationConfig(authcfg, config, True), config)
        if not ok or config.method() != "Basic":
            raise ModelError("The client key could not be read from QGIS's encrypted credential store. Unlock QGIS authentication or save the key again in Settings.")
        return validate_client_key(config.config("password"))

    def remove(self, authcfg):
        if authcfg and not self.manager.removeAuthenticationConfig(authcfg):
            raise ModelError("The saved client key could not be removed from QGIS authentication. Unlock QGIS authentication and try again.")


def connection_client_key(connection):
    # Callers without a persisted connection (including older local clients)
    # keep their unauthenticated transport until configured through Settings.
    if connection.get("mode") != "remote":
        return ""
    if not connection.get("authcfg"):
        raise ModelError("The remote engine needs a client key. Enter it in Settings.")
    return QgisCredentialStore().load(connection["authcfg"])


def utc_timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def capabilities_problem(value):
    """Return the reason this frontend cannot use a discovered engine profile."""
    if not isinstance(value, dict):
        return "Fetch capabilities to discover an engine."
    version = value.get("api_version")
    if not isinstance(version, str) or version.split(".")[0] != "1":
        return "This frontend requires calculation protocol version 1."
    implementation = value.get("implementation")
    if not isinstance(implementation, dict) or not all(
            isinstance(implementation.get(key), str) and implementation[key].strip()
            for key in ("name", "version")):
        return "Capabilities must identify the engine implementation and version."
    if value.get("method") != "ISO 9613-2:2024":
        return "This frontend does not yet support the advertised calculation method."
    if value.get("atmospheric_absorption") != "ISO 9613-1:1993":
        return "This frontend requires ISO 9613-1:1993 atmospheric absorption."
    if value.get("frequencies_hz") != FREQUENCIES:
        return "The engine must support the eight 63–8,000 Hz octave bands."
    if (type(value.get("max_reflect_order")) is not int
            or value["max_reflect_order"] < 0):
        return "Capabilities must specify the supported reflection order."
    if not all(isinstance(value.get(key), list) and all(isinstance(x, str) for x in value[key])
               for key in ("source_kinds", "receiver_kinds")):
        return "Capabilities must specify source and receiver kinds."
    return ""


def engine_identity(connection):
    capabilities = connection.get("capabilities") or {}
    implementation = capabilities.get("implementation") or {}
    implementation = implementation if isinstance(implementation, dict) else {}
    # A backend identity includes the connection and method/implementation, not
    # its editable display name or a service version update.
    return "api:" + connection["id"] + ":" + str(capabilities.get("method", "")) + ":" + str(implementation.get("name", ""))


class ApiConnectionStore:
    def __init__(self, settings=None, credentials=None):
        self.settings = settings if settings is not None else QSettings()
        self.credentials = credentials if credentials is not None else QgisCredentialStore()

    def connections(self):
        try:
            value = json.loads(self.settings.value(SETTINGS_KEY, "[]"))
            if not isinstance(value, list):
                return []
            return [dict(deepcopy(item), mode=connection_mode(item)) for item in value if isinstance(item, dict)
                    and all(isinstance(item.get(key), str) and item[key] for key in ("id", "name", "base_url"))]
        except (TypeError, ValueError):
            return []

    def get(self, connection_id):
        return next((c for c in self.connections() if c["id"] == connection_id), None)

    def _write(self, connections):
        self.settings.setValue(SETTINGS_KEY, json.dumps(connections, allow_nan=False))
        self.settings.sync()

    def save(self, name, base_url=LOCAL_BASE_URL, connection_id=None, *, mode=None, client_key=None):
        name = str(name).strip()
        if not name:
            raise ModelError("Give this engine connection a name.")
        base_url = normalize_base_url(base_url)
        mode = mode or connection_mode({"base_url": base_url})
        if mode not in ("local", "remote"):
            raise ModelError("Choose Local or Remote for the engine connection.")
        if mode == "local":
            base_url = LOCAL_BASE_URL
            if client_key:
                raise ModelError("Local connections do not use a client key. Choose Remote to enter a key.")
        connections = self.connections()
        if any(c["id"] != connection_id and c["name"].casefold() == name.casefold() for c in connections):
            raise ModelError("An engine connection already has this name.")
        existing = next((c for c in connections if c["id"] == connection_id), None)
        if connection_id is not None and existing is None:
            raise ModelError("This engine connection has been removed. Add it again.")
        value = deepcopy(existing) if existing else {"id": str(uuid4())}
        endpoint_changed = value.get("base_url") != base_url or value.get("mode") != mode
        old_authcfg = value.get("authcfg")
        if endpoint_changed:
            value = {"id": value["id"]}
        if mode == "remote" and client_key is not None:
            value["authcfg"] = self.credentials.save(client_key, name, value.get("authcfg"))
            for key in tuple(value):
                if key.startswith(("health", "capabilities", "discovery", "engine_profiles")):
                    value.pop(key)
        if old_authcfg and old_authcfg != value.get("authcfg"):
            self.credentials.remove(old_authcfg)
        value.update(name=name, base_url=base_url, mode=mode, revision=str(uuid4()))
        value.pop("backend_settings", None)
        if existing:
            connections[connections.index(existing)] = value
        else:
            connections.append(value)
        self._write(connections)
        return deepcopy(value)

    def remove(self, connection_id):
        value = self.get(connection_id)
        if value and value.get("authcfg"):
            self.credentials.remove(value["authcfg"])
        self._write([c for c in self.connections() if c["id"] != connection_id])

    def record_check(self, connection_id, kind, data=None, error=None, expected_url=None):
        if kind not in ("health", "capabilities"):
            raise ValueError("Unknown engine check")
        connections = self.connections()
        value = next((c for c in connections if c["id"] == connection_id), None)
        # A late reply must not resurrect a removed connection or describe a
        # different endpoint after the URL was edited.
        if value is None or (expected_url is not None and value["base_url"] != expected_url):
            return False
        if error is None and not isinstance(data, dict):
            error = "The engine response must be a JSON object."
        if kind == "health" and error is None:
            error = health_problem(data, value) or None
        value[kind + "_checked_at"] = utc_timestamp()
        value[kind + "_status"] = "error" if error else "ok"
        value[kind + "_error"] = str(error) if error else ""
        if error is None:
            if kind == "capabilities" and isinstance(value.get("capabilities"), dict):
                previous = engine_identity(value)
                current = engine_identity({**value, "capabilities": data})
                if current != previous:
                    value.pop("backend_settings", None)
            value[kind] = deepcopy(data)
        self._write(connections)
        return True

    def record_discovery(self, expected, result):
        """Commit a complete saved-connection check, never a draft or stale reply."""
        connections = self.connections()
        value = next((c for c in connections if c["id"] == expected["id"]), None)
        if value is None or any(value.get(k) != expected.get(k) for k in
                                ("revision", "base_url", "mode", "authcfg")):
            return False
        now = utc_timestamp()
        value.update(discovery_checked_at=now, discovery_error=result.get("error", ""),
                     health_checked_at=now, health_error=result.get("health_error", ""),
                     health_status="error" if result.get("health_error") else "ok")
        if result.get("health") is not None:
            value["health"] = deepcopy(result["health"])
        if not result.get("error"):
            value["engine_profiles"] = deepcopy(result["engines"])
            default = next((p for p in result["engines"] if p.get("default")), None)
            value["capabilities_status"] = "ok" if default and not default.get("error") and default.get("capabilities") else "error"
            value["capabilities_checked_at"] = now
            if default and default.get("capabilities"):
                value["capabilities"] = deepcopy(default["capabilities"])
        self._write(connections)
        return True

    def usable_connections(self):
        return [c for c in self.connections()
                if c.get("health_status") != "error" and not c.get("discovery_error")
                and (any(p.get("state") == "ready" and not p.get("error")
                         and not capabilities_problem(p.get("capabilities"))
                         for p in c["engine_profiles"]) if "engine_profiles" in c else
                     c.get("capabilities_status") == "ok" and not capabilities_problem(c.get("capabilities")))]
