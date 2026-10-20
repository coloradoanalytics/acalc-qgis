# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""A face-on drawing with draggable point and resizable area sources."""

import math

from qgis.PyQt.QtCore import QPointF, QRectF, QSize, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QPainter, QPainterPath, QPen, QPolygonF
from qgis.PyQt.QtWidgets import QSizePolicy, QWidget


from .units import length_factor


class FaceSourcePreview(QWidget):
    source_selected = pyqtSignal(int)
    rectangle_changed = pyqtSignal(int, object)

    def __init__(self, face, parent=None, input_unit="m"):
        super().__init__(parent)
        self.input_unit = input_unit
        self.factor = length_factor(input_unit)
        self.face = face
        self.sources = []
        self.selected_row = -1
        self.invalid_rows = set()
        self._drag = None
        self._pan_drag = None
        self._zoom = 1.0
        self._pan = QPointF()
        self.setMinimumSize(280, 220)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAccessibleName('2D building face source editor')
        self.setAccessibleDescription('The selected source is highlighted. Other sources are grey. '
                                      'Drag a point marker or anywhere inside an area source to select and move it, '
                                      f'or an area’s bottom-right handle to resize it on a 0.01 {input_unit} grid. '
                                      'Scroll to zoom; drag empty space or middle-drag to pan. Home resets the view.')

    def sizeHint(self):
        return QSize(700, 320)

    def set_sources(self, sources, selected_row, invalid_rows=()):
        self.sources = sources
        self.selected_row = selected_row
        self.invalid_rows = set(invalid_rows)
        if self._drag and not any(s['row'] == selected_row and s['id'] == self._drag['source']['id'] for s in sources):
            self._drag = None
        self.update()

    def face_bounds(self):
        points = self.face['uv']
        left, top = min(p[0] for p in points), min(p[1] for p in points)
        return QRectF(left, top, max(p[0] for p in points) - left, max(p[1] for p in points) - top)

    def _transform(self):
        bounds = self.face_bounds()
        available = QRectF(self.rect()).adjusted(64, 42, -30, -30)
        scale = min(available.width() / max(bounds.width(), 1e-8),
                    available.height() / max(bounds.height(), 1e-8))
        scale *= self._zoom
        origin = available.center() + self._pan - bounds.center() * scale
        return origin, scale

    def face_to_screen(self, x, y):
        origin, scale = self._transform()
        return origin + QPointF(x * scale, y * scale)

    def source_rect(self, source):
        if source.get('type') == 'point':
            center = self.face_to_screen(source['x_m'], source['y_m'])
            return QRectF(center.x() - 7, center.y() - 7, 14, 14)
        return QRectF(self.face_to_screen(source['x_m'], source['y_m']),
                      self.face_to_screen(source['x_m'] + source['width_m'],
                                          source['y_m'] + source['height_m']))

    def _draw_order(self):
        # Point markers remain visible and selectable when they overlap an area.
        return sorted(self.sources, key=lambda source: (source.get('type') == 'point', source['row'] == self.selected_row))

    def source_at(self, position):
        for source in reversed(self._draw_order()):
            if self.source_rect(source).contains(QPointF(position)):
                return source['row']
        return None

    def handle_rects(self, source):
        if source.get('type') == 'point':
            return {}
        rectangle = self.source_rect(source)
        # Keep the resize target outside the body so even a tiny source can
        # still be moved by grabbing its interior.
        return {'resize': QRectF(rectangle.right(), rectangle.bottom(), 8, 8)}

    def handle_at(self, position):
        source = next((s for s in self.sources if s['row'] == self.selected_row), None)
        if source:
            for name, rectangle in self.handle_rects(source).items():
                if rectangle.adjusted(0, 0, 2, 2).contains(QPointF(position)):
                    return name
        return None

    def _drag_to(self, position):
        source = self._drag['source']
        delta = (QPointF(position) - self._drag['start']) / self._drag['scale']
        keys = ('x_m', 'y_m') if source.get('type') == 'point' else ('x_m', 'y_m', 'width_m', 'height_m')
        values = {key: source[key] for key in keys}
        if self._drag['mode'] == 'move':
            values['x_m'] = max(0, round((source['x_m'] + delta.x()) / self.factor, 2) * self.factor)
            values['y_m'] = max(0, round((source['y_m'] + delta.y()) / self.factor, 2) * self.factor)
        else:
            values['width_m'] = max(0.01 * self.factor, round((source['width_m'] + delta.x()) / self.factor, 2) * self.factor)
            values['height_m'] = max(0.01 * self.factor, round((source['height_m'] + delta.y()) / self.factor, 2) * self.factor)
        self.rectangle_changed.emit(source['row'], values)

    @staticmethod
    def _grid_step(span):
        target = max(span, 1e-8) / 6
        magnitude = 10 ** math.floor(math.log10(target))
        return next(factor * magnitude for factor in (1, 2, 5, 10) if factor * magnitude >= target)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        canvas = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QPen(QColor('#d6dfe8'), 1))
        painter.setBrush(QColor('#f4f7fa'))
        painter.drawRoundedRect(canvas, 6, 6)
        painter.setClipRect(canvas.adjusted(1, 1, -1, -1))
        boundary = QPolygonF([self.face_to_screen(*p) for p in self.face['uv']])
        bounds = self.face_bounds()
        screen = boundary.boundingRect()
        painter.setBrush(QColor('#ffffff'))
        painter.setPen(Qt.NoPen)
        painter.drawPolygon(boundary)
        step = self._grid_step(max(bounds.width(), bounds.height()) / self.factor) * self.factor
        xs = [i * step for i in range(math.ceil(bounds.left() / step), math.floor(bounds.right() / step) + 1)]
        ys = [i * step for i in range(math.ceil(bounds.top() / step), math.floor(bounds.bottom() / step) + 1)]
        painter.save()
        clip = QPainterPath()
        clip.addPolygon(boundary)
        painter.setClipPath(clip, Qt.IntersectClip)
        painter.setPen(QPen(QColor('#e5ebf0'), 1))
        for x in xs:
            painter.drawLine(self.face_to_screen(x, bounds.top()), self.face_to_screen(x, bounds.bottom()))
        for y in ys:
            painter.drawLine(self.face_to_screen(bounds.left(), y), self.face_to_screen(bounds.right(), y))
        painter.restore()
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor('#6c8091'), 1.5))
        painter.drawPolygon(boundary)
        painter.setPen(QColor('#56697b'))
        for x in xs:
            point = self.face_to_screen(x, bounds.top())
            painter.drawText(QRectF(point.x() - 35, screen.top() - 26, 70, 22), Qt.AlignCenter, f'{x/self.factor:g}')
        for y in ys:
            point = self.face_to_screen(bounds.left(), y)
            painter.drawText(QRectF(screen.left() - 57, point.y() - 11, 48, 22), Qt.AlignRight | Qt.AlignVCenter, f'{y/self.factor:g}')
        for source in self._draw_order():
            rectangle = self.source_rect(source)
            selected = source['row'] == self.selected_row
            invalid = source['row'] in self.invalid_rows
            fill = '#f8d6d0' if selected and invalid else '#ffc36a' if selected else '#cbd5de'
            stroke = '#ba4232' if selected and invalid else '#a85010' if selected else '#8093a4'
            painter.setBrush(QColor(fill))
            painter.setPen(QPen(QColor(stroke), 2.5 if selected else 1, Qt.DashLine if invalid else Qt.SolidLine))
            point = source.get('type') == 'point'
            if point:
                painter.drawEllipse(rectangle)
                center = rectangle.center()
                painter.drawLine(center - QPointF(3, 0), center + QPointF(3, 0))
                painter.drawLine(center - QPointF(0, 3), center + QPointF(0, 3))
            else:
                painter.drawRect(rectangle)
            painter.setPen(QColor('#692f08' if selected else '#526575'))
            if point:
                label = painter.fontMetrics().elidedText(source['name'], Qt.ElideRight, max(0, int(canvas.right() - rectangle.right() - 12)))
                painter.drawText(rectangle.center() + QPointF(11, -7), label)
                continue
            label_bounds = rectangle.adjusted(6, 3, -6, -3)
            if label_bounds.width() > 25 and label_bounds.height() >= painter.fontMetrics().height():
                label = painter.fontMetrics().elidedText(source['name'], Qt.ElideRight, int(label_bounds.width()))
                painter.drawText(label_bounds, Qt.AlignCenter, label)
        origin = self.face_to_screen(0, 0)
        painter.setPen(QPen(QColor('#234d6c'), 1.5))
        painter.setBrush(QColor('#ffffff'))
        painter.drawEllipse(origin, 3.5, 3.5)
        painter.drawText(origin + QPointF(7, 17), '0, 0')
        selected = next((s for s in self.sources if s['row'] == self.selected_row), None)
        if selected:
            painter.setPen(QPen(QColor('#a85010'), 1.5))
            painter.setBrush(QColor('#ffffff'))
            for rectangle in self.handle_rects(selected).values():
                painter.drawRect(rectangle)

    def mousePressEvent(self, event):
        if self._drag or self._pan_drag:
            event.accept()
            return
        if event.button() == Qt.LeftButton:
            handle = self.handle_at(event.localPos())
            row = self.selected_row if handle else self.source_at(event.localPos())
            if row is not None:
                self.source_selected.emit(row)
                source = next(s for s in self.sources if s['row'] == row)
                mode = self.handle_at(event.localPos()) or 'move'
                self._drag = dict(source=dict(source), mode=mode,
                                  start=event.localPos(), scale=self._transform()[1])
                self.setFocus(Qt.MouseFocusReason)
                self.setCursor(Qt.ClosedHandCursor if mode == 'move' else Qt.SizeFDiagCursor)
                event.accept()
                return
        if event.button() in (Qt.LeftButton, Qt.MiddleButton):
            self._pan_drag = dict(start=event.localPos(), pan=QPointF(self._pan), button=event.button())
            self.setFocus(Qt.MouseFocusReason)
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._pan_drag and event.buttons() & self._pan_drag['button']:
            self._pan = self._pan_drag['pan'] + event.localPos() - self._pan_drag['start']
            self.update()
            event.accept()
            return
        if self._drag and event.buttons() & Qt.LeftButton:
            self._drag_to(event.localPos())
            event.accept()
            return
        handle = self.handle_at(event.localPos())
        if handle:
            self.setCursor(Qt.SizeFDiagCursor)
            self.setToolTip(f'Drag to resize the source · 0.01 {self.input_unit} grid')
            return
        row = self.source_at(event.localPos())
        self.setCursor(Qt.OpenHandCursor)
        source = next((s for s in self.sources if s['row'] == row), None)
        detail = ''
        if source:
            detail = (("On surface" if source.get('mounting') == 'surface' else
                       f"Standoff {source['distance_m']/self.factor:.2f} {self.input_unit}") if source.get('type') == 'point' else
                      f"{source['width_m']/self.factor:.2f} × {source['height_m']/self.factor:.2f} {self.input_unit}")
        self.setToolTip(f"{source['name']} · Drag to move · X {source['x_m']/self.factor:.2f}, Y {source['y_m']/self.factor:.2f} {self.input_unit} · "
                        f"{detail}" if source else 'Scroll to zoom · Drag to pan · Home resets the view')
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._pan_drag and event.button() == self._pan_drag['button']:
            self._pan = self._pan_drag['pan'] + event.localPos() - self._pan_drag['start']
            self._pan_drag = None
            self.setCursor(Qt.OpenHandCursor)
            self.update()
            event.accept()
            return
        if event.button() == Qt.LeftButton and self._drag:
            self._drag_to(event.localPos())
            self._drag = None
            self.setCursor(Qt.ArrowCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event):
        # Keep the transform stable until an active source edit or pan ends.
        if not self._drag and not self._pan_drag:
            delta = event.angleDelta().y() or event.pixelDelta().y()
            origin, scale = self._transform()
            position = event.position() if hasattr(event, 'position') else event.posF()
            anchor = (position - origin) / scale
            self._zoom = max(0.15, min(12.0, self._zoom * math.exp(max(-10, min(10, delta / 800.0)))))
            self._pan += position - self.face_to_screen(anchor.x(), anchor.y())
            self.update()
        event.accept()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape and self._pan_drag:
            self._pan = self._pan_drag['pan']
            self._pan_drag = None
            self.setCursor(Qt.OpenHandCursor)
            self.update()
            event.accept()
            return
        if event.key() == Qt.Key_Home and not self._drag and not self._pan_drag:
            self._zoom = 1.0
            self._pan = QPointF()
            self.update()
            event.accept()
            return
        if event.key() == Qt.Key_Escape and self._drag:
            source = self._drag['source']
            self._drag = None
            keys = ('x_m', 'y_m') if source.get('type') == 'point' else ('x_m', 'y_m', 'width_m', 'height_m')
            self.rectangle_changed.emit(source['row'], {key: source[key] for key in keys})
            self.setCursor(Qt.ArrowCursor)
            event.accept()
            return
        super().keyPressEvent(event)
