# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Native, dependency-free orbitable directionality preview for Qt/QGIS."""

from copy import deepcopy
import math

from qgis.PyQt.QtCore import QPointF, QSize, Qt
from qgis.PyQt.QtGui import QColor, QPainter, QPalette, QPen, QPolygonF
from qgis.PyQt.QtWidgets import QComboBox, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from .directivity import IDENTITY, direction, dot, principal_vector, transform


class DirectivityCanvas(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(280, 200)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.OpenHandCursor)
        self.setAccessibleName("3D directionality preview")
        self.setToolTip("Drag to orbit · Scroll to zoom · Double-click to reset\nOrange: principal · Blue: reference axis")
        self.azimuth, self.elevation, self.zoom = -45., 28., 1.
        self.pose = deepcopy(IDENTITY)
        self.world = False
        self.principal = [1., 0., 0.]
        self.show_principal = False
        self.selected = None
        self.mesh = []
        self.message = "Enter directions and frequency values."
        self._drag = None

    def sizeHint(self):
        return QSize(440, 340)

    def set_pattern(self, value, frequency):
        self.mesh = []
        self.message = ""
        self.principal = principal_vector(value) if value else [1., 0., 0.]
        self.show_principal = value is not None
        if value is None:
            self.message = "Omnidirectional"
            rows = [(direction(0, 0), 0.)]
        else:
            if frequency not in value["frequencies_hz"]:
                self.message = "Choose a frequency."
                self.update()
                return
            index = value["frequencies_hz"].index(frequency)
            rows = [(direction(r["azimuth_deg"], r["elevation_deg"]), r["levels_db"][index]) for r in value["directions"]]
            if not rows or all(level is None for v, level in rows):
                self.message = "No values at this frequency"
                self.update()
                return
        vertices = {}
        for j in range(13):
            for i in range(24):
                v = direction(i * 15, j * 15 - 90)
                nearest = max(rows, key=lambda row: v[0]*row[0][0] + v[1]*row[0][1] + v[2]*row[0][2])
                level = nearest[1]
                # Display radius only: -30..+6 dB, never a propagation calculation.
                radius = .12 + .88 * max(0, min(1, (level + 30) / 36)) if level is not None else None
                vertices[i, j] = [c * radius for c in v] if radius is not None else None
        for j in range(12):
            for i in range(24):
                face = [vertices[i, j], vertices[(i+1) % 24, j], vertices[(i+1) % 24, j+1], vertices[i, j+1]]
                if all(v is not None for v in face):
                    self.mesh.append(face)
        self.update()

    def reset_view(self):
        self.azimuth, self.elevation, self.zoom = -45., 28., 1.
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        palette = self.palette()
        base, text = palette.color(QPalette.Base), palette.color(QPalette.Text)
        dark = base.lightness() < 128
        neutral = palette.color(QPalette.Mid)
        green = QColor("#9bcfbd" if dark else "#367b66")
        orange = QColor("#ffbc81" if dark else "#a24b08")
        blue = QColor("#99cdec" if dark else "#256f9a")
        painter.fillRect(self.rect(), base)
        a, e = math.radians(self.azimuth), math.radians(self.elevation)
        camera = [[-math.sin(a), math.cos(a), 0], [-math.sin(e)*math.cos(a), -math.sin(e)*math.sin(a), math.cos(e)],
                  [math.cos(e)*math.cos(a), math.cos(e)*math.sin(a), math.sin(e)]]
        scale = min(self.width() / 3.6, self.height() / 3.8) * self.zoom
        origin = QPointF(self.width() * .46, self.height() * .56)

        def project(v):
            return QPointF(origin.x() + dot(v, camera[0]) * scale, origin.y() - dot(v, camera[1]) * scale)

        def line(a, b, color, width=1):
            painter.setPen(QPen(color, width))
            painter.drawLine(project(a), project(b))

        for i in range(-4, 5):
            line([i*.35, -1.4, -1], [i*.35, 1.4, -1], neutral)
            line([-1.4, i*.35, -1], [1.4, i*.35, -1], neutral)
        faces = [[transform(self.pose, v) for v in face] for face in self.mesh]
        faces.sort(key=lambda face: sum(dot(v, camera[2]) for v in face))
        fill = QColor(green)
        fill.setAlpha(35)
        painter.setBrush(fill)
        stroke = QColor(green)
        stroke.setAlpha(130)
        painter.setPen(QPen(stroke, .7))
        for face in faces:
            painter.drawPolygon(QPolygonF([project(v) for v in face]))
        painter.setBrush(text)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(origin, 3, 3)
        labels = []

        def arrow(vector, color, label, length=1.35):
            end = [c * length for c in vector]
            q = project(end)
            line([0, 0, 0], end, color, 2)
            angle = math.atan2(q.y()-origin.y(), q.x()-origin.x())
            painter.setBrush(color)
            painter.drawPolygon(QPolygonF([q, QPointF(q.x()-7*math.cos(angle-.45), q.y()-7*math.sin(angle-.45)),
                                           QPointF(q.x()-7*math.cos(angle+.45), q.y()-7*math.sin(angle+.45))]))
            width = painter.fontMetrics().horizontalAdvance(label)
            x = max(6, min(self.width()-width-6, q.x()+5))
            y = max(18, min(self.height()-32, q.y()-5))
            while any(abs(y-previous) < 18 for previous in labels):
                y += 18
            labels.append(y)
            painter.setPen(color)
            painter.drawText(QPointF(x, y), label)

        if self.show_principal:
            arrow(transform(self.pose, self.principal), orange, "Principal")
            vertical = abs(self.principal[2]) > .9
            arrow(transform(self.pose, [0, 1, 0] if vertical else [0, 0, 1]), blue,
                  "Reference left" if vertical else "Reference top", .9)
        if self.selected:
            arrow(transform(self.pose, self.selected), text, "Selected", 1.15)
        # This compass always uses project axes, regardless of source pose.
        corner = QPointF(self.width()-58, self.height()-53)
        for vector, label in zip(IDENTITY, ("E", "N", "Up") if self.world else ("X", "Y", "Z")):
            q = QPointF(corner.x()+dot(vector, camera[0])*22, corner.y()-dot(vector, camera[1])*22)
            painter.setPen(QPen(text, 1))
            painter.drawLine(corner, q)
            painter.drawText(q + QPointF(3, -3), label)
        if self.message:
            painter.setPen(text)
            painter.drawText(self.rect().adjusted(8, 6, -8, -6), Qt.AlignTop | Qt.TextWordWrap, self.message)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag = event.pos()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag is not None:
            delta = event.pos() - self._drag
            self.azimuth -= delta.x() * .5
            self.elevation = max(-85, min(90, self.elevation + delta.y() * .5))
            self._drag = event.pos()
            self.update()

    def mouseReleaseEvent(self, event):
        self._drag = None
        self.setCursor(Qt.OpenHandCursor)

    def wheelEvent(self, event):
        self.zoom = max(.5, min(1.7, self.zoom * (1.1 if event.angleDelta().y() > 0 else 1/1.1)))
        self.update()
        event.accept()

    def mouseDoubleClickEvent(self, event):
        self.reset_view()


class DirectivityPreview(QWidget):
    def __init__(self, parent=None, world=False):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        self.canvas = DirectivityCanvas()
        self.canvas.world = world
        for title, view in (("3D", None), ("Top", (-90, 90)), ("Side", (-90, 0))):
            button = QPushButton(title)
            button.clicked.connect(lambda checked=False, v=view: self.set_view(v))
            bar.addWidget(button)
        bar.addStretch()
        bar.addWidget(QLabel("Hz"))
        self.band = QComboBox()
        bar.addWidget(self.band)
        layout.addLayout(bar)
        layout.addWidget(self.canvas, 1)
        self.caption = QLabel("Illustrative balloon · nearest direction · drag to orbit")
        self.caption.setWordWrap(True)
        layout.addWidget(self.caption)
        self.value = None
        self.band.currentIndexChanged.connect(self.refresh)

    def set_view(self, view):
        if view is None:
            self.canvas.reset_view()
        else:
            self.canvas.azimuth, self.canvas.elevation = view
            self.canvas.update()

    def set_definition(self, value):
        self.value = value
        previous = self.band.currentData() or 1000
        self.band.blockSignals(True)
        self.band.clear()
        for f in value["frequencies_hz"] if value else [1000]:
            self.band.addItem(f"{f:g}", f)
        self.band.setCurrentIndex(max(0, self.band.findData(previous)))
        self.band.blockSignals(False)
        self.refresh()

    def refresh(self):
        self.canvas.set_pattern(self.value, self.band.currentData())

    def set_pose(self, pose):
        self.canvas.pose = deepcopy(pose)
        self.canvas.update()


class PolarProfile(QWidget):
    """A planar polar sketch of the input profile, before revolution."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(145)
        self.setMaximumHeight(185)
        self.points = []
        self.frequency = 1000
        self.setAccessibleName("2D polar profile before revolution")

    def set_profile(self, points, frequency):
        self.points = points
        self.frequency = frequency
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        palette = self.palette()
        painter.fillRect(self.rect(), palette.color(QPalette.Base))
        centre = QPointF(self.width()/2, self.height()/2 - 5)
        scale = min((self.width()-100)/2, (self.height()-45)/2)
        painter.setPen(QPen(palette.color(QPalette.Mid), 1))
        for fraction in (1/3, 2/3, 1):
            painter.drawEllipse(centre, scale*fraction, scale*fraction)
        painter.drawLine(centre-QPointF(scale, 0), centre+QPointF(scale, 0))
        if self.points:
            polygon = []
            for angle, level in self.points + [(360-a, v) for a, v in reversed(self.points)]:
                radius = scale * (.08+.92*max(0, min(1, (level+30)/36)))
                polygon.append(centre + QPointF(radius*math.cos(math.radians(angle)), -radius*math.sin(math.radians(angle))))
            color = QColor("#9bcfbd" if palette.color(QPalette.Base).lightness() < 128 else "#367b66")
            painter.setPen(QPen(color, 2))
            fill = QColor(color)
            fill.setAlpha(35)
            painter.setBrush(fill)
            painter.drawPolygon(QPolygonF(polygon))
        painter.setPen(palette.color(QPalette.Text))
        painter.drawText(centre+QPointF(scale+7, 5), "0°")
        painter.drawText(centre-QPointF(scale+43, -5), "180°")
        painter.drawText(8, self.height()-7, f"{self.frequency:g} Hz · profile points")
