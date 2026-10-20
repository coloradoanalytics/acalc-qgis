# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Persistent dock. QGIS selections remain the authority for feature selection."""

from qgis.PyQt.QtCore import Qt, QTimer, QItemSelection, QItemSelectionModel
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDockWidget, QFormLayout, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QTreeWidget, QTreeWidgetItem, QMenu, QPushButton,
    QPlainTextEdit, QScrollArea, QStackedWidget, QTabWidget, QTableWidget, QTableWidgetItem, QTextEdit, QToolButton,
    QVBoxLayout, QWidget, QSizePolicy,
)
from qgis.core import QgsFeatureRequest

from .dialogs import null_text
from .schema import LAYER_TYPES
from .layer_presentation import layer_label
from .spectra_ui import SpectraPage
from .directivity_ui import DirectivityPage
from .terrain_ui import TerrainPage
from .calculation_ui import CalculationPage, ResultsPage
from .calculation_settings import ENGINE_ID


def button(label, callback, layout):
    widget = QPushButton(label)
    widget.clicked.connect(callback)
    layout.addWidget(widget)
    return widget


def hint(text):
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet("color: palette(text); font-size: 12px;")
    return label


class LayerTabsPage(QWidget):
    """Hosts the shared object editor under one tab per layer type."""

    def __init__(self, kinds, on_change):
        super().__init__()
        kinds = tuple(kinds)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)
        self.hosts = {}
        self.kinds = tuple(kind for kind, _ in kinds)
        for kind, label in kinds:
            host = QWidget()
            host_layout = QVBoxLayout(host)
            host_layout.setContentsMargins(0, 0, 0, 0)
            self.hosts[kind] = host
            self.tabs.addTab(host, label)
        self.tabs.currentChanged.connect(lambda index: on_change(self.current_kind()))

    def current_kind(self):
        return self.kinds[self.tabs.currentIndex()]

    def set_kind(self, kind):
        previous = self.tabs.blockSignals(True)
        try:
            self.tabs.setCurrentIndex(self.kinds.index(kind))
        finally:
            self.tabs.blockSignals(previous)


class ModelDock(QDockWidget):
    def __init__(self, controller):
        super().__init__("Acalc", controller.iface.mainWindow())
        self.controller = controller
        self.setObjectName("AcalcModelDock")
        self.setAllowedAreas(Qt.AllDockWidgetAreas)
        self.setMinimumWidth(365)
        self._updating = False
        self._calculation_busy = False
        self._log_dialog = None
        self.current_kind = None
        self.current_layer = None
        self._layer_connections = []
        self._feature_refresh = QTimer(self)
        self._feature_refresh.setSingleShot(True)
        self._feature_refresh.timeout.connect(self.refresh_features)
        widget = QWidget()
        self.setWidget(widget)
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(12, 10, 12, 10)
        header = QHBoxLayout()
        menu_button = QToolButton()
        menu_button.setText("☰")
        menu_button.setToolTip("Acalc views")
        menu_button.setAccessibleName("Acalc main menu")
        menu_button.setPopupMode(QToolButton.InstantPopup)
        menu_button.setMinimumSize(32, 32)
        menu = QMenu(menu_button)
        for title, key in (("Project", "project"), ("Calculation", "calculation"),
                           ("Spectra", "spectra"), ("Directivity", "directivity"),
                           ("Sources", "sources"), ("Receivers", "receivers"),
                           ("Terrain", "terrain"), ("Objects", "objects"),
                           ("Results", "results"), ("Settings", "settings")):
            menu.addAction(title, lambda checked=False, view=key: self.show_view(view))
        menu_button.setMenu(menu)
        header.addWidget(menu_button)
        self.heading = QLabel("Project")
        self.heading.setStyleSheet("font-size: 18px; font-weight: 600;")
        header.addWidget(self.heading, 1)
        brand = QLabel("ACALC")
        brand.setStyleSheet("color: #21818b; font-weight: 700; letter-spacing: 2px;")
        header.addWidget(brand)
        layout.addLayout(header)
        self.project_label = QLabel("No model open")
        self.project_label.setWordWrap(True)
        layout.addWidget(self.project_label)
        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)
        self.build_project_page()
        self.build_feature_page()
        self.spectra_page = SpectraPage(controller)
        self.stack.addWidget(self.spectra_page)
        self.results_page = ResultsPage(controller)
        self.stack.addWidget(self.results_page)
        self.build_variant_controls()
        self.calculation_page = CalculationPage(controller, self.variant_controls, self.variant_combo)
        self.stack.addWidget(self.calculation_page)
        from .api_settings_ui import ApiSettingsPage
        self.settings_page = ApiSettingsPage()
        self.settings_page.connectionsChanged.connect(self.api_connections_changed)
        self.settings_scroll = QScrollArea()
        self.settings_scroll.setWidgetResizable(True)
        self.settings_scroll.setFrameShape(QScrollArea.NoFrame)
        self.settings_scroll.setWidget(self.settings_page)
        self.stack.addWidget(self.settings_scroll)
        self.calculate_button = self.calculation_page.calculate_button
        self.check_calculation_button = self.calculation_page.check_button
        self.terrain_page = TerrainPage(controller)
        self.stack.addWidget(self.terrain_page)
        self.directivity_page = DirectivityPage(controller)
        self.stack.addWidget(self.directivity_page)
        self.sources_page = LayerTabsPage(
            (("point_source", "Points"), ("line_source", "Lines"), ("area_source", "Areas"),
             ("source_building", "Source Buildings")),
            self.show_view)
        self.stack.addWidget(self.sources_page)
        self.receivers_page = LayerTabsPage(
            (("point_receiver", "Points"), ("area_receiver", "Areas"),
             ("measurement", "Measurements")), self.show_view)
        self.stack.addWidget(self.receivers_page)
        self.objects_page = LayerTabsPage(
            (("building", "Buildings"), ("barrier", "Barriers")), self.show_view)
        self.stack.addWidget(self.objects_page)
        self.status = QLabel("Create or open an Acalc GeoPackage to begin.")
        self.status.setTextFormat(Qt.PlainText)
        self.status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.status.setWordWrap(True)
        self.status.setMaximumHeight(64)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.status.hide()
        layout.addWidget(self.status)
        self.refresh_project()

    def guarded(self, callback):
        return lambda *args: self.controller.run(callback)

    def build_project_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        actions = QHBoxLayout()
        button("New…", self.guarded(self.controller.new_project), actions)
        button("Open…", self.guarded(self.controller.choose_project), actions)
        self.close_button = button("Close", self.guarded(self.controller.close_project), actions)
        layout.addLayout(actions)
        self.import_button = QPushButton("Import layer…")
        self.import_button.clicked.connect(self.guarded(self.controller.import_layer))
        imports = QHBoxLayout()
        imports.addWidget(self.import_button)
        self.import_osm_button = QPushButton("Import OSM buildings…")
        self.import_osm_button.clicked.connect(self.guarded(self.controller.import_osm_buildings))
        imports.addWidget(self.import_osm_button)
        layout.addLayout(imports)
        content = QWidget()
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 6, 0)
        self.save_metadata_button = QPushButton("Save project details")
        self.save_metadata_button.clicked.connect(self.guarded(self.controller.save_metadata))
        body.addWidget(self.save_metadata_button)
        self.project_form = QWidget()
        form = QFormLayout(self.project_form)
        form.setContentsMargins(0, 10, 0, 4)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.WrapAllRows)
        self.project_name = QLineEdit()
        self.author = QLineEdit()
        self.description = QTextEdit()
        self.description.setMaximumHeight(100)
        self.vertical_datum = QLineEdit()
        self.extra_metadata = QTextEdit()
        self.extra_metadata.setMaximumHeight(85)
        self.extra_metadata.setPlaceholderText('{"client": "", "site": ""}')
        self.crs_label = QLabel()
        self.crs_label.setWordWrap(True)
        self.path_label = QLabel()
        self.path_label.setWordWrap(True)
        self.path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        form.addRow("Name", self.project_name)
        form.addRow("Author", self.author)
        form.addRow("Description", self.description)
        form.addRow("Coordinates", self.crs_label)
        coordinates_actions = QWidget()
        coordinates_layout = QVBoxLayout(coordinates_actions)
        coordinates_layout.setContentsMargins(0, 0, 0, 0)
        self.coordinates_button = QPushButton("Change CRS and units…")
        self.coordinates_button.clicked.connect(self.guarded(self.controller.change_project_coordinates))
        coordinates_layout.addWidget(self.coordinates_button)
        operations = QHBoxLayout()
        self.validate_button = button("Validate model", self.guarded(self.controller.show_validation), operations)
        self.reload_button = button("Reload layers", self.guarded(self.controller.reload_layers), operations)
        self.restore_button = button("Restore layer…", self.guarded(self.controller.restore_layer), operations)
        coordinates_layout.addLayout(operations)
        form.addRow("", coordinates_actions)
        form.addRow("Vertical datum", self.vertical_datum)
        form.addRow("GeoPackage", self.path_label)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        body.addWidget(self.project_form)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        self.overview = QLabel()
        self.overview.setWordWrap(True)
        body.addWidget(self.overview)
        log_row = QHBoxLayout()
        self.project_log = QPlainTextEdit()
        self.project_log.setReadOnly(True)
        self.project_log.setMinimumHeight(160)
        self.project_log.setAccessibleName("Project log")
        self.project_log.setPlainText("Create or open an Acalc GeoPackage to begin.")
        log_row.addWidget(self.project_log, 1)
        self.view_log_button = button("View log…", self.show_log, log_row)
        log_row.setAlignment(self.view_log_button, Qt.AlignTop)
        body.addLayout(log_row)
        body.addWidget(QLabel("Other metadata (JSON)"))
        body.addWidget(self.extra_metadata)
        self.project_page = page
        self.stack.addWidget(page)

    def append_message(self, text):
        self.status.setText(text)
        self.status.setToolTip(text)
        self.project_log.appendPlainText(str(text))
        if self._log_dialog is not None:
            self._log_text.appendPlainText(str(text))

    def show_log(self):
        if self._log_dialog is None:
            self._log_dialog = QDialog(self)
            self._log_dialog.setWindowTitle("Acalc log")
            self._log_dialog.resize(800, 560)
            layout = QVBoxLayout(self._log_dialog)
            self._log_text = QPlainTextEdit()
            self._log_text.setReadOnly(True)
            self._log_text.setPlainText(self.project_log.toPlainText())
            layout.addWidget(self._log_text)
            buttons = QDialogButtonBox(QDialogButtonBox.Close)
            buttons.rejected.connect(self._log_dialog.reject)
            layout.addWidget(buttons)
        self._log_dialog.show()
        self._log_dialog.raise_()
        self._log_dialog.activateWindow()

    def build_variant_controls(self):
        self.variant_controls = QWidget()
        layout = QVBoxLayout(self.variant_controls)
        layout.setContentsMargins(0, 0, 0, 12)
        layout.addWidget(QLabel("Variant"))
        selector = QHBoxLayout()
        self.variant_combo = QComboBox()
        self.variant_combo.setAccessibleName("Current variant")
        self.variant_combo.setMinimumContentsLength(12)
        self.variant_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.variant_combo.currentIndexChanged.connect(self.variant_selected)
        selector.addWidget(self.variant_combo, 1)
        self.variant_menu_button = QToolButton()
        self.variant_menu_button.setText("⋯")
        self.variant_menu_button.setToolTip("Variant commands")
        self.variant_menu_button.setAccessibleName("Variant commands")
        self.variant_menu_button.setPopupMode(QToolButton.InstantPopup)
        self.variant_menu_button.setMinimumSize(32, 32)
        self.variant_menu = QMenu(self.variant_menu_button)
        self.create_variant_action = self.variant_menu.addAction(
            "Create…", self.guarded(lambda: self.controller.new_variant(False)))
        self.rename_variant_action = self.variant_menu.addAction(
            "Rename…", self.guarded(self.controller.rename_variant))
        self.delete_variant_action = self.variant_menu.addAction(
            "Delete", self.guarded(self.controller.delete_variant))
        self.duplicate_variant_action = self.variant_menu.addAction(
            "Duplicate…", self.guarded(lambda: self.controller.new_variant(True)))
        self.variant_menu.addSeparator()
        self.export_button = self.variant_menu.addAction(
            "Export variant definition…", self.guarded(self.controller.export_variant))
        self.variant_actions = [self.create_variant_action, self.rename_variant_action,
                                self.delete_variant_action, self.duplicate_variant_action,
                                self.export_button]
        self.variant_menu_button.setMenu(self.variant_menu)
        selector.addWidget(self.variant_menu_button)
        layout.addLayout(selector)
        self.members = QTreeWidget()
        self.members.setHeaderHidden(True)
        self.members.setAccessibleName("Layers included in the current variant")
        self.members.setMinimumHeight(60)
        self.members.setUniformRowHeights(True)
        self.members.itemChanged.connect(self.member_changed)
        self.members.itemDoubleClicked.connect(self.open_member_layer)
        layout.addWidget(self.members)
        self.variant_summary = QLabel()
        self.variant_summary.setWordWrap(True)
        layout.addWidget(self.variant_summary)
        layout.addWidget(hint("Check layers to include; changes save immediately. Terrain uses ground points in the included layers."))

    def member_items(self):
        """Iterate layer leaves, excluding the type headings."""
        for index in range(self.members.topLevelItemCount()):
            group = self.members.topLevelItem(index)
            for child in range(group.childCount()):
                yield group.child(child)

    def open_member_layer(self, item, column):
        layer_id = item.data(0, Qt.UserRole)
        if layer_id:
            self.controller.run(lambda: self.controller.open_layer_properties(layer_id))

    def build_feature_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        self.layer_combo = QComboBox()
        self.layer_combo.currentIndexChanged.connect(self.layer_selected)
        selector = QHBoxLayout()
        selector.addWidget(self.layer_combo, 1)
        self.layer_menu_button = QToolButton()
        self.layer_menu_button.setText("☰")
        self.layer_menu_button.setToolTip("Layer commands")
        self.layer_menu_button.setAccessibleName("Layer commands")
        self.layer_menu_button.setPopupMode(QToolButton.InstantPopup)
        self.layer_menu_button.setMinimumSize(32, 32)
        self.layer_menu = QMenu(self.layer_menu_button)
        self.add_layer_button = self.layer_menu.addAction("New layer…", self.guarded(self.controller.add_layer))
        self.import_layer_button = self.layer_menu.addAction("Import layer…", self.guarded(self.controller.import_layer))
        self.rename_layer_button = self.layer_menu.addAction("Rename layer…", self.guarded(self.controller.rename_layer))
        self.remove_layer_button = self.layer_menu.addAction("Delete layer…", self.guarded(self.controller.remove_layer))
        self.set_z_values_button = self.layer_menu.addAction("Set Z values…", self.guarded(self.controller.set_layer_z_values))
        self.layer_menu_button.setMenu(self.layer_menu)
        selector.addWidget(self.layer_menu_button)
        layout.addLayout(selector)
        actions = QHBoxLayout()
        self.save_edits_button = button("Save layer edits", self.guarded(self.controller.save_layer), actions)
        self.discard_edits_button = button("Discard edits", self.guarded(self.controller.discard_layer), actions)
        layout.addLayout(actions)
        actions = QHBoxLayout()
        self.add_feature_button = button("Add coordinates…", self.guarded(lambda: self.controller.edit_feature(True)), actions)
        self.draw_button = button("Draw on map", self.guarded(self.controller.draw_feature), actions)
        layout.addLayout(actions)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Find features by name…")
        self.filter.textChanged.connect(lambda: self._feature_refresh.start(180))
        layout.addWidget(self.filter)
        self.features = QTableWidget(0, 1)
        self.features.setHorizontalHeaderLabels(["Name"])
        self.features.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.features.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.features.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.features.verticalHeader().hide()
        self.features.setMinimumHeight(135)
        self.features.horizontalHeader().setMinimumSectionSize(40)
        self.features.horizontalHeader().setDefaultSectionSize(85)
        self.features.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.features.itemSelectionChanged.connect(self.table_selection_changed)
        self.features.cellDoubleClicked.connect(self.feature_double_clicked)
        self.features.setContextMenuPolicy(Qt.CustomContextMenu)
        self.features.customContextMenuRequested.connect(self.feature_context_menu)
        layout.addWidget(self.features, 1)
        self.feature_count = QLabel()
        self.feature_count.setWordWrap(True)
        self.feature_count.setToolTip("The list shows up to 500 name matches plus all selected features.")
        layout.addWidget(self.feature_count)
        actions = QHBoxLayout()
        self.edit_feature_button = button("Properties…", self.guarded(lambda: self.controller.edit_feature(False)), actions)
        self.zoom_button = button("Zoom to selection", self.guarded(self.controller.zoom_selection), actions)
        self.delete_feature_button = button("Delete features", self.guarded(self.controller.delete_features), actions)
        layout.addLayout(actions)
        layout.addWidget(hint("Double-click a feature to edit its properties. Right-click a list item to duplicate or delete it, or right-click it on the map and choose Acalc properties."))
        layout.addWidget(hint("Coordinate edits use QGIS’s edit buffer and undo history. Save layer edits to write them to the GeoPackage."))
        self.feature_page = page
        self.feature_host = QWidget()
        self.feature_host_layout = QVBoxLayout(self.feature_host)
        self.feature_host_layout.setContentsMargins(0, 0, 0, 0)
        self.feature_host_layout.addWidget(page)
        self.stack.addWidget(self.feature_host)

    def feature_destination(self, kind):
        if kind in self.sources_page.hosts:
            self.sources_page.set_kind(kind)
            return self.sources_page, self.sources_page.hosts[kind], "Sources"
        if kind in self.receivers_page.hosts:
            self.receivers_page.set_kind(kind)
            return self.receivers_page, self.receivers_page.hosts[kind], "Receivers"
        if kind in self.objects_page.hosts:
            self.objects_page.set_kind(kind)
            return self.objects_page, self.objects_page.hosts[kind], "Objects"
        if kind in self.terrain_page.hosts:
            self.terrain_page.set_view(self.terrain_page.object_views[kind])
            return self.terrain_page, self.terrain_page.hosts[kind], "Terrain"
        return self.feature_host, self.feature_host, LAYER_TYPES[kind].title

    def show_view(self, key):
        if key == "variants":  # Redirect older callers to the Calculation page.
            key = "calculation"
        elif key == "directionality":
            key = "directivity"
        if key == "sources":
            key = self.sources_page.current_kind()
        elif key == "receivers":
            key = self.receivers_page.current_kind()
        elif key == "objects":
            key = self.objects_page.current_kind()
        if key == "terrain":
            key = self.terrain_page.current_kind() or key
        if key in LAYER_TYPES:
            page, host, heading = self.feature_destination(key)
            host.layout().addWidget(self.feature_page)
            self.feature_page.show()
        else:
            page = {"project": self.project_page,
                    "spectra": self.spectra_page, "results": self.results_page,
                    "calculation": self.calculation_page, "terrain": self.terrain_page,
                    "directivity": self.directivity_page,
                    "settings": self.settings_scroll}.get(key, self.stack.widget(0))
            heading = key.title()
        self.current_kind = key if key in LAYER_TYPES else None
        self.heading.setText(heading)
        self.stack.setCurrentWidget(page)
        self.status.setVisible(page not in (self.calculation_page, self.project_page))
        if self.current_kind:
            self.refresh_layers()
        else:
            self.bind_layer(None)
            if key == "calculation":
                self.refresh_variants()
                self.calculation_page.refresh()
            elif key == "spectra":
                self.spectra_page.refresh()
            elif key == "directivity":
                self.directivity_page.refresh()
            elif key == "results":
                self.results_page.refresh()
            elif key == "terrain":
                self.terrain_page.refresh()
                self.terrain_page.rasters_page.open_terrain()
            elif key == "settings":
                self.settings_page.refresh()

    def api_connections_changed(self):
        self.calculation_page.refresh()
        # Rebuild idle setup panels against the newly discovered connections.
        if not self.controller.engine_runner.active:
            self.controller.close_calculation_dialog()
            self.controller.close_back_calculation_dialog()

    def set_calculating(self, busy):
        self._calculation_busy = busy
        active = self.controller.store is not None
        for widget in (self.project_form, self.extra_metadata, self.save_metadata_button, self.import_button, self.import_osm_button, self.feature_page,
                       self.sources_page, self.receivers_page, self.objects_page, self.spectra_page,
                       self.directivity_page, self.terrain_page, self.variant_controls,
                       *self.variant_actions):
            widget.setEnabled(active and not busy)
        self.delete_variant_action.setEnabled(active and not busy and self.variant_combo.count() > 1)
        self.calculation_page.set_calculating(busy)
        self.settings_page.setEnabled(not busy)
        self.results_page.set_calculating(busy)

    def refresh_project(self, metadata=True):
        store = self.controller.store
        self._updating = True
        try:
            active = store is not None
            self.project_form.setEnabled(active)
            self.extra_metadata.setEnabled(active)
            self.save_metadata_button.setEnabled(active)
            self.close_button.setEnabled(active)
            for widget in (self.validate_button, self.reload_button, self.restore_button, self.variant_controls):
                widget.setEnabled(active)
            if active:
                data = store.metadata()
                self.project_label.setText(data["name"])
                if metadata:
                    import json
                    self.project_name.setText(data["name"])
                    self.author.setText(data["author"])
                    self.description.setPlainText(data["description"])
                    self.vertical_datum.setText(data["vertical_datum"])
                    self.extra_metadata.setPlainText(json.dumps(json.loads(data["metadata_json"]), indent=2, ensure_ascii=False))
                    self.controller.metadata_snapshot = self.metadata_values()
                from .units import elevation_unit, display_unit_name
                self.crs_label.setText(f"{data['crs_authid'] or 'Custom projected CRS'} · CRS units: {display_unit_name(data['horizontal_unit'])}\nElevation / height units: {elevation_unit(store)}")
                self.path_label.setText(store.path)
                self.overview.setText(f"{len(store.layers())} model layers  ·  {len(store.variants())} variants\n{len(store.spectra('sound_power'))} sound power spectra · {len(store.spectra('absorption'))} absorption spectra\nModel schema {data['schema_version']} · Created {data['created_at'][:10]}")
            else:
                self.project_label.setText("No model open")
                for field in (self.project_name, self.author, self.vertical_datum, self.description, self.extra_metadata, self.crs_label, self.path_label, self.overview):
                    field.clear()
        finally:
            self._updating = False
        self.refresh_variants()
        self.refresh_layers()
        self.spectra_page.refresh()
        self.directivity_page.refresh()
        # Saved result metadata can contain large per-source diagnostics. Read it
        # on navigation, not while opening a model or editing project details.
        if store is None or self.stack.currentWidget() is self.results_page:
            self.results_page.refresh()
        self.calculation_page.refresh()
        self.terrain_page.refresh()
        self.set_calculating(bool(self.controller.engine_runner and self.controller.engine_runner.active))

    def metadata_values(self):
        return (self.project_name.text(), self.author.text(), self.description.toPlainText(),
                self.vertical_datum.text(), self.extra_metadata.toPlainText())

    def refresh_variants(self, preferred=None):
        self._updating = True
        selected = preferred or self.variant_combo.currentData()
        self.variant_combo.clear()
        store = self.controller.store
        if store:
            for config in store.variants():
                self.variant_combo.addItem(config["name"], config["variant_id"])
            index = self.variant_combo.findData(selected)
            self.variant_combo.setCurrentIndex(max(0, index))
        self._updating = False
        self.variant_selected()

    def variant_selected(self, *args):
        if self._updating:
            return
        self._updating = True
        expanded = {self.members.topLevelItem(i).data(0, Qt.UserRole + 1):
                    self.members.topLevelItem(i).isExpanded()
                    for i in range(self.members.topLevelItemCount())}
        current = self.members.currentItem()
        selected = current.data(0, Qt.UserRole) if current else None
        scroll = self.members.verticalScrollBar().value()
        self.members.clear()
        config = self.controller.selected_variant()
        if config:
            included = set(config["definition"]["layer_ids"])
            records = self.controller.store.layers()
            generated = self.controller.store.generated_terrain_ids()
            groups = [(kind.key, kind.title) for kind in LAYER_TYPES.values()] + [('generated_terrain', 'Generated terrain')]
            for key, title in groups:
                layers = [record for record in records
                          if ('generated_terrain' if record['layer_id'] in generated else record['feature_type']) == key]
                if not layers:
                    continue
                group = QTreeWidgetItem(self.members, [title])
                group.setData(0, Qt.UserRole + 1, key)
                group.setFlags(Qt.ItemIsEnabled)
                for record in layers:
                    item = QTreeWidgetItem(group, [layer_label(self.controller, record)])
                    item.setData(0, Qt.UserRole, record["layer_id"])
                    item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
                    item.setCheckState(0, Qt.Checked if record["layer_id"] in included else Qt.Unchecked)
                    if record["layer_id"] == selected:
                        self.members.setCurrentItem(item)
                group.setExpanded(expanded.get(key, True))
            self.members.verticalScrollBar().setValue(scroll)
            self.variant_summary.setText(f"{len(included)} of {len(records)} object layers included")
        else:
            self.variant_summary.clear()
        self.delete_variant_action.setEnabled(bool(config) and not self._calculation_busy
                                              and self.variant_combo.count() > 1)
        self._updating = False

    def member_changed(self, item, column):
        if not self._updating and item.data(0, Qt.UserRole):
            self.controller.run(self.controller.save_membership)

    def refresh_layer_labels(self):
        """Update text in place so selection, check state and double clicks survive."""
        if not self.controller.store:
            return
        records = {row["layer_id"]: row for row in self.controller.store.layers()}
        previous = self.members.blockSignals(True)
        try:
            for item in self.member_items():
                record = records.get(item.data(0, Qt.UserRole))
                if record:
                    item.setText(0, layer_label(self.controller, record))
            for index in range(self.layer_combo.count()):
                record = records.get(self.layer_combo.itemData(index))
                if record:
                    self.layer_combo.setItemText(index, layer_label(self.controller, record))
        finally:
            self.members.blockSignals(previous)
        self.terrain_page.rasters_page.refresh_labels()

    def authored_layers(self, kind):
        store = self.controller.store
        if not store:
            return []
        generated = store.generated_terrain_ids() if kind == 'terrain_line' else set()
        return [record for record in store.layers()
                if record['feature_type'] == kind and record['layer_id'] not in generated]

    def refresh_layers(self, preferred=None):
        if not self.current_kind:
            self.bind_layer(None)
            return
        selected = preferred or self.layer_combo.currentData()
        self._updating = True
        self.layer_combo.clear()
        for record in self.authored_layers(self.current_kind):
            self.layer_combo.addItem(layer_label(self.controller, record), record["layer_id"])
        index = self.layer_combo.findData(selected)
        self.layer_combo.setCurrentIndex(max(0, index))
        self._updating = False
        self.layer_selected()

    def layer_selected(self, *args):
        if self._updating:
            return
        layer_id = self.layer_combo.currentData()
        if layer_id:
            self.controller.run(lambda: self.controller.activate_layer(layer_id))
        else:
            self.bind_layer(None)

    def select_layer(self, layer):
        kind = layer.customProperty("acalc/feature_type")
        if kind not in LAYER_TYPES:
            return
        layer_id = layer.customProperty("acalc/layer_id")
        if layer_id in self.controller.store.generated_terrain_ids():
            self.current_kind = None
            self.bind_layer(None)
            self.terrain_page.set_view('rasters')
            self.heading.setText('Terrain')
            self.stack.setCurrentWidget(self.terrain_page)
            self.status.show()
            self.terrain_page.rasters_page.refresh(layer_id)
            return
        page, host, heading = self.feature_destination(kind)
        if self.current_layer is layer and self.current_kind == kind and self.stack.currentWidget() is page:
            self.sync_selection()
            return
        self.current_kind = kind
        host.layout().addWidget(self.feature_page)
        self.feature_page.show()
        self.heading.setText(heading)
        self.stack.setCurrentWidget(page)
        self._updating = True
        self.layer_combo.clear()
        for record in self.authored_layers(kind):
            self.layer_combo.addItem(layer_label(self.controller, record), record["layer_id"])
        self.layer_combo.setCurrentIndex(self.layer_combo.findData(layer.customProperty("acalc/layer_id")))
        self._updating = False
        self.bind_layer(layer)

    def bind_layer(self, layer):
        if layer is not self.current_layer:
            for signal, slot in self._layer_connections:
                try:
                    signal.disconnect(slot)
                except (TypeError, RuntimeError):
                    pass
            self._layer_connections.clear()
            self.current_layer = layer
            if layer is not None:
                layer.selectionChanged.connect(self.sync_selection)
                self._layer_connections.append((layer.selectionChanged, self.sync_selection))
                for signal in (layer.featureAdded, layer.featureDeleted,
                               layer.editingStopped, layer.afterRollBack, layer.afterCommitChanges):
                    slot = lambda *args: self._feature_refresh.start(0)
                    signal.connect(slot)
                    self._layer_connections.append((signal, slot))
                # The list only displays names. Geometry and other attribute
                # edits need new save/discard state, not another provider scan.
                layer.attributeValueChanged.connect(self.feature_attribute_changed)
                self._layer_connections.append((layer.attributeValueChanged, self.feature_attribute_changed))
                for signal in (layer.editingStarted, layer.layerModified, layer.undoStack().indexChanged):
                    signal.connect(self.refresh_edit_state)
                    self._layer_connections.append((signal, self.refresh_edit_state))
        self.refresh_features()

    def feature_attribute_changed(self, fid, index, value):
        if self.current_layer is not None and index == self.current_layer.fields().indexFromName("name"):
            self._feature_refresh.start(0)

    def refresh_edit_state(self, *args):
        dirty = self.current_layer is not None and self.current_layer.isModified()
        self.save_edits_button.setEnabled(dirty)
        self.discard_edits_button.setEnabled(dirty)

    def refresh_features(self):
        self._feature_refresh.stop()
        layer = self.current_layer
        self._updating = True
        self.features.setRowCount(0)
        self.layer_menu_button.setEnabled(self.controller.store is not None)
        self.add_layer_button.setEnabled(self.controller.store is not None)
        self.set_z_values_button.setVisible(self.current_kind in LAYER_TYPES and LAYER_TYPES[self.current_kind].ground_points)
        self.set_z_values_button.setEnabled(layer is not None and layer.featureCount() > 0)
        for widget in (self.rename_layer_button, self.remove_layer_button, self.add_feature_button, self.draw_button, self.filter):
            widget.setEnabled(layer is not None)
        for widget in (self.edit_feature_button, self.zoom_button, self.delete_feature_button, self.save_edits_button, self.discard_edits_button):
            widget.setEnabled(False)
        if layer is None:
            self.feature_count.setText("No layer yet. Create one to start adding features.")
            self._updating = False
            return
        selected_ids = set(layer.selectedFeatureIds())
        request = (QgsFeatureRequest().setFlags(QgsFeatureRequest.NoGeometry)
                   .setSubsetOfAttributes(["name"], layer.fields()).setLimit(500))
        if self.filter.text().strip():
            from qgis.core import QgsExpression
            request.setFilterExpression('strpos(lower(coalesce("name",\'\')), lower(' + QgsExpression.quotedString(self.filter.text().strip()) + ')) > 0')
        features = {feature.id(): feature for feature in layer.getFeatures(request)}
        # Selected map features remain visible even outside the filter / first 500.
        missing = selected_ids - features.keys()
        if missing:
            selected_request = (QgsFeatureRequest().setFilterFids(list(missing))
                                .setFlags(QgsFeatureRequest.NoGeometry)
                                .setSubsetOfAttributes(["name"], layer.fields()))
            features.update((feature.id(), feature) for feature in layer.getFeatures(selected_request))
        self.features.setRowCount(len(features))
        selection = QItemSelection()
        for row, feature in enumerate(features.values()):
            name = QTableWidgetItem(null_text(feature["name"]))
            name.setData(Qt.UserRole, feature.id())
            self.features.setItem(row, 0, name)
            if feature.id() in selected_ids:
                index = self.features.model().index(row, 0)
                selection.select(index, index)
        self.features.selectionModel().select(selection, QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows)
        self.feature_count.setText(f"{layer.featureCount()} features · {len(selected_ids)} selected · {len(features)} shown")
        self.edit_feature_button.setEnabled(len(selected_ids) == 1)
        self.zoom_button.setEnabled(bool(selected_ids))
        self.delete_feature_button.setEnabled(bool(selected_ids))
        self.refresh_edit_state()
        self._updating = False

    def sync_selection(self, *args):
        """Keep row objects intact between the two clicks of a double-click."""
        layer = self.current_layer
        if self._updating or layer is None:
            return
        selected = set(layer.selectedFeatureIds())
        rows = {self.features.item(row, 0).data(Qt.UserRole): row for row in range(self.features.rowCount())}
        if not selected <= rows.keys():
            self.refresh_features()
            return
        self._updating = True
        try:
            selection = QItemSelection()
            for fid in selected:
                index = self.features.model().index(rows[fid], 0)
                selection.select(index, index)
            self.features.selectionModel().select(selection, QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows)
            self.feature_count.setText(f"{layer.featureCount()} features · {len(selected)} selected · {len(rows)} shown")
            self.edit_feature_button.setEnabled(len(selected) == 1)
            self.zoom_button.setEnabled(bool(selected))
            self.delete_feature_button.setEnabled(bool(selected))
        finally:
            self._updating = False

    def feature_double_clicked(self, row, column):
        if self.current_layer is None:
            return
        item = self.features.item(row, 0)
        if item is not None:
            layer_id, fid = self.current_layer.id(), item.data(Qt.UserRole)
            self.controller.run(lambda: self.controller.open_feature_properties(layer_id, fid))

    def table_selection_changed(self):
        if self._updating or self.current_layer is None:
            return
        rows = {index.row() for index in self.features.selectedIndexes()}
        ids = [self.features.item(row, 0).data(Qt.UserRole) for row in rows]
        self.current_layer.selectByIds(ids)

    def feature_context_menu(self, position):
        item = self.features.itemAt(position)
        if self.current_layer is None or item is None:
            return
        layer_id = self.current_layer.id()
        fid = self.features.item(item.row(), 0).data(Qt.UserRole)
        menu = QMenu(self.features)
        menu.addAction("Properties…", self.guarded(lambda: self.controller.open_feature_properties(layer_id, fid)))
        menu.addAction("Duplicate", self.guarded(lambda: self.controller.duplicate_feature(layer_id, fid)))
        move_menu = menu.addMenu("Move to layer")
        destinations = self.controller.feature_move_destinations(self.current_layer)
        for record in destinations:
            move_menu.addAction(record["name"], self.guarded(
                lambda target=record["layer_id"]: self.controller.move_feature(layer_id, fid, target)))
        move_menu.setEnabled(bool(destinations))
        menu.addAction("Delete", self.guarded(lambda: self.controller.delete_feature(layer_id, fid)))
        try:
            menu.exec(self.features.viewport().mapToGlobal(position))
        finally:
            menu.deleteLater()

    def shutdown(self):
        self.bind_layer(None)
        self._feature_refresh.stop()
        if self._log_dialog is not None:
            self._log_dialog.close()
