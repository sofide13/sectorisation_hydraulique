# -*- coding: utf-8 -*-
"""
Test synthétique bout-en-bout du module core/partition_secteurs.py (build actuel).

S'exécute HORS QGIS (backend shapely) :

    python3 tests/test_partition_synthetic.py

Scénario : petite ville synthétique (grille viaire 200 m, une avenue diagonale,
une poche non fermée par le réseau routier, une route pendulaire dans le
« désert »), 3 secteurs hydrauliques décidés par un « moteur » simulé
(secteur_de_noeud). Vérifications :

  1. AUCUN chevauchement entre territoires (partition stricte) ;
  2. AUCUN îlot < SEUIL_AIRE_ILOT_M2 ni partie sans nœud du secteur ;
  3. couverture complète des îlots urbains (pas de trou dans le tissu urbain) ;
  4. les limites suivent les axes routiers (ligne centrale) ;
  5. nettoyer_polygones() résout un cas sale délibéré (chevauchement + îlot) ;
  6. un patchwork délibéré (secteur en morceaux entremêlés avec son
     voisin + exclave lointaine) redevient 1 territoire d'un seul tenant, sans
     perte ni création de surface, sans secteur perdu ;
  7. un îlot sans aucun nœud de réseau et loin du réseau (désert)
     n'entre dans aucun secteur ; les deux comportements sont désactivables ;
  8. autant de polygones que de secteurs, tous d'un seul tenant ;
  9. rendu PNG comparatif AVANT (buffer + convex hull, logique des builds
     précédents) / APRÈS (partition build actuel).
"""

import os
import random
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from shapely.geometry import LineString, Point
from shapely.ops import unary_union

ICI = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(ICI)
sys.path.insert(0, PLUGIN)

from core import geom_backend as gb          # noqa: E402
from core import partition_secteurs as ps    # noqa: E402

assert gb.BACKEND == "shapely", f"backend attendu shapely, obtenu {gb.BACKEND}"

random.seed(42)

# ---------------------------------------------------------------------------
# 1. Ville synthétique
# ---------------------------------------------------------------------------
PAS = 200.0
NX, NY = 12, 8
routes = []
for i in range(NX + 1):                      # rues verticales
    x = i * PAS
    if i == 5:                               # trou volontaire dans la trame
        routes.append(LineString([(x, 0), (x, 3 * PAS)]))
        routes.append(LineString([(x, 4 * PAS + 60), (x, NY * PAS)]))
    else:
        routes.append(LineString([(x, 0), (x, NY * PAS)]))
for j in range(NY + 1):                      # rues horizontales
    routes.append(LineString([(0, j * PAS), (NX * PAS, j * PAS)]))
routes.append(LineString([(0, 0), (NX * PAS, NY * PAS)]))          # diagonale
routes.append(LineString([(NX * PAS, 4 * PAS),                     # pendulaire
                          (NX * PAS + 900, 4 * PAS + 250)]))       # « désert »
# « place » interne dont l'anneau n'est pas fermé (trou de 50 m dans la
# voirie) : doit être rattachée au secteur voisin, pas devenir un îlot perdu
routes += [
    LineString([(650, 1050), (650, 1250)]),
    LineString([(650, 1250), (850, 1250)]),
    LineString([(650, 1050), (850, 1050)]),
    LineString([(850, 1050), (850, 1150)]),
]

emprise = gb.box(-500, -500, NX * PAS + 1100, NY * PAS + 700)

# nœuds du réseau aux intersections + abonnés simulés
noeud_points, secteur_de_noeud, poids_noeud = {}, {}, {}
nid = 0
for i in range(NX + 1):
    for j in range(NY + 1):
        x, y = i * PAS, j * PAS
        nid += 1
        noeud_points[nid] = (x, y)
        secteur_de_noeud[nid] = "S1" if x < 2.5 * PAS else (
            "S2" if x < 7.5 * PAS else "S3")
        poids_noeud[nid] = random.randint(5, 40)

# cas réaliste : une antenne de S3 pénétrant dans la zone S2 (nœuds isolés)
# -> c'est ce cas qui faisait « exploser » l'enveloppe convexe des anciens builds
for n, (x, y) in list(noeud_points.items()):
    if (x, y) in ((1000.0, 1400.0), (1200.0, 1400.0)):
        secteur_de_noeud[n] = "S3"

CFG = {
    'ACTIVER_PARTITION_ILOTS': True,
    'SEUIL_AIRE_ILOT_M2': 2000.0,
    'LARGEUR_MAX_VOIE_M': 30.0,
    'AIRE_MAX_RATTACHEMENT_M2': 300000.0,
    'MAX_PASSES_NETTOYAGE': 4,
    'SNAP_LIMITE_M': 0.5,
}

LOGS = []
territoires, stats = ps.construire_territoires(
    routes, emprise, noeud_points, secteur_de_noeud, poids_noeud, CFG,
    log=LOGS.append)
print("\n".join(LOGS))
assert territoires, "aucun territoire produit"

# ---------------------------------------------------------------------------
# 2. Vérifications
# ---------------------------------------------------------------------------
qual = ps.statistiques_partition(territoires)
print("QUALITE :", qual)
assert qual['nb_chevauchements'] == 0, "CHEVAUCHEMENT RESIDUEL !"
assert qual['aire_chevauchements_m2'] == 0.0

seuil = CFG['SEUIL_AIRE_ILOT_M2']
for s, g in territoires.items():
    for p in g.parts():
        assert p.area >= seuil, f"îlot parasite {p.area:.0f} m2 dans {s}"
        nids = [n for n, (x, y) in noeud_points.items()
                if secteur_de_noeud.get(n) == s
                and (p.contains_xy(x, y) or p.distance_xy(x, y) <= 0.5)]
        assert nids, f"partie sans nœud du secteur dans {s}"
print("CHECK 1-2 OK : aucun chevauchement, aucun îlot parasite, "
      "toute partie porte du réseau.")

union_terr = unary_union([g.impl for g in territoires.values()])
ilots = [b for b in gb.polygonize(routes)
         if b.area > 1.0 and emprise.impl.contains(Point(b.centroid_xy()))]
union_ilots = unary_union([b.impl for b in ilots])
manque = union_ilots.difference(union_terr).area
print(f"couverture du tissu urbain : manquants = {manque:.2f} m2")
assert manque < 1.0, "trou dans le tissu urbanisé"
print("CHECK 3 OK : couverture complète des îlots urbains.")

s1, s2 = gb.Geom.wrap(territoires['S1']), gb.Geom.wrap(territoires['S2'])
partage = s1.shared_length(s2)
print(f"frontière S1/S2 : {partage:.0f} m partagés")
assert partage > 3 * PAS, "limite inter-secteur trop courte"
# la frontière inter-secteurs doit suivre les axes routiers de la grille :
# tous ses sommets ont un x multiple du pas de la grille (lignes centrales).
front = s1.boundary.intersection(s2.boundary).impl
coords = []
if front.geom_type == "LineString":
    coords = [list(front.coords)]
else:
    coords = [list(g.coords) for g in front.geoms]
xs = [x for ligne in coords for x, _y in ligne]
assert xs, "frontière S1/S2 vide"
assert all(abs(x - round(x / PAS) * PAS) < 1e-6 for x in xs), \
    f"la frontière ne suit pas les axes routiers : {sorted(set(xs))}"
print("CHECK 4 OK : limites calées sur les lignes centrales de rues.")

# ---------------------------------------------------------------------------
# 3. Cas sale délibéré pour nettoyer_polygones
# ---------------------------------------------------------------------------
carre_a = gb.box(0, 0, 400, 400)
carre_b = gb.box(300, 0, 700, 400)              # chevauche A de 100 m
ilot_loin = gb.box(900, 900, 930, 930)          # 900 m2 isolé de B
sales = {'A': carre_a, 'B': gb.unary_union([carre_b, ilot_loin])}
noeuds2 = {'A': [(50, 50)], 'B': [(500, 50)]}
np2, sn2, pw2 = {}, {}, {}
for k, i in enumerate(noeuds2['A']):
    np2[f"a{k}"] = i
    sn2[f"a{k}"] = 'A'
    pw2[f"a{k}"] = 10
for k, i in enumerate(noeuds2['B']):
    np2[f"b{k}"] = i
    sn2[f"b{k}"] = 'B'
    pw2[f"b{k}"] = 10
logs2 = []
propres, stats2 = ps.nettoyer_polygones(sales, np2, sn2, pw2, CFG,
                                        log=logs2.append)
print("\n".join(logs2))
q2 = ps.statistiques_partition(propres)
assert q2['nb_chevauchements'] == 0, "chevauchement non résolu"
for s, g in propres.items():
    for p in g.parts():
        assert p.area >= CFG['SEUIL_AIRE_ILOT_M2'], "îlot non réaffecté"
assert set(propres) == {'A', 'B'}
print("CHECK 5 OK : chevauchement résolu + îlot réaffecté au voisin.", stats2)

# ---------------------------------------------------------------------------
# 3bis. CONTIGUITÉ : un secteur = un territoire d'un seul tenant
#       (reproduction du défaut constaté dans QGIS : un secteur identifié
#        s'allumait en une dizaine de morceaux dispersés dans toute la ville,
#        entremêlés avec ses voisins, plus un morceau en plein désert)
# ---------------------------------------------------------------------------
CEL = 250.0
cells = {}
for i in range(6):
    for j in range(4):
        cells[(i, j)] = gb.box(i * CEL, j * CEL, (i + 1) * CEL, (j + 1) * CEL)

# B = bloc de droite (son noyau) + 2 morceaux éparpillés au milieu de A
morceaux_b = [(4, j) for j in range(4)] + [(5, j) for j in range(4)]
eparpilles = [(1, 1), (1, 3)]
morceaux_a = [k for k in cells if k not in morceaux_b and k not in eparpilles]
ile_lointaine = gb.box(9000, 9000, 9300, 9300)       # exclave isolée de A

patch = {
    'A': gb.unary_union([cells[k] for k in morceaux_a] + [ile_lointaine]),
    'B': gb.unary_union([cells[k] for k in morceaux_b]
                        + [cells[k] for k in eparpilles]),
}
np3, sn3, pw3 = {}, {}, {}
for k, (i, j) in enumerate(sorted(cells)):
    x, y = (i + 0.5) * CEL, (j + 0.5) * CEL
    s = 'B' if ((i, j) in morceaux_b or (i, j) in eparpilles) else 'A'
    np3[f"n{k}"] = (x, y)
    sn3[f"n{k}"] = s
    pw3[f"n{k}"] = 20
np3["nile"] = (9150, 9150)          # l'exclave lointaine porte du réseau réel
sn3["nile"] = 'A'
pw3["nile"] = 12

logs3 = []
propres3, stats3 = ps.nettoyer_polygones(patch, np3, sn3, pw3, CFG,
                                         log=logs3.append)
print("\n".join(logs3))
q3 = ps.statistiques_partition(propres3)
print("QUALITE contiguïté :", q3, stats3)

assert set(propres3) == {'A', 'B'}, "un secteur a disparu de la partition"
assert q3['nb_chevauchements'] == 0, "chevauchement introduit par la contiguïté"
# les 2 morceaux entremêlés repartent chez le voisin, que ce soit par l'étape 1
# (réaffectation d'exclave) ou par le filet de sécurité de l'étape 2bis
absorbes = stats3['ilots_reaffectes'] + stats3['fragments_absorbes']
assert absorbes == 2, \
    f"les 2 morceaux éparpillés devaient être absorbés ({stats3})"
assert len(propres3['B'].parts()) == 1, "B n'est pas d'un seul tenant"
# A garde uniquement son exclave PHYSIQUEMENT isolée (aucun voisin en contact),
# jamais un morceau entremêlé avec un voisin.
assert stats3['exclaves_isolees'] == 1, f"exclave isolée non signalée ({stats3})"
parties_a = propres3['A'].parts()
assert len(parties_a) == 2, "A devrait garder son noyau + son exclave isolée"
# aucune surface perdue ni créée
aire_avant = patch['A'].area + patch['B'].area
aire_apres = propres3['A'].area + propres3['B'].area
assert abs(aire_avant - aire_apres) < 1.0, \
    f"surface non conservée : {aire_avant:.0f} -> {aire_apres:.0f}"
# les morceaux éparpillés sont bien passés à A (le voisin le plus frontalier)
for (i, j) in eparpilles:
    c_centre = ((i + 0.5) * CEL, (j + 0.5) * CEL)
    assert propres3['A'].contains_xy(*c_centre), \
        f"le morceau {i},{j} n'a pas été absorbé par A"
print("CHECK 6 OK : 1 secteur = 1 territoire d'un seul tenant "
      "(morceaux éparpillés absorbés, exclave isolée signalée, surface "
      "conservée, aucun secteur perdu).")

# option de tolérance : une grande annexe peut être conservée si l'exploitant
# le demande explicitement (AIRE_EXCLAVE_TOLEREE_M2)
CFG_TOL = dict(CFG)
CFG_TOL['AIRE_EXCLAVE_TOLEREE_M2'] = 10000.0     # une cellule = 62 500 m2
propres3b, stats3b = ps.nettoyer_polygones(patch, np3, sn3, pw3, CFG_TOL,
                                           log=lambda *_a: None)
assert stats3b['fragments_absorbes'] == 0 \
    and len(propres3b['B'].parts()) == 3, \
    f"AIRE_EXCLAVE_TOLEREE_M2 doit préserver les grandes annexes ({stats3b})"
CFG_OFF = dict(CFG)
CFG_OFF['ASSURER_CONTIGUITE_TERRITOIRES'] = False
propres3c, stats3c = ps.nettoyer_polygones(patch, np3, sn3, pw3, CFG_OFF,
                                           log=lambda *_a: None)
assert stats3c['fragments_absorbes'] == 0 \
    and len(propres3c['B'].parts()) == 3, \
    f"ASSURER_CONTIGUITE_TERRITOIRES=false doit rendre le comportement precedent ({stats3c})"
print("CHECK 6bis OK : contiguïté désactivable et tolérance d'annexe "
      "opérationnelles (retour au comportement precedent).")

# ---------------------------------------------------------------------------
# 3ter. un îlot routier SANS nœud et LOIN du réseau (désert, terres
#       agricoles enclavées entre deux routes) n'entre dans aucun secteur
# ---------------------------------------------------------------------------
routes_desert = list(routes) + [
    LineString([(6000, 6000), (7000, 6000)]),
    LineString([(7000, 6000), (7000, 7000)]),
    LineString([(7000, 7000), (6000, 7000)]),
    LineString([(6000, 7000), (6000, 6000)]),
]
emprise_desert = gb.box(-500, -500, 8000, 8000)
terr_d, stats_d = ps.construire_territoires(
    routes_desert, emprise_desert, noeud_points, secteur_de_noeud, poids_noeud,
    CFG, log=lambda *_a: None)
assert stats_d['ilots_hors_tissu'] >= 1, \
    f"le bloc de désert devait être exclu ({stats_d})"
bloc_desert = gb.box(6000, 6000, 7000, 7000)
for s, g in terr_d.items():
    inter = g.intersection(bloc_desert)
    assert inter is None or inter.area < 1.0, \
        f"le secteur {s} couvre du terrain non desservi (désert)"
print(f"CHECK 7 OK : {stats_d['ilots_hors_tissu']} îlot(s) hors tissu "
      f"desservi exclu(s) ({stats_d['aire_hors_tissu_m2']:,.0f} m2) — plus de "
      f"secteur qui déborde en plein désert.")

# et avec la borne désactivée (0 = comportement precedent), le désert est ré-avalé
CFG_SANS_BORNE = dict(CFG)
CFG_SANS_BORNE['DIST_MAX_ILOT_SANS_NOEUD_M'] = 0.0
terr_sans_borne, stats_sans_borne = ps.construire_territoires(
    routes_desert, emprise_desert, noeud_points, secteur_de_noeud, poids_noeud,
    CFG_SANS_BORNE, log=lambda *_a: None)
assert stats_sans_borne['ilots_hors_tissu'] == 0
assert any((g.intersection(bloc_desert) or gb.box(0, 0, 0, 0)).area > 1000.0
           for g in terr_sans_borne.values()), \
    "DIST_MAX_ILOT_SANS_NOEUD_M=0 doit restaurer le comportement precedent"
print("CHECK 7bis OK : borne désactivable (DIST_MAX_ILOT_SANS_NOEUD_M=0).")

# ---------------------------------------------------------------------------
# 3quater. aucun secteur ne disparaît de la couche de territoires
# ---------------------------------------------------------------------------
sn4 = dict(secteur_de_noeud)
pw4 = dict(poids_noeud)
# petit secteur "S4" : un seul nœud, minoritaire dans SON îlot (S2 y pèse plus)
cible = None
for n, (x, y) in noeud_points.items():
    if (x, y) == (600.0, 600.0):
        cible = n
if cible is not None:
    sn4[cible] = 'S4'
    pw4[cible] = 1
terr4, stats4 = ps.construire_territoires(
    routes, emprise, noeud_points, sn4, pw4, CFG, log=lambda *_a: None)
assert 'S4' in terr4, ("un secteur hydraulique a disparu de la couche de "
                       "territoires (vote majoritaire)")
assert len(terr4['S4'].parts()) == 1
assert set(terr4) == {'S1', 'S2', 'S3', 'S4'}
q4 = ps.statistiques_partition(terr4)
assert q4['nb_chevauchements'] == 0
assert q4['nb_parties'] == q4['nb_secteurs'], \
    f"territoires encore fragmentés : {q4}"
print("CHECK 8 OK : autant de polygones que de secteurs, tous d'un seul "
      f"tenant ({q4}).")

# ---------------------------------------------------------------------------
# 4. Rendu comparatif AVANT / APRÈS
# ---------------------------------------------------------------------------
fig, axes = plt.subplots(1, 2, figsize=(17.5, 7.0))
for r in routes:
    xs, ys = r.xy
    for ax in axes:
        ax.plot(xs, ys, color="#888888", lw=0.7, zorder=1)

COUL = {'S1': "#d62728", 'S2': "#1f77b4", 'S3': "#2ca02c"}

# --- AVANT : logique des builds précédents (buffer nœuds + convex hull) ---
ax = axes[0]
for s in ('S1', 'S2', 'S3'):
    bufs = [Point(noeud_points[n]).buffer(85, quad_segs=8)
            for n in secteur_de_noeud if secteur_de_noeud[n] == s]
    u = unary_union(bufs)
    parts = [u] if u.geom_type == 'Polygon' else list(u.geoms)
    grandes = [p for p in parts if p.area >= 0.005 * u.area]
    geom = unary_union(grandes).convex_hull if len(grandes) > 1 else (
        grandes[0] if grandes else u)          # <- l'enveloppe convexe fautive
    x, y = geom.exterior.xy
    ax.fill(x, y, color=COUL[s], alpha=0.45, zorder=2)
    ax.plot(x, y, color=COUL[s], lw=1.0, zorder=3)
ax.set_title("AVANT (builds précédents) : buffer + enveloppe convexe\n"
             "-> chevauchements massifs, limites hors voirie", fontsize=11)

# --- APRÈS : partition contiguë ---
ax = axes[1]
for s, g in territoires.items():
    for p in g.parts():
        x, y = p.impl.exterior.xy
        ax.fill(x, y, color=COUL[s], alpha=0.45, zorder=2)
        ax.plot(x, y, color=COUL[s], lw=1.2, zorder=3)
for n, (x, y) in noeud_points.items():
    ax.plot(x, y, '.', color='black', ms=2, zorder=4)
ax.set_title("APRÈS  : partition par îlots urbains\n"
             "-> 0 chevauchement, 0 îlot parasite, limites sur les rues",
             fontsize=11)

for ax in axes:
    ax.set_aspect("equal")
    ax.axis("off")
fig.suptitle("Sectorisation hydraulique — validation synthétique du module "
             "core/partition_secteurs.py", fontsize=13)
fig.tight_layout(rect=(0, 0, 1, 0.95))
out_png = os.path.join(ICI, "resultat_partition_synthetic.png")
fig.savefig(out_png, dpi=130)
print(f"\nRendu : {out_png}")
print("TOUS LES TESTS ONT PASSE ✔")
