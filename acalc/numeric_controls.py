# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Typed numeric inputs: validation without spinner or wheel adjustments."""

from qgis.PyQt.QtWidgets import (
    QAbstractSpinBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFormLayout, QSpinBox,
)


class TypedNumberMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setButtonSymbols(QAbstractSpinBox.NoButtons)

    def wheelEvent(self, event):
        # Let the surrounding scroll area handle the wheel, even while focused.
        event.ignore()

    def stepBy(self, steps):
        # Numbers change only through text entry, including paste.
        pass


class DecimalInput(TypedNumberMixin, QDoubleSpinBox):
    pass


class IntegerInput(TypedNumberMixin, QSpinBox):
    pass


def get_decimal(parent, title, label, value, minimum, maximum, decimals=2):
    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    layout = QFormLayout(dialog)
    field = DecimalInput(dialog)
    field.setDecimals(decimals)
    field.setRange(minimum, maximum)
    field.setValue(value)
    layout.addRow(label, field)
    buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addRow(buttons)
    field.selectAll()
    accepted = dialog.exec() == QDialog.Accepted
    return field.value(), accepted
