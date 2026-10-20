# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Draft area and point sources attached to a selected building surface."""

from qgis.PyQt.QtCore import QEvent, QSignalBlocker, Qt, QTimer
from qgis.PyQt.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QDialogButtonBox,
                                QDoubleSpinBox, QHBoxLayout, QHeaderView, QLabel,
                                QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from .building_faces import MIN_STANDOFF_M, point_mounting, read_face_sources, source_polygon
from .face_source_preview import FaceSourcePreview
from .schema import ModelError, new_id
from .spectra import OCTAVE_BANDS, compatible_power, missing_bands
from .spectrum_table import SpectrumTable
from .units import LengthSpinBox, length_factor


class UnattachedSourcesDialog(QDialog):
    """Explicit draft recovery for sources whose original surface is unavailable."""

    def __init__(self, parent, sources, faces, vertex_count):
        super().__init__(parent)
        self.original_sources = sources
        self.sources = list(sources)
        self.faces, self.vertex_count = faces, vertex_count
        self.setWindowTitle('Unattached building sources')
        self.resize(820, 340)
        layout = QVBoxLayout(self)
        hint = QLabel('These sources no longer match a building surface. Choose a new surface or remove each source. '
                      'Reassigning keeps its position values, dimensions, distance and spectrum. '
                      'Select the new surface in the building preview and use Sources to adjust its position; '
                      'every source must fit before applying the building. Changes remain in this building draft until you apply it.')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.table = QTableWidget(len(sources), 3)
        self.table.setHorizontalHeaderLabels(['Source', 'Original surface', 'Action'])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(42)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        for row, source in enumerate(sources):
            self.table.setItem(row, 0, QTableWidgetItem(source['name']))
            original = faces.get(source['face'], {}).get('name')
            if original:
                original += ' (perimeter changed)'
            else:
                parts = source['face'].split(':')
                original = ('Roof' if parts[0] == 'roof' else 'Wall')
                if len(parts) > 1 and all(part.isdecimal() for part in parts[1:]):
                    original += (' facet ' if parts[0] == 'roof' else ' ') + '–'.join(str(int(part)+1) for part in parts[1:])
            self.table.setItem(row, 1, QTableWidgetItem(original))
            destination = QComboBox()
            destination.addItem('Keep unattached', None)
            destination.addItem('Remove source', '__remove__')
            for identity, face in faces.items():
                destination.addItem('Move to ' + face['name'], identity)
            self.table.setCellWidget(row, 2, destination)
        layout.addWidget(self.table, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText('Update sources')
        buttons.accepted.connect(self.apply_changes)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def apply_changes(self):
        sources = []
        for row, original in enumerate(self.original_sources):
            destination = self.table.cellWidget(row, 2).currentData()
            if destination == '__remove__':
                continue
            source = dict(original)
            if destination is not None:
                source.update(face=destination, vertex_count=self.vertex_count)
                if destination.startswith('roof'):
                    source['frame'] = 'building_local'
                else:
                    source.pop('frame', None)
            sources.append(source)
        self.sources = sources
        self.accept()


class FaceSourcesDialog(QDialog):
    def __init__(self, parent, face_id, faces, vertex_count, sources, spectra, input_unit="m"):
        super().__init__(parent)
        self.input_unit = input_unit
        factor = length_factor(input_unit)
        self.face_id, self.faces, self.vertex_count = face_id, faces, vertex_count
        self.sources = []
        self.records = {s['spectrum_id']: s for s in spectra if s['spectrum']['kind'] == 'sound_power'
                        and compatible_power(s['spectrum'], 'area_source')}
        face = faces[face_id]
        self.setWindowTitle(f"Sources · {face['name']}")
        self.resize(1240, 860)
        layout = QVBoxLayout(self)
        hint = QLabel(f"{face['name']} · {face['width']/factor:.2f} {input_unit} wide × {face['height']/factor:.2f} {input_unit} high\n"
                      "Position is measured from the face’s marked 0, 0 origin: X right, Y down. "
                      "Area-source width and height are measured along the face. "
                      "A surface point is an opening, louvre or panel on the face: it radiates outward only and "
                      "the face does not reflect it. A standoff point is a unit mounted clear of the face, at least "
                      f"{MIN_STANDOFF_M / factor:.2f} {input_unit} out; the face reflects it like any other source.")
        hint.setWordWrap(True)
        if face_id.startswith('roof'):
            hint.setText(hint.text() + " Roof axes turn with the building’s local coordinates.")
            if any(source.get('frame') != 'building_local' for source in sources):
                hint.setText(hint.text() + " Applying aligns existing sources to these axes using the offsets and sizes below.")
        layout.addWidget(hint)
        self.splitter = QSplitter(Qt.Vertical)
        self.splitter.setChildrenCollapsible(False)
        preview_page = QWidget()
        preview_layout = QVBoxLayout(preview_page)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        self.face_preview = FaceSourcePreview(face, input_unit=input_unit)
        preview_layout.addWidget(self.face_preview, 1)
        navigation_hint = QLabel('Scroll to zoom · Drag empty space or middle-drag to pan · Home resets the view')
        navigation_hint.setWordWrap(True)
        preview_layout.addWidget(navigation_hint)
        self.preview_status = QLabel()
        self.preview_status.setWordWrap(True)
        preview_layout.addWidget(self.preview_status)
        self.splitter.addWidget(preview_page)
        source_page = QWidget()
        source_layout = QVBoxLayout(source_page)
        source_layout.setContentsMargins(0, 0, 0, 0)
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(['Name'] + [f'{label} ({input_unit})' for label in ('X', 'Y', 'Width', 'Height')]
                                            + ['Spectrum', f'Distance from\nsurface ({input_unit})', 'Type'])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.horizontalHeader().setMinimumSectionSize(90)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.Interactive)
        self.table.setColumnWidth(5, 250)
        # Keep the established coordinate/spectrum column indices while showing
        # type beside the name and the spectrum after the position fields.
        self.table.horizontalHeader().moveSection(7, 1)
        self.table.horizontalHeader().moveSection(self.table.horizontalHeader().visualIndex(5), 7)
        self.table.verticalHeader().setDefaultSectionSize(42)
        self.table.setMinimumHeight(210)
        source_layout.addWidget(self.table, 1)
        actions = QHBoxLayout()
        self.add_button = QPushButton('Add area source')
        self.add_point_button = QPushButton('Add point source')
        self.remove_button = QPushButton('Remove source')
        self.add_button.clicked.connect(lambda: self.add_source())
        self.add_point_button.clicked.connect(lambda: self.add_source(source_type='point'))
        self.remove_button.clicked.connect(self.remove_source)
        actions.addWidget(self.add_button)
        actions.addWidget(self.add_point_button)
        actions.addWidget(self.remove_button)
        actions.addStretch()
        source_layout.addLayout(actions)
        self.splitter.addWidget(source_page)
        self.splitter.setStretchFactor(0, 2)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([340, 180])
        layout.addWidget(self.splitter, 1)
        self.spectrum_preview = SpectrumTable(1, 8)
        self.spectrum_preview.setHorizontalHeaderLabels([f'{band} Hz' for band in OCTAVE_BANDS])
        self.spectrum_preview.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.spectrum_preview.horizontalHeader().setMinimumSectionSize(60)
        self.spectrum_preview.verticalHeader().hide()
        self.spectrum_preview.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.spectrum_preview.setFixedHeight(88)
        layout.addWidget(self.spectrum_preview)
        self.spectrum_status = QLabel()
        self.spectrum_status.setWordWrap(True)
        layout.addWidget(self.spectrum_status)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet('color: #c24f36;')
        self.error.hide()
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText('Apply sources')
        buttons.accepted.connect(self.validate)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.table.currentCellChanged.connect(self.refresh_spectrum)
        self.table.itemChanged.connect(self.refresh_preview)
        self.face_preview.source_selected.connect(self.select_source)
        self.face_preview.rectangle_changed.connect(self.update_rectangle)
        for source in sources:
            self.add_source(source)
        self.refresh_spectrum()

    def add_source(self, source=None, source_type='area'):
        row = self.table.rowCount()
        face = self.faces[self.face_id]
        if source is None:
            source = dict(id=new_id(), name=f'{source_type.title()} source {row + 1}', x_m=0, y_m=0, spectrum_id=None)
            source.update(dict(type='point', mounting='surface', distance_m=0) if source_type == 'point' else
                          dict(width_m=min(1, face['width']), height_m=min(1, face['height'])))
        point = source.get('type') == 'point'
        self.table.insertRow(row)
        name = QTableWidgetItem(source['name'])
        name.setData(Qt.UserRole, source['id'])
        name.setData(Qt.UserRole + 1, 'point' if point else 'area')
        self.table.setItem(row, 0, name)
        kind = QTableWidgetItem('Point' if point else 'Area')
        kind.setFlags(kind.flags() & ~Qt.ItemIsEditable)
        self.table.setItem(row, 7, kind)
        if point:
            mounting = QComboBox()
            mounting.addItem('Surface point', 'surface')
            mounting.addItem('Standoff point', 'standoff')
            mounting.setItemData(0, 'Opening, louvre or panel on the face: outward radiation, no reflection from this face.', Qt.ToolTipRole)
            mounting.setItemData(1, 'Unit mounted clear of the face: an ordinary point source that this face reflects.', Qt.ToolTipRole)
            try:
                current = point_mounting(source)
            except ModelError:
                # Undeclared or below the minimum standoff: propose a choice for
                # review; saving the dialog records it explicitly.
                current = 'surface' if source['distance_m'] == 0 else 'standoff'
            mounting.setCurrentIndex(mounting.findData(current))
            mounting.setProperty('source_id', source['id'])
            mounting.installEventFilter(self)
            self.table.setCellWidget(row, 7, mounting)
        for column, key in ((1, 'x_m'), (2, 'y_m'), (3, 'width_m'), (4, 'height_m'), (6, 'distance_m')):
            spin = LengthSpinBox(self.input_unit)
            spin.setSuffix("")  # Units are in the column headings.
            enabled = not (point and column in (3, 4) or not point and column == 6)
            spin.setRange(0 if column in (1, 2, 6) or not enabled else 0.01 * length_factor(self.input_unit), 1000000000)
            spin.setSingleStep(0.01 * length_factor(self.input_unit))
            spin.setValue(source.get(key, 0))
            if not enabled:
                spin.setSpecialValueText('—')
                spin.setEnabled(False)
            if column == 6:
                spin.setToolTip(f'Perpendicular distance outward from the building surface; at least {MIN_STANDOFF_M:g} m for a standoff point.')
            spin.setProperty('source_id', source['id'])
            spin.lineEdit().setProperty('source_id', source['id'])
            spin.installEventFilter(self)
            spin.lineEdit().installEventFilter(self)
            spin.valueChanged.connect(self.refresh_preview)
            self.table.setCellWidget(row, column, spin)
        if point:
            mounting.currentIndexChanged.connect(lambda _, combo=mounting: self.mounting_changed(combo))
            self.mounting_changed(mounting)
        combo = QComboBox()
        combo.addItem('Unassigned', None)
        for identity, record in self.records.items():
            if compatible_power(record['spectrum'], 'point_source' if point else 'area_source'):
                combo.addItem(record['name'] + (' (unknown bands)' if missing_bands(record['spectrum']) else ''), identity)
        identity = source['spectrum_id']
        if identity and combo.findData(identity) < 0:
            combo.addItem('Unavailable spectrum', identity)
        combo.setCurrentIndex(max(0, combo.findData(identity)))
        combo.setProperty('source_id', source['id'])
        combo.installEventFilter(self)
        combo.currentIndexChanged.connect(self.refresh_spectrum)
        self.table.setCellWidget(row, 5, combo)
        self.table.selectRow(row)
        self.refresh_preview()

    def mounting_changed(self, combo):
        row = next(row for row in range(self.table.rowCount()) if self.table.cellWidget(row, 7) is combo)
        distance = self.table.cellWidget(row, 6)
        if distance is None:
            return
        blocker = QSignalBlocker(distance)
        if combo.currentData() == 'surface':
            if distance.isEnabled():
                combo.setProperty('standoff_m', distance.value())
            distance.setMinimum(0)
            distance.setValue(0)
            distance.setEnabled(False)
        else:
            distance.setMinimum(MIN_STANDOFF_M)
            distance.setValue(max(distance.value(), combo.property('standoff_m') or 0, MIN_STANDOFF_M))
            distance.setEnabled(True)
        del blocker
        self.refresh_preview()

    def eventFilter(self, obj, event):
        if event.type() == QEvent.FocusIn and obj.property('source_id'):
            identity = obj.property('source_id')
            for row in range(self.table.rowCount()):
                item = self.table.item(row, 0)
                if item and item.data(Qt.UserRole) == identity and self.table.currentRow() != row:
                    self.table.selectRow(row)
                    break
            spin = obj if isinstance(obj, QDoubleSpinBox) else obj.parent()
            if isinstance(spin, QDoubleSpinBox):
                # Table cell widgets receive OtherFocusReason even on Tab.
                # Select after Qt finishes its own focus/selection handling.
                QTimer.singleShot(0, spin.selectAll)
        return super().eventFilter(obj, event)

    def select_source(self, row):
        # Commit any active coordinate edit before changing the graphical target.
        self.table.setFocus(Qt.MouseFocusReason)
        self.table.selectRow(row)

    def update_rectangle(self, row, values):
        if not 0 <= row < self.table.rowCount():
            return
        fields = [(column, key) for column, key in enumerate(('x_m', 'y_m', 'width_m', 'height_m'), 1) if key in values]
        widgets = [self.table.cellWidget(row, column) for column, _ in fields]
        blockers = [QSignalBlocker(widget) for widget in widgets]
        for widget, (_, key) in zip(widgets, fields):
            widget.setValue(values[key])
        del blockers
        self.refresh_preview()

    def draft_source(self, row):
        name = self.table.item(row, 0)
        widgets = [self.table.cellWidget(row, column) for column in range(1, 7)]
        if name is None or any(widget is None for widget in widgets):
            return None  # A row is still being constructed.
        source = dict(id=name.data(Qt.UserRole), name=name.text().strip(), face=self.face_id,
                      vertex_count=self.vertex_count, spectrum_id=widgets[4].currentData())
        if self.face_id.startswith('roof'):
            source['frame'] = 'building_local'
        source.update(x_m=widgets[0].value(), y_m=widgets[1].value())
        if name.data(Qt.UserRole + 1) == 'point':
            mounting = self.table.cellWidget(row, 7)
            if mounting is None:
                return None
            source.update(type='point', mounting=mounting.currentData(),
                          distance_m=0 if mounting.currentData() == 'surface' else widgets[5].value())
        else:
            source.update(width_m=widgets[2].value(), height_m=widgets[3].value())
        return source

    def refresh_preview(self, *args):
        sources, invalid, errors = [], [], {}
        for row in range(self.table.rowCount()):
            source = self.draft_source(row)
            if source is None:
                continue
            try:
                source_polygon(source, self.faces, self.vertex_count)
            except ModelError as error:
                invalid.append(row)
                errors[row] = str(error)
            sources.append(dict(source, row=row))
        row = self.table.currentRow()
        self.face_preview.set_sources(sources, row, invalid)
        active = next((s for s in sources if s['row'] == row), None)
        if row in errors:
            self.preview_status.setText(errors[row])
        elif active:
            action = 'Drag the marker to move' if active.get('type') == 'point' else 'Drag the source to move; bottom-right handle to resize'
            self.preview_status.setText(f"Editing: {active['name'] or 'Unnamed source'} · {action} · "
                                        f"0.01 {self.input_unit} grid · Other sources are grey.")
        else:
            self.preview_status.setText(f'Add an area or point source to this face, or select one in the drawing or table. X →, Y ↓. Inputs use {self.input_unit}.')

    def remove_source(self):
        row = self.table.currentRow()
        if row >= 0:
            self.table.removeRow(row)
        self.refresh_spectrum()

    def refresh_spectrum(self, *args):
        row = self.table.currentRow()
        self.remove_button.setEnabled(row >= 0)
        combo = self.table.cellWidget(row, 5) if row >= 0 else None
        record = self.records.get(combo.currentData()) if combo else None
        source = self.draft_source(row) if row >= 0 else None
        if record and source and not compatible_power(record['spectrum'], 'point_source' if source.get('type') == 'point' else 'area_source'):
            record = None
        assigned = bool(combo and combo.currentData())
        for i in range(8):
            value = record['spectrum']['values'][i] if record else None
            self.spectrum_preview.setItem(0, i, QTableWidgetItem(('Unknown' if assigned else '') if value is None else str(value)))
        self.spectrum_status.setText(
            ('Total source sound power' if record['spectrum']['power_basis'] == 'total' else 'Sound power per square meter')
            + ' · dB · 0 = no energy' if record else
            'Assign a project sound power spectrum before calculation. Manage spectra in the project’s Spectra view.')
        self.refresh_preview()

    def validate(self):
        sources = []
        try:
            for row in range(self.table.rowCount()):
                source = self.draft_source(row)
                read_face_sources([source])
                source_polygon(source, self.faces, self.vertex_count)
                if source['spectrum_id']:
                    record = self.records.get(source['spectrum_id'])
                    point = source.get('type') == 'point'
                    if not record or not compatible_power(record['spectrum'], 'point_source' if point else 'area_source'):
                        description = 'total sound power' if point else 'area sound power'
                        raise ModelError(f"{source['name']}: choose an available {description} spectrum or clear its assignment.")
                sources.append(source)
        except ModelError as error:
            self.error.setText(str(error))
            self.error.show()
            return
        self.sources = sources
        self.accept()
