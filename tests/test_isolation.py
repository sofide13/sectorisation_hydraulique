# -*- coding: utf-8 -*-
"""Test de la règle réseau d'isolabilité, sans dépendance QGIS."""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.isolation import construire_vannes_sectorisation, verifier_isolabilite

# Deux conduites parallèles entre S1 et S2 doivent produire deux vannes.
frontieres = [
    {'pipe_id': 11, 'secteur_amont': 'S1', 'secteur_aval': 'S2'},
    {'pipe_id': 12, 'secteur_amont': 'S1', 'secteur_aval': 'S2'},
    {'pipe_id': 13, 'secteur_amont': 'S2', 'secteur_aval': 'S3'},
]
vannes, attendues, couvertes, sans_vanne = construire_vannes_sectorisation(frontieres)
assert len(vannes) == 3
assert attendues == {('S1', 'S2'), ('S2', 'S3')}
assert couvertes == attendues
assert sans_vanne == set()
assert verifier_isolabilite(vannes, ['S1', 'S2', 'S3']) == {('S1', 'S3')}
print('TEST ISOLABILITE OK')
