# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Asynchronous calculation engine lifecycle and frontend result persistence."""

from copy import deepcopy
import math
import shutil
import sqlite3
import tempfile

from qgis.PyQt.QtCore import QCoreApplication, QObject, QTimer, pyqtSignal

from .api_client import ApiClient, diagnostic_text, validate_capabilities
from .schema import ModelError
from .run_info import validate_path_counts, validate_work_progress
from .api_diagnostics import named_diagnostic


def _number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def validate_job(value, expected_id=None):
    """Validate lifecycle/progress, keeping service IDs opaque."""
    if not isinstance(value, dict) or not isinstance(value.get("id"), str) or not value["id"]:
        raise ModelError("The engine returned a calculation snapshot without an ID.")
    if expected_id is not None and value["id"] != expected_id:
        raise ModelError("The engine returned a different calculation ID while polling.")
    state = value.get("state")
    if state not in ("queued", "running", "completed", "failed", "cancelled"):
        raise ModelError("The engine returned an unknown calculation state.")
    progress = value.get("progress")
    if not isinstance(progress, dict):
        raise ModelError("The engine calculation snapshot has no progress object.")
    total, done, percent = (progress.get(key) for key in
                            ("total_task_points", "completed_task_points", "percent"))
    if (not all(_number(number) for number in (total, done, percent)) or total <= 0
            or not 0 <= done <= total or not 0 <= percent <= 100
            or not isinstance(progress.get("phase"), str)
            or not math.isclose(percent, 100 * done / total, rel_tol=1e-7, abs_tol=1e-7)):
        raise ModelError("The engine returned invalid calculation progress.")
    if (state == "completed" and (done != total or percent != 100)
            or state != "completed" and percent == 100):
        raise ModelError("The engine returned completion progress inconsistent with its calculation state.")
    summary = progress.get("model_summary")
    validate_path_counts(progress.get("path_counts"))
    validate_work_progress(progress.get("work"))
    if summary is not None and (not isinstance(summary, dict) or any(
            type(summary.get(key)) is not int or summary[key] < 0
            for key in ("source_count", "receiver_count"))):
        raise ModelError("The engine returned invalid source or receiver counts.")
    links = value.get("links")
    if not isinstance(links, dict) or any(not isinstance(links.get(key), str) or not links[key]
                                          for key in ("status", "results")):
        raise ModelError("The engine calculation snapshot is missing status or result links.")
    if state in ("failed", "cancelled") and diagnostic_text(value.get("error")) is None:
        raise ModelError("The engine terminal snapshot is missing its diagnostic.")
    sequence = value.get("sequence")
    if type(sequence) is not int or sequence < 0:
        raise ModelError("The engine calculation snapshot has an invalid sequence.")
    return value


class EngineRunner(QObject):
    succeeded = pyqtSignal(object, object)
    failed = pyqtSignal(object, str)
    stopped = pyqtSignal(object)
    busyChanged = pyqtSignal(bool)
    variantStarted = pyqtSignal(object)
    progressChanged = pyqtSignal(object, object)

    POLL_INTERVAL_MS = 500
    MAX_GET_RETRIES = 3
    # Source contributions scale with sources × receivers. Completed results
    # can be much larger than discovery/status responses (2,800 sources ×
    # 369 receivers: about 668 MiB).
    RESULT_MAX_RESPONSE_BYTES = 1024 * 1024 * 1024
    RESULT_TIMEOUT_MS = 5 * 60 * 1000

    def __init__(self, parent=None):
        super().__init__(parent)
        self.context = None
        self._busy = False
        self._pending = []
        self._client = None
        self._job = None
        self._snapshot = None
        self._cancelled = False
        self._delete_sent = False
        self._submitting = False
        self._generation = 0
        self._staging = None
        self._poll_timer = QTimer(self)
        self._poll_timer.setSingleShot(True)
        self._poll_timer.timeout.connect(self._poll)

    @property
    def active(self):
        return self._busy

    def start(self, store, variant_id, validate=False, engine_id=None):
        self.start_batch(store, [variant_id], validate=validate, engine_id=engine_id)

    def start_batch(self, store, variant_ids, validate=False, engine_id=None, *, relationships=False):
        if self.active:
            raise ModelError("A calculation is already running.")
        from .calculation_engines import calculation_engine
        engine_id = engine_id or store.calculation()["engine"]
        self._engine = calculation_engine(engine_id)
        self._connection = deepcopy(self._engine.connection)
        self._expected_capabilities = deepcopy(self._engine.capabilities)
        if relationships and "source_receiver_transfers" not in self._expected_capabilities.get("features", []):
            raise ModelError("The selected calculation engine does not provide source-power back-calculation. Update the engine and refresh its capabilities in Settings.")
        variants = {row["variant_id"]: row["name"] for row in store.variants()}
        selected = list(dict.fromkeys(variant_ids))
        if not selected or any(value not in variants for value in selected):
            raise ModelError("Choose at least one existing variant to calculate.")
        self._client = ApiClient(self, connection=self._connection)
        self._pending = [{"store": store, "model_path": store.path,
                          "variant_id": value, "variant_name": variants[value],
                          "validate": validate, "relationships": relationships, "engine": engine_id,
                          "api_name": self._connection["name"],
                          "batch_index": index, "batch_total": len(selected)}
                         for index, value in enumerate(selected)]
        self._generation += 1
        self._cancelled = False
        self._busy = True
        self.busyChanged.emit(True)
        if self._cancelled:
            self.shutdown()
        else:
            self._start_next()

    def start_relationships(self, store, variant_id, engine_id=None):
        self.start_batch(store, [variant_id], engine_id=engine_id, relationships=True)

    def _start_next(self):
        if not self._busy or not self._pending:
            return
        self.context = self._pending.pop(0)
        self._snapshot = None
        self._job = None
        self._submitting = False
        self._delete_sent = False
        self.variantStarted.emit(self.context)
        if self.context is None:
            return
        self.progressChanged.emit(self.context, {"phase": "loading", "completed": 0, "total": 0})
        if self.context is None:
            return
        self._get("healthz", self._health_received)

    def _health_received(self, health):
        from .api_connections import health_problem
        problem = health_problem(health, self._connection)
        if problem:
            raise ModelError(problem)
        self._get(getattr(self._engine, "capabilities_path", "v1/capabilities"), self._capabilities_received)

    def _get(self, url, callback, attempt=0, **request_options):
        generation = self._generation

        def received(data, error, headers):
            if generation != self._generation or self.context is None:
                return
            if error and error.transient and attempt < self.MAX_GET_RETRIES:
                QTimer.singleShot(min(2000, self.POLL_INTERVAL_MS * (attempt + 1)),
                                  lambda: self._retry_get(generation, url, callback, attempt + 1,
                                                          **request_options))
                return
            if error:
                detail = str(error)
                if self._job:
                    detail += (f" The existing job {self._job['id']} was not resubmitted. "
                               "The service may still retain it; previous saved results are unchanged.")
                self._complete("failed", detail)
                return
            if headers.get(":status") != 200:
                self._complete("failed", "The engine returned an unexpected response status for a GET request.")
                return
            try:
                callback(data)
            except (ModelError, ValueError, TypeError, KeyError, AttributeError,
                    OSError, RuntimeError, sqlite3.Error) as problem:
                self._complete("failed", str(problem))

        try:
            self._client.request("GET", url, None, received, **request_options)
        except (ModelError, ValueError) as problem:
            self._complete("failed", str(problem))

    def _retry_get(self, generation, url, callback, attempt, **request_options):
        if generation == self._generation and self.context is not None:
            self._get(url, callback, attempt, **request_options)

    def _capabilities_received(self, capabilities):
        from .api_model import prepare_calculation, prepare_relationships
        validate_capabilities(capabilities)
        expected = self._expected_capabilities
        if (capabilities["method"] != expected.get("method")
                or capabilities["implementation"]["name"] != expected.get("implementation", {}).get("name")):
            raise ModelError("The engine now advertises a different engine. Refresh capabilities in Settings and select it again.")
        self._backend = {"transport": "http_json_v1", "api_version": capabilities["api_version"],
                         "connection_id": self._connection["id"], "api_name": self._connection["name"],
                         "base_url": self._connection["base_url"], "engine_id": self.context["engine"],
                         "implementation": deepcopy(capabilities["implementation"]),
                         "method": capabilities["method"],
                         "atmospheric_absorption": capabilities["atmospheric_absorption"],
                         "capabilities": deepcopy(capabilities)}
        self.progressChanged.emit(self.context, {"phase": "checking", "completed": 0, "total": 0})
        if self.context is None:
            return
        prepare = prepare_relationships if self.context.get("relationships") else prepare_calculation
        self._snapshot = prepare(self.context["store"], self.context["variant_id"], capabilities)
        request = self._snapshot["request"]
        self.context["model_summary"] = {
            "source_count": len(request["sources"]),
            "receiver_count": len(request.get("receivers", [])),
            "area_receiver_count": len(request.get("area_receivers", [])),
            "building_count": len(request.get("buildings", [])),
            "barrier_count": len(request.get("barriers", [])) + len(request.get("reflectors", [])),
            "receivers_resolved": not request.get("area_receivers"),
        }
        self.progressChanged.emit(self.context, {"phase": "preparing"})
        if self.context is None:
            return
        if self.context["validate"]:
            self._complete("succeeded", {"valid": True, "validation_scope": "local_api_adapter",
                                         "source_count": len(request["sources"]),
                                         "receiver_count": len(request.get("receivers", [])),
                                         "area_receiver_count": len(request.get("area_receivers", [])),
                                         "building_count": len(request.get("buildings", [])),
                                         "ground_area_count": len(request.get("ground", {}).get("regions", [])),
                                         "tree_area_count": len(request.get("misc_regions", [])),
                                         "barrier_count": len(request.get("barriers", [])),
                                         "reflector_count": len(request.get("reflectors", []))})
            return
        self._submitting = True
        self._client.request("POST", getattr(self._engine, "calculations_path", "v1/calculations"), request, self._submitted)

    def _submitted(self, data, error, headers):
        self._submitting = False
        if self.context is None:
            return
        if error:
            self._complete("failed", str(error) + (
                " The submission was not retried. Its outcome may be unknown; check the engine before starting another job."
                if error.transient else " The submission was not retried."))
            return
        if headers.get(":status") != 202:
            self._complete("failed", "The engine did not acknowledge the submission with HTTP 202. It was not retried.")
            return
        try:
            self._accept_job(data)
        except (ModelError, ValueError, TypeError, KeyError) as problem:
            self._complete("failed", str(problem) + " The submission was not retried.")

    def _accept_job(self, value):
        previous = self._job
        job = validate_job(value, previous["id"] if previous else None)
        if previous and previous["state"] == "running" and job["state"] == "queued":
            raise ModelError("The engine calculation state moved backwards from running to queued.")
        if (previous and job["sequence"] == previous["sequence"]
                and (job["state"] != previous["state"] or job["progress"] != previous["progress"])):
            raise ModelError("The engine changed calculation progress without advancing its sequence.")
        if previous and (job["sequence"] < previous["sequence"]
                         or job["progress"]["completed_task_points"] < previous["progress"]["completed_task_points"]
                         or job["progress"]["total_task_points"] != previous["progress"]["total_task_points"]):
            raise ModelError("The engine returned decreasing progress or changed the calculation total.")
        # Resolve both links before using either; opaque IDs never become paths.
        old_counts = previous["progress"].get("path_counts") if previous else None
        counts = job["progress"].get("path_counts")
        if old_counts is not None and (counts is None or any(
                counts[key] < old_counts[key] for key in ("checked", "pruned"))):
            raise ModelError("The engine returned decreasing calculation counts.")
        job = deepcopy(job)
        for key in ("status", "results"):
            job["links"][key] = self._client.resolve_url(job["links"][key])
        self._job = job
        if job["progress"].get("model_summary") is not None:
            summary = job["progress"]["model_summary"]
            if summary["source_count"] != len(self._snapshot["request"]["sources"]):
                raise ModelError("The engine source count disagrees with the submitted model.")
            self.context["model_summary"].update(
                source_count=summary["source_count"], receiver_count=summary["receiver_count"],
                receivers_resolved=True)
        self.context["api_job"] = {"id": job["id"], "status_url": job["links"]["status"]}
        generation = self._generation
        self.progressChanged.emit(self.context, dict(job["progress"], state=job["state"]))
        if self.context is None or generation != self._generation:
            return
        if job["state"] == "completed":
            self._get(job["links"]["results"], self._results_received,
                      max_response_bytes=self.RESULT_MAX_RESPONSE_BYTES,
                      timeout_ms=self.RESULT_TIMEOUT_MS)
        elif job["state"] == "failed":
            self._complete("failed", diagnostic_text(job["error"]))
        elif job["state"] == "cancelled":
            self._complete("stopped")
        elif self._cancelled and not self._delete_sent:
            self._delete()
        else:
            self._poll_timer.start(self.POLL_INTERVAL_MS)

    def _poll(self):
        if self.context is not None and self._job is not None:
            self._get(self._job["links"]["status"], self._accept_job)

    def _results_received(self, result):
        if self.context.get("relationships"):
            from .api_model import relationships_from_result
            self._complete("succeeded", relationships_from_result(self._snapshot, result))
            return
        from .api_model import area_raster_downloads
        from .area_results import results_directory
        downloads = area_raster_downloads(self._snapshot, result)
        self._result, self._rasters = result, {}
        if downloads:
            # Stage beside the final folder so finished files stay on one volume.
            directory = results_directory(self.context["store"])
            directory.mkdir(exist_ok=True)
            self._staging = tempfile.mkdtemp(prefix=".download-", dir=directory)
        self._download_next(downloads, 0)

    def _download_next(self, downloads, attempt):
        """Fetch area rasters one at a time, after the result document is held."""
        if not downloads:
            from .api_model import save_completed_calculation
            summary = save_completed_calculation(self.context["store"], self._snapshot, self._result,
                                                 self._backend, self._job, self._rasters)
            self._complete("succeeded", summary)
            return
        identity, raster = downloads[0]
        self.progressChanged.emit(self.context, {"phase": "downloading", "downloaded": len(self._rasters),
                                                 "total_rasters": len(self._rasters) + len(downloads)})
        if self.context is None:
            return
        generation = self._generation
        path = f"{self._staging}/area-{len(self._rasters)}.tif"

        def received(info, error, headers):
            if generation != self._generation or self.context is None:
                return
            if error and error.transient and attempt < self.MAX_GET_RETRIES:
                QTimer.singleShot(min(2000, self.POLL_INTERVAL_MS * (attempt + 1)),
                                  lambda: generation == self._generation and self.context is not None
                                  and self._download_next(downloads, attempt + 1))
                return
            try:
                if error:
                    raise ModelError("Could not download an area result raster: " + str(error)
                                     + " Previous saved results are unchanged.")
                if headers.get(":status") != 200:
                    raise ModelError("The engine returned an unexpected response status for an area raster.")
                self._rasters[identity] = info
                self._download_next(downloads[1:], 0)
            except (ModelError, ValueError, TypeError, KeyError, AttributeError,
                    OSError, RuntimeError, sqlite3.Error) as problem:
                self._complete("failed", str(problem))

        try:
            self._client.download(raster["href"], path, received, media_type=raster["media_type"],
                                  max_response_bytes=raster["bytes"], timeout_ms=self.RESULT_TIMEOUT_MS)
        except (ModelError, ValueError, OSError) as problem:
            self._complete("failed", str(problem))

    def _complete(self, outcome, value=None):
        context = self.context
        if context is None:
            return
        if outcome == "failed":
            context["api_error_raw"] = str(value)
            value = named_diagnostic(str(value), self._snapshot)
        self.context = None
        self._submitting = False
        self._poll_timer.stop()
        self._client.abort_all()
        self._generation += 1
        self._discard_staging()
        continuing = outcome == "succeeded" and bool(self._pending) and not self._cancelled
        if not continuing:
            self._pending.clear()
            self._busy = False
            self._client.deleteLater()
            self._client = None
            self.busyChanged.emit(False)
        if outcome == "succeeded":
            self.succeeded.emit(context, value)
        elif outcome == "failed":
            self.failed.emit(context, value)
        elif outcome == "stopped":
            self.stopped.emit(context)
        if continuing:
            if self._cancelled:
                self._pending.clear()
                self._busy = False
                self._client.deleteLater()
                self._client = None
                self.busyChanged.emit(False)
            elif self._busy:
                self._start_next()

    def _discard_staging(self):
        # Saved rasters were moved out; anything left is a partial download.
        if self._staging:
            shutil.rmtree(self._staging, ignore_errors=True)
        self._staging = None
        self._result = self._rasters = None

    def _delete(self):
        self._delete_sent = True
        self._poll_timer.stop()
        self._client.abort_all()
        # Invalidate any delayed status retry which predates cancellation.
        self._generation += 1

        def received(data, error, headers):
            if self.context is None:
                return
            if error:
                self._complete("failed", "Could not confirm cancellation: " + str(error)
                               + " The engine job may still be running. Previous saved results are unchanged.")
                return
            try:
                if headers.get(":status") != 200:
                    raise ModelError("The engine returned an unexpected cancellation status.")
                self._accept_job(data)
            except (ModelError, ValueError, TypeError, KeyError) as problem:
                self._complete("failed", str(problem))

        self._client.request("DELETE", self._job["links"]["status"], None, received)

    def cancel(self):
        self._cancelled = True
        self._pending.clear()
        if self.context is None:
            return
        if self._submitting:
            # Keep the one POST alive so its ID can be cancelled after acceptance.
            return
        if self._job and self._job["state"] in ("queued", "running"):
            if not self._delete_sent:
                self._delete()
        elif self._job is None:
            self._complete("stopped")

    def shutdown(self):
        """Detach a bounded best-effort DELETE before this QObject disappears."""
        self._pending.clear()
        self._poll_timer.stop()
        if self._client is not None and self._submitting:
            # Retain only the client until its finite timeout, then cancel the
            # returned job without accessing the departing model or this runner.
            client = self._client
            client.setParent(QCoreApplication.instance())

            def finish(data, error, headers):
                if error is None and isinstance(data, dict) and isinstance(data.get("links"), dict):
                    link = data["links"].get("status")
                    if link:
                        try:
                            client.request("DELETE", link, None, lambda *_: client.deleteLater())
                            return
                        except (ModelError, ValueError):
                            pass
                client.deleteLater()

            for item in client._pending.values():
                item["callback"] = finish
            self._client = None
        elif self._client is not None:
            self._client.abort_all()
            if self._job and self._job["state"] in ("queued", "running"):
                client = self._client
                client.setParent(QCoreApplication.instance())
                client.request("DELETE", self._job["links"]["status"], None, lambda *_: client.deleteLater())
            else:
                self._client.deleteLater()
            self._client = None
        self.context = None
        self._job = None
        self._submitting = False
        self._generation += 1
        self._discard_staging()
        if self._busy:
            self._busy = False
            self.busyChanged.emit(False)
