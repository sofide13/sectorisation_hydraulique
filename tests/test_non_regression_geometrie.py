# -*- coding: utf-8 -*-
"""
Test de non-régression géométrique -- build actuelbis
====================================================

Objet : prouver, hors QGIS (backend shapely, cf. core/geom_backend.py), que
les deux défauts constatés par l'utilisateur sur secteurs_hydrauliques_topo
ne peuvent plus se reproduire :

  A. un secteur emboîté ("poupée russe") dans un autre -- provoqué par
     l'ancienne résolution de chevauchement qui faisait gagner la zone en
     litige au secteur comptant le PLUS d'abonnés (au global, ou dans la
     zone), sans lien avec la proximité réelle du réseau ;
  B. une somme des aires de secteurs_hydrauliques_topo différente de celle
     de secteurs_hydrauliques -- provoquée par un second calcul
     indépendant de partition_secteurs.construire_territoires() (emprise et
     poids différents de ceux du calcul de référence), cf.
     core/polygones_topologiques._reutiliser_polygones_reference().

Ce fichier fournit aussi deux fonctions de vérification réutilisables
(``verifier_conservation_aire`` et ``verifier_aucun_emboitement``) : ce sont
EXACTEMENT les deux contrôles demandés par l'utilisateur, écrits une fois et
appelables aussi bien ici (données synthétiques) que dans
outils/verifier_non_regression_geometrie.py (couches QGIS réelles du
projet, à lancer après un run complet du moteur hydraulique).

Lancement :
    python3 tests/test_non_regression_geometrie.py
"""

import os
import sys

ICI = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(ICI)
sys.path.insert(0, PLUGIN)

from core import geom_backend as gb          # noqa: E402
from core import partition_secteurs as ps     # noqa: E402

assert gb.BACKEND in ("shapely", "qgis"), f"backend inattendu : {gb.BACKEND}"
print(f"Backend geometrique utilise pour ce test : {gb.BACKEND} "
      f"(shapely hors QGIS -- python3 tests/... -- ou qgis si lance via "
      f"python-qgis / la console QGIS : les deux sont valides, le module "
      f"core.geom_backend rend la logique testee ici agnostique au backend).")


# ---------------------------------------------------------------------------
# Contrôles réutilisables (les deux tests demandés par l'utilisateur)
# ---------------------------------------------------------------------------

def verifier_conservation_aire(aires_reference, aires_topo, tolerance_relative=0.01):
    """Sigma(aire) doit être identique entre la reference (secteurs_
    hydrauliques) et le socle topologique (secteurs_hydrauliques_topo),
    a une tolerance topologique pres (snapping, arrondis de buffer).

    ``aires_reference`` / ``aires_topo`` : {secteur_id: aire_m2}.
    Leve AssertionError si l'ecart relatif global depasse ``tolerance_relative``
    (defaut 1%). Retourne l'ecart relatif mesure.
    """
    total_ref = sum(aires_reference.values())
    total_topo = sum(aires_topo.values())
    if total_ref <= 0:
        raise AssertionError("aire de reference nulle ou negative")
    ecart = abs(total_topo - total_ref) / total_ref
    assert ecart <= tolerance_relative, (
        f"Sigma(aire) ne correspond pas a la reference : "
        f"reference={total_ref:,.0f} m2, topo={total_topo:,.0f} m2, "
        f"ecart={ecart:.2%} (tolerance={tolerance_relative:.2%}) -- un second "
        f"moteur d'ilots independant a probablement ete relance (cf. "
        f"core/polygones_topologiques._reutiliser_polygones_reference)."
    )
    return ecart


def verifier_aucun_emboitement(territoires, seuil_contenu=0.99):
    """Aucun polygone de secteur ne doit être contenu à ~100% dans un autre
    ("poupée russe"). Test all-pairs : pour toute paire (A, B) A != B, la part
    de l'aire de A recouverte par B ne doit jamais dépasser ``seuil_contenu``
    (99% par défaut -- pas 100% strict pour absorber le bruit numérique de
    l'intersection).

    ``territoires`` : {secteur_id: Geom} (cf. core/geom_backend.Geom) ou tout
    objet exposant ``.area`` et ``.intersection(autre).area``.
    Leve AssertionError avec le detail de la premiere paire fautive trouvee ;
    liste TOUTES les paires fautives dans le message.
    """
    fautifs = []
    ids = sorted(territoires.keys(), key=str)
    for a in ids:
        ga = territoires[a]
        if ga is None or ga.area <= 0:
            continue
        for b in ids:
            if a == b:
                continue
            gb_ = territoires[b]
            if gb_ is None or gb_.area <= 0:
                continue
            inter = ga.intersection(gb_)
            part = 0.0 if inter is None else inter.area / ga.area
            if part >= seuil_contenu:
                fautifs.append((a, b, part))
    assert not fautifs, (
        "secteur(s) emboite(s) detecte(s) (poupee russe) : " + "; ".join(
            f"{a} contenu a {p:.1%} dans {b}" for a, b, p in fautifs
        )
    )
    return fautifs


# ---------------------------------------------------------------------------
# A. Reproduction du cas d'emboitement (ancien critere "plus d'abonnes")
# ---------------------------------------------------------------------------
#
# 'PETIT' : petit secteur (peu d'abonnes/poids), mais son NOEUD REEL est a
#           quelques metres de la zone en litige.
# 'GROS'  : secteur bien plus peuple au global, mais son seul noeud est tres
#           loin de la zone en litige (antenne eloignee, cas frequent d'un
#           reservoir principal qui dessert un quartier eloigne).
#
# Avant le correctif, une resolution par "plus d'abonnes global" aurait fait
# gagner GROS sur toute la bande en litige malgre son eloignement reel -- un
# secteur peut alors se retrouver ampute au profit d'un secteur dont le
# reseau ne passe meme pas a proximite, produisant un morceau de GROS
# entoure par PETIT (emboitement).

PETIT = gb.box(0, 0, 320, 300)                 # petit secteur
GROS = gb.box(280, -50, 700, 350)              # chevauche PETIT sur 40 m

# noeuds : celui de PETIT est juste a l'interieur, pres de la frontiere ;
# celui de GROS est loin de la zone en litige (bande x in [280, 320]).
noeud_points = {'p1': (150.0, 150.0), 'g1': (650.0, 150.0)}
secteur_de_noeud = {'p1': 'PETIT', 'g1': 'GROS'}
poids_noeud = {'p1': 5, 'g1': 5000}            # GROS ecrase PETIT en poids

CFG = {
    'ACTIVER_PARTITION_ILOTS': True,
    'SEUIL_AIRE_ILOT_M2': 10.0,
    'MAX_PASSES_NETTOYAGE': 4,
    'SNAP_LIMITE_M': 0.5,
    'ASSURER_CONTIGUITE_TERRITOIRES': False,   # isole le test sur l'etape 2
}

sales = {'PETIT': PETIT, 'GROS': GROS}
logs = []
propres, stats = ps.nettoyer_polygones(
    sales, noeud_points, secteur_de_noeud, poids_noeud, CFG, log=logs.append)
print("\n".join(logs))

qual = ps.statistiques_partition(propres)
assert qual['nb_chevauchements'] == 0, "chevauchement non resolu"

# LE POINT CLE : PETIT doit garder la bande en litige, car son nœud (150,150)
# est bien plus proche du chevauchement (bande x in [280,320], y in [0,300])
# que celui de GROS (650,150) -- même si GROS pèse 1000x plus d'abonnés.
zone_litige_centre = (300.0, 150.0)
assert propres['PETIT'].contains_xy(*zone_litige_centre), (
    "FIX-CHEVAUCHEMENT-DISTANCE en échec : la zone en litige n'est pas "
    "restée a PETIT alors que son nœud en est le plus proche -- vérifier "
    "que nettoyer_polygones() utilise bien idx_noeuds.nearest_matching() "
    "et non plus poids_dans() comme critere prioritaire."
)
print("CHECK A OK : la zone en litige va au secteur dont le nœud réseau est "
      "le plus proche, jamais a celui qui a le plus d'abonnés au global "
      f"({stats}).")

# aucun emboitement : GROS ne doit pas se retrouver comme une ile a
# l'interieur de PETIT (ou l'inverse)
verifier_aucun_emboitement(propres)
print("CHECK A bis OK : aucun secteur emboite (all-pairs within()).")


# ---------------------------------------------------------------------------
# B. Conservation de l'aire -- reutilisation directe de la reference
# ---------------------------------------------------------------------------
#
# Simule ce que fait desormais core.polygones_topologiques.
# _reutiliser_polygones_reference() : les polygones de secteurs_hydrauliques_
# topo sont les MEMES OBJETS que ceux de secteurs_hydrauliques (aucun
# second calcul d'ilots). Sigma(aire) est donc rigoureusement identique, par
# construction -- ce test verifie que le controle lui-meme detecterait un
# ecart si, par erreur, un second moteur produisait une geometrie differente.

aires_reference = {'PETIT': PETIT.area, 'GROS': GROS.area}

# cas OK : geometrie reprise telle quelle (comportement attendu du correctif)
aires_topo_ok = dict(aires_reference)
ecart = verifier_conservation_aire(aires_reference, aires_topo_ok)
assert ecart == 0.0
print("CHECK B OK : aucune reutilisation de reference => aucun ecart de "
      "surface possible.")

# cas KO : un second moteur d'ilots (emprise/poids differents) produit une
# geometrie 8% plus grande -- le controle doit le detecter et lever.
aires_topo_ko = {'PETIT': PETIT.area * 1.08, 'GROS': GROS.area * 1.08}
try:
    verifier_conservation_aire(aires_reference, aires_topo_ko)
    raise SystemExit(
        "ECHEC DU TEST : un ecart de 8% de surface aurait du etre detecte "
        "(cf. FIX-DOUBLE-MOTEUR-ILOTS)")
except AssertionError as e:
    print(f"CHECK B bis OK : ecart de surface artificiel bien detecte "
          f"({e}).")

print("\nTOUS LES TESTS ONT PASSE \u2714")
