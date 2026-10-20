# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Bounded asynchronous JSON requests for calculation protocol version 1.

Requests never retry automatically, in particular non-idempotent submissions.
All callbacks run on the owning Qt thread. Aborted requests have no callback.
"""

import hashlib
from copy import deepcopy
import json
import math
import os
from urllib.parse import urljoin, urlsplit

from qgis.PyQt.QtCore import QObject, QTimer, QUrl
from qgis.PyQt.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from .schema import ModelError


ERROR_BODY_LIMIT = 1024 * 1024


class ApiError(ModelError):
    def __init__(self, message, *, status=None, code=None, transient=False):
        super().__init__(message)
        self.status = status
        self.code = code
        self.transient = transient


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("nonfinite number")
    return number


def _invalid_constant(value):
    raise ValueError("nonfinite number " + value)


def validate_capabilities(value):
    """Validate discovery structure, without claiming acoustic compatibility."""
    if not isinstance(value, dict):
        raise ModelError("The engine capabilities response must be a JSON object.")
    version = value.get("api_version")
    if not isinstance(version, str) or version.split(".")[0] != "1":
        raise ModelError("This plugin requires calculation protocol version 1.")
    implementation = value.get("implementation")
    if not isinstance(implementation, dict) or any(
            not isinstance(implementation.get(key), str) or not implementation[key].strip()
            for key in ("name", "version")):
        raise ModelError("The engine capabilities response is missing its implementation name or version.")
    for key in ("method", "atmospheric_absorption"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise ModelError("The engine capabilities response is missing " + key + ".")
    return value


def diagnostic_text(value):
    if not isinstance(value, dict) or not isinstance(value.get("message"), str):
        return None
    message = value["message"]
    if isinstance(value.get("field"), str) and value["field"]:
        message = value["field"] + ": " + message
    if isinstance(value.get("code"), str) and value["code"]:
        message += " [" + value["code"] + "]"
    return message


def _origin(url):
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ModelError("An engine address must be an http:// or https:// URL with a host.")
    if parts.username is not None or parts.password is not None or parts.fragment:
        raise ModelError("Engine addresses cannot contain credentials or fragments.")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as error:
        raise ModelError("The engine address has an invalid port.") from error
    return parts.scheme, parts.hostname.lower(), port


class ApiClient(QObject):
    def __init__(self, parent=None, *, base_url=None, connection=None, timeout_ms=15000):
        super().__init__(parent)
        self.base_url = base_url.rstrip("/") if base_url else None
        self.timeout_ms = timeout_ms
        self.manager = QNetworkAccessManager(self)
        self._pending = {}
        self._connection = None
        self._client_key = None
        if connection is not None:
            self.configure_connection(connection)
        if self.base_url:
            _origin(self.base_url)

    def configure_connection(self, connection, *, client_key=None):
        """Pin the endpoint and auth reference; leave secret lookup until needed."""
        if self._pending:
            raise ModelError("Wait for the current engine request before changing its connection.")
        base_url = connection["base_url"].rstrip("/")
        _origin(base_url)
        self.base_url = base_url
        self._connection = deepcopy(connection)
        if client_key is not None:
            from .api_connections import validate_client_key
            validate_client_key(client_key)
        self._client_key = client_key

    def clear_credentials(self):
        """Discard transient credentials after a check or cancellation."""
        self._client_key = None

    def _authorize(self, request, method, absolute):
        # Health must work before unlocking the encrypted credential store, and
        # never sends a key (including when the URL was returned by a server).
        health_path = urlsplit(urljoin(self.base_url + "/", "healthz")).path if self.base_url else "/healthz"
        if method == "GET" and urlsplit(absolute).path in ("/healthz", health_path):
            return
        if self._connection is None:
            return
        if self._client_key is None:
            from .api_connections import connection_client_key
            key = connection_client_key(self._connection)
            if not isinstance(key, str) or any(ord(c) < 33 or ord(c) > 126 for c in key):
                raise ModelError("The saved client key is invalid. Update the key in the engine connection settings.")
            self._client_key = key
        if self._client_key:
            request.setRawHeader(b"Authorization", b"Bearer " + self._client_key.encode("ascii"))

    def _redact(self, value):
        """Remove reflected credentials before diagnostics or JSON can be saved.

        Walk decoded responses in place to avoid copying large result documents.
        The credential itself lives only in this client and QgsAuthManager.
        """
        if not self._client_key:
            return value
        if isinstance(value, str):
            return value.replace(self._client_key, "[redacted]")
        if isinstance(value, dict):
            for key in list(value):
                clean_key = self._redact(key)
                item = self._redact(value[key])
                if clean_key != key:
                    del value[key]
                value[clean_key] = item
        elif isinstance(value, list):
            for index, item in enumerate(value):
                value[index] = self._redact(item)
        return value

    def _http_error(self, status, diagnostic=None):
        if status == 401:
            return ApiError("HTTP 401: The calculation server did not accept the client key. "
                            "Check the key in the Remote connection settings.",
                            status=401, code="unauthorized")
        diagnostic = self._redact(diagnostic)
        return ApiError(f"HTTP {status}: " + (diagnostic_text(diagnostic) or "The service rejected the request."),
                        status=status, code=diagnostic.get("code") if isinstance(diagnostic, dict) else None,
                        transient=status in (429, 502, 503, 504))

    def resolve_url(self, url):
        if not isinstance(url, str) or not url.strip():
            raise ModelError("The calculation engine returned a missing or invalid link.")
        absolute = urljoin(self.base_url + "/", url) if self.base_url else url
        origin = _origin(absolute)
        if self.base_url and origin != _origin(self.base_url):
            raise ModelError("The calculation engine returned a link to a different server. Refresh its capabilities and address.")
        return absolute

    def request(self, method, url, json_data, callback, timeout_ms=None,
                max_response_bytes=128 * 1024 * 1024):
        """Invoke callback(object, ApiError-or-None, headers) exactly once."""
        absolute = self.resolve_url(url)
        method = method.upper()
        request = QNetworkRequest(QUrl(absolute))
        request.setRawHeader(b"Accept", b"application/json")
        request.setAttribute(QNetworkRequest.RedirectPolicyAttribute, QNetworkRequest.ManualRedirectPolicy)
        self._authorize(request, method, absolute)
        payload = b""
        if json_data is not None:
            try:
                payload = json.dumps(json_data, allow_nan=False, separators=(",", ":")).encode("utf-8")
            except (TypeError, ValueError, OverflowError) as error:
                raise ModelError("The calculation request cannot be encoded as finite JSON: " + str(error)) from error
            request.setHeader(QNetworkRequest.ContentTypeHeader, "application/json")
        reply = self.manager.sendCustomRequest(request, method.encode("ascii"), payload)
        timer = QTimer(self)
        timer.setSingleShot(True)
        item = {"callback": callback, "buffer": bytearray(), "timer": timer,
                "problem": None, "limit": max_response_bytes}
        self._pending[reply] = item
        reply.readyRead.connect(lambda: self._read(reply))
        reply.finished.connect(lambda: self._finished(reply))
        timer.timeout.connect(lambda: self._timeout(reply))
        timer.start(self.timeout_ms if timeout_ms is None else timeout_ms)
        return reply

    def download(self, url, path, callback, *, media_type, max_response_bytes, timeout_ms=None):
        """Stream a binary artifact to path; invoke callback(info, error, headers) once.

        info is {"path", "bytes", "sha256"}. The partial file is removed on error.
        """
        absolute = self.resolve_url(url)
        request = QNetworkRequest(QUrl(absolute))
        request.setRawHeader(b"Accept", media_type.encode("ascii"))
        request.setAttribute(QNetworkRequest.RedirectPolicyAttribute, QNetworkRequest.ManualRedirectPolicy)
        self._authorize(request, "GET", absolute)
        output = open(path, "wb")
        reply = self.manager.get(request)
        timer = QTimer(self)
        timer.setSingleShot(True)
        item = {"callback": callback, "buffer": bytearray(), "timer": timer, "problem": None,
                "limit": max_response_bytes, "file": output, "path": path, "hash": hashlib.sha256(),
                "written": 0, "media_type": media_type}
        self._pending[reply] = item
        reply.readyRead.connect(lambda: self._read(reply))
        reply.finished.connect(lambda: self._finished(reply))
        timer.timeout.connect(lambda: self._timeout(reply))
        timer.start(self.timeout_ms if timeout_ms is None else timeout_ms)
        return reply

    def _read(self, reply):
        item = self._pending.get(reply)
        if item is None:
            return
        if reply.isOpen():
            chunk = bytes(reply.readAll())
            status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
            if item.get("file") is not None and status and 200 <= status < 300:
                # Artifacts stream to disk; only error bodies are buffered.
                item["written"] += len(chunk)
                if item["problem"] is None and item["written"] <= item["limit"]:
                    item["hash"].update(chunk)
                    item["file"].write(chunk)
            else:
                item["buffer"].extend(chunk)
        # A download's limit is the artifact size; its JSON error body has its own.
        buffered_limit = ERROR_BODY_LIMIT if item.get("file") is not None else item["limit"]
        if item["problem"] is None and (len(item["buffer"]) > buffered_limit
                                        or item.get("written", 0) > item["limit"]):
            item["problem"] = ApiError(
                f"The engine response exceeds the allowed size ({item['limit']:,} bytes).",
                code="response_too_large")
            reply.abort()

    def _timeout(self, reply):
        item = self._pending.get(reply)
        if item is not None:
            item["problem"] = ApiError("The engine request timed out. Check the connection and service health.", transient=True)
            reply.abort()

    def _finished(self, reply):
        if reply not in self._pending:
            return
        self._read(reply)
        item = self._pending.pop(reply, None)
        if item is None:
            return
        item["timer"].stop()
        item["timer"].deleteLater()
        status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
        headers = {bytes(key).decode("latin-1").lower(): bytes(value).decode("latin-1")
                   for key, value in reply.rawHeaderPairs()}
        self._redact(headers)
        headers[":status"] = status
        if item.get("file") is not None:
            self._finish_download(reply, item, status, headers)
            return
        error = self._http_error(status) if status == 401 else item["problem"]
        data = None
        if error is None:
            try:
                data = json.loads(item["buffer"].decode("utf-8"),
                                  parse_float=_finite_float, parse_constant=_invalid_constant)
                if not isinstance(data, dict):
                    raise ValueError("expected a JSON object")
                self._redact(data)
            except (ValueError, UnicodeError, RecursionError) as problem:
                if status and 200 <= status < 300:
                    error = ApiError("The engine returned invalid JSON: " + self._redact(str(problem)), status=status)
            if status and not 200 <= status < 300:
                diagnostic = data.get("error") if data else None
                error = self._http_error(status, diagnostic)
            elif reply.error() != QNetworkReply.NoError:
                error = ApiError("Could not reach the calculation engine: " + self._redact(reply.errorString()),
                                 status=status, transient=True)
            elif error is None and not headers.get("content-type", "").lower().split(";", 1)[0].strip() == "application/json":
                error = ApiError("The engine response has an invalid content type; expected application/json.", status=status)
        # Persistence can itself be memory intensive. Release the downloaded
        # JSON before passing the decoded object to the consumer.
        item["buffer"].clear()
        reply.deleteLater()
        item["callback"](data if error is None else None, error, headers)

    def _finish_download(self, reply, item, status, headers):
        item["file"].close()
        error = self._http_error(status) if status == 401 else item["problem"]
        if error is None and status and not 200 <= status < 300:
            try:
                diagnostic = json.loads(item["buffer"].decode("utf-8")).get("error")
            except (ValueError, UnicodeError, AttributeError):
                diagnostic = None
            error = self._http_error(status, diagnostic)
        elif error is None and reply.error() != QNetworkReply.NoError:
            error = ApiError("Could not reach the calculation engine: " + self._redact(reply.errorString()),
                             status=status, transient=True)
        elif error is None and headers.get("content-type", "").lower().split(";", 1)[0].strip() != item["media_type"]:
            error = ApiError(f"The engine artifact has an invalid content type; expected {item['media_type']}.", status=status)
        info = None
        if error is None:
            info = {"path": item["path"], "bytes": item["written"], "sha256": item["hash"].hexdigest()}
        else:
            _remove(item["path"])
        item["buffer"].clear()
        reply.deleteLater()
        item["callback"](info, error, headers)

    def abort_all(self):
        """Discard callbacks before aborting, including synchronous Qt signals."""
        pending, self._pending = self._pending, {}
        for reply, item in pending.items():
            item["timer"].stop()
            item["timer"].deleteLater()
            if item.get("file") is not None:
                item["file"].close()
                _remove(item["path"])
            reply.abort()
            reply.deleteLater()


def _remove(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
