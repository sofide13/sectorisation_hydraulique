# -*- coding: utf-8 -*-
"""Sectorisation Hydraulique - Extension QGIS.

Auteur : Kouidri Sofiane
Copyright (c) 2026 Kouidri Sofiane
"""
__author__ = "Kouidri Sofiane"


def classFactory(iface):
    from .plugin_main import SectorisationHydrauliquePlugin
    return SectorisationHydrauliquePlugin(iface)
