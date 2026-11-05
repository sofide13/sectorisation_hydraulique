# -*- coding: utf-8 -*-
"""
Test synthétique -- FIX-ILOTS-INTERIEURS-SANS-NOEUD (complement au build actuel)
==============================================================

Constat terrain (captures QGIS fournies par l'utilisateur) : des îlots avec
de vraies habitations, conduites ou routes (campus, stade, grand ensemble)
restaient BLANCS -- hors de tout secteur -- alors qu'ils sont entièrement
entourés par le territoire déjà tranché d'un seul secteur. Cause : ces îlots
n'ont aucune jonction AEP modélisée en leur sein (antenne périmétrale non
représentée comme nœud) et étaient donc jugés "hors tissu desservi" par la
seule règle de distance (DIST_MAX_ILOT_SANS_NOEUD_M), qui ne regardait
jamais si l'îlot touchait vraiment un secteur voisin.

Ce test vérifie les deux moitiés du correctif SANS RÉGRESSION :
  A. un îlot INTÉRIEUR sans nœud, entouré par un seul secteur, lui est
     rattaché par ADJACENCE, quelle que soit la distance à un nœud ;
  B. un îlot qui touche la frange EXTÉRIEURE de l'emprise reste soumis à la
     règle de distance (DIST_MAX_ILOT_SANS_NOEUD_M) même s'il est adjacent à
     un secteur déjà affecté -- l'exclusion des terrains non desservis en
     bordure de ville (désert, agricole) n'est pas réintroduite.

Lancement :
    python3 tests/test_ilots_interieurs_sans_noeud.py
"""

import os
import sys

ICI = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(ICI)
sys.path.insert(0, PLUGIN)

from core import geom_backend as gb          # noqa: E402
from core import partition_secteurs as ps     # noqa: E402

assert gb.BACKEND in ("shapely", "qgis")

PAS = 300.0  # cote d'un ilot (grille 3x3)

# ---------------------------------------------------------------------------
# A. Grille 3x3 : l'ilot central (1,1) n'a AUCUN nœud -- ex. un stade/campus
#    alimente par son perimetre seulement. Tous les autres ilots (perimetre,
#    donc tous en contact avec le bord de l'emprise) portent un nœud du
#    secteur unique 'S'.
# ---------------------------------------------------------------------------
NX = NY = 3

# Construction des routes (grille) via segments explicites -- on passe par le
# backend directement pour rester agnostique QGIS/shapely.
def _ligne(p1, p2):
    if gb.BACKEND == "shapely":
        from shapely.geometry import LineString
        return gb.Geom(LineString([p1, p2]))
    from qgis.core import QgsGeometry, QgsPointXY
    return gb.Geom(QgsGeometry.fromPolylineXY(
        [QgsPointXY(*p1), QgsPointXY(*p2)]))


routes = []
for i in range(NX + 1):
    x = i * PAS
    routes.append(_ligne((x, 0), (x, NY * PAS)))
for j in range(NY + 1):
    y = j * PAS
    routes.append(_ligne((0, y), (NX * PAS, y)))

emprise = gb.box(0, 0, NX * PAS, NY * PAS)

noeud_points, secteur_de_noeud, poids_noeud = {}, {}, {}
nid = 0
for i in range(NX):
    for j in range(NY):
        if (i, j) == (1, 1):
            continue  # l'ilot central n'a pas de nœud (cas "campus/stade")
        nid += 1
        cx, cy = (i + 0.5) * PAS, (j + 0.5) * PAS
        noeud_points[nid] = (cx, cy)
        secteur_de_noeud[nid] = "S"
        poids_noeud[nid] = 10

CFG = {
    'ACTIVER_PARTITION_ILOTS': True,
    'SEUIL_AIRE_ILOT_M2': 1000.0,
    'LARGEUR_MAX_VOIE_M': 10.0,
    'AIRE_MAX_RATTACHEMENT_M2': 300000.0,
    'MAX_PASSES_NETTOYAGE': 4,
    'SNAP_LIMITE_M': 0.5,
    'DIST_MAX_ILOT_SANS_NOEUD_M': 300.0,
    'ASSURER_CONTIGUITE_TERRITOIRES': False,
}

logs = []
territoires, stats = ps.construire_territoires(
    routes, emprise, noeud_points, secteur_de_noeud, poids_noeud, CFG,
    log=logs.append)
print("\n".join(logs))

assert territoires and "S" in territoires, "le secteur S a disparu"
assert stats['ilots_par_voisinage'] >= 1, (
    f"l'ilot central sans nœud aurait dû être rattaché par adjacence "
    f"({stats})")
assert stats['ilots_hors_tissu'] == 0, (
    f"l'ilot central (interieur, entoure par S) n'aurait jamais dû être "
    f"exclu comme 'hors tissu' ({stats})")
centre = (1.5 * PAS, 1.5 * PAS)
assert territoires["S"].contains_xy(*centre), (
    "l'ilot central (campus/stade) n'est pas rattache a S alors qu'il en "
    "est entierement entoure")
print(f"CHECK A OK : ilot interieur sans nœud rattache par adjacence a son "
      f"seul secteur voisin ({stats['ilots_par_voisinage']} ilot(s) "
      f"concerne(s), 0 exclu a tort).")

# ---------------------------------------------------------------------------
# B. Meme grille, mais DIST_MAX_ILOT_SANS_NOEUD_M tres petit (10 m) : l'ilot
#    central reste rattache par ADJACENCE (il ne touche pas le bord de
#    l'emprise -- comportement inchange, la borne de distance ne s'applique
#    qu'en frange exterieure). En revanche un ilot de PERIMETRE (qui, lui,
#    touche le bord de l'emprise) sans nœud ne doit PAS être rattache par la
#    seule adjacence : avec une borne de distance minuscule, il doit être
#    exclu comme les vrais terrains non desservis en bordure de ville.
# ---------------------------------------------------------------------------
secteur_de_noeud_b = dict(secteur_de_noeud)
poids_noeud_b = dict(poids_noeud)
# on retire aussi le nœud du coin (0,0) : cet ilot de PERIMETRE touche donc
# le bord de l'emprise ET n'a plus de nœud propre.
for nid_, (x, y) in list(noeud_points.items()):
    if (x, y) == (0.5 * PAS, 0.5 * PAS):
        del secteur_de_noeud_b[nid_]
        del poids_noeud_b[nid_]

CFG_B = dict(CFG)
CFG_B['DIST_MAX_ILOT_SANS_NOEUD_M'] = 10.0  # bien plus petit que la grille

logs_b = []
territoires_b, stats_b = ps.construire_territoires(
    routes, emprise, noeud_points, secteur_de_noeud_b, poids_noeud_b, CFG_B,
    log=logs_b.append)
print("\n".join(logs_b))

coin = (0.5 * PAS, 0.5 * PAS)
dans_S = "S" in territoires_b and territoires_b["S"].contains_xy(*coin)
assert not dans_S, (
    "REGRESSION : l'ilot de PERIMETRE sans nœud a ete rattache par la seule "
    "adjacence alors qu'il touche le bord de l'emprise -- la regle de "
    "distance (frange exterieure, cf. DIST_MAX_ILOT_SANS_NOEUD_M) doit "
    "rester prioritaire pour ecarter les terrains reellement non desservis "
    "en bordure de ville (desert, agricole)."
)
assert stats_b['ilots_hors_tissu'] >= 1, (
    f"l'ilot de perimetre aurait dû être exclu comme hors tissu, borne "
    f"de distance trop petite pour lui ({stats_b})")
print("CHECK B OK : un ilot qui touche la frange exterieure de l'emprise "
      "reste soumis a la regle de distance, meme adjacent a un secteur "
      "deja affecte -- l'exclusion des terrains non desservis en bordure "
      "de ville n'est pas reintroduite.")

print("\nTOUS LES TESTS ONT PASSE \u2714")
