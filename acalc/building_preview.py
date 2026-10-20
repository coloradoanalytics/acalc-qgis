# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Interactive, dependency-free building preview for the properties dialog.

This is a display mesh, never an acoustic input. Roof triangulation deliberately
uses the authored-building convention: CCW ring,
first authored vertex start, then the first available ear. Keeping this
choice matters when ground elevations or vertex heights make a roof nonplanar.
"""

import math

from qgis.PyQt.QtCore import QPointF, QRectF, QSize, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen, QPolygonF
from qgis.PyQt.QtWidgets import QSizePolicy, QWidget

from .schema import ModelError


from .building_faces import _EPSILON, _normal, _triangulate, building_faces, source_polygon


def _split_polygon(polygon, anchor, normal, tolerance):
    """Split a convex face at a plane for correct painter occlusion ordering."""
    distances = [sum((point[i] - anchor[i]) * normal[i] for i in range(3)) for point in polygon]
    has_front = any(distance > tolerance for distance in distances)
    has_back = any(distance < -tolerance for distance in distances)
    if not has_front:
        return (), polygon, not has_back
    if not has_back:
        return polygon, (), False
    front, back = [], []
    for index, point in enumerate(polygon):
        previous, before = polygon[index - 1], distances[index - 1]
        distance = distances[index]
        if (before > tolerance and distance < -tolerance) or (before < -tolerance and distance > tolerance):
            ratio = before / (before - distance)
            intersection = tuple(previous[i] + ratio * (point[i] - previous[i]) for i in range(3))
            front.append(intersection)
            back.append(intersection)
        if distance >= -tolerance:
            front.append(point)
        if distance <= tolerance:
            back.append(point)
    return tuple(front), tuple(back), False


def _face_tree(faces, tolerance):
    """Partition once per geometry edit, then traverse cheaply while orbiting.

    Average-depth sorting alone can put a concave building's nearer walls behind
    its roof. Plane splits make ordering spatial, including nonplanar roofs.
    Faces carry their original edge segments so splitting creates no new lines.
    """
    root = {}
    pending = [(root, faces)]
    while pending:
        node, group = pending.pop()
        polygon, _, normal, _ = group[len(group) // 2]
        node["normal"] = normal
        node["faces"] = []
        front, back = [], []
        for face, kind, face_normal, edges in group:
            above, below, coplanar = _split_polygon(face, polygon[0], normal, tolerance)
            if coplanar:
                node["faces"].append((face, kind, face_normal, edges))
            else:
                if len(above) >= 3:
                    front.append((above, kind, face_normal, edges))
                if len(below) >= 3:
                    back.append((below, kind, face_normal, edges))
        for key, items in (("front", front), ("back", back)):
            node[key] = {} if items else None
            if items:
                pending.append((node[key], items))
    return root


def _ordered_faces(tree, view):
    pending = [tree]
    while pending:
        node = pending.pop()
        if isinstance(node, tuple):
            yield node
            continue
        if not node:
            continue
        near, far = ("front", "back") if sum(node["normal"][i] * view[i] for i in range(3)) >= 0 else ("back", "front")
        pending.append(node[near])
        pending.extend(reversed(node["faces"]))
        pending.append(node[far])


class BuildingPreview(QWidget):
    """Orbitable preview of resolved ``(ring, x, y, ground_z, height)`` rows.

    ``vertices`` contains base XYZ points followed by roof XYZ points, in authored
    row order, with all axes in meters. ``roof_triangles`` indexes ``vertices``.
    Azimuth/elevation are degrees and zoom is a dimensionless camera scale.
    """

    face_selected = pyqtSignal(object)

    def __init__(self, parent=None, *, face_selection_enabled=True):
        super().__init__(parent)
        self.setMinimumSize(280, 280)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.OpenHandCursor)
        self.setAccessibleName("Interactive 3D building preview")
        self.face_selection_enabled = face_selection_enabled
        self.setAccessibleDescription(
            ("Click a face to select. " if face_selection_enabled else "")
            + "Drag to rotate. Middle-drag or Shift-drag to pan. Scroll to zoom. Double-click to reset the view.")
        self.setToolTip(
            ("Click a face to select · " if face_selection_enabled else "")
            + "Drag to rotate · Middle-drag or Shift-drag to pan · Scroll to zoom · Double-click to reset\n"
              "N: grid north · E: grid east · Z: up")
        self.vertices = ()
        self.roof_triangles = ()
        self.perimeter = ()
        self.message = "Enter at least three perimeter vertices to see the building."
        self.azimuth = -45.0
        self.elevation = 28.0
        self.zoom = 1.0
        self.pan = QPointF()
        self.selected_vertex = None
        self.selected_face = None
        self.faces = {}
        self.sources = []
        self.source_polygons = []
        self.source_errors = []
        self._faces = ()
        self._local_vertices = ()
        self._surface_edges = frozenset()
        self._face_tree = None
        self._centre = (0.0, 0.0, 0.0)
        self._radius = 1.0
        self._source_radius = 0.0
        self._frame_radius = None
        self._drag_position = None
        self._press_position = None
        self._drag_button = None
        self._panning = False
        self._dragged = False

    def sizeHint(self):
        return QSize(460, 460)

    def set_building(self, rows, horizontal_scale=1.0):
        """Replace the display mesh without changing the current orbit or zoom.

        XY is converted from the projected CRS unit to meters. Z and height are
        already meters. Invalid input raises ModelError; call clear() to display
        the validation message while an unfinished edit is being corrected.
        """
        try:
            scale = float(horizontal_scale)
            if not math.isfinite(scale) or scale <= 0:
                raise ModelError("The building needs a projected coordinate system with known units.")
            points = []
            for row in rows:
                ring, x, y, z, height = row
                if ring != 0:
                    raise ModelError("The building must contain a single perimeter.")
                x, y, z, height = float(x), float(y), float(z), float(height)
                if not all(math.isfinite(value) for value in (x, y, x * scale, y * scale, z, height, z + height)):
                    raise ModelError("Enter finite coordinates and heights to see the building.")
                if height < 0:
                    raise ModelError("Building heights cannot be negative.")
                points.append((x, y, z, height))
        except (TypeError, ValueError, OverflowError) as error:
            if isinstance(error, ModelError):
                raise
            raise ModelError("Enter numeric coordinates and heights to see the building.") from error
        if len(points) > 1 and points[0] == points[-1]:
            points.pop()
        if len(points) < 3:
            raise ModelError("Enter at least three perimeter vertices to see the building.")
        # Subtract before unit conversion: multiplying large projected eastings
        # first can lose the small differences that define a building footprint.
        anchor = points[0]
        local_points = [((p[0] - anchor[0]) * scale, (p[1] - anchor[1]) * scale,
                         p[2] - anchor[2], p[3]) for p in points]
        if not all(math.isfinite(value) for point in local_points for value in point):
            raise ModelError("The building's coordinates are too large to display.")
        perimeter, triangles = _triangulate(local_points, local_coordinates=True)
        count = len(points)
        vertices = (tuple((p[0] * scale, p[1] * scale, p[2]) for p in points)
                    + tuple((p[0] * scale, p[1] * scale, p[2] + p[3]) for p in points))
        local_vertices = (tuple(p[:3] for p in local_points)
                          + tuple((p[0], p[1], p[2] + p[3]) for p in local_points))
        lower = tuple(min(p[axis] for p in local_vertices) for axis in range(3))
        upper = tuple(max(p[axis] for p in local_vertices) for axis in range(3))
        span = tuple(upper[axis] - lower[axis] for axis in range(3))
        radius = math.hypot(*span) / 2
        if not math.isfinite(radius):
            raise ModelError("The building's coordinates are too large to display.")
        centre = tuple(lower[axis] + span[axis] / 2 for axis in range(3))
        faces = [(tuple(i + count for i in triangle), "roof") for triangle in triangles]
        faces += [(tuple(reversed(triangle)), "base") for triangle in triangles]
        for i, a in enumerate(perimeter):
            b = perimeter[(i + 1) % count]
            faces.append(((a, b, b + count, a + count), "wall"))
        self.vertices = vertices
        self._local_vertices = local_vertices
        self.perimeter = perimeter
        self.roof_triangles = tuple(tuple(i + count for i in triangle) for triangle in triangles)
        self._faces = tuple(faces)
        self._surface_edges = self._visible_surface_edges(local_vertices, faces)
        self._centre = centre
        self._radius = max(radius, _EPSILON)
        self.faces = building_faces(local_vertices, perimeter, triangles)
        self._face_tree = self._prepare_face_tree()
        if self.selected_face not in self.faces:
            self.set_selected_face(None)
        self.set_sources(self.sources)
        if self._frame_radius is None:
            self._frame_radius = self._radius
        if self.selected_vertex is not None and self.selected_vertex >= count:
            self.selected_vertex = None
        self.message = ""
        self.update()

    def clear(self, message="Enter at least three perimeter vertices to see the building."):
        """Discard stale geometry while retaining the camera for the next edit."""
        self.vertices = ()
        self.roof_triangles = ()
        self.perimeter = ()
        self._faces = ()
        self._local_vertices = ()
        self._surface_edges = frozenset()
        self._face_tree = None
        self.faces = {}
        self.source_polygons = []
        self.set_selected_face(None)
        self.message = str(message)
        self.update()

    def reset_view(self):
        self.azimuth = -45.0
        self.elevation = 28.0
        self.zoom = 1.0
        self.pan = QPointF()
        self._frame_radius = self._radius if self.vertices else None
        self.update()

    def set_selected_vertex(self, index):
        count = len(self.vertices) // 2
        self.selected_vertex = index if isinstance(index, int) and 0 <= index < count else None
        self.update()

    def set_selected_face(self, face_id):
        face_id = face_id if self.face_selection_enabled and face_id in self.faces else None
        if face_id != self.selected_face:
            self.selected_face = face_id
            self.face_selected.emit(face_id)
        self.update()

    def set_sources(self, sources):
        self.sources = sources
        self.source_polygons, self.source_errors = [], []
        for source in sources:
            try:
                polygon = source_polygon(source, self.faces, len(self.vertices) // 2)
                self.source_polygons.append((source, polygon))
            except ModelError as error:
                self.source_errors.append(str(error))
        self._source_radius = max((math.dist(point, self._centre)
                                   for _, polygon in self.source_polygons for point in polygon), default=0)
        self.update()

    def _projection(self):
        scale = max(1.0, min(self.width() - 52, self.height() - 68)) / (2 * max(self._frame_radius or self._radius, self._source_radius)) * self.zoom
        return self._camera(), scale, QPointF(self.width() / 2, self.height() / 2) + self.pan

    def face_at(self, position):
        """Use the same split faces and painter order as the visible mesh."""
        camera, scale, origin = self._projection()
        selected = None
        for polygon, (_, face_id), normal, _ in _ordered_faces(self._face_tree, camera[2]):
            if sum(normal[i] * camera[2][i] for i in range(3)) <= 0:
                continue
            screen = QPolygonF([self._project(p, camera, scale, origin)[0] for p in polygon])
            if screen.containsPoint(QPointF(position), Qt.OddEvenFill):
                selected = face_id
        return selected

    def _camera(self):
        azimuth, elevation = math.radians(self.azimuth), math.radians(self.elevation)
        ca, sa, ce, se = math.cos(azimuth), math.sin(azimuth), math.cos(elevation), math.sin(elevation)
        return ((-sa, ca, 0.0), (-se * ca, -se * sa, ce), (ce * ca, ce * sa, se))

    def _project(self, point, camera, scale, origin):
        local = tuple(point[i] - self._centre[i] for i in range(3))
        horizontal, vertical, depth = (sum(local[i] * axis[i] for i in range(3)) for axis in camera)
        return QPointF(origin.x() + horizontal * scale, origin.y() - vertical * scale), depth

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        bounds = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        gradient = QLinearGradient(bounds.topLeft(), bounds.bottomLeft())
        gradient.setColorAt(0.0, QColor("#f6f8fb"))
        gradient.setColorAt(1.0, QColor("#e7edf3"))
        painter.setPen(QPen(QColor("#d6dfe8"), 1.0))
        painter.setBrush(gradient)
        painter.drawRoundedRect(bounds, 8, 8)
        painter.setClipRect(bounds.adjusted(1, 1, -1, -1))
        if not self.vertices:
            painter.setPen(QColor("#627286"))
            painter.drawText(bounds.adjusted(30, 35, -30, -35), Qt.AlignCenter | Qt.TextWordWrap, self.message)
            return
        camera, scale, origin = self._projection()
        projected = [self._project(point, camera, scale, origin) for point in self._local_vertices]
        self._draw_grid(painter, camera, scale, origin)
        for polygon, (kind, face_id), normal, edges in _ordered_faces(self._face_tree, camera[2]):
            # The surfaces form a closed volume. Hide the backs of its faces.
            if sum(normal[i] * camera[2][i] for i in range(3)) <= 0:
                continue
            screen_points = [self._project(point, camera, scale, origin)[0] for point in polygon]
            light = max(0.0, sum(normal[i] * (-0.36, -0.48, 0.8)[i] for i in range(3)))
            base = QColor("#72a6ac" if kind == "roof" else "#b2c0ce" if kind == "wall" else "#8e9eac")
            if face_id is not None and face_id == self.selected_face:
                base = QColor("#70b8f5")
            factor = 0.78 + 0.25 * light
            color = QColor(*(min(255, round(channel * factor)) for channel in (base.red(), base.green(), base.blue())))
            painter.setBrush(color)
            # Matching fill/edge colors seal antialiasing seams in a flat roof.
            painter.setPen(QPen(color, 0.8))
            painter.drawPolygon(QPolygonF(screen_points))
            painter.setPen(QPen(QColor("#526b80"), 1.0))
            for position, a in enumerate(polygon):
                b = polygon[(position + 1) % len(polygon)]
                if any(self._on_original_edge(a, b, edge) for edge in edges):
                    painter.drawLine(screen_points[position], screen_points[(position + 1) % len(polygon)])
            # Draw on this surface while it is in painter order: nearer walls
            # still occlude sources, including on concave/nonplanar buildings.
            painter.save()
            clip = QPainterPath()
            clip.addPolygon(QPolygonF(screen_points))
            painter.setClipPath(clip, Qt.IntersectClip)
            for source, rectangle in self.source_polygons:
                if source["face"] == face_id and len(rectangle) > 1:
                    screen = QPolygonF([self._project(p, camera, scale, origin)[0] for p in rectangle])
                    painter.setBrush(QColor("#f6a343"))
                    painter.setPen(QPen(QColor("#9d4b0d"), 1.5))
                    painter.drawPolygon(screen)
            painter.restore()
            if face_id is not None and face_id == self.selected_face:
                face = self.faces[face_id]
                point = self._project(face["origin"], camera, scale, origin)[0]
                painter.setPen(QPen(QColor("#155188"), 2))
                painter.setBrush(QColor("#ffffff"))
                painter.drawEllipse(point, 4, 4)
                painter.drawText(point + QPointF(7, -6), "0, 0")
        self._draw_point_sources(painter, camera, scale, origin)
        if self.selected_vertex is not None:
            self._draw_selected(painter, projected)
        self._draw_axes(painter, camera)

    def _point_visible(self, point, camera, scale, origin):
        """Test depth against actual faces, including concave roof facets."""
        screen = self._project(point, camera, scale, origin)[0]
        for indices, _ in self._faces:
            polygon = tuple(self._local_vertices[i] for i in indices)
            normal = _normal(polygon)
            denominator = sum(normal[i] * camera[2][i] for i in range(3))
            if denominator <= _EPSILON:
                continue
            depth = sum(normal[i] * (polygon[0][i] - point[i]) for i in range(3)) / denominator
            if depth > _EPSILON:
                boundary = QPolygonF([self._project(p, camera, scale, origin)[0] for p in polygon])
                if boundary.containsPoint(screen, Qt.OddEvenFill):
                    return False
        return True

    def _draw_point_sources(self, painter, camera, scale, origin):
        for source, geometry in self.source_polygons:
            if len(geometry) != 1 or not self._point_visible(geometry[0], camera, scale, origin):
                continue
            screen = self._project(geometry[0], camera, scale, origin)[0]
            anchor = source_polygon(dict(source, distance_m=0, mounting='surface'), self.faces, len(self.vertices) // 2)[0]
            if source['distance_m'] and self._point_visible(anchor, camera, scale, origin):
                painter.setPen(QPen(QColor('#9d4b0d'), 1, Qt.DashLine))
                painter.drawLine(self._project(anchor, camera, scale, origin)[0], screen)
            painter.setBrush(QColor('#f6a343'))
            painter.setPen(QPen(QColor('#9d4b0d'), 1.5))
            painter.drawEllipse(screen, 5, 5)
            painter.drawText(screen + QPointF(8, -7), source['name'])

    def _prepare_face_tree(self):
        faces = []
        for indices, kind in self._faces:
            polygon = tuple(self._local_vertices[i] for i in indices)
            normal = _normal(polygon)
            if normal == (0.0, 0.0, 0.0):
                continue
            edges = []
            for i, a in enumerate(indices):
                b = indices[(i + 1) % len(indices)]
                if kind == "wall" or tuple(sorted((a, b))) in self._surface_edges:
                    edges.append((self._local_vertices[a], self._local_vertices[b]))
            face_id = None
            if kind == "wall":
                face_id = f"wall:{indices[0]}:{indices[1]}"
            elif kind == "roof":
                face_id = "roof" if "roof" in self.faces else "roof:" + ":".join(map(str, sorted(i - len(self.vertices) // 2 for i in indices)))
            faces.append((polygon, (kind, face_id), normal, tuple(edges)))
        return _face_tree(faces, max(_EPSILON, self._radius * 1e-10)) if faces else None

    def _on_original_edge(self, a, b, edge):
        start, end = edge
        vector = tuple(end[i] - start[i] for i in range(3))
        length = math.hypot(*vector)
        if length == 0:
            return False
        direction = tuple(value / length for value in vector)
        tolerance = max(_EPSILON, self._radius * 1e-9)
        for point in (a, b):
            offset = tuple(point[i] - start[i] for i in range(3))
            along = sum(offset[i] * direction[i] for i in range(3))
            if not -tolerance <= along <= length + tolerance:
                return False
            if math.hypot(*(offset[i] - along * direction[i] for i in range(3))) > tolerance:
                return False
        return True

    @staticmethod
    def _visible_surface_edges(vertices, faces):
        adjacent = {}
        for indices, kind in faces:
            if kind == "wall":
                continue
            normal = _normal([vertices[i] for i in indices])
            for i, a in enumerate(indices):
                edge = tuple(sorted((a, indices[(i + 1) % len(indices)])))
                adjacent.setdefault(edge, []).append(normal)
        return frozenset(edge for edge, normals in adjacent.items()
                         if len(normals) == 1 or math.hypot(*(normals[0][i] - normals[1][i] for i in range(3))) > 1e-8)

    def _draw_grid(self, painter, camera, scale, origin):
        extent = self._radius * 1.25
        spacing = extent / 4
        ground = min(point[2] for point in self._local_vertices)
        painter.setPen(QPen(QColor("#d9e2eb"), 0.8))
        for i in range(-4, 5):
            offset = spacing * i
            for a, b in (((self._centre[0] - extent, self._centre[1] + offset, ground),
                          (self._centre[0] + extent, self._centre[1] + offset, ground)),
                         ((self._centre[0] + offset, self._centre[1] - extent, ground),
                          (self._centre[0] + offset, self._centre[1] + extent, ground))):
                painter.drawLine(self._project(a, camera, scale, origin)[0], self._project(b, camera, scale, origin)[0])

    def _draw_selected(self, painter, projected):
        index = self.selected_vertex
        count = len(self.vertices) // 2
        bottom, top = projected[index][0], projected[index + count][0]
        painter.setPen(QPen(QColor("#d78032"), 2.0, Qt.DashLine))
        painter.drawLine(bottom, top)
        painter.setPen(QPen(QColor("#ffffff"), 1.6))
        painter.setBrush(QColor("#d78032"))
        painter.drawEllipse(bottom, 4.5, 4.5)
        painter.drawEllipse(top, 5.5, 5.5)
        painter.setPen(QColor("#80501f"))
        painter.drawText(top + QPointF(9, -8), str(index + 1))

    def _draw_axes(self, painter, camera):
        origin = QPointF(self.width() - 51, self.height() - 51)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(246, 248, 251, 225))
        painter.drawEllipse(origin, 37, 37)
        for axis in sorted(range(3), key=lambda i: camera[2][i]):
            end = origin + QPointF(camera[0][axis] * 24, -camera[1][axis] * 24)
            label = origin + QPointF(camera[0][axis] * 34, -camera[1][axis] * 34)
            color = QColor(("#b56560", "#559078", "#537da6")[axis])
            painter.setPen(QPen(color, 1.8))
            painter.drawLine(origin, end)
            # Projected X/Y give grid east/north; Z remains the vertical axis.
            painter.drawText(QRectF(label.x() - 7, label.y() - 9, 14, 18), Qt.AlignCenter, "ENZ"[axis])

    def mousePressEvent(self, event):
        if event.button() in (Qt.LeftButton, Qt.MiddleButton) and self.vertices:
            self._drag_button = event.button()
            self._panning = event.button() == Qt.MiddleButton or bool(event.modifiers() & Qt.ShiftModifier)
            self._drag_position = event.pos()
            self._press_position = event.pos()
            self._dragged = False
            self.setCursor(Qt.ClosedHandCursor)
            self.setFocus(Qt.MouseFocusReason)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_position is not None and event.buttons() & self._drag_button:
            if not self._dragged and (event.pos() - self._press_position).manhattanLength() < 5:
                return
            self._dragged = True
            delta = event.pos() - self._drag_position
            if self._panning:
                self.pan += QPointF(delta)
            else:
                self.azimuth = (self.azimuth - delta.x() * 0.6) % 360
                self.elevation = max(-85.0, min(85.0, self.elevation + delta.y() * 0.5))
            self._drag_position = event.pos()
            self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == self._drag_button:
            if (self.face_selection_enabled and not self._panning
                    and self._press_position is not None and not self._dragged):
                self.set_selected_face(self.face_at(event.pos()))
            self._press_position = None
            self._drag_position = None
            self._drag_button = None
            self.setCursor(Qt.OpenHandCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event):
        if self.vertices:
            delta = event.angleDelta().y() or event.pixelDelta().y()
            self.zoom = max(0.15, min(12.0, self.zoom * math.exp(max(-10, min(10, delta / 800.0)))))
            self.update()
            event.accept()
            return
        super().wheelEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_position = None
            self._press_position = None
            self._drag_button = None
            self.setCursor(Qt.OpenHandCursor)
            self.reset_view()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Home, Qt.Key_R):
            self.reset_view()
        elif event.key() in (Qt.Key_Plus, Qt.Key_Equal):
            self.zoom = min(12.0, self.zoom * 1.15)
        elif event.key() == Qt.Key_Minus:
            self.zoom = max(0.15, self.zoom / 1.15)
        elif event.key() in (Qt.Key_Left, Qt.Key_Right):
            self.azimuth = (self.azimuth + (5 if event.key() == Qt.Key_Left else -5)) % 360
        elif event.key() in (Qt.Key_Up, Qt.Key_Down):
            self.elevation = max(-85.0, min(85.0, self.elevation + (5 if event.key() == Qt.Key_Up else -5)))
        else:
            super().keyPressEvent(event)
            return
        self.update()
        event.accept()
