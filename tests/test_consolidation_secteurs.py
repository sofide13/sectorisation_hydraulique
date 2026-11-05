# -*- coding: utf-8 -*-
"""
Test de non-regression du correctif (FIX-COMPTE-SECTEURS)
=============================================================

Objet : prouver, hors QGIS, que le socle topologique produit EXACTEMENT
autant de secteurs que la couche de reference `secteurs_hydrauliques`,
alors que le comportement anterieur en produisait des dizaines a des centaines.

Le reseau synthetique reproduit exactement le mecanisme observe sur le reseau
reel de l'utilisateur : une conduite sur deux depasse SEUIL_DN_TRANSPORT_MM
(150 mm), donc le sous-graphe "distribution seule" se fragmente en une
multitude de composantes de 2 noeuds -- et chacune devenait un "secteur".

Aucune dependance a QGIS : core/graphe_topologique.py et la voie
"conduites de reference" de core/consolidation_secteurs.py sont du Python +
networkx pur. Les couches QGIS sont remplacees par des doublures minimales
(meme protocole d'appel : fields()/getFeatures()/feat[champ]/feat.id()).

Lancement :
    python tests/test_consolidation_secteurs.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import consolidation_secteurs as cons          # noqa: E402
from core import graphe_topologique as gt                 # noqa: E402


# ---------------------------------------------------------------------------
# Doublures de couches QGIS (protocole d'appel identique)
# ---------------------------------------------------------------------------

class FauxChamp:
    def __init__(self, nom):
        self._nom = nom

    def name(self):
        return self._nom


class FauxPoint:
    def __init__(self, x, y):
        self._x, self._y = float(x), float(y)

    def x(self):
        return self._x

    def y(self):
        return self._y


class FausseGeomLigne:
    def __init__(self, p1, p2):
        self.p1, self.p2 = p1, p2

    def isEmpty(self):
        return False

    def length(self):
        return ((self.p2.x() - self.p1.x()) ** 2
                + (self.p2.y() - self.p1.y()) ** 2) ** 0.5


class FausseGeomPoint:
    def __init__(self, pt):
        self.pt = pt

    def isEmpty(self):
        return False

    def asPoint(self):
        return self.pt


class FausseEntite:
    def __init__(self, fid, attributs, geom=None):
        self._fid = fid
        self._attrs = attributs
        self._geom = geom

    def id(self):
        return self._fid

    def geometry(self):
        return self._geom

    def __getitem__(self, cle):
        return self._attrs[cle]


class FausseCouche:
    def __init__(self, nom, champs, entites):
        self._nom = nom
        self._champs = [FauxChamp(c) for c in champs]
        self._entites = entites

    def name(self):
        return self._nom

    def fields(self):
        return self._champs

    def getFeatures(self, *_args, **_kwargs):
        return list(self._entites)


def snap_key(x, y, tolerance=0.1):
    return (round(x / tolerance) * tolerance, round(y / tolerance) * tolerance)


def extremites_ligne(geom):
    return geom.p1, geom.p2


# ---------------------------------------------------------------------------
# Reseau synthetique : chaine de 40 noeuds, 2 reservoirs, 1 conduite sur 2
# en DN 200 (transport) -> distribution fragmentee en composantes de 2 noeuds
# ---------------------------------------------------------------------------

NB_NOEUDS = 40
PAS_M = 100.0


def construire_couches():
    pts = [FauxPoint(i * PAS_M, 0.0) for i in range(NB_NOEUDS)]
    conduites = []
    for i in range(NB_NOEUDS - 1):
        geom = FausseGeomLigne(pts[i], pts[i + 1])
        diam = 200.0 if i % 2 == 0 else 100.0
        conduites.append(FausseEntite(
            i, {'DIAMETRE': diam, 'LENGTH': geom.length()}, geom))
    aep = FausseCouche("AEP", ['DIAMETRE', 'LENGTH'], conduites)

    tanks = [
        FausseEntite(1, {}, FausseGeomPoint(FauxPoint(0.0, 0.0))),
        FausseEntite(2, {}, FausseGeomPoint(FauxPoint((NB_NOEUDS - 1) * PAS_M, 0.0))),
    ]
    tank = FausseCouche("tank", [], tanks)
    return aep, tank


def construire_reference():
    """Partition de record : 2 secteurs, coupure au milieu de la chaine."""
    milieu = (NB_NOEUDS - 1) // 2
    conduites_ref = []
    for i in range(NB_NOEUDS - 1):
        s = 'TANK_1' if i < milieu else 'TANK_2'
        conduites_ref.append(FausseEntite(
            i, {'pipe_id': i, 'secteur_id': s}))
    couche_conduites = FausseCouche(
        "conduites_sectorisees", ['pipe_id', 'secteur_id'], conduites_ref)

    polys = [
        FausseEntite(0, {'secteur_id': 'TANK_1', 'nb_abonnes': 800}),
        FausseEntite(1, {'secteur_id': 'TANK_2', 'nb_abonnes': 900}),
    ]
    couche_polys = FausseCouche(
        "secteurs_hydrauliques", ['secteur_id', 'nb_abonnes'], polys)
    return couche_conduites, couche_polys


CONFIG = {
    'AEP_LAYER': "AEP",
    'TANK_LAYER': "tank",
    'PIPE_DIAMETER_FIELD': "DIAMETRE",
    'PIPE_LENGTH_FIELD': "LENGTH",
    'SNAP_TOLERANCE': 0.1,
    'TANK_RESEARCH_M': 2000.0,
    'ABONNES_MAX_PAR_SECTEUR': 3000,
    'SEUIL_DN_TRANSPORT_MM': 150.0,
    'SCISSION_PAR_ANTENNE_TRANSPORT': True,
    'ZONE_MIN_NOEUDS': 2,
    'CONSOLIDER_ZONES_EN_SECTEURS': True,
    'ALIGNER_SUR_SECTEURS_REFERENCES': True,
    'COUCHE_REFERENCE_SECTEURS': "secteurs_hydrauliques",
    'COUCHE_REFERENCE_CONDUITES': "conduites_sectorisees",
}

_JOURNAL = []


def _log(msg):
    _JOURNAL.append(str(msg))


def _executer_socle(config):
    aep, tank = construire_couches()
    couches = {"AEP": aep, "tank": tank}

    def get_layer(nom):
        return couches[nom]

    def get_layer_optional(nom):
        return couches.get(nom)

    return gt.executer_socle_topologique(
        config, get_layer, get_layer_optional, snap_key, extremites_ligne, _log)


def main():
    ref_conduites, ref_polys = construire_reference()
    couches_ref = {"conduites_sectorisees": ref_conduites,
                   "secteurs_hydrauliques": ref_polys}

    def get_layer_optional_ref(nom):
        return couches_ref.get(nom)

    # ---- 1) comportement anterieur : chaque bloc de distribution = 1 secteur
    cfg_ancien = dict(CONFIG)
    cfg_ancien['CONSOLIDER_ZONES_EN_SECTEURS'] = False
    socle_ancien = _executer_socle(cfg_ancien)
    zones_anciennes = set(socle_ancien['scission']['secteur_final_de_noeud'].values())
    print(f"avant consolidation (CONSOLIDER_ZONES_EN_SECTEURS=false) : "
          f"{len(zones_anciennes)} \"secteurs\" pour 2 reservoirs")
    assert len(zones_anciennes) >= 10, (
        "le cas de fragmentation n'est pas reproduit par ce reseau de test")

    # ---- 2) les sous-zones restent des attributs
    socle = _executer_socle(CONFIG)
    secteurs_socle = set(socle['scission']['secteur_final_de_noeud'].values())
    print(f"consolidation sans alignement : {len(secteurs_socle)} secteur(s) "
          f"({sorted(secteurs_socle)})")
    assert len(secteurs_socle) == 2, secteurs_socle
    sous_zones = set(socle['scission']['sous_zone_de_noeud'].values())
    assert len(sous_zones) >= 10, "l'analyse 'qui alimente qui' doit rester calculee"
    print(f"          sous-zones conservees comme attribut : {len(sous_zones)}")

    # ---- 3) alignement sur la partition de reference
    controle = cons.consolider_sur_reference(
        socle, CONFIG, get_layer_optional_ref, _log)
    assert controle['applique'], controle
    assert controle['source'] == 'conduites', controle['source']
    secteurs_finaux = set(socle['scission']['secteur_final_de_noeud'].values())
    print(f"consolidation avec alignement : {len(secteurs_finaux)} secteur(s) "
          f"{sorted(secteurs_finaux)} pour {controle['nb_reference']} "
          f"secteur(s) de reference")
    assert secteurs_finaux == {'TANK_1', 'TANK_2'}, secteurs_finaux
    assert controle['nb_apres'] == controle['nb_reference'] == 2, controle
    assert controle['nb_non_rattaches'] == 0, controle
    assert cons.verifier_compte(controle, _log) is True
    assert controle['abonnes'] == {'TANK_1': 800, 'TANK_2': 900}
    print("CHECK 1 OK : nombre de secteurs du socle = nombre de secteurs de "
          "secteurs_hydrauliques.")

    # ---- 4) chaque noeud est affecte, et l'affectation suit la reference
    secteur_de_noeud = socle['scission']['secteur_final_de_noeud']
    assert len(secteur_de_noeud) == NB_NOEUDS, len(secteur_de_noeud)
    xs_par_secteur = {}
    for noeud, s in secteur_de_noeud.items():
        xs_par_secteur.setdefault(s, []).append(noeud[0])
    # la coupure de reference est au milieu : TANK_1 a gauche, TANK_2 a droite
    assert max(xs_par_secteur['TANK_1']) < min(xs_par_secteur['TANK_2']), \
        xs_par_secteur
    print("CHECK 2 OK : partition conforme a la reference (aucun noeud "
          "orphelin, aucune zone qui traverse la frontiere de reference).")

    # ---- 5) sous-zones regroupees par secteur + antennes recalculees
    sz = cons.regrouper_sous_zones(
        socle['scission']['sous_zone_de_noeud'], secteur_de_noeud)
    assert set(sz.keys()) == {'TANK_1', 'TANK_2'}, sz
    assert all(len(v) >= 1 for v in sz.values()), sz
    antennes = gt.calculer_antennes_transport(
        socle['graphe_data'], secteur_de_noeud, CONFIG)
    assert set(antennes.keys()) <= {'TANK_1', 'TANK_2'}, antennes
    print(f"CHECK 3 OK : sous-zones rattachees a leur secteur "
          f"({ {k: len(v) for k, v in sz.items()} }), antennes de transport "
          f"recalculees sur la partition retenue.")

    # ---- 6) repli documente : sans couche de reference, rien n'est invente
    socle2 = _executer_socle(CONFIG)
    controle2 = cons.consolider_sur_reference(
        socle2, CONFIG, lambda nom: None, _log)
    assert controle2['applique'] is False
    assert controle2['nb_reference'] is None
    assert set(socle2['scission']['secteur_final_de_noeud'].values()) \
        == secteurs_socle
    print("CHECK 4 OK : sans couche de reference, la partition du socle est "
          "conservee telle quelle et le journal l'explique.")

    print("\nTOUS LES TESTS ONT PASSE \u2714")


if __name__ == "__main__":
    main()
