# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""One cancellable connection check shared by saved and draft connections."""

from copy import deepcopy

from qgis.PyQt.QtCore import QObject, pyqtSignal

from .api_connections import capabilities_problem, connection_mode, health_problem
from .schema import ModelError


def engine_summary(profile):
    """A single plain-text row; service versions are deliberately never used."""
    caps = profile.get("capabilities") or {}
    implementation = caps.get("implementation") or {}
    implementation = implementation if isinstance(implementation, dict) else {}
    method = caps.get("method") or profile.get("method") or "Calculation method unknown"
    engine = profile.get("engine") or implementation.get("name") or profile.get("name") or "Engine"
    name = profile.get("name")
    if name and name not in ("default", engine):
        engine = f"{name} ({engine})"
    version = profile.get("version") or implementation.get("version") or "version unknown"
    state = profile.get("state", "unavailable")
    problem = profile.get("error") or capabilities_problem(caps)
    suffix = (" — " + state) if state != "ready" else (" — unavailable" if profile.get("error") else " — unsupported" if problem else "")
    return " ".join(f"{method} — {engine} {version}{suffix}".split())


class ConnectionCheck(QObject):
    progress = pyqtSignal(str)
    finished = pyqtSignal(object)

    def __init__(self, parent=None, client=None):
        super().__init__(parent)
        if client is None:
            from .api_client import ApiClient
            client = ApiClient(self)
        self.client = client
        self.busy = False
        self._generation = 0

    def cancel(self):
        self._generation += 1
        self.busy = False
        self.client.abort_all()
        self.client.clear_credentials()

    def start(self, connection, *, client_key=None):
        self.cancel()
        self.busy = True
        generation = self._generation
        self._result = {"health": None, "health_error": "", "engines": [], "error": ""}

        def received_health(data, error, headers):
            self._result["health"] = data if isinstance(data, dict) else None
            problem = str(error) if error else health_problem(data, connection)
            self._result["health_error"] = problem
            if problem:
                self._result["local_key_required"] = (not error and isinstance(data, dict)
                    and data.get("role") != "node" and connection_mode(connection) == "local"
                    and data.get("key_required") is True)
                self._finish(problem)
                return
            self.progress.emit("Engine reachable. Checking installed engines and client access…")
            request("v1/engines", received_engines)

        def received_engines(data, error, headers):
            if error:
                # Older single-engine services have only the default routes.
                if getattr(error, "status", None) in (404, 405, 501):
                    request("v1/capabilities", received_legacy)
                else:
                    self._finish(str(error))
                return
            if not isinstance(data, dict) or not isinstance(data.get("engines"), list):
                self._finish("The engine list response must contain an engines array.")
                return
            seen = set()
            for entry in data["engines"]:
                if (not isinstance(entry, dict) or not isinstance(entry.get("name"), str)
                        or not entry["name"].strip() or entry["name"] in seen):
                    self._finish("The engine list contains an invalid or duplicate engine name.")
                    return
                seen.add(entry["name"])
                profile = {key: deepcopy(entry[key]) for key in (
                    "name", "default", "state", "engine", "title", "version", "method", "atmosphere", "message", "links") if key in entry}
                if profile.get("state") != "ready":
                    # An old cached hello must never make an unavailable engine look installed/ready.
                    profile.update(engine=None, title=None, version=None)
                self._result["engines"].append(profile)
            check_next(0)

        def check_next(index):
            profiles = self._result["engines"]
            if index >= len(profiles):
                self._finish()
                return
            profile = profiles[index]
            if profile.get("state") != "ready":
                check_next(index + 1)
                return
            links = profile.get("links")
            try:
                if not isinstance(links, dict):
                    raise ModelError("The engine did not provide its capability and calculation links.")
                for key in ("capabilities", "calculations"):
                    self.client.resolve_url(links.get(key))
            except (ModelError, ValueError) as error:
                profile["error"] = str(error)
                check_next(index + 1)
                return

            def received_caps(data, error, headers):
                if error or not isinstance(data, dict):
                    profile["error"] = str(error) if error else "The engine capabilities response must be a JSON object."
                else:
                    profile["capabilities"] = data
                check_next(index + 1)

            request(links["capabilities"], received_caps)

        def received_legacy(data, error, headers):
            if error or not isinstance(data, dict):
                self._finish(str(error) if error else "The engine capabilities response must be a JSON object.")
                return
            implementation = data.get("implementation") or {}
            self._result["engines"] = [{"name": "default", "default": True, "state": "ready",
                "engine": implementation.get("name") if isinstance(implementation, dict) else None,
                "version": implementation.get("version") if isinstance(implementation, dict) else None,
                "capabilities": data,
                "links": {"capabilities": "v1/capabilities", "calculations": "v1/calculations"}}]
            self._finish()

        def request(path, callback):
            def guarded(data, error, headers):
                if self.busy and generation == self._generation:
                    callback(data, error, headers)
            try:
                self.client.request("GET", path, None, guarded)
            except (ModelError, ValueError, RuntimeError) as error:
                guarded(None, error, {})

        self.progress.emit("Checking engine reachability…")
        try:
            self.client.configure_connection(connection, client_key=client_key)
        except (ModelError, ValueError, RuntimeError) as error:
            self._result["health_error"] = str(error)
            self._finish(str(error))
            return
        request("healthz", received_health)

    def _finish(self, error=""):
        self.busy = False
        self._result["error"] = error
        self.client.clear_credentials()
        self.finished.emit(deepcopy(self._result))


def check_message(result):
    if result.get("error"):
        return result["error"]
    profiles = result["engines"]
    usable = sum(1 for p in profiles if p.get("state") == "ready" and not p.get("error")
                 and not capabilities_problem(p.get("capabilities")))
    return f"Engine reachable. {usable} of {len(profiles)} engines available for calculation."
