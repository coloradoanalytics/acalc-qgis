# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Acalc acoustical model authoring for QGIS."""


def classFactory(iface):
    from .plugin import AcalcPlugin

    return AcalcPlugin(iface)
