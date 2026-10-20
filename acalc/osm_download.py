# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Cancellable Overpass requests through QGIS's proxy-aware network manager."""

import json
from urllib.parse import urlencode

from qgis.PyQt.QtCore import QByteArray, QObject, QTimer, QUrl, pyqtSignal
from qgis.PyQt.QtNetwork import QNetworkReply, QNetworkRequest
from qgis.core import QgsNetworkAccessManager

from .osm_import import overpass_query


OVERPASS_URL = "https://overpass-api.de/api/interpreter"
MAX_RESPONSE_BYTES = 20 * 1024 * 1024


class OsmDownload(QObject):
    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._reply = None
        self._data = bytearray()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._timed_out)

    def start(self, bbox):
        query = overpass_query(bbox)
        self.cancel()
        request = QNetworkRequest(QUrl(OVERPASS_URL))
        request.setHeader(QNetworkRequest.ContentTypeHeader, "application/x-www-form-urlencoded")
        request.setRawHeader(b"User-Agent", b"Acalc/OpenStreetMap-building-import")
        request.setAttribute(QNetworkRequest.RedirectPolicyAttribute, QNetworkRequest.NoLessSafeRedirectPolicy)
        self._reply = QgsNetworkAccessManager.instance().post(
            request, QByteArray(urlencode({"data": query}).encode("utf-8")))
        self._reply.setReadBufferSize(1024 * 1024)
        self._reply.readyRead.connect(self._read)
        self._reply.finished.connect(self._finished)
        self._timer.start(90000)

    def cancel(self):
        self._timer.stop()
        reply, self._reply = self._reply, None
        self._data.clear()
        if reply is not None:
            reply.readyRead.disconnect(self._read)
            reply.finished.disconnect(self._finished)
            reply.abort()
            reply.deleteLater()

    def _fail(self, message):
        self.cancel()
        self.failed.emit(message)

    def _timed_out(self):
        self._fail("The OpenStreetMap download timed out. Try a smaller area or try again later.")

    def _read(self):
        if self._reply is None:
            return
        self._data.extend(bytes(self._reply.readAll()))
        if len(self._data) > MAX_RESPONSE_BYTES:
            self._fail("The OpenStreetMap response is too large. Select a smaller area.")

    def _finished(self):
        self._read()
        reply = self._reply
        if reply is None:
            return
        status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
        if status is not None and int(status) != 200:
            self._fail(f"OpenStreetMap download failed (HTTP {status}). The service may be busy; try again later or use a smaller area.")
            return
        if reply.error() != QNetworkReply.NoError:
            self._fail(f"OpenStreetMap download failed: {reply.errorString()}")
            return
        try:
            payload = json.loads(bytes(self._data))
        except (ValueError, UnicodeError):
            self._fail("The OpenStreetMap service returned an invalid response. Try again later.")
            return
        self.cancel()
        self.succeeded.emit(payload)
