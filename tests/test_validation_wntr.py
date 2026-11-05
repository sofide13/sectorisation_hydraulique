# -*- coding: utf-8 -*-
"""Test minimal du pont WNTR, indépendant de QGIS."""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import wntr  # noqa: F401
except ImportError:
    print("SKIP : wntr non installe")
else:
    from core.validation_wntr import valider_pressions_wntr

    resultat = valider_pressions_wntr(
        noeud_altitudes={'N0': 100.0, 'N1': 98.0},
        pipe_edges=[{
            'pipe_id': 1, 'n1': 'N0', 'n2': 'N1',
            'longueur_m': 100.0, 'diametre_mm': 200.0,
        }],
        abonnes_par_noeud={'N0': 0, 'N1': 10},
        q_pointe_abonne=0.00001,
        secteur_de_noeud={'N0': 'S1', 'N1': 'S1'},
        source_par_secteur={'S1': 'N0'},
        cote_piezo_par_secteur={'S1': 130.0},
        pression_min_par_noeud={'N0': 10.0, 'N1': 10.0},
        pression_min_m=10.0,
        pression_max_m=60.0,
        log=lambda msg: None,
    )
    assert resultat['statut'] == 'OK', resultat
    assert resultat['nb_noeuds'] == 2
    assert resultat['nb_violations_pression'] == 0
    assert set(resultat['noeuds']) == {'N0', 'N1'}

    # Une conduite frontière doit être comptée puis fermée par secteur.
    resultat_frontiere = valider_pressions_wntr(
        noeud_altitudes={'A0': 100.0, 'A1': 99.0, 'B0': 100.0},
        pipe_edges=[
            {'pipe_id': 1, 'n1': 'A0', 'n2': 'A1', 'longueur_m': 100.0, 'diametre_mm': 200.0},
            {'pipe_id': 2, 'n1': 'A1', 'n2': 'B0', 'longueur_m': 100.0, 'diametre_mm': 200.0},
        ],
        abonnes_par_noeud={'A0': 0, 'A1': 10, 'B0': 10},
        q_pointe_abonne=0.00001,
        secteur_de_noeud={'A0': 'S1', 'A1': 'S1', 'B0': 'S2'},
        source_par_secteur={'S1': 'A0', 'S2': 'B0'},
        cote_piezo_par_secteur={'S1': 130.0, 'S2': 130.0},
        pression_min_par_noeud={'A0': 10.0, 'A1': 10.0, 'B0': 10.0},
        pression_min_m=10.0,
        pression_max_m=60.0,
        log=lambda msg: None,
    )
    assert resultat_frontiere['statut'] == 'OK', resultat_frontiere
    assert resultat_frontiere['nb_conduites_frontieres_fermees'] == 1
    print('TEST WNTR OK')
