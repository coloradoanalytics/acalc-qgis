# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Real Qt input events must not change numbers merely by navigating the UI."""

import unittest

from qgis.PyQt.QtCore import QPoint, QPointF, Qt
from qgis.PyQt.QtGui import QWheelEvent
from qgis.PyQt.QtTest import QTest
from qgis.PyQt.QtWidgets import (
    QAbstractSpinBox, QLineEdit, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from acalc.calculation_ui import ISOSettingsEditor
from acalc.dialogs import InactiveCellDelegate
from acalc.layers import load_layer
from acalc.numeric_controls import DecimalInput, IntegerInput
from acalc.schema import LAYER_TYPES
from acalc.units import LengthSpinBox
from tests.test_model import APP, ModelFixture


class NumericControlTests(unittest.TestCase):
    def host(self, field):
        window = QWidget()
        layout = QVBoxLayout(window)
        layout.addWidget(field)
        other = QLineEdit()
        layout.addWidget(other)
        window.show()
        other.setFocus()
        APP.processEvents()
        self.addCleanup(window.close)
        return window, other

    def test_wheel_and_step_keys_never_change_focused_or_unfocused_numbers(self):
        for cls in (DecimalInput, IntegerInput, LengthSpinBox):
            field = cls()
            window, other = self.host(field)
            field.setValue(12)
            self.assertEqual(field.buttonSymbols(), QAbstractSpinBox.NoButtons)
            for focused in (False, True):
                (field if focused else other).setFocus()
                APP.processEvents()
                event = QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(),
                                    QPoint(0, 120), Qt.NoButton, Qt.NoModifier,
                                    Qt.NoScrollPhase, False)
                APP.sendEvent(field, event)
                self.assertFalse(event.isAccepted())
                for key in (Qt.Key_Up, Qt.Key_Down, Qt.Key_PageUp, Qt.Key_PageDown):
                    QTest.keyClick(field, key)
                self.assertEqual(field.value(), 12)
            field.setFocus()
            field.selectAll()
            APP.clipboard().setText('27')
            QTest.keyClick(field, Qt.Key_V, Qt.ControlModifier)
            QTest.keyClick(field, Qt.Key_Tab)
            self.assertEqual(field.value(), 27)

    def test_length_display_focus_paste_and_units_preserve_precision(self):
        for unit in ('m', 'ft'):
            field = LengthSpinBox(unit)
            field.setRange(-1000000, 1000000)
            original = 1.234567891234
            field.setValue(original)
            window, other = self.host(field)
            self.assertEqual(field.text(), ('1.23 m' if unit == 'm' else '4.05 ft'))
            for _ in range(2):
                field.setFocus()
                APP.processEvents()
                QTest.keyClick(field, Qt.Key_Tab)
                APP.processEvents()
                field.interpretText()
                self.assertEqual(field.value(), original)
            field.set_unit('ft' if unit == 'm' else 'm')
            self.assertEqual(field.value(), original)
            field.setFocus()
            field.selectAll()
            APP.clipboard().setText('12.3456')
            QTest.keyClick(field, Qt.Key_V, Qt.ControlModifier)
            QTest.keyClick(field, Qt.Key_Tab)
            self.assertAlmostEqual(field.value(), 12.3456 * field.factor)
            self.assertTrue(field.text().startswith('12.35'))

    def test_small_calculation_lengths_and_settings_survive_display_round_trip(self):
        editor = ISOSettingsEditor()
        self.addCleanup(editor.close)
        settings = editor.settings()
        settings['area_max_extent_m'] = .001
        settings['area_relative_tolerance'] = 0.000123456789
        editor.set_settings(settings)
        self.assertEqual(editor.settings(), settings)
        self.assertEqual(float(editor.optional_fields['area_max_extent_m'].text()), .001)
        editor.set_input_unit('ft')
        editor.set_input_unit('m')
        self.assertEqual(editor.settings(), settings)

    def test_coordinate_display_leaves_full_precision_available_to_editor(self):
        table = QTableWidget(1, 1)
        self.addCleanup(table.close)
        delegate = InactiveCellDelegate(table, '#eeeeee', '#888888')
        table.setItemDelegate(delegate)
        table.setItem(0, 0, QTableWidgetItem('123456.78912345'))
        index = table.model().index(0, 0)
        self.assertEqual(delegate.displayText(index.data(), table.locale()), '123456.79')
        self.assertEqual(index.data(Qt.EditRole), '123456.78912345')
        self.assertEqual(delegate.displayText('Calculated', table.locale()), 'Calculated')


class NativeNumericEditorTests(ModelFixture):
    def test_model_attribute_editors_use_text_and_retain_ground_constraints(self):
        for kind in LAYER_TYPES.values():
            record = self.store.add_layer(kind.key, kind.title)
            layer = load_layer(self.store, record)
            for index, field in enumerate(layer.fields()):
                if field.isNumeric():
                    self.assertIn(layer.editorWidgetSetup(index).type(), ('TextEdit', 'Hidden'))
            if kind.key == 'ground_absorption':
                field = layer.fields().field('ground_factor')
                self.assertEqual(field.constraints().constraintExpression(),
                                 '"ground_factor" >= 0 AND "ground_factor" <= 1')
