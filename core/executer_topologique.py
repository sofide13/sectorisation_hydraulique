# -*- coding: utf-8 -*-
"""
Point d'entree de test bout-en-bout du NOUVEAU socle topologique
(graphe_topologique.py + polygones_topologiques.py), independant de
pipeline.py.

USAGE (console Python de QGIS, avec le projet AEP deja charge) :

    import sys
    sys.path.insert(0, r"<chemin vers ce plugin>")
    from core import executer_v68_topologique
    resultat = executer_v68_topologique.executer(config=None)

Ne fait PAS (volontairement, cf. cahier des charges) : pression, capacite,
altimetrie, PRV physiques, controle de vitesse -- ce sont les etapes
suivantes, a rebrancher en aval une fois le socle valide sur vos donnees
reelles (chevauchement = 0, secteurs coherents).
"""

import logging
import sys

from qgis.core import QgsProject

from . import consolidation_secteurs as cs
from . import graphe_topologique as gt
from . import polygones_topologiques as pt

_CONFIG_DEFAUT = {
    'AEP_LAYER': "AEP",
    'TANK_LAYER': "tank",
    'PIPE_DIAMETER_FIELD': "DIAMETRE",
    'PIPE_LENGTH_FIELD': "LENGTH",
    'SNAP_TOLERANCE': 0.1,
    'TANK_RESEARCH_M': 2000.0,
    'ABONNES_MAX_PAR_SECTEUR': 3000,
    'LARGEUR_TAMPON_CONDUITE_M': 30.0,
    'BUFFER_SEGMENTS': 8,
    'SOUDURE_MAX_M': 0.0,
    'SEUIL_DN_TRANSPORT_MM': 150.0,
    # Scission "qui alimente qui" (etape 3bis, cf. graphe_topologique.
    # scinder_par_continuite_distribution) : un bloc relie au reste de son
    # secteur UNIQUEMENT par une conduite de transport devient sa propre
    # zone (secteur_zone1, secteur_zone2, ...), avec sa propre antenne de
    # transport amont. Mettre a False pour retrouver le comportement anterieur
    # d'origine (secteurs potentiellement multi-parties, non scindes).
    'SCISSION_PAR_ANTENNE_TRANSPORT': True,
    # correctif FIX-COMPTE-SECTEURS (cf. core/consolidation_secteurs.py) : les
    # blocs de distribution disjoints restent des SOUS-ZONES de leur secteur
    # (attribut `sous_zones`) au lieu de devenir chacun un secteur -- sans
    # quoi le socle produit des centaines de "secteurs" non viables au sens
    # DMA (460 observes pour quelques dizaines de secteurs reels).
    'CONSOLIDER_ZONES_EN_SECTEURS': True,
    # Alignement final du socle sur la partition hydraulique de record
    # (couches produites par pipeline) : garantit que
    # secteurs_hydrauliques_topo compte EXACTEMENT autant de secteurs que
    # secteurs_hydrauliques.
    'ALIGNER_SUR_SECTEURS_REFERENCES': True,
    'COUCHE_REFERENCE_SECTEURS': "secteurs_hydrauliques",
    'COUCHE_REFERENCE_CONDUITES': "conduites_sectorisees",
    # FIX-DOUBLE-MOTEUR-ILOTS : quand ALIGNER_SUR_SECTEURS_REFERENCES est actif et
    # que la couche de reference couvre tous les secteurs, les polygones de
    # secteurs_hydrauliques_topo sont REPRIS DIRECTEMENT de
    # secteurs_hydrauliques au lieu de relancer partition_secteurs.
    # construire_territoires() une seconde fois avec sa propre emprise et ses
    # propres poids -- deux moteurs de partition independants sur la meme
    # appartenance produisaient des ilots differents pres des frontieres,
    # d'ou les secteurs emboites ("poupees russes") et l'ecart de surface
    # totale entre secteurs_hydrauliques et secteurs_hydrauliques_topo.
    # Mettre a False pour forcer un second calcul independant (mode test
    # autonome du socle, cf. docstring de ce module).
    'REUTILISER_GEOMETRIE_REFERENCE': True,
    # partition stricte des ilots urbains (remplace l'habillage
    # « buffer de conduites » fragmente des que la couche routes existe)
    'ROUTE_LAYER': "routes",
    'ACTIVER_PARTITION_ILOTS': True,
    'SEUIL_AIRE_ILOT_M2': 2000.0,
    'LARGEUR_MAX_VOIE_M': 30.0,
    'AIRE_MAX_RATTACHEMENT_M2': 300000.0,
    'MAX_PASSES_NETTOYAGE': 4,
    'SNAP_LIMITE_M': 0.5,
    # Taille minimale (en noeuds) pour qu'un bloc de distribution disjoint
    # soit promu zone independante. En-dessous, il est rattache a la zone
    # la plus proche du meme secteur (plus court chemin, transport inclus).
    'ZONE_MIN_NOEUDS': 2,
    # Controle de non-regression (cf. outils/verifier_non_regression_
    # geometrie.py) : Sigma(aire) identique a la reference + zero secteur
    # emboite ("poupee russe"), verifie AUTOMATIQUEMENT a la fin de ce run,
    # avant meme que l'utilisateur n'inspecte la couche dans QGIS.
    'VERIFIER_NON_REGRESSION_GEOMETRIE': True,
}


def executer(config=None, on_etape=None):
    CONFIG = dict(_CONFIG_DEFAUT)
    if config:
        CONFIG.update(config)

    def _log(msg):
        print(msg)
        if sys.stdout is not None:
            try:
                sys.stdout.flush()
            except Exception as exc:
                logging.getLogger(__name__).debug(
                    "Exception ignoree : %s", exc)

    def get_layer(name):
        layers = QgsProject.instance().mapLayersByName(name)
        if not layers:
            raise RuntimeError(f"Couche introuvable : '{name}'")
        return layers[0]

    def get_layer_optional(name):
        layers = QgsProject.instance().mapLayersByName(name)
        return layers[0] if layers else None

    def snap_key(x, y, tolerance=0.1):
        return (round(x / tolerance) * tolerance, round(y / tolerance) * tolerance)

    def extremites_ligne(geom):
        from qgis.core import QgsPointXY
        if geom is None or geom.isEmpty():
            return None, None
        try:
            if geom.isMultipart():
                parts = geom.asMultiPolyline()
                if parts and len(parts) > 0:
                    line = parts[0]
                    if line and len(line) >= 2:
                        return QgsPointXY(line[0]), QgsPointXY(line[-1])
            else:
                line = geom.asPolyline()
                if line and len(line) >= 2:
                    return QgsPointXY(line[0]), QgsPointXY(line[-1])
        except Exception as exc:
            logging.getLogger(__name__).debug(
                "Exception ignoree : %s", exc)
        return None, None

    # CoucheDifferee (cf. core/couche_differee.py) construites pendant le
    # run, pas encore de vrai QgsVectorLayer et pas encore inserees dans le
    # projet QGIS. FIX-THREAD-SAFETY : cette fonction est appelee
    # depuis TopoWorker (QThread, cf. ui/panneau_principal.py) et ne doit
    # donc jamais construire de QgsVectorLayer ni toucher QgsProject
    # (access violation observee sur les builds precedents). La
    # materialisation en vrais QgsVectorLayer et l'enregistrement dans le
    # projet sont faits par l'appelant, depuis le thread principal, une
    # fois executer() termine.
    _couches_en_attente = []

    def _ajouter_couche(couche):
        _couches_en_attente.append(couche)

    _log("=" * 70)
    _log("SECTORISATION HYDRAULIQUE -- SOCLE TOPOLOGIQUE (test bout-en-bout)")
    _log("Sans pression/capacite/altimetrie -- socle de connectivite pure.")
    _log("=" * 70)

    socle = gt.executer_socle_topologique(
        CONFIG, get_layer, get_layer_optional, snap_key, extremites_ligne,
        _log, _etape=on_etape)

    # --- agregation sous contrainte sur la partition de record -------
    _log("-" * 70)
    _log("CONSOLIDATION DES SECTEURS sur la partition de reference "
         "(%s)." % CONFIG.get('COUCHE_REFERENCE_SECTEURS'))
    controle = cs.consolider_sur_reference(
        socle, CONFIG, get_layer_optional, _log)
    socle['controle_consolidation'] = controle
    secteur_final = socle['scission']['secteur_final_de_noeud']
    if controle['applique']:
        socle['scission']['antennes_par_zone'] = gt.calculer_antennes_transport(
            socle['graphe_data'], secteur_final, CONFIG)
        socle['frontalieres'] = gt.detecter_conduites_frontalieres(
            socle['graphe_data'], secteur_final, _log)
    _log("-" * 70)

    # Abonnes par secteur : ceux de la couche de reference si disponibles
    # (valeur hydraulique reelle), sinon le nombre de noeuds du secteur
    # (proxy du socle nu).
    abonnes_par_secteur = {}
    for secteur in set(secteur_final.values()):
        abonnes_par_secteur[secteur] = controle['abonnes'].get(
            secteur,
            sum(1 for s in secteur_final.values() if s == secteur))

    sous_zones_par_secteur = cs.regrouper_sous_zones(
        socle['scission'].get('sous_zone_de_noeud'), secteur_final)

    polygones = pt.generer_polygones_secteurs(
        socle['graphe_data'], socle['rattachement'], socle['scission'],
        CONFIG, get_layer, _ajouter_couche, _log,
        abonnes_par_secteur=abonnes_par_secteur,
        antennes_par_zone=socle['scission'].get('antennes_par_zone'),
        sous_zones_par_secteur=sous_zones_par_secteur)

    _log("=" * 70)
    _log("TERMINE -- %d secteur(s), %d chevauchement(s) residuel(s) resolu(s), "
         "%d secteur(s) reste(nt) multi-parties (antennes isolees assumees)."
         % (len(polygones['polygones_par_secteur']),
            polygones['nb_chevauchements_resolus'],
            polygones['nb_multipart']))
    controle['nb_apres'] = len(polygones['polygones_par_secteur'])
    cs.verifier_compte(controle, _log)
    _log("=" * 70)

    if bool(CONFIG.get('VERIFIER_NON_REGRESSION_GEOMETRIE', True)):
        try:
            from ..outils import verifier_non_regression_geometrie as _vng
            # 'secteurs_hydrauliques_topo' n'est pas encore dans le
            # projet a ce stade (cf. FIX-THREAD-SAFETY plus haut) -- on
            # passe directement l'objet couche construit en memoire.
            _vng.executer(log=_log, couche_topo=polygones.get('couche'))
        except Exception as e_vng:
            _log(
                "  [ALERTE] Controle de non-regression geometrique "
                "impossible a executer (%s) -- verifier manuellement avec "
                "outils/verifier_non_regression_geometrie.py." % e_vng
            )

    return {
        'socle': socle,
        'polygones': polygones,
        'controle_consolidation': controle,
        # FIX-THREAD-SAFETY : cf. _ajouter_couche() ci-dessus.
        'couches_en_attente': _couches_en_attente,
    }
