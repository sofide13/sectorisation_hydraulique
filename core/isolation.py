# -*- coding: utf-8 -*-
"""Contrôles purs de couverture des frontières par vannes d'isolement.

Ce module ne dépend ni de QGIS ni de WNTR afin que la règle d'isolabilité
puisse être testée indépendamment du rendu cartographique.
"""


def construire_vannes_sectorisation(conduites_frontalieres):
    """Retourne une vanne logique par conduite inter-secteurs.

    Une paire de secteurs peut donc avoir plusieurs vannes si plusieurs
    conduites parallèles la relient. La couverture ne doit pas être déduite
    des seuls polygones.
    """
    vannes = []
    paires_attendues = set()
    for conduite in conduites_frontalieres:
        a = conduite.get('secteur_amont', conduite.get('secteur_a'))
        b = conduite.get('secteur_aval', conduite.get('secteur_b'))
        if a is None or b is None or a == b:
            continue
        paire = tuple(sorted((str(a), str(b))))
        paires_attendues.add(paire)
        vannes.append({
            'pipe_id': conduite.get('pipe_id'),
            'secteur_a': a,
            'secteur_b': b,
            'diametre': conduite.get('diametre'),
            'longueur': conduite.get('longueur'),
            'geometry': conduite.get('geometry'),
        })
    paires_vanne = {
        tuple(sorted((str(v['secteur_a']), str(v['secteur_b']))))
        for v in vannes
    }
    return vannes, paires_attendues, paires_vanne, paires_attendues - paires_vanne


def verifier_isolabilite(vannes, secteurs):
    """Retourne les paires de secteurs qui ne sont pas couvertes."""
    secteurs = sorted(str(s) for s in secteurs)
    attendues = {tuple((a, b)) for i, a in enumerate(secteurs) for b in secteurs[i + 1:]}
    couvertes = {
        tuple(sorted((str(v['secteur_a']), str(v['secteur_b']))))
        for v in vannes
        if v.get('secteur_a') is not None and v.get('secteur_b') is not None
    }
    return attendues - couvertes
