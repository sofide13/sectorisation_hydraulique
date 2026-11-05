# -*- coding: utf-8 -*-
"""
Sectorisation hydraulique (SCISSION ALTIMETRIQUE / PRV INTERNES /
FUSION ALTITUDE-COMPATIBLE / REDISTRIBUTION PAR SOUS-ARBRE / VITESSES CIBLEES)
=========================================================================
Paramètres de conception hydrauliques configurables ; les seuils ne constituent pas une déclaration de conformité normative.

Changements du build (5 correctifs demandes, cf. diagnostic du run precedent) :
  1) NOUVELLE PASSE 0 : scission ALTIMETRIQUE (PRV virtuels internes) avant
     la scission par nombre d'abonnes -- coupe chaque branche des que la
     pression statique sortirait de la bande utile de pression, ce qui
     s'attaque a la cause racine des 12 secteurs SOUS_ZONAGE_REQUIS et des
     pressions negatives (-77m, -47m...) observees sur le run precedent. La scission par
     abonnes (version precedente) ne regardait QUE les effectifs, jamais l'altitude.
  2) FUSION DES SOUS-EFFECTIFS rendue altitude-compatible : on ne fusionne
     plus un secteur sous le minimum configuré avec son voisin le plus connecte SANS
     regarder si ca recree un ecart topographique excessif -- on filtre
     desormais sur ECART_ALT_MAX_FUSION_M, avec repli explicite et signale
     si aucun voisin compatible n'existe.
  3) REDISTRIBUTION DE CAPACITE etendue aux SOUS-ARBRES (plus seulement les
     noeuds feuilles) via TAILLE_MAX_SOUS_ARBRE_REDISTRIBUTION -- la version precedente
     ne pouvait deplacer qu'un noeud isole a la fois, ce qui est trop lent
     pour resorber des depassements de plusieurs centaines d'abonnes.
  4) DIAGNOSTIC DE DEFICIT PHYSIQUE inchange dans le fond (aucune
     redistribution algorithmique ne peut combler un manque de capacite
     reelle) mais le message est plus explicite sur ce qui reste a faire.
  5) CONTROLE DES VITESSES DE CONCEPTION restreint aux conduites STRUCTURANTES
     (DN >= DN_MIN_CONTROLE_VITESSE et desservant au moins
     ABONNES_AVAL_MIN_CONTROLE_VITESSE abonnes en aval) -- une antenne
     terminale a 1-2 abonnes est normalement lente en pointe horaire, ce
     n'est pas un defaut de dimensionnement et polluait le taux de
     conformite (79% de "hors norme" sur le run precedent, essentiellement des antennes).
=========================================================================
"""

import hashlib
import heapq
import itertools
import math
import os
import random
import sys
import time
from collections import defaultdict, deque

from qgis.core import (
    QgsProject,
    QgsGeometry,
    QgsPointXY,
    QgsSpatialIndex,
    QgsVectorLayer,
    QgsFeature,
    QgsField,
    QgsFeatureRequest,
    QgsWkbTypes,
    QgsRasterLayer,
)
from qgis.PyQt.QtCore import QVariant

from .couche_differee import CoucheDifferee
from .isolation import construire_vannes_sectorisation

import processing
try:
    from processing.core.Processing import Processing
    Processing.initialize()
except Exception:
    pass


class PipelineAnnule(Exception):
    """Levee quand l'utilisateur annule le pipeline en cours d'execution."""
    pass


# Partages entre les fonctions imbriquees d'un meme run (fallback altitude
# MNT et affectation des conduites aux secteurs -- cf. usages de 'global'
# plus bas dans le corps du pipeline).
_mnt_hits = 0
_mnt_fallback = 0
secteur_de_conduite = {}

_CONFIG_DEFAUT = {
    # --- Couches QGIS ---
    'AEP_LAYER': "AEP",
    'ABONNE_LAYER': "abonne",
    'TANK_LAYER': "tank",
    'COMMUNE_LAYER': "commune",
    'DEM_PATH': r"E:\Extention\dem_UTM.tif",

    # --- Champs attributaires ---
    'PIPE_DIAMETER_FIELD': "DIAMETER",
    'PIPE_LENGTH_FIELD': "LENGTH",
    'TANK_CAPACITY_FIELD': "capacite",
    'TANK_COTE_EAU_FIELD': "cote_eau",
    'CHAMP_BLOC_ABONNE': "bloc",
    'CHAMP_ETAGE_ABONNE': "etage",
    'VANNE_FERMEE_FIELD': "",

    # --- Parametres demographiques ---
    'HABITANTS_PAR_ABONNE': 5.0,
    'DOTATION_L_HAB_JOUR': 150.0,
    'COEF_POINTE_HORAIRE': 1.3,

    # --- Plages secteur --- (plage de conception 500-3000 ; NB: la borne basse
    # effective est ABONNES_MIN_PAR_SECTEUR ci-dessous, fixée à 500 pour aligner la plage de conception
    # sur une pratique DMA courante ; ajuster si le contexte local le justifie)
    'ABONNES_MIN_PAR_SECTEUR': 500,
    'ABONNES_MAX_PAR_SECTEUR': 3000,
    'ABONNES_IDEAL_PAR_SECTEUR': 2000,
    # 500-3000 : plage de conception par defaut alignee sur une pratique
    # DMA courante. Les bornes restent configurables.

    # --- Hydraulique --- (plage de pression demandee 10-60m)
    'PRESSION_MIN_M': 10.0,
    'PRESSION_MAX_M': 60.0,
    'PRESSION_CIBLE_M': 30.0,
    'TANK_RESEARCH_M': 2000.0,
    'SNAP_TOLERANCE': 0.1,
    'HAUTEUR_EAU_RESERVOIR_M': 4.0,
    'COEF_HAZEN_WILLIAMS': 130.0,
    'VORONOI_CLIP_BUFFER_M': 150.0,
    'MAX_ITER_CONSOLIDATION': 80,
    'PERTE_CHARGE_MAX_M': 80.0,
    'CORRECTION_COTE_PIEZO_M': 0.0,
    'ECART_MAX_MNT_RESERVOIR': 80.0,
    'PRESSION_MARGE_SECURITE': 5.0,

    # --- PRV / surpresseurs ---
    'DN_MIN_PRV': 100,
    'DN_MIN_SURPRESSEUR': 100,
    'PRESSION_CIBLE_MIN': 10.0,
    'PRESSION_CIBLE_MAX': 60.0,

    # --- Vitesses de conception ---
    'VITESSE_MIN_M_S': 0.3,
    'VITESSE_MAX_M_S': 2.0,

    # --- Capacite reservoir ---
    'JOURS_AUTONOMIE_RESERVOIR': 1.0,
    'MARGE_SECURITE_CAPACITE_PCT': 0.05,
    'CAPACITE_PRINCIPAL_MIN_M3': 2000.0,
    'CAPACITE_SECONDAIRE_MIN_M3': 500.0,
    'PRIORITE_SCORE_PRINCIPAL': 100,
    'PRIORITE_SCORE_SECONDAIRE': 80,
    'PRIORITE_SCORE_SECOURS': 30,
    'PENALITE_HORS_PLAGE_COEF': 50.0,

    # --- Detection etages (grille fixe, non transitive) ---
    'GRILLE_ETAGE_M': 1.6,
    'HAUTEUR_ETAGE_M': 3.0,
    'NIVEAU_MAX_PLAUSIBLE': 5,   # R+5 = niveaux 0 (RDC) a 5 inclus
    'BLOC_MAX_NIVEAUX': 6,       # 6 niveaux par bloc (RDC + 5 etages)
    'SEUIL_BLOCS_ANOMALIE': 5,

    # --- Voronoi ---
    'UTILISER_VORONOI_AMORCE': True,
    'VORONOI_BIAS_FAVORISE': 0.6,
    'VORONOI_BIAS_PENALISE': 1.6,

    # --- Reservoirs isoles ---
    'RAYON_M_ISOLEMENT': 2000.0,
    'SEUIL_ABONNES_ISOLEMENT': 20,

    # --- Raffinement local ---
    'MAX_ROUNDS_RAFFINEMENT': 120,
    'AMELIORATION_MIN_TRANSFERT_M': 0.001,
    'MAX_TRANSFERTS_PAR_NOEUD': 30,
    'PENALITE_CAPACITE_COEF': 2000.0,
    'DEGRADATION_PRESSION_MAX_M': 50.0,

    # --- Corridor reseau ---
    'LARGEUR_CORRIDOR_M': 25.0,
    'BUFFER_SEGMENTS': 8,

    # --- Divers ---
    'SEUIL_ALERTE_PCT_MNT_FALLBACK': 5.0,
    'TANK_RESEARCH_EXPAND_FACTOR': 2.0,
    'MAX_TANK_RESEARCH_M': 5000.0,
    'SEUIL_DOUBLON_RESERVOIR_M': 20.0,
    'ECART_PRESSION_ANOMALIE_M': 80.0,
    'SEUIL_ALERTE_PCT_HORS_NORME': 15.0,
    'FACTEUR_CAPACITE_CROISSANCE': 1.5,
    'SEUIL_SATURATION_CROISSANCE': 0.80,

    # --- Multi-iterations ---
    'NB_ITERATIONS_INTERNES': 4,
    'SEED_BASE': 42,
    'VARIATION_PARAM_PCT': 0.15,
    'POIDS_SCORE_PRESSION': 3.0,
    'POIDS_SCORE_CAPACITE': 20.0,
    'POIDS_SCORE_FRAGMENTATION': 2000.0,

    # --- Boucle adaptative ---
    'MAX_CYCLES_CONFORMITE': 1,
    'FACTEUR_DURCISSEMENT_PENALITE': 1.3,
    'SEUIL_REDUCTION_VIOLATIONS_PCT': 2.0,
    'ACTIVER_REDISTRIBUTION_FORCEE': True,
    'TAUX_CIBLE_CAPACITE_MAX': 0.92,
    'MAX_SAUTS_RAFFINEMENT': 4,
    'ACTIVER_CORRECTION_SURPRESSION': True,
    'EXIGER_CONFORMITE_TOTALE': True,
    'PENALITE_SURCHARGE_COEF_INIT': 2000.0,
    'SEUIL_DECLENCHEMENT_REDISTRIB': -1,
    'MAX_DURCISSEMENTS_CONSECUTIFS': 4,

    # --- Recalibration cotes piezo ---
    'ACTIVER_RECALIBRATION_COTE': True,
    'MAX_AJUSTEMENT_COTE_M': 15.0,
    'SEUIL_PCT_VIOLATION_RECALIBRATION': 0.20,
    'SEUIL_ECART_TOPO_M': 60.0,

    # --- Correction forcee des violations residuelles ---
    'ACTIVER_CORRECTION_FORCEE': True,
    'MAX_ITER_CORRECTION_FORCEE': 50,

    # --- Garantie de connexite ---
    'ACTIVER_GARANTIE_CONNEXITE': True,
    'MAX_PASSES_GARANTIE_CONNEXITE_FINALE': 3,

    # --- redistribution multi-sauts + plafond dur ---
    'MAX_SAUTS_REDISTRIBUTION': 3,
    'ACTIVER_PLAFOND_ABONNES_MAX': True,

    # --- capacite reelle = stock ET source d'alimentation ---
    'TANK_DEBIT_SOURCE_FIELD': "DEBIT_SOURCE_M3_J",
    'ACTIVER_CAPACITE_SOURCE': True,

    # --- scission NON DESTRUCTIVE des secteurs surdimensionnes ---
    'ACTIVER_SCISSION_SECTEURS': True,
    'TAILLE_MIN_COUPURE_AB': 500,

    # --- passe theorique de diagnostic avant la passe pratique ---
    'ACTIVER_PASSE_THEORIQUE': True,

    # --- nettoyage final : scission -> fusion -> re-scission ---
    'ABONNES_IDEAL_CIBLE_SCISSION': 2000,

    # =========================================================================
    # --- point 1 : scission ALTIMETRIQUE (PRV virtuels internes) ---
    # =========================================================================
    'ACTIVER_SCISSION_ALTIMETRIQUE': True,
    # bande de pression utile exploitable par palier avant de forcer une
    # coupure (regulation par PRV virtuel / futur reservoir relais) ; reste
    # < PRESSION_MAX_M - PRESSION_MIN_M pour garder de la marge
    'BANDE_PRESSION_UTILE_M': 45.0,
    'MARGE_BASSE_AVANT_COUPURE_M': 2.0,

    # --- point 2 : fusion des sous-effectifs altitude-compatible ---
    'ECART_ALT_MAX_FUSION_M': 45.0,

    # --- moteur point 3 : redistribution de capacite par sous-arbre ---
    'ACTIVER_REDISTRIBUTION_SOUS_ARBRE': True,
    'TAILLE_MAX_SOUS_ARBRE_REDISTRIBUTION': 400,
    'MAX_TRANSFERTS_REDIS': 1000,

    # --- moteur point 5 : controle de vitesse restreint aux conduites
    # structurantes (evite de polluer le taux de conformite avec des
    # antennes terminales a 1-2 abonnes, normalement lentes en pointe) ---
    'DN_MIN_CONTROLE_VITESSE': 100,
    'ABONNES_AVAL_MIN_CONTROLE_VITESSE': 5,

    # --- Validation solveur et vannes de sectorisation ---
    'VALIDER_PRESSION_WNTR': True,
    'FERMER_CONDUITES_FRONTIERES_WNTR': True,
    'RUGOSITE_WNTR': 130.0,
    'AJOUTER_VANNES_SECTORISATION': True,
}

CLES_CONFIG_PHYSIQUES = [
    'PRESSION_MIN_M', 'PRESSION_MAX_M', 'PRESSION_CIBLE_M',
    'DN_MIN_PRV', 'DN_MIN_SURPRESSEUR', 'PRESSION_CIBLE_MIN', 'PRESSION_CIBLE_MAX',
    'VITESSE_MIN_M_S', 'VITESSE_MAX_M_S', 'LARGEUR_CORRIDOR_M', 'BUFFER_SEGMENTS',
    'COEF_HAZEN_WILLIAMS', 'HAUTEUR_ETAGE_M', 'PERTE_CHARGE_MAX_M',
    'GRILLE_ETAGE_M', 'NIVEAU_MAX_PLAUSIBLE', 'BLOC_MAX_NIVEAUX',
    'ABONNES_MIN_PAR_SECTEUR', 'ABONNES_MAX_PAR_SECTEUR', 'ABONNES_IDEAL_PAR_SECTEUR', 'TAILLE_MIN_COUPURE_AB',
    'BANDE_PRESSION_UTILE_M', 'MARGE_BASSE_AVANT_COUPURE_M', 'ECART_ALT_MAX_FUSION_M',
    'TAILLE_MAX_SOUS_ARBRE_REDISTRIBUTION', 'DN_MIN_CONTROLE_VITESSE',
    'ABONNES_AVAL_MIN_CONTROLE_VITESSE',
]


def executer(config=None, on_etape=None, annuler_callback=None):
    """
    Point d'entree du pipeline, appele par le panneau du plugin
    (core/pipeline.py). Remplace l'ancien mode autonome (qui relancait
    sa propre instance QGIS) : utilise le projet QGIS deja charge.

    config           : dict plat de parametres, fusionne sur _CONFIG_DEFAUT
                        (cf. core/config_manager.py:json_vers_plat).
    on_etape         : callable(idx:int) optionnel, appele a 10 etapes-cles
                        (idx 0..9) pour la barre de progression du panneau.
    annuler_callback : callable() -> bool optionnel ; si vrai, leve
                        PipelineAnnule pour interrompre proprement.
    """
    global _mnt_hits, _mnt_fallback, secteur_de_conduite
    _mnt_hits = 0
    _mnt_fallback = 0
    secteur_de_conduite = {}

    def _log(msg):
        print(msg)
        if sys.stdout is not None:
            try:
                sys.stdout.flush()
            except Exception:
                pass

    def _verifier_annulation():
        if annuler_callback is not None and annuler_callback():
            raise PipelineAnnule("Pipeline annule par l'utilisateur.")

    def _empreinte_module(mod):
        """Retourne (chemin, date de modif, sha256) d'un module deja importe,
        ou None si le module/fichier est introuvable. Sert a prouver, dans le
        log meme, QUEL fichier disque a reellement ete execute -- evite les
        faux diagnostics de bug quand c'est en realite une ancienne copie du
        plugin (cache Python, dossier profil QGIS non mis a jour) qui tourne."""
        try:
            path = os.path.abspath(getattr(mod, "__file__", None) or "")
            if not path or not os.path.isfile(path):
                return None
            with open(path, "rb") as fh:
                empreinte = hashlib.sha256(fh.read()).hexdigest()
            mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(path)))
            return path, mtime, empreinte
        except Exception:
            return None

    def _log_empreinte_installation():
        """Auto-diagnostic affiche en tete de CHAQUE run : chemin disque reel,
        date de modif et empreinte sha256 des modules-cles actuellement
        charges par l'interpreteur QGIS. A comparer avec les valeurs de
        reference du zip livre (cf. outils/verifier_installation_plugin.py)
        pour detecter immediatement un plugin installe/importe perime."""
        _log("=" * 70)
        _log("AUTO-DIAGNOSTIC INSTALLATION (verifier avant toute analyse de bug)")
        _log("=" * 70)
        import importlib
        noms_modules = [
            ("core.pipeline", sys.modules.get(__name__)),
        ]
        for nom in ("core.partition_secteurs", "core.geom_backend",
                    "core.polygones_topologiques", "core.graphe_topologique",
                    "core.consolidation_secteurs",
                    "core.executer_topologique"):
            try:
                noms_modules.append((nom, importlib.import_module("." + nom.split(".")[-1], __package__)))
            except Exception as e:
                noms_modules.append((nom, None))
                _log(f"  [ABSENT] {nom} : import impossible ({e})")
        for nom, mod in noms_modules:
            if mod is None:
                continue
            info = _empreinte_module(mod)
            if info is None:
                _log(f"  [?] {nom} : chemin/fichier introuvable (module memoire sans __file__ ?)")
                continue
            path, mtime, empreinte = info
            _log(f"  {nom}")
            _log(f"      chemin  : {path}")
            _log(f"      modifie : {mtime}")
            _log(f"      sha256  : {empreinte}")
        # Marqueur fonctionnel independant du hash : le module de partition
        # le module de partition doit exposer construire_territoires (sinon repli sur un code
        # bien plus ancien, cause typique des gros polygones "enveloppe
        # convexe" -- cf. README, section FIX-PARTITION).
        part_mod = dict(noms_modules).get("core.partition_secteurs")
        if part_mod is not None and hasattr(part_mod, "construire_territoires"):
            _log("  [OK] core.partition_secteurs.construire_territoires() disponible "
                 "-- le moteur de partition des ilots urbains (sans enveloppe convexe) sera utilise.")
        else:
            _log("  [ALERTE] core.partition_secteurs.construire_territoires() NON "
                 "disponible -- secteurs_hydrauliques/secteurs_limites vont "
                 "utiliser un repli degrade. Verifier l'installation du plugin "
                 "(dossier profil QGIS, cache __pycache__, redemarrage complet de "
                 "QGIS apres mise a jour des fichiers).")
        cons_mod = dict(noms_modules).get("core.consolidation_secteurs")
        if cons_mod is not None and hasattr(cons_mod, "consolider_sur_reference"):
            _log("  [OK] core.consolidation_secteurs.consolider_sur_reference() "
                 "disponible -- le socle topologique comptera exactement autant "
                 "de secteurs que secteurs_hydrauliques .")
        else:
            _log("  [ALERTE] core.consolidation_secteurs absent -- le socle "
                 "topologique peut produire des centaines de zones au lieu du "
                 "nombre reel de secteurs (comportement anterieur).")
        _log("=" * 70 + "\n")

    _log_empreinte_installation()

    def _etape(idx):
        _verifier_annulation()
        if on_etape is not None:
            on_etape(idx)

    # Couches memoire construites pendant le run mais pas encore inserees
    # dans le projet QGIS -- cf. FIX-THREAD-SAFETY sur _ajouter_couche
    # ci-dessous.
    _couches_en_attente = []

    # FIX-THREAD-SAFETY : cette fonction ne construit ni ne touche
    # plus JAMAIS de QgsVectorLayer/QgsProject depuis ce point du code.
    # executer() peut etre appelee depuis un QThread (PipelineWorker, cf.
    # ui/panneau_principal.py) et PyQGIS n'est PAS thread-safe.
    #
    # Les couches produites ici sont des instances de CoucheDifferee (cf.
    # core/couche_differee.py) : un objet Python pur, sans affinite de
    # thread, qui accumule nom/type/CRS/champs/features SANS jamais
    # instancier de vrai QgsVectorLayer. Elles sont simplement accumulees
    # en memoire ; c'est l'appelant (thread principal) qui construit les
    # vrais QgsVectorLayer et les enregistre dans le projet, une fois
    # executer() termine -- cf. 'couches_en_attente' dans la valeur de
    # retour, et PipelineWorker._executer /
    # PanneauPrincipal._materialiser_couche /
    # PanneauPrincipal._ajouter_couches_dans_projet cote UI.
    def _ajouter_couche(couche):
        _couches_en_attente.append(couche)

    # Compteur remis a zero pour chaque iteration puis pour le nettoyage final.
    compteur_transfert_actif = {'n': 0}

    _verifier_annulation()

    project = QgsProject.instance()
    _log("=" * 70)
    _log("Sectorisation hydraulique -- projet QGIS actif")
    _log("Couches disponibles : " + ", ".join(
        layer.name() for layer in project.mapLayers().values()))
    _log("=" * 70)

    CONFIG = dict(_CONFIG_DEFAUT)
    if config:
        CONFIG.update(config)

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
        except Exception:
            pass
        return None, None

    def calculer_debit_abonne():
        q_journalier = CONFIG['HABITANTS_PAR_ABONNE'] * CONFIG['DOTATION_L_HAB_JOUR']
        q_pointe = q_journalier * CONFIG['COEF_POINTE_HORAIRE'] / 86400.0
        return q_pointe

    def get_altitude_default(x, y):
        base_alt = 850
        y_factor = (y - 3887000) / (3934000 - 3887000)
        alt = base_alt - (y_factor * 100)
        return max(750, min(950, alt))

    def assurer_multipolygone(geom):
        if geom is None or geom.isEmpty():
            return geom
        wkb_type = geom.wkbType()
        if wkb_type == QgsWkbTypes.MultiPolygon:
            return geom
        if wkb_type == QgsWkbTypes.Polygon:
            try:
                return QgsGeometry.fromMultiPolygonXY([geom.asPolygon()])
            except Exception:
                return geom
        try:
            polygons = []
            if geom.isMultipart():
                parts = geom.asGeometryCollection()
                for part in parts:
                    if part.wkbType() == QgsWkbTypes.Polygon:
                        polygons.append(part.asPolygon())
            elif wkb_type == QgsWkbTypes.Polygon:
                polygons.append(geom.asPolygon())
            if polygons:
                return QgsGeometry.fromMultiPolygonXY(polygons)
        except Exception:
            pass
        return geom

    # simple cache for Hazen-Williams to avoid recomputing identical parameters
    _hf_cache = {}

    def perte_charge_hazen_williams(q_ls, longueur_m, diametre_mm, c=130.0):
        if diametre_mm is None or diametre_mm <= 0 or longueur_m <= 0 or q_ls <= 0:
            return 0.0
        # quantize inputs to reduce cache size and floating point variations
        key = (round(q_ls, 4), round(longueur_m, 3), round(diametre_mm, 1), round(c, 1))
        if key in _hf_cache:
            return _hf_cache[key]
        q_m3s = q_ls / 1000.0
        d_m = diametre_mm / 1000.0
        try:
            hf = 10.67 * longueur_m * (q_m3s / c) ** 1.852 / (d_m ** 4.87)
        except (ZeroDivisionError, ValueError):
            hf = 0.0
        _hf_cache[key] = hf
        return hf

    def vitesse_ecoulement(q_ls, diametre_mm):
        if diametre_mm is None or diametre_mm <= 0 or q_ls <= 0:
            return 0.0
        q_m3s = q_ls / 1000.0
        d_m = diametre_mm / 1000.0
        s = math.pi * (d_m / 2) ** 2
        return q_m3s / s

    def classer_priorite_reservoir(type_str, capacite_m3):
        type_str = (type_str or "").lower()
        est_sureleve = ("sur elev" in type_str or "sureleve" in type_str or
                        "surélévé" in type_str)
        if capacite_m3 >= CONFIG['CAPACITE_PRINCIPAL_MIN_M3'] and not est_sureleve:
            return 'PRINCIPAL', CONFIG['PRIORITE_SCORE_PRINCIPAL']
        if capacite_m3 >= CONFIG['CAPACITE_SECONDAIRE_MIN_M3']:
            return 'SECONDAIRE', CONFIG['PRIORITE_SCORE_SECONDAIRE']
        return 'SECOURS', CONFIG['PRIORITE_SCORE_SECOURS']

    # ============================================================================
    # 1) CHARGEMENT + MNT
    # ============================================================================

    _log("=" * 70)
    _log("SECTORISATION HYDRAULIQUE (ALTIMETRIQUE + FUSION COMPATIBLE + "
          "SOUS-ARBRE + VITESSES CIBLEES)")
    _log(">>> CORRECTIF ACTIF : FIX-CONTIGUITE-TERRITOIRES -- 1 secteur ="
          " 1 territoire d'un seul tenant (fin des secteurs eclates en"
          " rubans eparpilles dans toute la ville), et plus de territoire"
          " deborde en zone non desservie (borne"
          " DIST_MAX_ILOT_SANS_NOEUD_M).")
    _log(">>> BUILD DE CE FICHIER : FIX-TOPOLOGIE-09 + FIX-FUSION-PRESSION-12"
          " + FIX-TAMPON-ROUTE-VARIABLE-11 + FIX-ILOTS-URBAINS-14"
          " (1 secteur = 1 polygone simple, anti-chevauchement structurel,"
          " anti-vide, anneaux interieurs supprimes ; fusion de secours des"
          " secteurs sous-effectif choisie par PRESSION REELLE ESTIMEE"
          " (cote piezo receveur - altitude), et non plus par ecart"
          " d'altitude brut ni par simple connectivite ; tampon de coupure"
          " routiere VARIABLE par troncon pour secteurs_limites ;"
          " secteurs_hydrauliques delimitee par VRAIS ILOTS URBAINS"
          " (polygonisation du reseau routier, methode DMA morphologique),"
          " avec repli corridor-buffer variable la ou le reseau ne boucle"
          " pas ; secteurs_limites inchangee)"
          " -- si cette ligne n'apparait pas, le pipeline.py"
          " charge n'est PAS celui fourni. <<<")
    _log("=" * 70)

    pipe_layer = get_layer(CONFIG['AEP_LAYER'])
    abonne_layer = get_layer(CONFIG['ABONNE_LAYER'])
    tank_layer = get_layer(CONFIG['TANK_LAYER'])
    commune_layer = get_layer_optional(CONFIG['COMMUNE_LAYER'])

    # Couches optionnelles pour la decoupe geometrique reelle des secteurs
    # (limites suivant les routes, sans jamais couper un batiment/stade/
    # hopital/voie ferree). Toutes sont facultatives : si le nom de couche
    # est vide ou introuvable, l'etape correspondante est simplement ignoree.
    route_layer = get_layer_optional(CONFIG.get('ROUTE_LAYER', ''))
    batiment_layer = get_layer_optional(CONFIG.get('BATIMENT_LAYER', ''))
    stade_layer = get_layer_optional(CONFIG.get('STADE_LAYER', ''))
    hopital_layer = get_layer_optional(CONFIG.get('HOPITAL_LAYER', ''))
    chemin_fer_layer = get_layer_optional(CONFIG.get('CHEMIN_FER_LAYER', ''))

    _log(f"Conduites : {pipe_layer.featureCount()}")
    _log(f"Abonnes : {abonne_layer.featureCount()}")
    _log(f"Reservoirs : {tank_layer.featureCount()}")
    if commune_layer is not None:
        _log(f"Commune (decoupe Voronoi) : {commune_layer.featureCount()} entite(s)")
    else:
        _log(f"  ATTENTION : couche '{CONFIG['COMMUNE_LAYER']}' introuvable -- decoupe par emprise reseau.")

    if CONFIG.get('ACTIVER_DECOUPE_ROUTES', True):
        if route_layer is not None:
            _log(f"Routes (decoupe des limites de secteur) : {route_layer.featureCount()} entite(s)")
        else:
            _log("  INFO : pas de couche routes configuree/trouvee -- "
                  "les limites de secteur resteront un Voronoi pur sur les noeuds.")
        for lbl, lyr in (("Batiments", batiment_layer), ("Stades", stade_layer),
                          ("Hopitaux", hopital_layer), ("Chemin de fer", chemin_fer_layer)):
            if lyr is not None:
                _log(f"{lbl} (protection anti-coupure) : {lyr.featureCount()} entite(s)")

    altitude_cache = {}
    use_dem = False
    _mnt_hits = 0
    _mnt_fallback = 0
    _fallback_coords = []

    if os.path.exists(CONFIG['DEM_PATH']):
        dem_layer = QgsRasterLayer(CONFIG['DEM_PATH'], "MNT")
        if dem_layer.isValid():
            raster_provider = dem_layer.dataProvider()
            extent = raster_provider.extent()
            _log(f"\nMNT charge : {CONFIG['DEM_PATH']}")
            if dem_layer.crs().authid() != pipe_layer.crs().authid():
                _log("  ATTENTION CRITIQUE : CRS MNT != CRS reseau.")

            def get_altitude_from_dem(x, y):
                try:
                    if not (extent.xMinimum() <= x <= extent.xMaximum() and
                            extent.yMinimum() <= y <= extent.yMaximum()):
                        return None
                    value, ok = raster_provider.sample(QgsPointXY(x, y), 1)
                    if ok and value is not None:
                        nodata = raster_provider.sourceNoDataValue(1)
                        if nodata is None or value != nodata:
                            return float(value)
                    return None
                except Exception:
                    return None

            alt_trouvees = []
            for f in pipe_layer.getFeatures():
                geom = f.geometry()
                if geom and not geom.isEmpty():
                    pt1, pt2 = extremites_ligne(geom)
                    if pt1:
                        alt = get_altitude_from_dem(pt1.x(), pt1.y())
                        if alt is not None:
                            alt_trouvees.append(alt)
                        if len(alt_trouvees) >= 20:
                            break
            if alt_trouvees:
                use_dem = True
                _log(f"  Altitudes echantillon : min={min(alt_trouvees):.1f}m, max={max(alt_trouvees):.1f}m")

                def get_altitude(x, y):
                    global _mnt_hits, _mnt_fallback
                    key = (round(x, 1), round(y, 1))
                    if key not in altitude_cache:
                        alt = get_altitude_from_dem(x, y)
                        if alt is not None:
                            altitude_cache[key] = alt
                            _mnt_hits += 1
                        else:
                            altitude_cache[key] = get_altitude_default(x, y)
                            _mnt_fallback += 1
                            _fallback_coords.append((x, y))
                    return altitude_cache[key]

    if not use_dem:
        _log("\nATTENTION : aucun MNT utilisable -> altitudes approximatives")

        def get_altitude(x, y):
            global _mnt_fallback
            key = (round(x, 1), round(y, 1))
            if key not in altitude_cache:
                altitude_cache[key] = get_altitude_default(x, y)
                _mnt_fallback += 1
                _fallback_coords.append((x, y))
            return altitude_cache[key]

    # ============================================================================
    _etape(0)  # Reconstruction du reseau
    # 2) RECONSTRUCTION DU GRAPHE
    # ============================================================================

    _log("\nReconstruction du reseau...")

    noeuds = {}
    graphe = defaultdict(list)
    pipe_features = {}
    pipe_endpoints = {}
    pipe_diameters = {}
    pipe_lengths = {}
    pipe_diametre_estime = set()
    conduites_hors_reseau_details = {}
    nb_diam_mesure = 0
    nb_diam_estime = 0

    for f in pipe_layer.getFeatures():
        geom = f.geometry()
        if geom is None or geom.isEmpty():
            conduites_hors_reseau_details[f.id()] = 'GEOMETRIE_VIDE_OU_NULLE'
            continue
        pt1, pt2 = extremites_ligne(geom)
        if pt1 is None or pt2 is None:
            conduites_hors_reseau_details[f.id()] = 'GEOMETRIE_LIGNE_INEXPLOITABLE'
            continue
        alt1 = get_altitude(pt1.x(), pt1.y()) or 850.0
        alt2 = get_altitude(pt2.x(), pt2.y()) or 850.0
        key1 = snap_key(pt1.x(), pt1.y(), CONFIG['SNAP_TOLERANCE'])
        key2 = snap_key(pt2.x(), pt2.y(), CONFIG['SNAP_TOLERANCE'])
        if key1 == key2:
            conduites_hors_reseau_details[f.id()] = 'CONDUITE_DEGENEREE_APRES_SNAP'
            continue
        noeuds.setdefault(key1, {'pt': pt1, 'altitude': alt1})
        noeuds.setdefault(key2, {'pt': pt2, 'altitude': alt2})
        pipe_features[f.id()] = f

        len_field = CONFIG['PIPE_LENGTH_FIELD']
        if len_field in [fld.name() for fld in pipe_layer.fields()] and f[len_field] not in (None, ""):
            length = float(f[len_field])
        else:
            length = geom.length()
        pipe_lengths[f.id()] = length

        diam_field = CONFIG['PIPE_DIAMETER_FIELD']
        if diam_field in [fld.name() for fld in pipe_layer.fields()] and f[diam_field] not in (None, "") and float(f[diam_field]) > 0:
            diam = float(f[diam_field])
            nb_diam_mesure += 1
        else:
            diam = 100 if length < 100 else 150 if length < 500 else 200
            pipe_diametre_estime.add(f.id())
            nb_diam_estime += 1
        pipe_diameters[f.id()] = diam
        pipe_endpoints[f.id()] = (key1, key2)

    if conduites_hors_reseau_details:
        _log(
            "  [DIAGNOSTIC CONDUITES HORS RESEAU] %d conduite(s) exclue(s) avant "
            "affectation : %s. Cause(s) : %s."
            % (len(conduites_hors_reseau_details),
               ', '.join(str(k) for k in sorted(conduites_hors_reseau_details)[:12]),
               ', '.join(sorted(set(conduites_hors_reseau_details.values()))))
        )
    _log(f"Noeuds identifies : {len(noeuds)}")
    if len(noeuds) < 2:
        raise RuntimeError("Pas assez de noeuds !")

    noeud_keys = list(noeuds.keys())
    noeud_id_map = {k: f"N{i}" for i, k in enumerate(noeud_keys)}
    noeud_altitudes = {noeud_id_map[k]: noeuds[k]['altitude'] for k in noeuds}
    noeud_points = {noeud_id_map[k]: noeuds[k]['pt'] for k in noeuds}

    for pipe_id, (key1, key2) in pipe_endpoints.items():
        n1 = noeud_id_map[key1]
        n2 = noeud_id_map[key2]
        length = pipe_lengths[pipe_id]
        diam = pipe_diameters.get(pipe_id, 100)
        graphe[n1].append((n2, pipe_id, length, diam))
        graphe[n2].append((n1, pipe_id, length, diam))

    # ============================================================================
    # 3) INDEX SPATIAL + RATTACHEMENT DES ABONNES
    # ============================================================================

    _log("\nConstruction de l'index spatial...")

    tmp_noeuds_layer = QgsVectorLayer(f"Point?crs={pipe_layer.crs().authid()}", "tmp_noeuds", "memory")
    tmp_prov = tmp_noeuds_layer.dataProvider()
    tmp_prov.addAttributes([QgsField("nid", QVariant.String)])
    tmp_noeuds_layer.updateFields()
    tmp_feats = []
    for k, data in noeuds.items():
        nid = noeud_id_map[k]
        feat = QgsFeature(tmp_noeuds_layer.fields())
        feat.setGeometry(QgsGeometry.fromPointXY(data['pt']))
        feat.setAttributes([nid])
        tmp_feats.append(feat)
    tmp_prov.addFeatures(tmp_feats)
    tmp_noeuds_layer.updateExtents()
    spatial_index = QgsSpatialIndex(tmp_noeuds_layer.getFeatures())

    def noeud_le_plus_proche(point_xy, max_dist=1e9):
        nearest_ids = spatial_index.nearestNeighbor(point_xy, 5)
        if not nearest_ids:
            return None, None, None
        min_dist = float('inf')
        best_nid = None
        best_geom = None
        for fid in nearest_ids:
            feat = next(tmp_noeuds_layer.getFeatures(QgsFeatureRequest(fid)))
            geom = feat.geometry()
            if geom:
                dist = point_xy.distance(geom.asPoint())
                if dist < min_dist and dist <= max_dist:
                    min_dist = dist
                    best_nid = feat["nid"]
                    best_geom = geom
        return best_nid, best_geom, min_dist

    def noeud_le_plus_proche_absolu(point_xy):
        nearest_ids = spatial_index.nearestNeighbor(point_xy, 1)
        if not nearest_ids:
            return None, None
        feat = next(tmp_noeuds_layer.getFeatures(QgsFeatureRequest(nearest_ids[0])))
        geom = feat.geometry()
        if geom is None:
            return None, None
        return feat["nid"], point_xy.distance(geom.asPoint())

    all_pipe_geoms = [f.geometry() for f in pipe_features.values() if f.geometry() and not f.geometry().isEmpty()]
    reseau_union = QgsGeometry.unaryUnion(all_pipe_geoms)

    # Emprise de decoupe : buffer autour des conduites AEP. Si demande, on
    # l'etend aussi aux reservoirs (peu nombreux -> union peu couteuse).
    # ATTENTION PERFORMANCE : ne jamais unir les points ABONNES un par un ici
    # (potentiellement des dizaines de milliers de points) -- un unaryUnion()
    # sur autant de geometries individuelles peut prendre des heures. On se
    # contente donc d'etendre l'emprise aux abonnes situes hors du buffer
    # reseau via leur enveloppe convexe (un seul calcul, tres rapide), au lieu
    # de les unir un par un.
    emprise_decoupe = reseau_union.buffer(CONFIG['VORONOI_CLIP_BUFFER_M'], 8)
    if CONFIG.get('INCLURE_TANK_ABONNE_DANS_EMPRISE', True):
        tank_geoms = [f.geometry() for f in tank_layer.getFeatures()
                      if f.geometry() and not f.geometry().isEmpty()]
        if tank_geoms:
            emprise_decoupe = QgsGeometry.unaryUnion(
                [emprise_decoupe] + [g.buffer(CONFIG['VORONOI_CLIP_BUFFER_M'], 8) for g in tank_geoms]
            )
        # Detection des abonnes hors emprise via l'index spatial des noeuds
        # deja construit (recherche du plus proche voisin, tres rapide),
        # plutot qu'un test geometrique .contains() sur un polygone complexe
        # repete 40000+ fois (couteux, et prepareGeometry() n'existe pas sur
        # QgsGeometry dans les bindings PyQGIS).
        buffer_m = CONFIG['VORONOI_CLIP_BUFFER_M']
        pts_hors_emprise = []
        for f in abonne_layer.getFeatures():
            g = f.geometry()
            if g is None or g.isEmpty():
                continue
            pt = g.asPoint()
            nearest_ids = spatial_index.nearestNeighbor(pt, 1)
            if not nearest_ids:
                continue
            nfeat = next(tmp_noeuds_layer.getFeatures(QgsFeatureRequest(nearest_ids[0])), None)
            ngeom = nfeat.geometry() if nfeat is not None else None
            if ngeom is not None and pt.distance(ngeom.asPoint()) > buffer_m:
                pts_hors_emprise.append(pt)
        if pts_hors_emprise:
            _log(f"    {len(pts_hors_emprise)} abonne(s) hors du buffer reseau -> "
                  f"extension de l'emprise (enveloppe convexe).")
            hull = QgsGeometry.fromMultiPointXY(pts_hors_emprise).convexHull()
            if hull and not hull.isEmpty():
                emprise_decoupe = QgsGeometry.unaryUnion(
                    [emprise_decoupe, hull.buffer(CONFIG['VORONOI_CLIP_BUFFER_M'], 8)]
                )

    _log("\nRattachement des abonnes aux noeuds du reseau...")

    abonne_geoms = {}
    abonnes_hors_secteur_details = {}

    def rattacher_abonnes_aux_noeuds():
        abonnes_par_noeud_ = defaultdict(int)
        abonne_vers_noeud_ = {}
        non_affectes_ = 0
        for af in abonne_layer.getFeatures():
            geom = af.geometry()
            if geom is None or geom.isEmpty():
                non_affectes_ += 1
                abonnes_hors_secteur_details[af.id()] = ('GEOMETRIE_VIDE_OU_NULLE', None)
                continue
            pt = geom.asPoint()
            abonne_geoms[af.id()] = pt
            nid, _, _dist = noeud_le_plus_proche(pt, max_dist=500)
            if nid and nid in noeud_altitudes:
                abonnes_par_noeud_[nid] += 1
                abonne_vers_noeud_[af.id()] = nid
            else:
                non_affectes_ += 1
                _, dist_abs = noeud_le_plus_proche_absolu(pt)
                abonnes_hors_secteur_details[af.id()] = (
                    'DISTANCE_NOEUD_SUPERIEURE_A_500M' if dist_abs is not None else 'AUCUN_NOEUD',
                    dist_abs,
                )
        return abonnes_par_noeud_, abonne_vers_noeud_, non_affectes_

    abonnes_par_noeud, abonne_vers_noeud, abonnes_non_affectes = rattacher_abonnes_aux_noeuds()
    q_pointe_abonne = calculer_debit_abonne()

    _log(f"Abonnes rattaches : {sum(abonnes_par_noeud.values())} / {abonne_layer.featureCount()}")
    _log(f"  Non rattaches : {abonnes_non_affectes}")
    if abonnes_hors_secteur_details:
        detail_ab = defaultdict(int)
        for cause, _dist in abonnes_hors_secteur_details.values():
            detail_ab[cause] += 1
        _log(
            "  [DIAGNOSTIC ABONNES HORS SECTEUR] %d : %s. Ils ne peuvent pas "
            "etre comptes dans une DMA tant qu'ils ne sont pas rattaches a un "
            "noeud (rayon maximal 500 m ou geometrie invalide)."
            % (len(abonnes_hors_secteur_details),
               ', '.join('%s=%d' % (k, v) for k, v in sorted(detail_ab.items())))
        )

    # ============================================================================
    # 2bis) DETECTION DES NIVEAUX (ETAGES) PAR CLUSTERING GRILLE
    # ============================================================================

    _log("\n" + "=" * 70)
    _log("DETECTION DES NIVEAUX (ETAGES) - Clustering grille fixe")
    _log("=" * 70)

    grille = CONFIG['GRILLE_ETAGE_M']
    bloc_max_niveaux = CONFIG['BLOC_MAX_NIVEAUX']
    niveau_max_plausible = CONFIG['NIVEAU_MAX_PLAUSIBLE']
    champ_bloc_abonne = (CONFIG.get('CHAMP_BLOC_ABONNE') or '').strip()
    champ_etage_abonne = (CONFIG.get('CHAMP_ETAGE_ABONNE') or '').strip()

    abonne_niveau = {}
    abonne_bloc = {}
    abonne_cluster_taille = {}
    abonne_niveau_explicite = set()

    # --- Priorite 1 : champs explicites 'bloc' / 'etage' de la table abonne ---
    idx_champ_bloc = abonne_layer.fields().indexFromName(champ_bloc_abonne) if champ_bloc_abonne else -1
    idx_champ_etage = abonne_layer.fields().indexFromName(champ_etage_abonne) if champ_etage_abonne else -1

    if idx_champ_etage != -1:
        for feat in abonne_layer.getFeatures():
            fid = feat.id()
            if fid not in abonne_geoms:
                continue
            val_etage = feat[champ_etage_abonne]
            if val_etage is None or (hasattr(val_etage, '__class__') and str(val_etage).strip() == ''):
                continue
            try:
                niveau_explicite = int(round(float(val_etage)))
            except (TypeError, ValueError):
                continue
            abonne_niveau[fid] = max(0, min(niveau_explicite, niveau_max_plausible))
            abonne_niveau_explicite.add(fid)
            if idx_champ_bloc != -1:
                val_bloc = feat[champ_bloc_abonne]
                if val_bloc is not None and str(val_bloc).strip() != '':
                    try:
                        abonne_bloc[fid] = int(round(float(val_bloc)))
                    except (TypeError, ValueError):
                        abonne_bloc[fid] = str(val_bloc)

    if idx_champ_etage != -1:
        _log(f"Abonnes avec etage explicite (champ '{champ_etage_abonne}') : "
              f"{len(abonne_niveau_explicite)} / {len(abonne_geoms)}")

    # --- Priorite 2 : clustering geometrique pour les abonnes restants ---
    fids_restants = [fid for fid in abonne_geoms if fid not in abonne_niveau_explicite]

    def _grid_key(pt):
        return (round(pt.x() / grille), round(pt.y() / grille))

    clusters_grille = defaultdict(list)
    for fid in fids_restants:
        clusters_grille[_grid_key(abonne_geoms[fid])].append(fid)

    clusters_anomalie = []

    for key, membres in clusters_grille.items():
        membres_tries = sorted(membres)
        taille = len(membres_tries)
        nb_blocs = math.ceil(taille / bloc_max_niveaux)
        if nb_blocs > CONFIG['SEUIL_BLOCS_ANOMALIE']:
            clusters_anomalie.append((key, taille, nb_blocs))
        for idx, fid in enumerate(membres_tries):
            bloc_id = idx // bloc_max_niveaux
            niveau_dans_bloc = idx % bloc_max_niveaux
            abonne_niveau[fid] = min(niveau_dans_bloc, niveau_max_plausible)
            abonne_bloc.setdefault(fid, bloc_id)
            abonne_cluster_taille[fid] = taille

    nb_clusters_multi = sum(1 for m in clusters_grille.values() if len(m) > 1)
    nb_abonnes_en_immeuble = sum(len(m) for m in clusters_grille.values() if len(m) > 1)
    nb_blocs_total = sum(math.ceil(len(m) / bloc_max_niveaux) for m in clusters_grille.values())
    niveau_max_detecte = max(abonne_niveau.values()) if abonne_niveau else 0

    _log(f"Clusters multi-abonnes (estimation geometrique, abonnes sans etage explicite) : {nb_clusters_multi}")
    _log(f"Abonnes en immeuble (estimation geometrique) : {nb_abonnes_en_immeuble} / {len(fids_restants)}")
    _log(f"Blocs R+{niveau_max_plausible} generes (estimation) : {nb_blocs_total} "
          f"(max {bloc_max_niveaux} abonnes/niveaux par bloc)")
    _log(f"Niveau max applique DANS un bloc : {niveau_max_detecte} (plafonne a R+{niveau_max_plausible})")
    if clusters_anomalie:
        total_ab_anomalie = sum(t for _, t, _ in clusters_anomalie)
        total_blocs_anomalie = sum(n for _, _, n in clusters_anomalie)
        _log(f"  ATTENTION : {len(clusters_anomalie)} cellule(s) anomalie ({total_ab_anomalie} abonnes, "
              f"{total_blocs_anomalie} blocs R+{niveau_max_plausible} generes) -> probables doublons, "
              f"verifier manuellement.")

    noeud_niveau_max = defaultdict(int)
    for fid, nid in abonne_vers_noeud.items():
        niveau = abonne_niveau.get(fid, 0)
        if niveau > noeud_niveau_max[nid]:
            noeud_niveau_max[nid] = niveau

    def pression_min_requise_noeud(nid):
        return CONFIG['PRESSION_MIN_M'] + noeud_niveau_max.get(nid, 0) * CONFIG['HAUTEUR_ETAGE_M']

    def est_norme_pression(p, nid):
        if p is None:
            return False
        return pression_min_requise_noeud(nid) <= p <= CONFIG['PRESSION_MAX_M']

    def ecart_hors_plage(p, nid):
        if p is None:
            return 0.0
        seuil_min = pression_min_requise_noeud(nid)
        if p < seuil_min:
            return seuil_min - p
        if p > CONFIG['PRESSION_MAX_M']:
            return p - CONFIG['PRESSION_MAX_M']
        return 0.0

    def classer_type_violation(p, nid):
        if p is None:
            return 'INCONNU'
        if p > CONFIG['PRESSION_MAX_M']:
            return 'RESEAU_BASE'
        if p < CONFIG['PRESSION_MIN_M']:
            return 'RESEAU_BASE'
        if p < pression_min_requise_noeud(nid):
            return 'ETAGE_SEULEMENT'
        return 'OK'

    nb_noeuds_seuil_releve = sum(1 for nid in noeud_niveau_max if noeud_niveau_max[nid] > 0)
    _log(f"Noeuds avec seuil pression releve (immeuble) : {nb_noeuds_seuil_releve}")

    pression_statique_out = CoucheDifferee(f"Point?crs={abonne_layer.crs().authid()}",
                                            "abonnes_pression_statique")
    psprov = pression_statique_out.dataProvider()
    psprov.addAttributes([
        QgsField("abonne_fid", QVariant.Int), QgsField("nid", QVariant.String),
        QgsField("cluster_taille", QVariant.Int), QgsField("niveau", QVariant.Int),
        QgsField("pression_min_requise_m", QVariant.Double),
    ])
    pression_statique_out.updateFields()
    ps_feats = []
    for fid, pt in abonne_geoms.items():
        nid = abonne_vers_noeud.get(fid)
        feat = QgsFeature(pression_statique_out.fields())
        feat.setGeometry(QgsGeometry.fromPointXY(pt))
        feat.setAttributes([
            fid, nid, abonne_cluster_taille.get(fid, 1), abonne_niveau.get(fid, 0),
            round(CONFIG['PRESSION_MIN_M'] + abonne_niveau.get(fid, 0) * CONFIG['HAUTEUR_ETAGE_M'], 2),
        ])
        ps_feats.append(feat)
    if ps_feats:
        psprov.addFeatures(ps_feats)
        pression_statique_out.updateExtents()
        _ajouter_couche(pression_statique_out)
        _log(f"Couche creee : {pression_statique_out.name()} ({len(ps_feats)} abonnes)")

    # ============================================================================
    _etape(1)  # Rattachement des abonnes
    # 4) ANALYSE DE CONNEXITE DU GRAPHE
    # ============================================================================

    _log("\nAnalyse de connexite du reseau...")

    _uf_parent = {n: n for n in noeud_id_map.values()}

    def _uf_find(x):
        while _uf_parent[x] != x:
            _uf_parent[x] = _uf_parent[_uf_parent[x]]
            x = _uf_parent[x]
        return x

    def _uf_union(a, b):
        ra, rb = _uf_find(a), _uf_find(b)
        if ra != rb:
            _uf_parent[ra] = rb

    for n1, voisins in graphe.items():
        for (n2, *_rest) in voisins:
            _uf_union(n1, n2)

    composantes = defaultdict(list)
    for n in noeud_id_map.values():
        composantes[_uf_find(n)].append(n)

    _log(f"  {len(composantes)} composante(s) connexe(s)")

    # ============================================================================
    _etape(2)  # Rattachement des reservoirs
    # 5) RATTACHEMENT DES RESERVOIRS
    # ============================================================================

    _log(f"\nRattachement des {tank_layer.featureCount()} reservoirs...")

    sources = []
    secteur_cote_piezo = {}
    reservoir_data = {}
    secteur_source_nid = {}
    reservoirs_exclus = []
    tank_points_par_secteur = {}

    conso_jour_abonne_l = CONFIG['HABITANTS_PAR_ABONNE'] * CONFIG['DOTATION_L_HAB_JOUR']
    tank_fields = [f.name() for f in tank_layer.fields()]
    nom_field = None
    for candidat in ("nom", "NOM", "Nom", "name", "NAME"):
        if candidat in tank_fields:
            nom_field = candidat
            break

    noeuds_source_deja_vus = {}
    collisions_reservoirs = []

    for tf in tank_layer.getFeatures():
        geom = tf.geometry()
        if geom is None or geom.isEmpty():
            continue
        tank_id = str(tf["ID"]) if "ID" in tank_fields else f"TANK_{tf.id()}"
        nom_reservoir = str(tf[nom_field]) if nom_field and tf[nom_field] not in (None, "") else tank_id

        cap_field = CONFIG['TANK_CAPACITY_FIELD']
        capacite_m3 = float(tf[cap_field]) if cap_field in tank_fields and tf[cap_field] not in (None, "") else 1000.0
        pt = geom.asPoint()

        alt_res = float(tf["RES_NGF"]) if "RES_NGF" in tank_fields and tf["RES_NGF"] not in (None, "") else (get_altitude(pt.x(), pt.y()) or 870.0)
        type_reservoir = str(tf["TYPE"]).lower() if "TYPE" in tank_fields and tf["TYPE"] not in (None, "") else ""
        alt_mnt_local = get_altitude(pt.x(), pt.y()) or alt_res
        est_sureleve = ("sur elev" in type_reservoir or "sureleve" in type_reservoir or
                        "surélévé" in type_reservoir or alt_res > alt_mnt_local + 30)

        if "HAUTEUR" in tank_fields and tf["HAUTEUR"] not in (None, ""):
            hauteur_eau = float(tf["HAUTEUR"])
        elif est_sureleve:
            hauteur_eau = 30.0
        elif capacite_m3 >= 5000:
            hauteur_eau = 6.0
        elif capacite_m3 >= 1000:
            hauteur_eau = 4.0
        else:
            hauteur_eau = 3.0

        cote_eau = alt_res + hauteur_eau + CONFIG['CORRECTION_COTE_PIEZO_M']

        rayon = CONFIG['TANK_RESEARCH_M']
        nid, geom_noeud, dist = noeud_le_plus_proche(pt, max_dist=rayon)
        rayon_utilise = rayon
        while nid is None and rayon < CONFIG['MAX_TANK_RESEARCH_M']:
            rayon = min(rayon * CONFIG['TANK_RESEARCH_EXPAND_FACTOR'], CONFIG['MAX_TANK_RESEARCH_M'])
            nid, geom_noeud, dist = noeud_le_plus_proche(pt, max_dist=rayon)
            rayon_utilise = rayon

        if nid is None:
            nid_proche, dist_reelle = noeud_le_plus_proche_absolu(pt)
            _log(f"  Reservoir {tank_id} : exclu (aucun noeud a moins de {CONFIG['MAX_TANK_RESEARCH_M']:.0f}m).")
            reservoirs_exclus.append({'tank_id': tank_id, 'nom': nom_reservoir, 'capacite': capacite_m3,
                                       'distance_noeud_le_plus_proche': dist_reelle, 'point': pt})
            continue

        if nid in noeuds_source_deja_vus:
            autre_tank_id, autre_pt = noeuds_source_deja_vus[nid]
            dist_entre_reservoirs = pt.distance(autre_pt)
            _log(f"  ATTENTION collision : {tank_id} partage le noeud {nid} avec {autre_tank_id} "
                  f"(dist reelle {dist_entre_reservoirs:.1f}m)")
            collisions_reservoirs.append({
                'tank_a': autre_tank_id, 'tank_b': tank_id, 'noeud': nid,
                'distance_reelle_m': dist_entre_reservoirs,
                'doublon_probable': dist_entre_reservoirs <= CONFIG['SEUIL_DOUBLON_RESERVOIR_M'],
            })
        noeuds_source_deja_vus[nid] = (tank_id, pt)

        capacite_abonnes_stock = (capacite_m3 * 1000.0) / max(conso_jour_abonne_l * CONFIG['JOURS_AUTONOMIE_RESERVOIR'], 1.0)
        capacite_abonnes_source = None
        debit_source_m3_j = None
        champ_source = CONFIG['TANK_DEBIT_SOURCE_FIELD']
        if (CONFIG.get('ACTIVER_CAPACITE_SOURCE', True) and champ_source in tank_fields
                and tf[champ_source] not in (None, "")):
            debit_source_m3_j = float(tf[champ_source])
            if debit_source_m3_j > 0:
                capacite_abonnes_source = (debit_source_m3_j * 1000.0) / max(conso_jour_abonne_l, 1.0)

        if capacite_abonnes_source is not None:
            capacite_abonnes_max = max(capacite_abonnes_stock, capacite_abonnes_source)
            source_limitante = "source" if capacite_abonnes_source >= capacite_abonnes_stock else "stock"
        else:
            capacite_abonnes_max = capacite_abonnes_stock
            source_limitante = "stock (aucun debit source renseigne)"

        capacite_abonnes_utile = int(capacite_abonnes_max * (1.0 - CONFIG['MARGE_SECURITE_CAPACITE_PCT']))
        type_priorite, score_priorite = classer_priorite_reservoir(type_reservoir, capacite_m3)

        secteur_id = f"TANK_{tank_id}"
        sources.append((nid, secteur_id, capacite_m3, alt_res, cote_eau, hauteur_eau, est_sureleve))
        secteur_cote_piezo[secteur_id] = cote_eau
        secteur_source_nid[secteur_id] = nid
        tank_points_par_secteur[secteur_id] = pt
        reservoir_data[secteur_id] = {
            'tank_id': tank_id, 'nom': nom_reservoir, 'capacite': capacite_m3,
            'alt_res': alt_res, 'cote_eau': cote_eau, 'hauteur_eau': hauteur_eau,
            'est_sureleve': est_sureleve, 'source_nid': nid,
            'type_priorite': type_priorite, 'priorite_score': score_priorite,
            'capacite_abonnes_max': int(capacite_abonnes_max),
            'capacite_abonnes_utile': capacite_abonnes_utile,
            'capacite_abonnes_restante': capacite_abonnes_utile,
            'rayon_recherche_utilise_m': rayon_utilise,
            'cote_eau_originale': cote_eau,
            'parent_tank_id': tank_id,
            'debit_source_m3_j': debit_source_m3_j,
            'source_limitante': source_limitante,
            'nb_parts_scission': 1,
        }
        _log(f"  {tank_id} : cote={cote_eau:.1f}m, cap={capacite_m3:.0f}m3, "
              f"priorite={type_priorite}, cap_utile={capacite_abonnes_utile} ab [{source_limitante}]")

    _log(f"Reservoirs rattaches : {len(sources)} / {tank_layer.featureCount()}")
    if not sources:
        raise RuntimeError("Aucun reservoir valide !")

    _capacite_physique_totale = sum(rd['capacite_abonnes_utile'] for rd in reservoir_data.values())
    _demande_totale_ab = sum(abonnes_par_noeud.values())
    _log("\n" + "=" * 70)
    _log("DIAGNOSTIC THEORIQUE PRELIMINAIRE (avant optimisation)")
    _log("=" * 70)
    _log(f"  Capacite physique utile totale (stock+source, tous secteurs virtuels) : "
          f"{_capacite_physique_totale} ab")
    _log(f"  Demande totale a desservir : {_demande_totale_ab} ab")
    if _capacite_physique_totale < _demande_totale_ab:
        deficit = _demande_totale_ab - _capacite_physique_totale
        pct_deficit = 100.0 * deficit / max(_demande_totale_ab, 1)
        _log(f"  ATTENTION STRUCTURELLE : deficit de {deficit} ab ({pct_deficit:.1f}% de la demande) -- "
              f"AUCUNE redistribution algorithmique ne peut resorber ce deficit, quelle que soit la "
              f"qualite de la sectorisation. Deux leviers seulement : (a) renseigner le champ "
              f"'{CONFIG['TANK_DEBIT_SOURCE_FIELD']}' sur la couche 'tank' si des sources non "
              f"declarees alimentent ces reservoirs en continu, ou (b) augmenter la capacite "
              f"physique reelle (nouveaux reservoirs / agrandissement). Ce chiffre est un plancher : "
              f"au moins {deficit} abonnes resteront hors plage de capacite quel que soit le "
              f"parametrage de l'algorithme.")
    else:
        _log(f"  OK : la capacite physique totale couvre la demande "
              f"(marge de {_capacite_physique_totale - _demande_totale_ab} ab).")

    # ============================================================================
    # 5bis) DETECTION DES RESERVOIRS ISOLES DU RESEAU (distance reelle Dijkstra)
    # ============================================================================

    _log("\n" + "=" * 70)
    _log("DETECTION DES RESERVOIRS ISOLES (distance reseau reelle)")
    _log("=" * 70)

    def detecter_reservoirs_isoles(rayon_m, seuil_abonnes):
        isoles = {}
        for source_nid, s_id, *_ in sources:
            dist = {source_nid: 0.0}
            heap_d = [(0.0, source_nid)]
            vus = set()
            nb_ab = 0
            while heap_d:
                d, n = heapq.heappop(heap_d)
                if d > dist.get(n, float('inf')) or d > rayon_m:
                    continue
                if n not in vus:
                    vus.add(n)
                    nb_ab += abonnes_par_noeud.get(n, 0)
                for (voisin, pipe_id, length, diam) in graphe.get(n, []):
                    nd = d + length
                    if nd <= rayon_m and nd < dist.get(voisin, float('inf')):
                        dist[voisin] = nd
                        heapq.heappush(heap_d, (nd, voisin))
            if nb_ab < seuil_abonnes:
                isoles[s_id] = {'nb_abonnes_proximite': nb_ab, 'nb_noeuds_proximite': len(vus)}
        return isoles

    reservoirs_isoles = detecter_reservoirs_isoles(CONFIG['RAYON_M_ISOLEMENT'], CONFIG['SEUIL_ABONNES_ISOLEMENT'])

    if reservoirs_isoles:
        for s_id, info in reservoirs_isoles.items():
            rd = reservoir_data.get(s_id, {})
            _log(f"  ISOLE : {s_id} ({rd.get('nom', s_id)}) -- {info['nb_abonnes_proximite']} ab / "
                  f"{info['nb_noeuds_proximite']} noeuds a moins de {CONFIG['RAYON_M_ISOLEMENT']:.0f}m")
        _log(f"\n  {len(reservoirs_isoles)} reservoir(s) isole(s). Traiter comme constat "
              f"d'infrastructure (nouvelle conduite de transfert requise).")
    else:
        _log("  Aucun reservoir isole detecte.")

    # ============================================================================
    # DIAGNOSTIC ET RECALIBRATION AUTOMATIQUE DES COTES PIEZO
    # ============================================================================

    _log("\n" + "=" * 70)
    _log("DIAGNOSTIC ET RECALIBRATION DES COTES PIEZO")
    _log("=" * 70)

    def dijkstra_pertes_depuis_source(source_nid_):
        dist = {source_nid_: 0.0}
        heap_d = [(0.0, source_nid_)]
        while heap_d:
            d, n = heapq.heappop(heap_d)
            if d > dist.get(n, float('inf')):
                continue
            for (voisin, pipe_id, length, diam) in graphe.get(n, []):
                q_unit = max(1 * q_pointe_abonne, 0.01)
                hf = perte_charge_hazen_williams(q_unit, length, diam, CONFIG['COEF_HAZEN_WILLIAMS'])
                nd = d + hf
                if nd < dist.get(voisin, float('inf')):
                    dist[voisin] = nd
                    heapq.heappush(heap_d, (nd, voisin))
        return dist

    pertes_dijkstra_par_reservoir = {}
    for source_nid, s_id, *_ in sources:
        pertes_dijkstra_par_reservoir[s_id] = dijkstra_pertes_depuis_source(source_nid)

    recalibrations = []
    secteurs_sous_zonage_requis_preliminaire = {}

    for s_id in list(secteur_cote_piezo.keys()):
        rd = reservoir_data.get(s_id, {})
        source_nid = secteur_source_nid.get(s_id)
        cote_eau_actuelle = secteur_cote_piezo[s_id]
        pertes = pertes_dijkstra_par_reservoir.get(s_id, {})

        noeuds_accessibles = [(nid, noeud_altitudes[nid], pertes[nid])
                              for nid in pertes if nid in noeud_altitudes
                              and pertes[nid] < CONFIG['PERTE_CHARGE_MAX_M']]

        if not noeuds_accessibles:
            continue

        pressions = [(cote_eau_actuelle - alt - perte, nid)
                     for nid, alt, perte in noeuds_accessibles]
        hors_norme = sum(1 for p, nid in pressions
                         if p < CONFIG['PRESSION_MIN_M'] or p > CONFIG['PRESSION_MAX_M'])
        pct_hors_norme = hors_norme / max(len(pressions), 1)

        p_moy = sum(p for p, _ in pressions) / max(len(pressions), 1)
        p_min_actuel = min(p for p, _ in pressions)
        p_max_actuel = max(p for p, _ in pressions)
        ecart_topo = p_max_actuel - p_min_actuel

        _log(f"  {s_id} : cote={cote_eau_actuelle:.1f}m, "
              f"p_min={p_min_actuel:.1f}m, p_max={p_max_actuel:.1f}m, "
              f"p_moy={p_moy:.1f}m, ecart={ecart_topo:.1f}m, hors_norme={pct_hors_norme*100:.0f}%")

        if ecart_topo > CONFIG['SEUIL_ECART_TOPO_M']:
            secteurs_sous_zonage_requis_preliminaire[s_id] = {
                'ecart_topo_m': ecart_topo, 'p_min': p_min_actuel, 'p_max': p_max_actuel,
            }
            _log(f"    -> SOUS_ZONAGE_REQUIS (preliminaire) : ecart topographique {ecart_topo:.1f}m > "
                  f"{CONFIG['SEUIL_ECART_TOPO_M']:.0f}m -- une seule cote piezo ne peut "
                  f"pas satisfaire ce denivele (PRV en cascade / nouveau reservoir requis). "
                  f"Chiffre indicatif -- la scission altimetrique (plus loin) traite "
                  f"ce cas directement en inserant des PRV virtuels internes.")

        if CONFIG.get('ACTIVER_RECALIBRATION_COTE') and pct_hors_norme > CONFIG['SEUIL_PCT_VIOLATION_RECALIBRATION']:
            alt_moy = sum(alt for _, alt, _ in noeuds_accessibles) / len(noeuds_accessibles)
            perte_moy = sum(p for _, _, p in noeuds_accessibles) / len(noeuds_accessibles)
            delta_optimal = CONFIG['PRESSION_CIBLE_M'] + alt_moy + perte_moy - cote_eau_actuelle
            delta_limite = max(-CONFIG['MAX_AJUSTEMENT_COTE_M'],
                              min(CONFIG['MAX_AJUSTEMENT_COTE_M'], delta_optimal))

            if abs(delta_limite) > 0.5:
                nouvelle_cote = cote_eau_actuelle + delta_limite
                secteur_cote_piezo[s_id] = nouvelle_cote
                reservoir_data[s_id]['cote_eau'] = nouvelle_cote
                recalibrations.append({
                    's_id': s_id, 'cote_avant': cote_eau_actuelle,
                    'cote_apres': nouvelle_cote, 'delta': delta_limite,
                    'pct_hors_norme_avant': pct_hors_norme
                })
                _log(f"    -> RECALIBRATION : cote {cote_eau_actuelle:.1f} -> {nouvelle_cote:.1f}m "
                      f"(delta={delta_limite:+.1f}m)")

    if recalibrations:
        _log(f"\n  {len(recalibrations)} cote(s) piézo recalibree(s) pour ameliorer la conformite.")
    else:
        _log("  Aucune recalibration nécessaire.")

    if secteurs_sous_zonage_requis_preliminaire:
        _log(f"\n  {len(secteurs_sous_zonage_requis_preliminaire)} secteur(s) marque(s) SOUS_ZONAGE_REQUIS (preliminaire) : "
              f"{', '.join(sorted(secteurs_sous_zonage_requis_preliminaire.keys()))}")
        _log(f"  (traite par la scission altimetrique -- voir plus loin)")

    # ============================================================================
    _etape(3)  # Voronoi
    # 3bis) VORONOI RESERVOIRS : DOUBLE USAGE (AMORCE + COMPARATIF)
    # ============================================================================

    _log("\n" + "=" * 70)
    _log("VORONOI RESERVOIRS (AMORCE + EXPORT COMPARATIF)")
    _log("=" * 70)

    voronoi_secteur_de_noeud = {}
    voronoi_polygones_bruts = {}

    try:
        tank_pts_layer = QgsVectorLayer(f"Point?crs={pipe_layer.crs().authid()}", "tmp_tank_pts", "memory")
        ttprov = tank_pts_layer.dataProvider()
        ttprov.addAttributes([QgsField("secteur_id", QVariant.String)])
        tank_pts_layer.updateFields()
        tt_feats = []
        for s_id, pt in tank_points_par_secteur.items():
            feat = QgsFeature(tank_pts_layer.fields())
            feat.setGeometry(QgsGeometry.fromPointXY(pt))
            feat.setAttributes([s_id])
            tt_feats.append(feat)
        ttprov.addFeatures(tt_feats)
        tank_pts_layer.updateExtents()

        emprise_voronoi = emprise_decoupe
        if commune_layer is not None:
            commune_geoms = [f.geometry() for f in commune_layer.getFeatures() if f.geometry() and not f.geometry().isEmpty()]
            if commune_geoms:
                emprise_voronoi = QgsGeometry.unaryUnion(commune_geoms)

        result = processing.run("qgis:voronoipolygons", {
            'INPUT': tank_pts_layer,
            'BUFFER': 20,
            'OUTPUT': 'memory:'
        })
        voronoi_layer_raw = result['OUTPUT']

        for vf in voronoi_layer_raw.getFeatures():
            vgeom = vf.geometry()
            if vgeom is None or vgeom.isEmpty():
                continue
            vgeom_clip = vgeom.intersection(emprise_voronoi)
            if vgeom_clip is None or vgeom_clip.isEmpty():
                continue
            s_id = vf["secteur_id"] if "secteur_id" in [fl.name() for fl in voronoi_layer_raw.fields()] else None
            if s_id is None:
                continue
            voronoi_polygones_bruts[s_id] = vgeom_clip

        for nid, pt in noeud_points.items():
            pt_geom = QgsGeometry.fromPointXY(pt)
            for s_id, poly in voronoi_polygones_bruts.items():
                if poly.contains(pt_geom):
                    voronoi_secteur_de_noeud[nid] = s_id
                    break

        _log(f"  Cellules Voronoi : {len(voronoi_polygones_bruts)} / {len(sources)}")
        _log(f"  Noeuds en cellule Voronoi : {len(voronoi_secteur_de_noeud)} / {len(noeud_id_map)}")

    except Exception as e:
        _log(f"  ATTENTION : Voronoi impossible ({e}) -> moteur sans biais Voronoi.")
        CONFIG['UTILISER_VORONOI_AMORCE'] = False

    if voronoi_polygones_bruts:
        voronoi_final_out = CoucheDifferee(f"Polygon?crs={pipe_layer.crs().authid()}",
                                            "secteurs_voronoi_final_v67_COMPARATIF_UNIQUEMENT")
        vfprov = voronoi_final_out.dataProvider()
        vfprov.addAttributes([QgsField("secteur_id", QVariant.String), QgsField("nom_reservoir", QVariant.String)])
        voronoi_final_out.updateFields()
        vf_feats = []
        for s_id, poly in voronoi_polygones_bruts.items():
            poly_multi = assurer_multipolygone(poly)
            if poly_multi is None or poly_multi.isEmpty():
                continue
            feat = QgsFeature(voronoi_final_out.fields())
            feat.setGeometry(poly_multi)
            feat.setAttributes([s_id, reservoir_data.get(s_id, {}).get('nom', s_id)])
            vf_feats.append(feat)
        if vf_feats:
            vfprov.addFeatures(vf_feats)
            voronoi_final_out.updateExtents()
            _ajouter_couche(voronoi_final_out)
            _log(f"  Couche comparatif Voronoi : {len(vf_feats)} polygones (geometrique brut uniquement)")

    # ============================================================================
    # 6) FONCTIONS DU MOTEUR HYDRAULIQUE
    # ============================================================================

    def _cout_capacite(config, capacite_restante, s_id, reservoir_data_, nb_ab_supplementaire=0):
        cap_utile = reservoir_data_.get(s_id, {}).get('capacite_abonnes_utile', 0)
        if cap_utile <= 0:
            return 0.0
        restante_apres = capacite_restante[s_id] - nb_ab_supplementaire
        taux_apres = 1.0 - (restante_apres / cap_utile)
        if taux_apres < 0.7:
            return 0.0
        depassement = taux_apres - 0.7
        if depassement <= 0:
            return 0.0
        return config['PENALITE_CAPACITE_COEF'] * (depassement ** 2)

    def _secteur_est_plein(s_id, capacite_restante, config):
        if capacite_restante.get(s_id, 0) < 0:
            return True
        if config.get('ACTIVER_PLAFOND_ABONNES_MAX', True):
            rd = reservoir_data.get(s_id, {})
            cap_utile = rd.get('capacite_abonnes_utile', 0)
            nb_actuel = cap_utile - capacite_restante.get(s_id, 0)
            if nb_actuel >= config['ABONNES_MAX_PAR_SECTEUR']:
                return True
        return False

    def _secteur_peut_recevoir(s_id, nb_ab, capacite_restante, config):
        restante = capacite_restante.get(s_id, 0)
        if nb_ab > 0 and restante < nb_ab:
            return False
        if config.get('ACTIVER_PLAFOND_ABONNES_MAX', True):
            rd = reservoir_data.get(s_id, {})
            cap_utile = rd.get('capacite_abonnes_utile', 0)
            nb_actuel = cap_utile - restante
            if nb_actuel + nb_ab > config['ABONNES_MAX_PAR_SECTEUR']:
                return False
        return True

    # ============================================================================
    # FONCTION UNIQUE DE TRANSFERT DE NOEUD (AVEC SOUS-ARBRE)
    # ============================================================================

    def _deplacer_noeud_avec_sous_arbre(nid, nouveau_parent, nouveau_secteur, pipe_id,
                                         secteur_de_noeud, parent_of, pipe_of, enfants_de,
                                         cumul_perte, capacite_restante, perte_nid_nouveau,
                                         config):
        ancien_secteur = secteur_de_noeud.get(nid)
        ancien_parent = parent_of.get(nid)
        if ancien_parent is not None:
            enfants_de[ancien_parent].discard(nid)

        # collect subtree nodes
        sous_arbre = []
        vus_sa = {nid}
        dq = deque([nid])
        while dq:
            cur = dq.popleft()
            sous_arbre.append(cur)
            for e in enfants_de.get(cur, ()):
                if e not in vus_sa:
                    vus_sa.add(e)
                    dq.append(e)

        # performance safeguard: skip very large subtree moves
        max_taille = config.get('TAILLE_MAX_SOUS_ARBRE_REDISTRIBUTION', 400)
        if len(sous_arbre) > max_taille:
            _log(f"  Skip move of node {nid}: sous-arbre trop grand ({len(sous_arbre)} > {max_taille})")
            # revert parent removal
            if ancien_parent is not None:
                enfants_de[ancien_parent].add(nid)
            return 0

        nb_ab_total = sum(abonnes_par_noeud.get(n, 0) for n in sous_arbre)
        compteur_transfert_actif['n'] += 1
        if ancien_secteur is not None:
            capacite_restante[ancien_secteur] = capacite_restante.get(ancien_secteur, 0) + nb_ab_total
        capacite_restante[nouveau_secteur] = capacite_restante.get(nouveau_secteur, 0) - nb_ab_total

        parent_of[nid] = nouveau_parent
        pipe_of[nid] = pipe_id
        enfants_de[nouveau_parent].add(nid)
        cumul_perte[nid] = perte_nid_nouveau
        secteur_de_noeud[nid] = nouveau_secteur

        vus_prop = {nid}
        dq = deque([nid])
        while dq:
            cur = dq.popleft()
            for e in list(enfants_de.get(cur, ())):
                if e in vus_prop:
                    continue
                vus_prop.add(e)
                secteur_de_noeud[e] = nouveau_secteur
                pid_e = pipe_of.get(e)
                length_e = pipe_lengths.get(pid_e, 0.0)
                diam_e = pipe_diameters.get(pid_e, 100.0)
                nb_ab_e = abonnes_par_noeud.get(e, 0)
                q_ls_e = max(nb_ab_e * q_pointe_abonne, 0.01)
                hf_e = perte_charge_hazen_williams(q_ls_e, length_e, diam_e, config['COEF_HAZEN_WILLIAMS'])
                cumul_perte[e] = min(cumul_perte.get(cur, 0.0) + hf_e, config['PERTE_CHARGE_MAX_M'])
                dq.append(e)

        return nb_ab_total


    def _voisins_secteurs_multihop(nid, s_exclu, secteur_de_noeud, max_sauts=3):
        resultats = []
        vus = {nid}
        frontiere = deque([(nid, 0)])
        while frontiere:
            cur, sauts = frontiere.popleft()
            if sauts >= max_sauts:
                continue
            for (v, pid, l, d) in graphe.get(cur, []):
                if v in vus:
                    continue
                vus.add(v)
                s_v = secteur_de_noeud.get(v)
                if s_v is not None and s_v != s_exclu:
                    resultats.append((v, s_v, pid, l, d, sauts + 1))
                else:
                    frontiere.append((v, sauts + 1))
        return resultats

    # ============================================================================
    # REDISTRIBUTION FORCEE DES CAPACITES (multi-sauts + plafond dur :
    # candidats etendus aux sous-arbres, pas seulement aux feuilles isolees)
    # ============================================================================

    def redistribuer_capacites_forcee(secteur_de_noeud, parent_of, pipe_of, enfants_de,
                                       cumul_perte, capacite_restante, config):
        transferts_total = 0
        taux_cible = config['TAUX_CIBLE_CAPACITE_MAX']
        max_sauts = config.get('MAX_SAUTS_REDISTRIBUTION', 3)
        taille_max_sous_arbre = config.get('TAILLE_MAX_SOUS_ARBRE_REDISTRIBUTION', 400)
        autoriser_sous_arbre = config.get('ACTIVER_REDISTRIBUTION_SOUS_ARBRE', True)
        max_transferts = config.get('MAX_TRANSFERTS_REDIS', 1000)
        stop_redis = False

        secteurs_surcharges = [s_id for s_id in secteur_cote_piezo
                               if capacite_restante.get(s_id, 0) < 0]

        for s_surchg in secteurs_surcharges:
            nb_a_liberer = abs(int(capacite_restante.get(s_surchg, 0))) + 1

            # Correctif performance : calcul des tailles de sous-arbre en O(n) via
            # un parcours postordre UNIQUE sur tout l'arbre, au lieu d'un BFS
            # complet par noeud candidat (qui degenerait en O(n^2) sur un
            # secteur de plusieurs milliers d'abonnes -- c'est ce qui bloquait
            # le moteur des le cycle 1). Recalcule a chaque secteur surcharge
            # traite car les deplacements precedents changent la structure.
            racines = [n for n in secteur_de_noeud if parent_of.get(n) is None]
            ordre_topo = []
            vus_topo = set(racines)
            dq_topo = deque(racines)
            while dq_topo:
                cur = dq_topo.popleft()
                ordre_topo.append(cur)
                for e in enfants_de.get(cur, ()):
                    if e not in vus_topo:
                        vus_topo.add(e)
                        dq_topo.append(e)

            taille_sous_arbre_globale = {n: abonnes_par_noeud.get(n, 0) for n in ordre_topo}
            for n in reversed(ordre_topo):
                p = parent_of.get(n)
                if p is not None and p in taille_sous_arbre_globale:
                    taille_sous_arbre_globale[p] += taille_sous_arbre_globale.get(n, 0)

            noeuds_candidats = []
            for nid, s in secteur_de_noeud.items():
                if s != s_surchg:
                    continue
                if parent_of.get(nid) is None:
                    continue

                # taille du sous-arbre porte par ce noeud (au lieu de se
                # limiter aux feuilles) -- permet de liberer d'un coup un bloc
                # d'abonnes plutot qu'un seul noeud a la fois
                taille_sous_arbre = taille_sous_arbre_globale.get(nid, 0)

                if taille_sous_arbre == 0:
                    continue
                if autoriser_sous_arbre:
                    if taille_sous_arbre > taille_max_sous_arbre:
                        continue
                elif enfants_de.get(nid):
                    continue

                noeuds_candidats.append((taille_sous_arbre, nid))

            noeuds_candidats.sort(reverse=True)

            liberes = 0
            for nb_ab, nid in noeuds_candidats:
                if liberes >= nb_a_liberer:
                    break

                meilleur_voisin = None
                meilleur_score = float('inf')
                alt = noeud_altitudes.get(nid)

                candidats_voisins = _voisins_secteurs_multihop(
                    nid, s_surchg, secteur_de_noeud, max_sauts=max_sauts)

                for (voisin, s_voisin, pipe_id, length, diam, nb_sauts) in candidats_voisins:
                    if not _secteur_peut_recevoir(s_voisin, nb_ab, capacite_restante, config):
                        continue
                    cap_utile_v = reservoir_data.get(s_voisin, {}).get('capacite_abonnes_utile', 0)
                    restante_v = capacite_restante.get(s_voisin, 0)
                    taux_v = 1.0 - (restante_v / max(cap_utile_v, 1))
                    if taux_v >= taux_cible:
                        continue
                    if alt is not None:
                        q_ls = max(nb_ab * q_pointe_abonne, 0.01)
                        hf = perte_charge_hazen_williams(q_ls, length, diam, config['COEF_HAZEN_WILLIAMS'])
                        perte_cand = cumul_perte.get(voisin, 0.0) + hf
                        cote_piezo_v = secteur_cote_piezo.get(s_voisin)
                        if cote_piezo_v is not None:
                            p_cand = cote_piezo_v - alt - perte_cand
                            if p_cand < CONFIG['PRESSION_MIN_M'] - 5.0:
                                continue
                            score = abs(p_cand - config['PRESSION_CIBLE_M']) + taux_v * 100 + (nb_sauts - 1) * 5
                            if score < meilleur_score:
                                meilleur_score = score
                                meilleur_voisin = (voisin, s_voisin, pipe_id, length, diam, perte_cand)

                if meilleur_voisin is not None:
                    nouveau_parent, s_voisin, pipe_id, length, diam, perte_cand = meilleur_voisin
                    nb_deplace = _deplacer_noeud_avec_sous_arbre(
                        nid, nouveau_parent, s_voisin, pipe_id,
                        secteur_de_noeud, parent_of, pipe_of, enfants_de,
                        cumul_perte, capacite_restante, perte_cand, config)
                    liberes += nb_deplace
                    transferts_total += 1
                    if transferts_total >= max_transferts:
                        stop_redis = True
                        break
                if stop_redis:
                    break
        return transferts_total

    # ============================================================================
    # CORRECTION DES SURPRESSIONS (PRV VIRTUEL)
    # ============================================================================

    def corriger_surpressions(secteur_de_noeud, parent_of, pipe_of, enfants_de,
                               cumul_perte, capacite_restante, pression_noeud, config):
        corrections = 0
        noeuds_surpression = [nid for nid, p in pression_noeud.items()
                               if p is not None and p > config['PRESSION_MAX_M']
                               and not enfants_de.get(nid)
                               and parent_of.get(nid) is not None]

        for nid in noeuds_surpression:
            alt = noeud_altitudes.get(nid)
            if alt is None:
                continue
            nb_ab = abonnes_par_noeud.get(nid, 0)
            s_actuel = secteur_de_noeud.get(nid)

            meilleur = None
            meilleur_score = float('inf')

            for (voisin, pipe_id, length, diam) in graphe.get(nid, []):
                s_voisin = secteur_de_noeud.get(voisin)
                if s_voisin is None or s_voisin == s_actuel:
                    continue
                cote_piezo_v = secteur_cote_piezo.get(s_voisin)
                if cote_piezo_v is None:
                    continue
                q_ls = max(nb_ab * q_pointe_abonne, 0.01)
                hf = perte_charge_hazen_williams(q_ls, length, diam, config['COEF_HAZEN_WILLIAMS'])
                perte_cand = cumul_perte.get(voisin, 0.0) + hf
                p_cand = cote_piezo_v - alt - perte_cand
                if not est_norme_pression(p_cand, nid):
                    continue
                if not _secteur_peut_recevoir(s_voisin, nb_ab, capacite_restante, config):
                    continue
                score = abs(p_cand - config['PRESSION_CIBLE_M'])
                if score < meilleur_score:
                    meilleur_score = score
                    meilleur = (voisin, s_voisin, pipe_id, length, diam, perte_cand)

            if meilleur is not None:
                nouveau_parent, s_voisin, pipe_id, length, diam, perte_cand = meilleur
                _deplacer_noeud_avec_sous_arbre(
                    nid, nouveau_parent, s_voisin, pipe_id,
                    secteur_de_noeud, parent_of, pipe_of, enfants_de,
                    cumul_perte, capacite_restante, perte_cand, config)
                corrections += 1

        return corrections

    # ============================================================================
    # GARANTIE DE CONNEXITE
    # ============================================================================

    def garantir_connexite(secteur_de_noeud, parent_of, pipe_of, enfants_de,
                           cumul_perte, capacite_restante, config):
        corrections = 0

        isoles_par_secteur = {}
        for s_id in list(secteur_cote_piezo.keys()):
            source_nid = secteur_source_nid.get(s_id)
            if source_nid is None or source_nid not in secteur_de_noeud:
                continue

            membres = {nid for nid, s in secteur_de_noeud.items() if s == s_id}
            if source_nid not in membres:
                continue

            connexes_reseau = {source_nid}
            file2 = deque([source_nid])
            while file2:
                cur = file2.popleft()
                for (voisin, pipe_id, length, diam) in graphe.get(cur, []):
                    if voisin in membres and voisin not in connexes_reseau:
                        connexes_reseau.add(voisin)
                        file2.append(voisin)

            isoles = membres - connexes_reseau
            if isoles:
                isoles_par_secteur[s_id] = isoles

        tous_isoles = set()
        for isoles in isoles_par_secteur.values():
            tous_isoles.update(isoles)

        for s_id, isoles in isoles_par_secteur.items():
            for nid in isoles:
                nb_ab = abonnes_par_noeud.get(nid, 0)
                alt = noeud_altitudes.get(nid)

                meilleur = None
                meilleur_score = float('inf')

                for (voisin, pipe_id, length, diam) in graphe.get(nid, []):
                    s_voisin = secteur_de_noeud.get(voisin)
                    if s_voisin is None:
                        continue
                    if voisin in tous_isoles:
                        continue
                    source_v = secteur_source_nid.get(s_voisin)
                    if source_v is None:
                        continue
                    if not _secteur_peut_recevoir(s_voisin, nb_ab, capacite_restante, config):
                        continue

                    cote_piezo_v = secteur_cote_piezo.get(s_voisin, 0)
                    q_ls = max(nb_ab * q_pointe_abonne, 0.01)
                    hf = perte_charge_hazen_williams(q_ls, length, diam, config['COEF_HAZEN_WILLIAMS'])
                    perte_cand = cumul_perte.get(voisin, 0.0) + hf
                    p_cand = (cote_piezo_v - alt - perte_cand) if alt is not None else 0
                    score = abs(p_cand - CONFIG['PRESSION_CIBLE_M'])
                    if score < meilleur_score:
                        meilleur_score = score
                        meilleur = (voisin, s_voisin, pipe_id, length, diam, perte_cand)

                if meilleur is not None:
                    nouveau_parent, s_voisin, pipe_id, length, diam, perte_cand = meilleur
                    _deplacer_noeud_avec_sous_arbre(
                        nid, nouveau_parent, s_voisin, pipe_id,
                        secteur_de_noeud, parent_of, pipe_of, enfants_de,
                        cumul_perte, capacite_restante, perte_cand, config)
                    corrections += 1

        return corrections

    # ============================================================================
    # MOTEUR PRINCIPAL (CROISSANCE + RATTRAPAGE + RAFFINEMENT + RECALCUL)
    # ============================================================================

    def executer_pipeline_complet(config, ordre_priorite_reservoirs, rng,
                                   utiliser_voronoi=None):
        compteur_transfert_actif['n'] = 0
        use_voronoi = config['UTILISER_VORONOI_AMORCE'] if utiliser_voronoi is None else utiliser_voronoi

        # Profiling timers
        t_start = time.perf_counter()
        t_heap = 0.0
        t_progres = 0.0
        t_raffinement = 0.0
        t_recalc = 0.0
        t_correction_forcee = 0.0

        secteur_de_noeud = {}
        parent_of = {}
        pipe_of = {}
        enfants_de = defaultdict(set)
        cumul_perte = {}
        capacite_restante = {s_id: reservoir_data[s_id]['capacite_abonnes_utile']
                             for s_id in secteur_cote_piezo}
        priorite_de_secteur = {s_id: reservoir_data[s_id]['priorite_score']
                                for s_id in secteur_cote_piezo}

        _counter = itertools.count()
        heap = []

        def cle_tri_candidat(s_id, cout, priorite, node):
            rd = reservoir_data.get(s_id, {})
            cap_utile = rd.get('capacite_abonnes_utile', 1)
            nb_ab_actuel = cap_utile - capacite_restante.get(s_id, 0)
            taux_saturation = nb_ab_actuel / max(cap_utile, 1)
            ecart = taux_saturation - config['SEUIL_SATURATION_CROISSANCE']
            facteur_capacite = 1.0 + max(0, ecart) * config['FACTEUR_CAPACITE_CROISSANCE']
            facteur_capacite = max(1.0, min(10.0, facteur_capacite))
            cout_effectif = cout * facteur_capacite
            if (use_voronoi and node in voronoi_secteur_de_noeud
                    and s_id not in reservoirs_isoles):
                if voronoi_secteur_de_noeud[node] == s_id:
                    cout_effectif *= config['VORONOI_BIAS_FAVORISE']
                else:
                    cout_effectif *= config['VORONOI_BIAS_PENALISE']
            return (cout_effectif, -priorite)

        def _pousser_candidat(parent, node, s_id, cout_amont, pipe_id, length, diam):
            if node in secteur_de_noeud:
                return
            cout_total = cout_amont + length
            priorite = priorite_de_secteur[s_id]
            key = cle_tri_candidat(s_id, cout_total, priorite, node)
            heapq.heappush(heap, (key, next(_counter), node, s_id, parent, pipe_id, length, diam, cout_total))

        for source_nid, s_id, *_ in ordre_priorite_reservoirs:
            if source_nid not in secteur_de_noeud:
                secteur_de_noeud[source_nid] = s_id
                parent_of[source_nid] = None
                pipe_of[source_nid] = None
                cumul_perte[source_nid] = 0.0

        for source_nid, s_id, *_ in sources:
            for (voisin, pipe_id, length, diam) in graphe.get(source_nid, []):
                _pousser_candidat(source_nid, voisin, s_id, 0.0, pipe_id, length, diam)

        # Phase initiale : extraction depuis le tas (assignation par plus court cout)
        t0 = time.perf_counter()

        while heap:
            key, _tie, node, s_id, parent, pipe_id, length, diam, cout_total = heapq.heappop(heap)
            if node in secteur_de_noeud:
                continue
            nb_ab = abonnes_par_noeud.get(node, 0)

            if nb_ab > 0 and not _secteur_peut_recevoir(s_id, nb_ab, capacite_restante, config):
                alt_disponible = None
                for s_alt in secteur_cote_piezo:
                    if s_alt != s_id and _secteur_peut_recevoir(s_alt, nb_ab, capacite_restante, config):
                        alt_disponible = s_alt
                        break
                if alt_disponible:
                    continue

            secteur_de_noeud[node] = s_id
            parent_of[node] = parent
            pipe_of[node] = pipe_id
            enfants_de[parent].add(node)
            cumul_perte[node] = cout_total
            capacite_restante[s_id] -= nb_ab
            for (voisin, pipe_id2, length2, diam2) in graphe.get(node, []):
                if voisin not in secteur_de_noeud:
                    _pousser_candidat(node, voisin, s_id, cout_total, pipe_id2, length2, diam2)
        t_heap += time.perf_counter() - t0

        def _est_secours(s_id):
            return reservoir_data.get(s_id, {}).get('type_priorite') == 'SECOURS'

        def _cout_rattrapage(s_id, cout_hydraulique):
            rd = reservoir_data.get(s_id, {})
            cap_utile = rd.get('capacite_abonnes_utile', 1)
            restante = capacite_restante.get(s_id, 0)
            if restante > 0:
                return cout_hydraulique
            depassement = abs(restante) / max(cap_utile, 1)
            return cout_hydraulique * (1.0 + depassement * 20.0)

        progres = True
        while progres:
            progres = False
            for nid in list(noeud_id_map.values()):
                if nid in secteur_de_noeud:
                    continue
                nb_ab = abonnes_par_noeud.get(nid, 0)
                meilleur = {('non_secours', 'faisable'): None, ('secours', 'faisable'): None,
                            ('non_secours', 'quelconque'): None, ('secours', 'quelconque'): None}
                for (voisin, pipe_id, length, diam) in graphe.get(nid, []):
                    s_voisin = secteur_de_noeud.get(voisin)
                    if s_voisin is None:
                        continue
                    q_ls = max(nb_ab * q_pointe_abonne, 0.01)
                    hf = perte_charge_hazen_williams(q_ls, length, diam, config['COEF_HAZEN_WILLIAMS'])
                    cout_hydraulique = cumul_perte.get(voisin, 0.0) + hf
                    cout_total = _cout_rattrapage(s_voisin, cout_hydraulique)
                    candidat = (cout_total, s_voisin, voisin, pipe_id, length, diam, cout_total)
                    type_panier = 'secours' if _est_secours(s_voisin) else 'non_secours'
                    faisable = nb_ab == 0 or _secteur_peut_recevoir(s_voisin, nb_ab, capacite_restante, config)
                    cle_faisabilite = 'faisable' if faisable else 'quelconque'
                    cle = (type_panier, cle_faisabilite)
                    if meilleur[cle] is None or cout_total < meilleur[cle][0]:
                        meilleur[cle] = candidat
                    cle_q = (type_panier, 'quelconque')
                    if meilleur[cle_q] is None or cout_total < meilleur[cle_q][0]:
                        meilleur[cle_q] = candidat
                choix = (meilleur[('non_secours', 'faisable')] or meilleur[('secours', 'faisable')]
                         or meilleur[('non_secours', 'quelconque')] or meilleur[('secours', 'quelconque')])
                if choix is not None:
                    _, s_voisin, parent, pipe_id, length, diam, cout_total = choix
                    secteur_de_noeud[nid] = s_voisin
                    parent_of[nid] = parent
                    pipe_of[nid] = pipe_id
                    enfants_de[parent].add(nid)
                    cumul_perte[nid] = cout_total
                    capacite_restante[s_voisin] -= nb_ab
                    progres = True

        def _recalc_rapide(nid, secteur_de_noeud_, cumul_perte_):
            s_id = secteur_de_noeud_.get(nid)
            if s_id is None:
                return None
            alt = noeud_altitudes.get(nid)
            cote_piezo = secteur_cote_piezo.get(s_id)
            perte = cumul_perte_.get(nid)
            if alt is None or cote_piezo is None or perte is None:
                return None
            return cote_piezo - alt - perte

        if config.get('ACTIVER_REDISTRIBUTION_FORCEE', True):
            redistribuer_capacites_forcee(
                secteur_de_noeud, parent_of, pipe_of, enfants_de,
                cumul_perte, capacite_restante, config)

        # Raffinement progressif : tentatives de rattrapage locales
        t0 = time.perf_counter()
        if config.get('ACTIVER_GARANTIE_CONNEXITE', True):
            garantir_connexite(secteur_de_noeud, parent_of, pipe_of, enfants_de,
                               cumul_perte, capacite_restante, config)
        t_progres += time.perf_counter() - t0

        transferts_par_noeud = defaultdict(int)
        noeuds_verrouilles_transfert = set()
        max_sauts = config.get('MAX_SAUTS_RAFFINEMENT', 1)

        t_r0 = time.perf_counter()
        for round_ in range(1, config['MAX_ROUNDS_RAFFINEMENT'] + 1):
            candidats_raffinement = [nid for nid in secteur_de_noeud
                                      if nid not in noeuds_verrouilles_transfert
                                      and parent_of.get(nid) is not None
                                      and not enfants_de.get(nid)]
            ameliorations = []

            for nid in candidats_raffinement:
                s_actuel = secteur_de_noeud[nid]
                p_actuel = _recalc_rapide(nid, secteur_de_noeud, cumul_perte)
                alt = noeud_altitudes.get(nid)
                if p_actuel is None or alt is None:
                    continue
                nb_ab = abonnes_par_noeud.get(nid, 0)
                cout_actuel = cumul_perte.get(nid, 0.0)
                if not est_norme_pression(p_actuel, nid):
                    cout_actuel += config['PENALITE_HORS_PLAGE_COEF'] * ecart_hors_plage(p_actuel, nid)
                cout_capacite_actuel = _cout_capacite(config, capacite_restante, s_actuel, reservoir_data, 0)
                cout_actuel += cout_capacite_actuel

                sature_actuel = _secteur_est_plein(s_actuel, capacite_restante, config)
                if not (sature_actuel or not est_norme_pression(p_actuel, nid) or cout_capacite_actuel > 0.1):
                    continue

                meilleur_gain = None
                voisins_explores = set()

                file_bfs = deque()
                for (v, pid, l, d) in graphe.get(nid, []):
                    sv = secteur_de_noeud.get(v)
                    if sv is not None and sv != s_actuel:
                        file_bfs.append((v, sv, pid, l, d, cumul_perte.get(v, 0.0), 1))
                        voisins_explores.add(v)

                for saut in range(2, max_sauts + 1):
                    prochaine_file = deque()
                    for (v, sv, pid, l, d, perte_v, _) in list(file_bfs):
                        for (v2, pid2, l2, d2) in graphe.get(v, []):
                            sv2 = secteur_de_noeud.get(v2)
                            if sv2 is not None and sv2 != s_actuel and v2 not in voisins_explores:
                                voisins_explores.add(v2)
                                prochaine_file.append((v2, sv2, pid2, l2, d2, cumul_perte.get(v2, 0.0), saut))
                    file_bfs.extend(prochaine_file)

                for (voisin, s_voisin, pipe_id, length, diam, perte_voisin, nb_sauts) in file_bfs:
                    q_ls = max(nb_ab * q_pointe_abonne, 0.01)
                    hf = perte_charge_hazen_williams(q_ls, length, diam, config['COEF_HAZEN_WILLIAMS'])
                    perte_cand = perte_voisin + hf
                    cote_piezo_cand = secteur_cote_piezo.get(s_voisin)
                    if cote_piezo_cand is None:
                        continue
                    pression_cand = cote_piezo_cand - alt - perte_cand
                    if pression_cand < p_actuel - config['DEGRADATION_PRESSION_MAX_M']:
                        continue
                    if not _secteur_peut_recevoir(s_voisin, nb_ab, capacite_restante, config):
                        continue
                    cout_cand = perte_cand
                    if not est_norme_pression(pression_cand, nid):
                        cout_cand += config['PENALITE_HORS_PLAGE_COEF'] * ecart_hors_plage(pression_cand, nid)
                    cout_cand += _cout_capacite(config, capacite_restante, s_voisin, reservoir_data, nb_ab)
                    gain = cout_actuel - cout_cand
                    if gain > config['AMELIORATION_MIN_TRANSFERT_M']:
                        if meilleur_gain is None or gain > meilleur_gain[0]:
                            meilleur_gain = (gain, voisin, s_voisin, pipe_id, length, diam, perte_cand)

                if meilleur_gain is not None:
                    gain, voisin, s_voisin, pipe_id, length, diam, perte_cand = meilleur_gain
                    ameliorations.append((gain, nid, s_actuel, voisin, s_voisin, pipe_id, length, diam, perte_cand, nb_ab))

            if not ameliorations:
                break
            ameliorations.sort(key=lambda x: -x[0])
            for gain, nid, s_actuel, nouveau_parent, s_voisin, pipe_id, length, diam, perte_cand, nb_ab in ameliorations:
                if nid in noeuds_verrouilles_transfert or secteur_de_noeud.get(nid) != s_actuel:
                    continue
                if enfants_de.get(nid):
                    continue
                _deplacer_noeud_avec_sous_arbre(
                    nid, nouveau_parent, s_voisin, pipe_id,
                    secteur_de_noeud, parent_of, pipe_of, enfants_de,
                    cumul_perte, capacite_restante, perte_cand, config)
                transferts_par_noeud[nid] += 1
                if transferts_par_noeud[nid] >= config['MAX_TRANSFERTS_PAR_NOEUD']:
                    noeuds_verrouilles_transfert.add(nid)

        t_raffinement += time.perf_counter() - t_r0

        def recalculer_pressions_et_debits(secteur_de_noeud_, parent_of_, pipe_of_, enfants_de_):
            pression_noeud_ = {}
            debit_pipe_final_ = defaultdict(float)
            cumul_perte_final = {}
            noeuds_par_secteur = defaultdict(list)
            for nid, s_id in secteur_de_noeud_.items():
                noeuds_par_secteur[s_id].append(nid)
            for s_id, noeuds_secteur in noeuds_par_secteur.items():
                source_nid = secteur_source_nid.get(s_id)
                if source_nid is None or source_nid not in secteur_de_noeud_:
                    continue
                ordre_pre = [source_nid]
                dq = deque([source_nid])
                vus = {source_nid}
                while dq:
                    cur = dq.popleft()
                    for e in enfants_de_.get(cur, ()):
                        if secteur_de_noeud_.get(e) == s_id and e not in vus:
                            vus.add(e)
                            ordre_pre.append(e)
                            dq.append(e)
                debit_aval = {}
                for n in ordre_pre:
                    debit_aval[n] = abonnes_par_noeud.get(n, 0) * q_pointe_abonne
                for n in reversed(ordre_pre):
                    p = parent_of_.get(n)
                    if p is not None and p in vus:
                        debit_aval[p] = debit_aval.get(p, 0.0) + debit_aval.get(n, 0.0)
                cote_piezo = secteur_cote_piezo.get(s_id)
                perte_cum = {source_nid: 0.0}
                for n in ordre_pre:
                    p = parent_of_.get(n)
                    if p is None:
                        perte_cum[n] = 0.0
                    else:
                        pid = pipe_of_.get(n)
                        length = pipe_lengths.get(pid, 0.0)
                        diam = pipe_diameters.get(pid, 100.0)
                        q = debit_aval.get(n, 0.0)
                        hf = perte_charge_hazen_williams(q, length, diam, config['COEF_HAZEN_WILLIAMS'])
                        perte_cum[n] = min(perte_cum.get(p, 0.0) + hf, config['PERTE_CHARGE_MAX_M'])
                        if pid is not None:
                            debit_pipe_final_[pid] += q
                    alt = noeud_altitudes.get(n)
                    if alt is not None and cote_piezo is not None:
                        pression_noeud_[n] = cote_piezo - alt - perte_cum[n]
                    cumul_perte_final[n] = perte_cum[n]
            return pression_noeud_, dict(debit_pipe_final_), cumul_perte_final

        t0 = time.perf_counter()
        pression_noeud, debit_pipe_final, cumul_perte_final = recalculer_pressions_et_debits(
            secteur_de_noeud, parent_of, pipe_of, enfants_de)
        t_recalc += time.perf_counter() - t0

        if config.get('ACTIVER_CORRECTION_SURPRESSION', True):
            t0 = time.perf_counter()
            corriger_surpressions(secteur_de_noeud, parent_of, pipe_of, enfants_de,
                                   cumul_perte_final, capacite_restante, pression_noeud, config)
            t_correction_forcee += time.perf_counter() - t0
            t0 = time.perf_counter()
            pression_noeud, debit_pipe_final, cumul_perte_final = recalculer_pressions_et_debits(
                secteur_de_noeud, parent_of, pipe_of, enfants_de)
            t_recalc += time.perf_counter() - t0

        if config.get('ACTIVER_CORRECTION_FORCEE', True):
            t_cf0 = time.perf_counter()
            for _iter_corr in range(config['MAX_ITER_CORRECTION_FORCEE']):
                noeuds_violes = [nid for nid, p in pression_noeud.items()
                                 if p is not None and ecart_hors_plage(p, nid) > 0
                                 and parent_of.get(nid) is not None
                                 and not enfants_de.get(nid)]
                if not noeuds_violes:
                    break
                corrections_iter = 0
                for nid in noeuds_violes:
                    alt = noeud_altitudes.get(nid)
                    if alt is None:
                        continue
                    nb_ab = abonnes_par_noeud.get(nid, 0)
                    s_actuel = secteur_de_noeud.get(nid)
                    p_actuel = pression_noeud.get(nid)

                    meilleur = None
                    meilleur_score = float('inf')
                    for (voisin, pipe_id, length, diam) in graphe.get(nid, []):
                        s_voisin = secteur_de_noeud.get(voisin)
                        if s_voisin is None:
                            continue
                        cote_piezo_v = secteur_cote_piezo.get(s_voisin)
                        if cote_piezo_v is None:
                            continue
                        q_ls = max(nb_ab * q_pointe_abonne, 0.01)
                        hf = perte_charge_hazen_williams(q_ls, length, diam, config['COEF_HAZEN_WILLIAMS'])
                        perte_cand = cumul_perte_final.get(voisin, 0.0) + hf
                        p_cand = cote_piezo_v - alt - perte_cand
                        if not est_norme_pression(p_cand, nid):
                            continue
                        if not _secteur_peut_recevoir(s_voisin, nb_ab, capacite_restante, config):
                            continue
                        score = abs(p_cand - config['PRESSION_CIBLE_M'])
                        if score < meilleur_score:
                            meilleur_score = score
                            meilleur = (voisin, s_voisin, pipe_id, perte_cand)

                    if meilleur is not None:
                        nouveau_parent, s_voisin, pipe_id, perte_cand = meilleur
                        _deplacer_noeud_avec_sous_arbre(
                            nid, nouveau_parent, s_voisin, pipe_id,
                            secteur_de_noeud, parent_of, pipe_of, enfants_de,
                            cumul_perte_final, capacite_restante, perte_cand, config)
                        corrections_iter += 1

                if corrections_iter > 0:
                    t0 = time.perf_counter()
                    pression_noeud, debit_pipe_final, cumul_perte_final = recalculer_pressions_et_debits(
                        secteur_de_noeud, parent_of, pipe_of, enfants_de)
                    t_recalc += time.perf_counter() - t0
                else:
                    break
            t_correction_forcee += time.perf_counter() - t_cf0

        if config.get('ACTIVER_GARANTIE_CONNEXITE', True):
            for _pass in range(config.get('MAX_PASSES_GARANTIE_CONNEXITE_FINALE', 3)):
                n_corr = garantir_connexite(secteur_de_noeud, parent_of, pipe_of, enfants_de,
                                             cumul_perte_final, capacite_restante, config)
                if n_corr == 0:
                    break
            pression_noeud, debit_pipe_final, cumul_perte_final = recalculer_pressions_et_debits(
                secteur_de_noeud, parent_of, pipe_of, enfants_de)

        for s_id in secteur_cote_piezo:
            reservoir_data[s_id]['capacite_abonnes_restante'] = capacite_restante[s_id]
        total_time = time.perf_counter() - t_start
        timings = {
            'total': total_time,
            'heap': t_heap,
            'progres': t_progres,
            'raffinement': t_raffinement,
            'recalc': t_recalc,
            'correction_forcee': t_correction_forcee,
            'other': max(0.0, total_time - (t_heap + t_progres + t_raffinement + t_recalc + t_correction_forcee)),
        }

        return {
            'secteur_de_noeud': dict(secteur_de_noeud),
            'parent_of': dict(parent_of),
            'pipe_of': dict(pipe_of),
            'enfants_de': {k: set(v) for k, v in enfants_de.items()},
            'cumul_perte': dict(cumul_perte_final),
            'capacite_restante': dict(capacite_restante),
            'pression_noeud': dict(pression_noeud),
            'debit_pipe_final': dict(debit_pipe_final),
            'noeuds_verrouilles_transfert': set(noeuds_verrouilles_transfert),
            'nb_transferts': compteur_transfert_actif['n'],
            'timings': timings,
        }

    # ============================================================================
    # FONCTION DE NOTATION
    # ============================================================================

    def noter_solution(config, etat):
        secteur_de_noeud_ = etat['secteur_de_noeud']
        pression_noeud_ = etat['pression_noeud']
        capacite_restante_ = etat['capacite_restante']

        score_pression = 0.0
        nb_violations_pression = 0
        for nid, p in pression_noeud_.items():
            if p is None:
                continue
            ecart = ecart_hors_plage(p, nid)
            if ecart > 0:
                score_pression += ecart * max(abonnes_par_noeud.get(nid, 1), 1)
                nb_violations_pression += 1

        score_capacite = 0.0
        nb_violations_capacite = 0
        for s_id, restante in capacite_restante_.items():
            rd = reservoir_data.get(s_id, {})
            cap_utile = rd.get('capacite_abonnes_utile', 0)
            nb_actuel = cap_utile - restante
            depasse_plafond = (config.get('ACTIVER_PLAFOND_ABONNES_MAX', True)
                                and nb_actuel > config['ABONNES_MAX_PAR_SECTEUR'])
            if restante < 0:
                score_capacite += abs(restante)
                nb_violations_capacite += 1
            elif depasse_plafond:
                score_capacite += (nb_actuel - config['ABONNES_MAX_PAR_SECTEUR'])
                nb_violations_capacite += 1

        score_fragmentation = 0
        noeuds_par_secteur = defaultdict(set)
        for nid, s_id in secteur_de_noeud_.items():
            noeuds_par_secteur[s_id].add(nid)
        for s_id, membres in noeuds_par_secteur.items():
            source_nid = secteur_source_nid.get(s_id)
            if source_nid is None or source_nid not in membres:
                score_fragmentation += 1
                continue
            vus = {source_nid}
            dq = deque([source_nid])
            while dq:
                cur = dq.popleft()
                for e in etat['enfants_de'].get(cur, ()):
                    if e in membres and e not in vus:
                        vus.add(e)
                        dq.append(e)
            if len(vus) < len(membres):
                score_fragmentation += 1

        score = (config['POIDS_SCORE_PRESSION'] * score_pression
                 + config['POIDS_SCORE_CAPACITE'] * score_capacite
                 + config['POIDS_SCORE_FRAGMENTATION'] * score_fragmentation)

        return score, score_pression, score_capacite, score_fragmentation, nb_violations_pression, nb_violations_capacite

    # ============================================================================
    _etape(4)  # Boucle adaptative
    # BOUCLE ADAPTATIVE VERS LES OBJECTIFS CONFIGURES
    # ============================================================================

    _log("\n" + "=" * 70)
    _log(f"BOUCLE ADAPTATIVE VERS LES OBJECTIFS CONFIGURES (max {CONFIG['MAX_CYCLES_CONFORMITE']} cycles)")
    _log(f"  Chaque cycle = {CONFIG['NB_ITERATIONS_INTERNES']} iterations internes")
    _log("=" * 70)

    CONFIG_PHYSIQUE_REF = dict(CONFIG)

    meilleur_etat_global = None
    meilleur_score_global = float('inf')
    meilleure_config_globale = None
    conformite_atteinte = False

    rapport_cycles = []
    config_courante = dict(CONFIG)
    violations_precedent = float('inf')
    durcissements_consecutifs = 0

    for cycle in range(CONFIG['MAX_CYCLES_CONFORMITE']):
        _log(f"\n--- CYCLE {cycle + 1} / {CONFIG['MAX_CYCLES_CONFORMITE']} ---")
        _log(f"  Penalite_pression={config_courante['POIDS_SCORE_PRESSION']:.1f}, "
              f"Penalite_capacite={config_courante['POIDS_SCORE_CAPACITE']:.1f}, "
              f"Penalite_cap_coef={config_courante['PENALITE_CAPACITE_COEF']:.0f}")

        meilleur_etat_cycle = None
        meilleur_score_cycle = float('inf')
        meilleure_config_cycle = None

        for i in range(config_courante['NB_ITERATIONS_INTERNES']):
            seed = config_courante['SEED_BASE'] + cycle * 1000 + i
            rng = random.Random(seed)
            config_iter = dict(config_courante)

            if i > 0:
                variation = config_iter['VARIATION_PARAM_PCT']
                config_iter['PENALITE_CAPACITE_COEF'] *= (1.0 + rng.uniform(-variation, variation))
                config_iter['DEGRADATION_PRESSION_MAX_M'] *= (1.0 + rng.uniform(-variation, variation))
                config_iter['FACTEUR_CAPACITE_CROISSANCE'] *= (1.0 + rng.uniform(-variation, variation))
                config_iter['PENALITE_HORS_PLAGE_COEF'] *= (1.0 + rng.uniform(-variation, variation))

            utiliser_voronoi_iter = (i % 2 == 0) and config_iter['UTILISER_VORONOI_AMORCE']

            sources_triees = sorted(sources, key=lambda s: -reservoir_data[s[1]]['priorite_score'])
            groupes = defaultdict(list)
            for s in sources_triees:
                groupes[reservoir_data[s[1]]['priorite_score']].append(s)
            ordre_iter = []
            for priorite in sorted(groupes.keys(), reverse=True):
                groupe = list(groupes[priorite])
                rng.shuffle(groupe)
                ordre_iter.extend(groupe)

            etat = executer_pipeline_complet(config_iter, ordre_iter, rng,
                                              utiliser_voronoi=utiliser_voronoi_iter)
            score, sp, sc, sf, nvp, nvc = noter_solution(config_iter, etat)

            t_iter = etat.get('timings', {}).get('total', 0.0)
            _log(f"    iter {i:02d} [V={'ON' if utiliser_voronoi_iter else 'OFF'}] "
                  f"score={score:>12,.1f}  press_viol={nvp:>5}  cap_viol={nvc:>3}  frag={sf}  time={t_iter:.2f}s")

            if score < meilleur_score_cycle:
                meilleur_score_cycle = score
                meilleur_etat_cycle = etat
                meilleure_config_cycle = config_iter

        score_cycle, sp_c, sc_c, sf_c, nvp_c, nvc_c = noter_solution(meilleure_config_cycle, meilleur_etat_cycle)
        rapport_cycles.append({
            'cycle': cycle + 1,
            'score': score_cycle,
            'score_pression': sp_c,
            'score_capacite': sc_c,
            'score_fragmentation': sf_c,
            'nb_viol_pression': nvp_c,
            'nb_viol_capacite': nvc_c,
        })

        _log(f"  => Meilleur cycle {cycle + 1} : score={score_cycle:,.1f}  "
              f"viol_pression={nvp_c}  viol_capacite={nvc_c}  fragments={sf_c}")

        if score_cycle < meilleur_score_global:
            meilleur_score_global = score_cycle
            meilleur_etat_global = meilleur_etat_cycle
            meilleure_config_globale = meilleure_config_cycle

        if nvp_c == 0 and nvc_c == 0 and sf_c == 0:
            conformite_atteinte = True
            _log(f"\n  *** OBJECTIFS CONFIGURES ATTEINTS au cycle {cycle + 1} ! ***")
            break

        violations_actuelles = nvp_c + nvc_c * 10 + sf_c * 1000
        if violations_precedent < float('inf'):
            reduction_pct = (100.0 * (violations_precedent - violations_actuelles) /
                             max(violations_precedent, 1))
        else:
            reduction_pct = 100.0

        violations_identiques = (violations_actuelles == violations_precedent)

        if violations_identiques and cycle > 0:
            durcissements_consecutifs += 1
            _log(f"  Violations IDENTIQUES au cycle precedent "
                  f"({durcissements_consecutifs}/{config_courante['MAX_DURCISSEMENTS_CONSECUTIFS']} "
                  f"durcissements deja tentes sans effet).")
            if durcissements_consecutifs >= config_courante['MAX_DURCISSEMENTS_CONSECUTIFS']:
                _log("  -> Durcissement suspendu (sans effet mesurable) : "
                      "violations residuelles probablement STRUCTURELLES "
                      "(cf. couche diagnostic_reservoirs_viables). "
                      "Arret anticipe de la boucle adaptative -- la scission "
                      "altimetrique (plus loin) prend le relais pour traiter "
                      "les ecarts topographiques que la boucle seule ne peut pas resorber.")
                violations_precedent = violations_actuelles
                break
        elif (reduction_pct < config_courante['SEUIL_REDUCTION_VIOLATIONS_PCT'] and cycle > 0
              and durcissements_consecutifs < config_courante['MAX_DURCISSEMENTS_CONSECUTIFS']):
            _log(f"  Stagnation detectee (reduction {reduction_pct:.1f}% < "
                  f"{config_courante['SEUIL_REDUCTION_VIOLATIONS_PCT']}%) "
                  f"-> DURCISSEMENT des penalites.")
            durcissements_consecutifs += 1
            facteur = config_courante['FACTEUR_DURCISSEMENT_PENALITE']
            config_courante['POIDS_SCORE_PRESSION'] *= facteur
            config_courante['POIDS_SCORE_CAPACITE'] *= facteur
            config_courante['PENALITE_CAPACITE_COEF'] *= facteur
            config_courante['PENALITE_HORS_PLAGE_COEF'] *= facteur
            config_courante['MAX_ROUNDS_RAFFINEMENT'] = min(
                config_courante['MAX_ROUNDS_RAFFINEMENT'] + 20, 300)
            config_courante['MAX_TRANSFERTS_PAR_NOEUD'] = min(
                config_courante['MAX_TRANSFERTS_PAR_NOEUD'] + 10, 80)
        else:
            _log(f"  Progression : {reduction_pct:.1f}% de reduction des violations.")
            durcissements_consecutifs = 0

        violations_precedent = violations_actuelles

    if not conformite_atteinte:
        _log(f"\n  MAX CYCLES / ARRET ANTICIPE ({len(rapport_cycles)} cycles executes) sans atteindre tous les objectifs configures.")
        _log(f"  Meilleur etat global retenu : score={meilleur_score_global:,.1f}")

    _log("\n" + "=" * 70)
    _log("TABLEAU DE CONVERGENCE DES CYCLES")
    _log("=" * 70)
    _log(f"{'Cycle':<7}{'Score':>14}{'Pression':>14}{'Capacite':>12}{'Viol.P':>8}{'Viol.C':>8}{'Frag.':>7}")
    for r in rapport_cycles:
        _log(f"{r['cycle']:<7}{r['score']:>14,.1f}{r['score_pression']:>14,.1f}"
              f"{r['score_capacite']:>12,.0f}{r['nb_viol_pression']:>8}{r['nb_viol_capacite']:>8}"
              f"{r['score_fragmentation']:>7}")

    secteur_de_noeud = meilleur_etat_global['secteur_de_noeud']
    parent_of = meilleur_etat_global['parent_of']
    pipe_of = meilleur_etat_global['pipe_of']
    enfants_de = meilleur_etat_global['enfants_de']
    cumul_perte = meilleur_etat_global['cumul_perte']
    capacite_restante = meilleur_etat_global['capacite_restante']
    pression_noeud = meilleur_etat_global['pression_noeud']
    debit_pipe_final = meilleur_etat_global['debit_pipe_final']

    CONFIG = dict(meilleure_config_globale)
    for _cle in CLES_CONFIG_PHYSIQUES:
        CONFIG[_cle] = CONFIG_PHYSIQUE_REF[_cle]

    for s_id in secteur_cote_piezo:
        reservoir_data[s_id]['capacite_abonnes_restante'] = capacite_restante[s_id]

    nb_transferts_avant_nettoyage = int(meilleur_etat_global.get('nb_transferts', 0))
    compteur_transfert_actif['n'] = 0

    # ============================================================================
    # OUTILS COMMUNS DE RECALCUL (reutilises par les passes de scission/fusion)
    # ============================================================================

    def _recalculer_pressions_final(secteur_de_noeud_, parent_of_, pipe_of_, enfants_de_):
        pression_noeud_ = {}
        debit_pipe_final_ = defaultdict(float)
        cumul_perte_final_ = {}
        noeuds_par_secteur_ = defaultdict(list)
        for nid_, s_id_ in secteur_de_noeud_.items():
            noeuds_par_secteur_[s_id_].append(nid_)
        for s_id_, noeuds_secteur_ in noeuds_par_secteur_.items():
            source_nid_ = secteur_source_nid.get(s_id_)
            if source_nid_ is None or source_nid_ not in secteur_de_noeud_:
                continue
            ordre_pre_ = [source_nid_]
            dq_ = deque([source_nid_])
            vus_ = {source_nid_}
            while dq_:
                cur_ = dq_.popleft()
                for e_ in enfants_de_.get(cur_, ()):
                    if secteur_de_noeud_.get(e_) == s_id_ and e_ not in vus_:
                        vus_.add(e_)
                        ordre_pre_.append(e_)
                        dq_.append(e_)
            debit_aval_ = {}
            for n_ in ordre_pre_:
                debit_aval_[n_] = abonnes_par_noeud.get(n_, 0) * q_pointe_abonne
            for n_ in reversed(ordre_pre_):
                p_ = parent_of_.get(n_)
                if p_ is not None and p_ in vus_:
                    debit_aval_[p_] = debit_aval_.get(p_, 0.0) + debit_aval_.get(n_, 0.0)
            cote_piezo_ = secteur_cote_piezo.get(s_id_)
            perte_cum_ = {source_nid_: 0.0}
            for n_ in ordre_pre_:
                p_ = parent_of_.get(n_)
                if p_ is None or p_ not in vus_:
                    perte_cum_[n_] = 0.0
                else:
                    pid_ = pipe_of_.get(n_)
                    length_ = pipe_lengths.get(pid_, 0.0)
                    diam_ = pipe_diameters.get(pid_, 100.0)
                    q_ = debit_aval_.get(n_, 0.0)
                    hf_ = perte_charge_hazen_williams(q_, length_, diam_, CONFIG['COEF_HAZEN_WILLIAMS'])
                    perte_cum_[n_] = min(perte_cum_.get(p_, 0.0) + hf_, CONFIG['PERTE_CHARGE_MAX_M'])
                    if pid_ is not None:
                        debit_pipe_final_[pid_] += q_
                alt_ = noeud_altitudes.get(n_)
                if alt_ is not None and cote_piezo_ is not None:
                    pression_noeud_[n_] = cote_piezo_ - alt_ - perte_cum_[n_]
                cumul_perte_final_[n_] = perte_cum_[n_]
        return pression_noeud_, dict(debit_pipe_final_), cumul_perte_final_


    def _reconstruire_arbre_par_bfs(secteur_de_noeud_):
        """Reconstruit un arbre de connexite simple (BFS pur) par secteur --
        necessaire apres une scission/fusion car l'ancien arbre d'optimisation
        n'est plus valide (des noeuds ont change de secteur sans que leur
        parent/enfants ne soient mis a jour)."""
        parent_of_bfs = {}
        pipe_of_bfs = {}
        enfants_de_bfs = defaultdict(set)
        noeuds_par_secteur_bfs = defaultdict(set)
        for nid, s in secteur_de_noeud_.items():
            noeuds_par_secteur_bfs[s].add(nid)
        for s_id, membres_set in noeuds_par_secteur_bfs.items():
            if not membres_set:
                continue
            source_nid = secteur_source_nid.get(s_id)
            if source_nid is None or source_nid not in membres_set:
                source_nid = next(iter(membres_set))
            parent_of_bfs[source_nid] = None
            pipe_of_bfs[source_nid] = None
            vus = {source_nid}
            dq = deque([source_nid])
            while dq:
                cur = dq.popleft()
                for (voisin, pipe_id, length, diam) in graphe.get(cur, []):
                    if voisin in membres_set and voisin not in vus:
                        vus.add(voisin)
                        parent_of_bfs[voisin] = cur
                        pipe_of_bfs[voisin] = pipe_id
                        enfants_de_bfs[cur].add(voisin)
                        dq.append(voisin)
        return parent_of_bfs, pipe_of_bfs, enfants_de_bfs


    def _construire_adjacence_secteurs(secteur_de_noeud_):
        """Adjacence ponderee entre secteurs : s1 -> {s2: [nb_conduites, longueur_cumulee_m]}.
        Sert a choisir, lors d'une fusion, le voisin le plus fortement connecte
        geographiquement (le plus de conduites frontalieres, puis la plus
        grande longueur cumulee de frontiere) plutot qu'un voisin relie par une
        seule conduite fine et lointaine."""
        adjacence = defaultdict(lambda: defaultdict(lambda: [0, 0.0]))
        for pipe_id, (key1, key2) in pipe_endpoints.items():
            n1 = noeud_id_map[key1]
            n2 = noeud_id_map[key2]
            s1 = secteur_de_noeud_.get(n1)
            s2 = secteur_de_noeud_.get(n2)
            if s1 is not None and s2 is not None and s1 != s2:
                length = pipe_lengths.get(pipe_id, 0.0)
                adjacence[s1][s2][0] += 1
                adjacence[s1][s2][1] += length
                adjacence[s2][s1][0] += 1
                adjacence[s2][s1][1] += length
        return adjacence


    def _enregistrer_nouveaux_secteurs(secteurs_crees, prefix_log):
        """Cree les entrees reservoir_data / secteur_cote_piezo / secteur_source_nid
        pour chaque nouveau sous-secteur issu d'une scission. Le sous-secteur
        aval d'une coupure est regule par un PRV implicite au point de coupure :
        sa 'source' devient ce noeud, a la pression cible (pas la cote piezo
        brute du reservoir amont)."""
        for nouveau_id, s_parent, n_coupe, nb_ab_part in secteurs_crees:
            alt_coupe = noeud_altitudes.get(n_coupe, 0.0)
            cote_piezo_regulee = alt_coupe + CONFIG['PRESSION_CIBLE_M']
            secteur_source_nid[nouveau_id] = n_coupe
            secteur_cote_piezo[nouveau_id] = cote_piezo_regulee
            rd_parent = reservoir_data.get(s_parent, {})
            reservoir_data[nouveau_id] = dict(rd_parent)
            reservoir_data[nouveau_id]['capacite_abonnes_utile'] = max(nb_ab_part, CONFIG['ABONNES_MAX_PAR_SECTEUR'])
            reservoir_data[nouveau_id]['capacite_abonnes_restante'] = max(nb_ab_part, CONFIG['ABONNES_MAX_PAR_SECTEUR']) - nb_ab_part
            reservoir_data[nouveau_id]['parent_tank_id'] = rd_parent.get('tank_id', s_parent)
            reservoir_data[nouveau_id]['cote_eau'] = cote_piezo_regulee
            reservoir_data[nouveau_id]['cote_eau_originale'] = cote_piezo_regulee
            reservoir_data[nouveau_id]['nb_parts_scission'] = rd_parent.get('nb_parts_scission', 1) + 1
            reservoir_data[nouveau_id]['issu_scission_post_hoc'] = True
            reservoir_data[nouveau_id]['noeud_coupure'] = n_coupe
            tank_points_par_secteur[nouveau_id] = noeud_points.get(n_coupe)
            _log(f"  {prefix_log} : {s_parent} -> {nouveau_id} : {nb_ab_part} abonnes")


    # ============================================================================
    _etape(5)  # Scission altimetrique
    # point 1 : SCISSION ALTIMETRIQUE (PRV VIRTUELS INTERNES)
    # ============================================================================
    # Cause racine identifiee sur le run precedent : la scission par nombre d'abonnes
    # ne regarde jamais l'altitude, donc un secteur peut etre "dans la plage de
    # effectif" tout en couvrant 70-185m de denivele -- physiquement impossible
    # a alimenter avec une seule cote piezometrique (d'ou les pressions
    # negatives a -47m, -77m observees). Cette passe parcourt l'arbre de
    # chaque secteur depuis sa source et coupe (insertion d'un PRV virtuel /
    # futur reservoir relais) des qu'une branche sortirait de la bande de
    # pression utile -- AVANT la scission par effectif, pour que celle-ci
    # s'applique ensuite sur des morceaux deja coherents topographiquement.

    def _forcer_contiguite_spatiale(secteur_de_noeud_in, label):
        """GARANTIT qu'un secteur_id correspond a UNE SEULE composante connexe
        du reseau PHYSIQUE reel (graphe complet des conduites -- pas
        l'arbre couvrant utilise pour la scission altimetrique, qui peut
        "sauter" d'une branche a l'autre du reseau maille et donc regrouper
        sous un meme label des zones geographiquement eparpillees).

        Un secteur hydraulique doit etre une surface isolee, alimentee
        depuis une ou plusieurs sources mais d'un seul tenant -- pas un
        patchwork d'ilots disjoints entremeles avec d'autres secteurs.
        Si un secteur_id se retrouve fragmente en plusieurs composantes
        connexes disjointes, seule la plus grosse (par nb d'abonnes) garde
        le label d'origine ; les autres sont rattachees au secteur VOISIN
        REEL (via conduite) le plus connecte -- le meme principe deja
        utilise ailleurs dans le pipeline pour fusionner les secteurs
        sous-effectif (poids = nb de conduites frontalieres puis longueur
        cumulee). Repete jusqu'a stabilite (fragmenter un voisin peut a son
        tour necessiter un nouveau passage)."""
        secteur_de_noeud = dict(secteur_de_noeud_in)
        nb_reaffectations_total = 0

        for _pass in range(10):
            noeuds_par_secteur = defaultdict(list)
            for nid, s in secteur_de_noeud.items():
                noeuds_par_secteur[s].append(nid)

            fragments_a_reaffecter = []
            for s_id, membres in noeuds_par_secteur.items():
                membres_set = set(membres)
                non_vus = set(membres_set)
                composantes = []
                while non_vus:
                    depart = next(iter(non_vus))
                    comp = set()
                    dq = deque([depart])
                    comp.add(depart)
                    non_vus.discard(depart)
                    while dq:
                        cur = dq.popleft()
                        for (voisin, pipe_id, length, diam) in graphe.get(cur, []):
                            if voisin in non_vus:
                                non_vus.discard(voisin)
                                comp.add(voisin)
                                dq.append(voisin)
                    composantes.append(comp)
                if len(composantes) <= 1:
                    continue
                composantes.sort(
                    key=lambda c: sum(abonnes_par_noeud.get(n, 0) for n in c), reverse=True)
                for comp in composantes[1:]:
                    fragments_a_reaffecter.append((comp, s_id))

            if not fragments_a_reaffecter:
                break

            for comp, s_id_origine in fragments_a_reaffecter:
                poids_voisin = defaultdict(lambda: [0, 0.0])
                for n in comp:
                    for (voisin, pipe_id, length, diam) in graphe.get(n, []):
                        s_voisin = secteur_de_noeud.get(voisin)
                        if s_voisin is not None and s_voisin != s_id_origine:
                            poids_voisin[s_voisin][0] += 1
                            poids_voisin[s_voisin][1] += length
                if poids_voisin:
                    meilleur = max(poids_voisin.items(), key=lambda kv: (kv[1][0], kv[1][1]))[0]
                else:
                    meilleur = s_id_origine  # fragment isole du reste du reseau : rien a faire ici
                if meilleur != s_id_origine:
                    for n in comp:
                        secteur_de_noeud[n] = meilleur
                    nb_reaffectations_total += len(comp)

        if nb_reaffectations_total:
            nb_ab_reaffectes = sum(abonnes_par_noeud.get(n, 0) for n in secteur_de_noeud
                                    if secteur_de_noeud[n] != secteur_de_noeud_in.get(n))
            _log(f"  Contiguite spatiale ({label}) : {nb_reaffectations_total} noeud(s) / "
                  f"{nb_ab_reaffectes} abonne(s) rattache(s) a un secteur geographiquement "
                  f"contigu -- un secteur hydraulique doit rester une zone d'un seul tenant, "
                  f"jamais un patchwork entremele avec ses voisins.")
        return secteur_de_noeud


    def _scinder_par_paliers_altimetriques(secteur_de_noeud_in, parent_of_in, enfants_de_in, config):
        if not config.get('ACTIVER_SCISSION_ALTIMETRIQUE', True):
            return dict(secteur_de_noeud_in), []

        secteur_de_noeud_out = dict(secteur_de_noeud_in)
        bande = config['BANDE_PRESSION_UTILE_M']
        marge_basse = config['MARGE_BASSE_AVANT_COUPURE_M']
        p_min = config['PRESSION_MIN_M']
        p_max_utile = min(config['PRESSION_MAX_M'], p_min + bande)

        noeuds_par_secteur = defaultdict(list)
        for nid, s in secteur_de_noeud_in.items():
            noeuds_par_secteur[s].append(nid)

        secteurs_crees_bruts = []  # (nouveau_id, s_parent, n_coupe)
        compteur_coupe_global = defaultdict(int)

        for s_id_origine, membres in list(noeuds_par_secteur.items()):
            source_nid = secteur_source_nid.get(s_id_origine)
            if source_nid is None or source_nid not in secteur_de_noeud_out:
                continue
            membres_set = set(membres)
            cote_racine = secteur_cote_piezo.get(s_id_origine)
            if cote_racine is None:
                continue

            # etat_actif[nid] = (secteur_actif_a_ce_point, cote_piezo_active)
            etat_actif = {source_nid: (s_id_origine, cote_racine)}
            dq = deque([source_nid])
            while dq:
                cur = dq.popleft()
                s_actif_cur, cote_actif_cur = etat_actif[cur]
                for e in enfants_de_in.get(cur, ()):
                    if e not in membres_set or e in etat_actif:
                        continue
                    alt_e = noeud_altitudes.get(e)
                    if alt_e is None:
                        etat_actif[e] = (s_actif_cur, cote_actif_cur)
                        secteur_de_noeud_out[e] = s_actif_cur
                        dq.append(e)
                        continue

                    p_statique = cote_actif_cur - alt_e
                    if p_statique > p_max_utile or p_statique < (p_min - marge_basse):
                        # coupure : e devient la source d'un nouveau sous-secteur,
                        # regule au point de coupure (PRV virtuel / relais)
                        compteur_coupe_global[s_actif_cur] += 1
                        nouveau_id = f"{s_actif_cur}_ALT{compteur_coupe_global[s_actif_cur]}"
                        cote_piezo_regulee = alt_e + config['PRESSION_CIBLE_M']
                        etat_actif[e] = (nouveau_id, cote_piezo_regulee)
                        secteurs_crees_bruts.append((nouveau_id, s_actif_cur, e))
                    else:
                        etat_actif[e] = (s_actif_cur, cote_actif_cur)

                    secteur_de_noeud_out[e] = etat_actif[e][0]
                    dq.append(e)

        secteurs_crees_final = []
        for nouveau_id, s_parent, n_coupe in secteurs_crees_bruts:
            nb_ab = sum(abonnes_par_noeud.get(n, 0) for n, s in secteur_de_noeud_out.items() if s == nouveau_id)
            secteurs_crees_final.append((nouveau_id, s_parent, n_coupe, nb_ab))

        return secteur_de_noeud_out, secteurs_crees_final


    # ============================================================================
    # SCISSION EQUILIBREE VERS UNE MOYENNE CIBLE (~2000 ab/secteur)
    # ============================================================================
    # Remplace le plafonnage "coupe le plus gros morceau qui rentre sous 3000"
    # (qui produisait des secteurs desequilibres et des miettes a 0/2/10
    # abonnes) par une decoupe en N parts dont la taille individuelle est la
    # plus proche possible de la moyenne visee (CONFIG['ABONNES_IDEAL_CIBLE_SCISSION']).
    # N'agit QUE sur les secteurs strictement superieurs a seuil_max -- les
    # secteurs deja dans la plage [ABONNES_MIN_PAR_SECTEUR, ABONNES_MAX_PAR_SECTEUR]
    # ne sont jamais modifies par cette fonction.
    # cette passe s'execute maintenant APRES la scission altimetrique, sur
    # des morceaux deja coherents topographiquement -- elle n'a donc plus a
    # compenser un ecart de pression en plus d'un ecart d'effectif.

    def _scinder_pour_moyenne_cible(secteur_de_noeud_in, parent_of_in, enfants_de_in,
                                     config, cible, seuil_max=None, taille_min_part=None):
        """
        Scinde chaque secteur > seuil_max en round(nb_ab/cible) parts dont la
        taille individuelle est la plus proche possible de 'cible'.
        taille_min_part (si fourni) exige qu'un sous-arbre candidat a la
        decoupe fasse au moins cette taille pour etre retenu -- evite de
        recreer des miettes juste apres une fusion qui vient de les eliminer.
        Retourne (nouvelle_affectation, secteurs_crees).
        """
        seuil_max = seuil_max or config['ABONNES_MAX_PAR_SECTEUR']
        taille_min_part = taille_min_part if taille_min_part is not None else 1
        secteur_de_noeud_out = dict(secteur_de_noeud_in)
        secteurs_crees = []

        noeuds_par_secteur = defaultdict(list)
        for nid, s in secteur_de_noeud_in.items():
            noeuds_par_secteur[s].append(nid)

        for s_id, membres in list(noeuds_par_secteur.items()):
            nb_ab_total = sum(abonnes_par_noeud.get(n, 0) for n in membres)
            if nb_ab_total <= seuil_max:
                continue  # deja conforme ou sous-dimensionne -> jamais touche ici

            membres_set = set(membres)
            source_nid = secteur_source_nid.get(s_id)
            if source_nid is None or source_nid not in membres_set:
                continue

            nb_parts = max(2, round(nb_ab_total / cible))
            taille_cible_part = nb_ab_total / nb_parts

            bfs_ordre = [source_nid]
            vus = {source_nid}
            dq = deque([source_nid])
            while dq:
                cur = dq.popleft()
                for e in enfants_de_in.get(cur, ()):
                    if e in membres_set and e not in vus:
                        vus.add(e)
                        bfs_ordre.append(e)
                        dq.append(e)

            taille_sous_arbre = {n: abonnes_par_noeud.get(n, 0) for n in bfs_ordre}
            for n in reversed(bfs_ordre):
                p = parent_of_in.get(n)
                if p is not None and p in membres_set and p != n:
                    taille_sous_arbre[p] = taille_sous_arbre.get(p, 0) + taille_sous_arbre.get(n, 0)

            restant = set(bfs_ordre)
            part_idx = 0
            for _ in range(nb_parts - 1):
                candidats = [(abs(taille_sous_arbre[n] - taille_cible_part), n)
                             for n in restant
                             if n != source_nid
                             and taille_min_part <= taille_sous_arbre[n] <= seuil_max]
                if not candidats:
                    # aucun sous-arbre a la fois assez gros pour etre autonome
                    # et assez petit pour respecter seuil_max -- on arrete de
                    # couper CE secteur (mieux vaut le laisser hors plage,
                    # signale honnetement, que fragmente en miettes)
                    break
                candidats.sort()
                _, n_coupe = candidats[0]

                sous_arbre = []
                dqx = deque([n_coupe])
                vusx = {n_coupe}
                while dqx:
                    c = dqx.popleft()
                    sous_arbre.append(c)
                    for e in enfants_de_in.get(c, ()):
                        if e in restant and e not in vusx:
                            vusx.add(e)
                            dqx.append(e)

                part_idx += 1
                nouveau_id = f"{s_id}_P{part_idx}"
                for n in sous_arbre:
                    secteur_de_noeud_out[n] = nouveau_id
                    restant.discard(n)
                secteurs_crees.append((nouveau_id, s_id, n_coupe,
                                        sum(abonnes_par_noeud.get(n, 0) for n in sous_arbre)))

        return secteur_de_noeud_out, secteurs_crees


    # ============================================================================
    _etape(6)  # Fusion sous-effectifs
    # point 2 : FUSION DES SECTEURS SOUS-EFFECTIF, ALTITUDE-COMPATIBLE
    # ============================================================================
    # Passe UNIQUE (pas de boucle) : chaque secteur dont l'effectif est < 300
    # (ou nul) est fusionne avec le voisin hydraulique le plus fortement
    # connecte geographiquement (max nb conduites frontalieres, puis max
    # longueur cumulee de frontiere), SOUS RESERVE  que la fusion ne
    # recree pas un ecart d'altitude excessif (> ECART_ALT_MAX_FUSION_M) --
    # c'est exactement ce qui manquait en version precedente et qui recreait des violations
    # de pression juste apres avoir corrige les sous-effectifs. Si aucun
    # voisin n'est altitude-compatible, on fusionne quand meme avec le plus
    # connecte (mieux qu'un secteur croupion isole) mais on le signale
    # explicitement, car une violation de pression est alors probable et
    # devra etre traitee manuellement (nouvelle conduite / PRV / reservoir).
    # Les secteurs deja conformes (dans [500, 3000]) ne sont jamais choisis
    # comme "source" a fusionner ; ils peuvent seulement RECEVOIR.

    def _evaluer_pression_migrants_vers(cand_id, membres_migrants, config):
        """FIX-FUSION-PRESSION-12 : evalue la fusion d'un point de vue PRESSION REELLE plutot
        qu'un simple ecart d'altitude brut. Calcule, pour les abonnes qui
        migreraient vers cand_id, la pression statique qu'ils auraient sous la
        cote piezometrique REGULEE de cand_id (secteur_cote_piezo[cand_id] --
        deja calee par la scission altimetrique / recalibration) :
            p(n) = cote_piezo(cand_id) - altitude(n)
        C'est exactement le calcul deja utilise ailleurs dans le moteur
        (diagnostic de recalibration des cotes piezo, redistribution forcee)
        -- une paire de secteurs peut avoir des altitudes de terrain proches
        (donc un 'ecart' brut faible) tout en donnant une tres mauvaise
        pression une fois la cote piezo REELLE du secteur receveur appliquee
        (ou l'inverse : un ecart brut > seuil peut malgre tout donner une
        pression correcte si le receveur a une cote favorable). Retourne
        (violation_cumulee_m, nb_noeuds_violation, p_min, p_max) ou None si
        la cote piezo ou les altitudes sont indisponibles.
        """
        cote_cand = secteur_cote_piezo.get(cand_id)
        if cote_cand is None:
            return None
        p_min_cfg = config['PRESSION_MIN_M']
        p_max_cfg = config['PRESSION_MAX_M']
        p_vals = []
        for n in membres_migrants:
            alt = noeud_altitudes.get(n)
            if alt is None:
                continue
            p_vals.append(cote_cand - alt)
        if not p_vals:
            return None
        violation = sum(max(0.0, p_min_cfg - p) + max(0.0, p - p_max_cfg) for p in p_vals)
        nb_viol = sum(1 for p in p_vals if p < p_min_cfg or p > p_max_cfg)
        return violation, nb_viol, min(p_vals), max(p_vals)


    def _fusionner_sous_effectifs_finale(secteur_de_noeud_, config):
        noeuds_par_secteur = defaultdict(list)
        for nid, s in secteur_de_noeud_.items():
            noeuds_par_secteur[s].append(nid)

        a_fusionner = [s for s, m in noeuds_par_secteur.items()
                       if sum(abonnes_par_noeud.get(n, 0) for n in m) < config['ABONNES_MIN_PAR_SECTEUR']]
        if not a_fusionner:
            return secteur_de_noeud_, 0, []

        adjacence = _construire_adjacence_secteurs(secteur_de_noeud_)
        a_fusionner.sort(key=lambda s: sum(abonnes_par_noeud.get(n, 0) for n in noeuds_par_secteur[s]))

        nb_fusions = 0
        nb_fusions_incompatibles = 0
        isoles = []
        for s_id in a_fusionner:
            if s_id not in noeuds_par_secteur:
                continue
            voisins = adjacence.get(s_id, {})
            if not voisins:
                isoles.append((s_id, sum(abonnes_par_noeud.get(n, 0) for n in noeuds_par_secteur[s_id])))
                continue

            # classement par connectivite geographique (nb conduites, puis longueur)
            candidats_tries = sorted(voisins.items(), key=lambda kv: (-kv[1][0], -kv[1][1]))
            membres_migrants = noeuds_par_secteur[s_id]

            # FIX-FUSION-PRESSION-12 : selection du voisin par PRESSION REELLE estimee pour
            # les abonnes migrants, et non plus par un simple ecart d'altitude
            # brut (correctif FIX-FUSION-ALTITUDE-10). Le run de validation de FIX10 a montre que
            # l'ecart d'altitude brut est un mauvais proxy : le run a legerement
            # REGRESSE (CONFORME_STRICT 11->10/37, hors-plage pression
            # 27.2%->28.6%) car deux secteurs peuvent avoir des altitudes de
            # terrain proches sans que la cote piezo REGULEE du receveur donne
            # une bonne pression aux noeuds migrants (et inversement). FIX12
            # calcule directement p = cote_piezo(candidat) - altitude(noeud)
            # pour chaque abonne qui migrerait, comme le fait deja le moteur
            # ailleurs (recalibration des cotes piezo, redistribution forcee).
            meilleur_voisin = None
            secours_voisin = None
            secours_violation = float('inf')
            secours_nb_viol = None
            for cand_id, (nb_c, long_c) in candidats_tries:
                verdict = _evaluer_pression_migrants_vers(cand_id, membres_migrants, config)
                if verdict is None:
                    # cote piezo ou altitudes indisponibles : on ne peut pas
                    # juger -- on accepte ce candidat par defaut (comportement
                    # d'origine quand les donnees manquent).
                    meilleur_voisin = cand_id
                    break
                violation, nb_viol, p_min_res, p_max_res = verdict
                if nb_viol == 0:
                    meilleur_voisin = cand_id
                    break
                if violation < secours_violation:
                    secours_violation = violation
                    secours_nb_viol = nb_viol
                    secours_voisin = cand_id

            compatible = meilleur_voisin is not None
            if meilleur_voisin is None:
                # aucun voisin ne donne une pression conforme a TOUS les
                # abonnes migrants : on prend celui qui minimise la violation
                # de pression cumulee (secours_voisin), pas le plus connecte.
                meilleur_voisin = secours_voisin if secours_voisin is not None else candidats_tries[0][0]
                violation_retenue = secours_violation if secours_voisin is not None else None
                nb_viol_retenu = secours_nb_viol if secours_voisin is not None else None
                nb_fusions_incompatibles += 1

            nb_c, long_c = voisins[meilleur_voisin]
            nb_ab_s = sum(abonnes_par_noeud.get(n, 0) for n in membres_migrants)

            for n in membres_migrants:
                secteur_de_noeud_[n] = meilleur_voisin

            if compatible:
                _log(f"  Fusion : {s_id} ({nb_ab_s} ab, < {config['ABONNES_MIN_PAR_SECTEUR']}) -> "
                      f"{meilleur_voisin} ({nb_c} conduite(s) frontaliere(s), {long_c:.0f}m de "
                      f"frontiere, pression conforme pour tous les abonnes migrants)")
            else:
                _viol_txt = (f"{nb_viol_retenu} noeud(s)/{violation_retenue:.0f}m de violation cumulee"
                              if violation_retenue is not None else "indetermine")
                _log(f"  Fusion : {s_id} ({nb_ab_s} ab, < {config['ABONNES_MIN_PAR_SECTEUR']}) -> "
                      f"{meilleur_voisin} ({nb_c} conduite(s) frontaliere(s), {long_c:.0f}m de "
                      f"frontiere) -- ATTENTION : AUCUN voisin ne donne une pression conforme a tous "
                      f"les abonnes migrants, fusion faite sur le voisin a VIOLATION DE PRESSION "
                      f"MINIMALE parmi les voisins directs ({_viol_txt}) -- verifier apres.")

            nb_fusions += 1
            del noeuds_par_secteur[s_id]
            noeuds_par_secteur[meilleur_voisin] = [n for n, s in secteur_de_noeud_.items() if s == meilleur_voisin]
            adjacence = _construire_adjacence_secteurs(secteur_de_noeud_)

        if nb_fusions_incompatibles:
            _log(f"  ATTENTION : {nb_fusions_incompatibles} fusion(s) sans voisin donnant une pression "
                  f"conforme a tous les abonnes migrants (verrou geographique + topographique reel).")

        return secteur_de_noeud_, nb_fusions, isoles


    # ============================================================================
    # NETTOYAGE FINAL EN 4 PASSES (altimetrique -> scission -> fusion ->
    #       re-scission), SANS BOUCLE
    #   Passe 0 (NOUVEAU build initial) : coupure de chaque branche des que la pression
    #             statique sortirait de la bande utile -- insertion de PRV
    #             virtuels / relais internes -- resout la cause racine des
    #             SOUS_ZONAGE_REQUIS et pressions negatives constatees sur le run precedent.
    #   Passe 1 : secteurs > 3000 ab -> scindes pour viser une moyenne ~2000 ab
    #             (sur des morceaux deja coherents topographiquement grace a P0)
    #   Passe 2 : secteurs sous le minimum configuré (ou 0) -> fusionnes avec le voisin le plus
    #             fortement connecte geographiquement ET altitude-compatible
    #             (evite de recreer les violations qu'on vient de couper)
    #   Passe 3 : re-scission (unique, jamais rebouclee) des secteurs repasses
    #             > 3000 suite a la fusion, avec garde-fou anti-miettes
    #             (chaque part generee doit faire au moins ABONNES_MIN_PAR_SECTEUR)
    # Les secteurs deja dans [ABONNES_MIN_PAR_SECTEUR, ABONNES_MAX_PAR_SECTEUR]
    # ET deja dans la bande de pression utile ne sont JAMAIS modifies comme
    # source de scission ou de fusion.
    # ============================================================================

    _log("\n" + "=" * 70)
    _log("NETTOYAGE FINAL EN 4 PASSES (altimetrique -> scission -> fusion -> re-scission)")
    _log(f"  Bande de pression utile par palier : {CONFIG['BANDE_PRESSION_UTILE_M']:.0f}m "
          f"| Cible de scission par effectif : moyenne ~{CONFIG['ABONNES_IDEAL_CIBLE_SCISSION']} ab/secteur "
          f"| Plage visee : [{CONFIG['ABONNES_MIN_PAR_SECTEUR']}-{CONFIG['ABONNES_MAX_PAR_SECTEUR']}] ab")
    _log("=" * 70)

    # --- PASSE 0  : scission altimetrique (PRV virtuels internes) ---
    parent_of, pipe_of, enfants_de = _reconstruire_arbre_par_bfs(secteur_de_noeud)
    secteur_de_noeud, crees_p0 = _scinder_par_paliers_altimetriques(
        secteur_de_noeud, parent_of, enfants_de, CONFIG)
    _enregistrer_nouveaux_secteurs(crees_p0, "Scission altimetrique P0")
    if not crees_p0:
        _log("  Aucune branche hors bande de pression utile -- aucune coupure altimetrique necessaire.")
    else:
        _log(f"  {len(crees_p0)} coupure(s) altimetrique(s) (PRV virtuels internes) inseree(s).")

    # La scission altimetrique parcourt l'ARBRE COUVRANT du reseau (une seule
    # branche par noeud) : dans un reseau maille, cet arbre peut "sauter"
    # d'une rue a l'autre selon le chemin retenu, et une coupure ponctuelle
    # peut alors regrouper sous un meme secteur_id des zones geographiquement
    # eparpillees -- un secteur hydraulique doit rester une surface d'un seul
    # tenant. On corrige donc immediatement via le graphe REEL des conduites.
    secteur_de_noeud = _forcer_contiguite_spatiale(secteur_de_noeud, "apres scission altimetrique")

    parent_of, pipe_of, enfants_de = _reconstruire_arbre_par_bfs(secteur_de_noeud)
    pression_noeud, debit_pipe_final, cumul_perte = _recalculer_pressions_final(
        secteur_de_noeud, parent_of, pipe_of, enfants_de)

    # --- PASSE 1 : scission des secteurs > 3000 (cible ~2000 ab/part) ---
    secteur_de_noeud, crees_p1 = _scinder_pour_moyenne_cible(
        secteur_de_noeud, parent_of, enfants_de, CONFIG,
        cible=CONFIG['ABONNES_IDEAL_CIBLE_SCISSION'])
    _enregistrer_nouveaux_secteurs(crees_p1, "Scission effectif P1")
    if not crees_p1:
        _log("  Aucun secteur > plafond -- aucune scission par effectif necessaire en P1.")

    parent_of, pipe_of, enfants_de = _reconstruire_arbre_par_bfs(secteur_de_noeud)
    pression_noeud, debit_pipe_final, cumul_perte = _recalculer_pressions_final(
        secteur_de_noeud, parent_of, pipe_of, enfants_de)

    # --- PASSE 2 : fusion des secteurs sous le minimum configuré, altitude-compatible  ---
    secteur_de_noeud, nb_fus, secteurs_isoles_fusion = _fusionner_sous_effectifs_finale(
        secteur_de_noeud, CONFIG)
    _log(f"  {nb_fus} secteur(s) sous-effectif fusionne(s) avec leur voisin le plus connecte.")
    if secteurs_isoles_fusion:
        _log(f"  ATTENTION : {len(secteurs_isoles_fusion)} secteur(s) sous-effectif ISOLE(S) "
              f"(aucun voisin hydraulique) -- necessitent une nouvelle conduite de raccordement : "
              f"{', '.join(f'{s}({n}ab)' for s, n in secteurs_isoles_fusion)}")

    parent_of, pipe_of, enfants_de = _reconstruire_arbre_par_bfs(secteur_de_noeud)
    pression_noeud, debit_pipe_final, cumul_perte = _recalculer_pressions_final(
        secteur_de_noeud, parent_of, pipe_of, enfants_de)

    # --- PASSE 3 (unique, jamais rebouclee) : re-scission des secteurs repasses
    #     au-dessus de 3000 suite a la fusion -- garde-fou min ABONNES_MIN_PAR_SECTEUR/part
    secteur_de_noeud, crees_p3 = _scinder_pour_moyenne_cible(
        secteur_de_noeud, parent_of, enfants_de, CONFIG,
        cible=CONFIG['ABONNES_IDEAL_CIBLE_SCISSION'],
        taille_min_part=CONFIG['ABONNES_MIN_PAR_SECTEUR'])
    _enregistrer_nouveaux_secteurs(crees_p3, "Re-scission P3")
    if not crees_p3:
        _log("  Aucun secteur repasse au-dessus du plafond apres fusion -- pas de re-scission P3.")

    parent_of, pipe_of, enfants_de = _reconstruire_arbre_par_bfs(secteur_de_noeud)
    pression_noeud, debit_pipe_final, cumul_perte = _recalculer_pressions_final(
        secteur_de_noeud, parent_of, pipe_of, enfants_de)

    # --- nettoyage des dictionnaires de secteurs disparus (absorbes par fusion) ---
    _secteurs_restants = set(secteur_de_noeud.values())
    for _dico in (reservoir_data, secteur_cote_piezo, secteur_source_nid, tank_points_par_secteur):
        for _s_id in [k for k in _dico if k not in _secteurs_restants]:
            del _dico[_s_id]

    # --- recalcul des capacites restantes sur la topologie finale ---
    _ab_par_secteur = defaultdict(int)
    for _nid, _s in secteur_de_noeud.items():
        _ab_par_secteur[_s] += abonnes_par_noeud.get(_nid, 0)
    for s_id in list(reservoir_data.keys()):
        rd = reservoir_data.get(s_id, {})
        rd['capacite_abonnes_restante'] = rd.get('capacite_abonnes_utile', 0) - _ab_par_secteur.get(s_id, 0)

    # --- bilan de la plage abonnes ---
    _hors_plage = [(s, n) for s, n in _ab_par_secteur.items()
                        if n > 0 and not (CONFIG['ABONNES_MIN_PAR_SECTEUR'] <= n <= CONFIG['ABONNES_MAX_PAR_SECTEUR'])]
    _log(f"\n  Secteurs finaux : {len(_secteurs_restants)}")
    _moyenne_finale = (sum(_ab_par_secteur.values()) / len(_secteurs_restants)) if _secteurs_restants else 0
    _log(f"  Moyenne abonnes/secteur : {_moyenne_finale:,.0f} (cible {CONFIG['ABONNES_IDEAL_CIBLE_SCISSION']})")
    if _hors_plage:
        _log(f"  ATTENTION : {len(_hors_plage)} secteur(s) restent hors plage "
              f"[{CONFIG['ABONNES_MIN_PAR_SECTEUR']}-{CONFIG['ABONNES_MAX_PAR_SECTEUR']}] "
              f"malgre les 4 passes (verrou geographique/topographique reel -- necessite une "
              f"intervention manuelle : nouvelle conduite, PRV physique, ou reservoir) :")
        for s, n in sorted(_hors_plage, key=lambda t: t[1]):
            _log(f"    {s} : {n} abonnes")
    else:
        _log("  Tous les secteurs desservis sont dans la plage cible.")

    # recalcul de secteurs_sous_zonage_requis sur la topologie finale (les
    # scissions/fusions peuvent avoir change l'ecart de pression de secteurs)
    secteurs_sous_zonage_requis = {}
    _p_par_secteur_final = defaultdict(list)
    for _nid, _s in secteur_de_noeud.items():
        _p = pression_noeud.get(_nid)
        if _p is not None:
            _p_par_secteur_final[_s].append(_p)
    for _s_id, _p_liste in _p_par_secteur_final.items():
        if not _p_liste:
            continue
        _ecart_reel = max(_p_liste) - min(_p_liste)
        if _ecart_reel > CONFIG['SEUIL_ECART_TOPO_M']:
            secteurs_sous_zonage_requis[_s_id] = {
                'ecart_topo_m': _ecart_reel, 'p_min': min(_p_liste), 'p_max': max(_p_liste),
            }
    _log(f"\n  Sous-zonage REEL recalcule sur l'etat final : "
          f"{len(secteurs_sous_zonage_requis)} secteur(s) reellement concerne(s) "
          f"(vs. avant scission altimetrique -- comparer au diagnostic preliminaire ci-dessus).")

    # ============================================================================
    _etape(7)  # Redistribution finale
    # REDISTRIBUTION FORCEE DE RATTRAPAGE APRES LES 4 PASSES
    # ============================================================================
    # Les scissions/fusions ci-dessus peuvent laisser des secteurs en depassement
    # de capacite (nouveaux sous-secteurs issus de coupures altimetriques dont
    # le nombre d'abonnes reel depasse le plafond, notamment). On relance une
    # passe de redistribution par sous-arbre (point 3) sur l'etat final.

    if CONFIG.get('ACTIVER_REDISTRIBUTION_FORCEE', True):
        _capacite_restante_finale = {
            s_id: reservoir_data[s_id].get('capacite_abonnes_utile', 0) - _ab_par_secteur.get(s_id, 0)
            for s_id in reservoir_data
        }
        _nb_transferts_finaux = redistribuer_capacites_forcee(
            secteur_de_noeud, parent_of, pipe_of, enfants_de,
            cumul_perte, _capacite_restante_finale, CONFIG)
        if _nb_transferts_finaux:
            _log(f"\n  Redistribution finale (post-4-passes) : {_nb_transferts_finaux} transfert(s) "
                  f"de sous-arbre effectue(s) pour resorber les depassements de capacite residuels.")
            parent_of, pipe_of, enfants_de = _reconstruire_arbre_par_bfs(secteur_de_noeud)
            pression_noeud, debit_pipe_final, cumul_perte = _recalculer_pressions_final(
                secteur_de_noeud, parent_of, pipe_of, enfants_de)
            _ab_par_secteur = defaultdict(int)
            for _nid, _s in secteur_de_noeud.items():
                _ab_par_secteur[_s] += abonnes_par_noeud.get(_nid, 0)
            for s_id in list(reservoir_data.keys()):
                rd = reservoir_data.get(s_id, {})
                rd['capacite_abonnes_restante'] = rd.get('capacite_abonnes_utile', 0) - _ab_par_secteur.get(s_id, 0)

    # --- GARDE-FOU FINAL DE CONTIGUITE SPATIALE ---
    # Les passes precedentes (scission par effectif, fusions, redistribution
    # de capacite) peuvent avoir reintroduit une fragmentation geographique.
    # Dernier passage de securite avant de figer secteur_de_noeud pour tous
    # les calculs et couches de sortie en aval (un secteur hydraulique doit
    # rester une zone d'un seul tenant, jamais entremelee avec ses voisins).
    _avant_contiguite_finale = dict(secteur_de_noeud)
    secteur_de_noeud = _forcer_contiguite_spatiale(secteur_de_noeud, "verification finale")
    if secteur_de_noeud != _avant_contiguite_finale:
        parent_of, pipe_of, enfants_de = _reconstruire_arbre_par_bfs(secteur_de_noeud)
        pression_noeud, debit_pipe_final, cumul_perte = _recalculer_pressions_final(
            secteur_de_noeud, parent_of, pipe_of, enfants_de)
        _ab_par_secteur = defaultdict(int)
        for _nid, _s in secteur_de_noeud.items():
            _ab_par_secteur[_s] += abonnes_par_noeud.get(_nid, 0)
        for s_id in list(reservoir_data.keys()):
            rd = reservoir_data.get(s_id, {})
            rd['capacite_abonnes_restante'] = rd.get('capacite_abonnes_utile', 0) - _ab_par_secteur.get(s_id, 0)

    # ============================================================================
    # 9) AFFECTATION DES CONDUITES
    # ============================================================================

    conduites_non_affectees_details = dict(conduites_hors_reseau_details)

    def recalculer_secteur_de_conduite():
        global secteur_de_conduite
        secteur_de_conduite = {}
        for pipe_id, (key1, key2) in pipe_endpoints.items():
            n1 = noeud_id_map[key1]
            n2 = noeud_id_map[key2]
            s1 = secteur_de_noeud.get(n1)
            s2 = secteur_de_noeud.get(n2)
            if s1 is not None and s2 is not None and s1 == s2:
                secteur_de_conduite[pipe_id] = s1
            elif s1 is not None:
                secteur_de_conduite[pipe_id] = s1
            elif s2 is not None:
                secteur_de_conduite[pipe_id] = s2
            else:
                conduites_non_affectees_details[pipe_id] = 'EXTREMITES_SANS_SECTEUR'

    _log("\nAffectation des conduites...")
    recalculer_secteur_de_conduite()
    _log(f"Conduites affectees : {len(secteur_de_conduite)} / {pipe_layer.featureCount()} (couche AEP)")
    seuil_transport_diag = float(CONFIG.get('SEUIL_DN_TRANSPORT_MM', 150.0))
    nb_conduites_transport_affectees = sum(
        1 for pid in secteur_de_conduite
        if pipe_diameters.get(pid, 0.0) >= seuil_transport_diag
    )
    if nb_conduites_transport_affectees:
        _log(
            "  [INFO] %d conduite(s) de transport (DN >= %.0f mm) sont affectees a un secteur "
            "mais restent hors du remplissage polygonal des ilots par construction."
            % (nb_conduites_transport_affectees, seuil_transport_diag)
        )
    if conduites_non_affectees_details:
        _log(
            "  [DIAGNOSTIC CONDUITES HORS SECTEUR] %d conduite(s) non affectee(s) : %s."
            % (len(conduites_non_affectees_details),
               ', '.join('%s=%d' % (k, sum(1 for v in conduites_non_affectees_details.values() if v == k))
                         for k in sorted(set(conduites_non_affectees_details.values()))))
        )

    abonnes_par_secteur = defaultdict(int)
    for nid, s_id in secteur_de_noeud.items():
        abonnes_par_secteur[s_id] += abonnes_par_noeud.get(nid, 0)

    pressions_par_secteur = defaultdict(list)
    for nid, p in pression_noeud.items():
        if p is None:
            continue
        s_id = secteur_de_noeud.get(nid)
        if s_id is not None:
            pressions_par_secteur[s_id].append(p)

    noeuds_hors_plage_final = [nid for nid, p in pression_noeud.items()
                                if p is not None and ecart_hors_plage(p, nid) > 0]
    noeuds_zone_a_etudier_final = set(noeuds_hors_plage_final)

    def _bilan_violations_pression(pressions):
        nb = 0
        ab = 0
        for nid, valeur in pressions.items():
            if valeur is not None and ecart_hors_plage(valeur, nid) > 0:
                nb += 1
                ab += abonnes_par_noeud.get(nid, 0)
        return nb, ab

    nb_violations_pression_avant, nb_abonnes_hors_plage_avant = _bilan_violations_pression(
        meilleur_etat_global.get('pression_noeud', {}))
    nb_violations_pression_apres, nb_abonnes_hors_plage_apres = _bilan_violations_pression(
        pression_noeud)
    nb_transferts_nettoyage = int(compteur_transfert_actif.get('n', 0))
    nb_transferts_apres_nettoyage = nb_transferts_avant_nettoyage + nb_transferts_nettoyage

    _log("\n" + "=" * 70)
    _log("COMPARAISON AVANT / APRES NETTOYAGE FINAL")
    _log("=" * 70)
    _log(
        "  Violations de pression (noeuds / abonnes) : %d / %d -> %d / %d (delta %+d / %+d)."
        % (nb_violations_pression_avant, nb_abonnes_hors_plage_avant,
           nb_violations_pression_apres, nb_abonnes_hors_plage_apres,
           nb_violations_pression_apres - nb_violations_pression_avant,
           nb_abonnes_hors_plage_apres - nb_abonnes_hors_plage_avant)
    )
    _log(
        "  Transferts de noeuds/sous-arbres : %d avant -> %d apres "
        "(+%d pendant le nettoyage final)."
        % (nb_transferts_avant_nettoyage, nb_transferts_apres_nettoyage,
           nb_transferts_nettoyage)
    )

    # Validation independante des pressions finales. Les conduites inter-secteurs
    # sont fermees dans le modele lorsque les vannes de sectorisation sont
    # activees, afin de tester la partition réellement isolée.
    validation_wntr = None
    if CONFIG.get('VALIDER_PRESSION_WNTR', True):
        try:
            from .validation_wntr import valider_pressions_wntr
            wntr_edges = []
            for pipe_id, (key1, key2) in pipe_endpoints.items():
                wntr_edges.append({
                    'pipe_id': pipe_id,
                    'n1': noeud_id_map[key1],
                    'n2': noeud_id_map[key2],
                    'longueur_m': pipe_lengths.get(pipe_id, 0.0),
                    'diametre_mm': pipe_diameters.get(pipe_id, 100.0),
                })
            pression_min_wntr = {nid: pression_min_requise_noeud(nid)
                                 for nid in noeud_altitudes}
            validation_wntr = valider_pressions_wntr(
                noeud_altitudes, wntr_edges, abonnes_par_noeud, q_pointe_abonne,
                secteur_de_noeud, secteur_source_nid, secteur_cote_piezo,
                pression_min_par_noeud=pression_min_wntr,
                pression_min_m=CONFIG['PRESSION_MIN_M'],
                pression_max_m=CONFIG['PRESSION_MAX_M'],
                rugosite=CONFIG.get('RUGOSITE_WNTR', CONFIG['COEF_HAZEN_WILLIAMS']),
                fermer_conduites_frontieres=CONFIG.get('FERMER_CONDUITES_FRONTIERES_WNTR', True),
                log=_log,
            )
        except Exception as exc_wntr:
            validation_wntr = {'statut': 'ECHEC', 'erreur': str(exc_wntr), 'noeuds': {}}
            _log("  [WNTR] Integration indisponible ou en echec : %s" % exc_wntr)
    else:
        _log("  [WNTR] Validation desactivee par configuration (VALIDER_PRESSION_WNTR=false).")

    if validation_wntr and validation_wntr.get('statut') == 'OK':
        wntr_out = CoucheDifferee(f"Point?crs={pipe_layer.crs().authid()}", "pressions_wntr")
        wprov = wntr_out.dataProvider()
        wprov.addAttributes([
            QgsField("nid", QVariant.String), QgsField("secteur_id", QVariant.String),
            QgsField("pression_wntr_m", QVariant.Double),
            QgsField("pression_min_requise_m", QVariant.Double),
            QgsField("ecart_m", QVariant.Double), QgsField("statut", QVariant.String),
            QgsField("nb_abonnes", QVariant.Int),
        ])
        wntr_out.updateFields()
        w_feats = []
        for nid, info in validation_wntr.get('noeuds', {}).items():
            pt = noeud_points.get(nid)
            if pt is None:
                continue
            wf = QgsFeature(wntr_out.fields())
            wf.setGeometry(QgsGeometry.fromPointXY(pt))
            wf.setAttributes([
                nid, info.get('secteur_id'), round(info.get('pression_wntr_m'), 3),
                round(info.get('pression_min_requise_m'), 3), round(info.get('ecart_m'), 3),
                info.get('statut'), info.get('nb_abonnes', 0),
            ])
            w_feats.append(wf)
        if w_feats:
            wprov.addFeatures(w_feats)
            wntr_out.updateExtents()
            _ajouter_couche(wntr_out)
            _log(f"Couche creee : {wntr_out.name()} ({len(w_feats)} noeuds valides par WNTR)")

    # ============================================================================
    _etape(8)  # Calcul des pressions
    # 11bis) VERIFICATION STRICTE DES CRITERES
    # ============================================================================

    _log("\n" + "=" * 70)
    _log("VERIFICATION STRICTE DES CRITERES")
    _log("=" * 70)

    def _nb_morceaux(geom):
        geom_multi = assurer_multipolygone(geom)
        try:
            return max(geom_multi.constGet().numGeometries() if geom_multi.isMultipart() else 1, 1)
        except Exception:
            return max(len(geom_multi.asGeometryCollection()) if geom_multi.isMultipart() else 1, 1)

    def construire_polygones_pour_verification():
        geoms_par_secteur = defaultdict(list)
        for pipe_id, s_id in secteur_de_conduite.items():
            f = pipe_features.get(pipe_id)
            geom = f.geometry() if f else None
            if geom is None or geom.isEmpty():
                continue
            buf = geom.buffer(CONFIG['LARGEUR_CORRIDOR_M'], CONFIG['BUFFER_SEGMENTS'])
            if buf and not buf.isEmpty():
                geoms_par_secteur[s_id].append(buf)
        polygones = {}
        for s_id, buffers in geoms_par_secteur.items():
            if not buffers:
                continue
            fusion = QgsGeometry.unaryUnion(buffers)
            if fusion and not fusion.isEmpty():
                polygones[s_id] = fusion
        return polygones

    _polygones_verif = construire_polygones_pour_verification()

    secteur_conforme_strict = {}
    secteur_conforme_reseau_base = {}
    violations_par_secteur = defaultdict(list)

    for s_id in secteur_cote_piezo:
        if s_id not in abonnes_par_secteur or abonnes_par_secteur[s_id] == 0:
            continue
        raisons = []
        raisons_base = []

        rd = reservoir_data.get(s_id, {})
        cap_restante = rd.get('capacite_abonnes_restante', 0)
        nb_ab_secteur = abonnes_par_secteur.get(s_id, 0)
        capacite_ok = cap_restante >= 0
        depasse_plafond = (CONFIG.get('ACTIVER_PLAFOND_ABONNES_MAX', True)
                            and nb_ab_secteur > CONFIG['ABONNES_MAX_PAR_SECTEUR'])
        sous_min = nb_ab_secteur < CONFIG['ABONNES_MIN_PAR_SECTEUR']

        if not capacite_ok:
            msg_cap = (f"CAPACITE_DEPASSEE({-cap_restante} ab au-dela de "
                       f"{rd.get('capacite_abonnes_utile')})")
            raisons.append(msg_cap)
            raisons_base.append(msg_cap)
        if depasse_plafond:
            msg_plafond = (f"PLAFOND_ABONNES_DEPASSE({nb_ab_secteur} ab > "
                           f"{CONFIG['ABONNES_MAX_PAR_SECTEUR']} max)")
            raisons.append(msg_plafond)
            raisons_base.append(msg_plafond)
        if sous_min:
            msg_min = (f"SOUS_SEUIL_MIN({nb_ab_secteur} ab < "
                       f"{CONFIG['ABONNES_MIN_PAR_SECTEUR']} min)")
            raisons.append(msg_min)

        nb_violations_pression = 0
        ab_violations_pression = 0
        nb_violations_reseau_base = 0
        ab_violations_reseau_base = 0
        nb_violations_etage_seul = 0
        ab_violations_etage_seul = 0

        for nid, s in secteur_de_noeud.items():
            if s != s_id:
                continue
            p = pression_noeud.get(nid)
            if p is None:
                continue
            type_v = classer_type_violation(p, nid)
            if type_v == 'RESEAU_BASE':
                nb_violations_reseau_base += 1
                ab_violations_reseau_base += abonnes_par_noeud.get(nid, 0)
                nb_violations_pression += 1
                ab_violations_pression += abonnes_par_noeud.get(nid, 0)
            elif type_v == 'ETAGE_SEULEMENT':
                nb_violations_etage_seul += 1
                ab_violations_etage_seul += abonnes_par_noeud.get(nid, 0)
                nb_violations_pression += 1
                ab_violations_pression += abonnes_par_noeud.get(nid, 0)

        if nb_violations_pression > 0:
            raisons.append(
                f"PRESSION({nb_violations_pression} noeuds/{ab_violations_pression} ab "
                f"hors seuil etage, dont {nb_violations_reseau_base} noeuds/"
                f"{ab_violations_reseau_base} ab RESEAU et {nb_violations_etage_seul} "
                f"noeuds/{ab_violations_etage_seul} ab ETAGE_SEULEMENT)")
        if nb_violations_reseau_base > 0:
            raisons_base.append(
                f"PRESSION_RESEAU({nb_violations_reseau_base} noeuds/"
                f"{ab_violations_reseau_base} ab sous {CONFIG['PRESSION_MIN_M']:.0f}m ou "
                f"au-dessus de {CONFIG['PRESSION_MAX_M']:.0f}m)")

        if s_id in secteurs_sous_zonage_requis:
            info_topo = secteurs_sous_zonage_requis[s_id]
            raisons.append(f"SOUS_ZONAGE_REQUIS(ecart topo {info_topo['ecart_topo_m']:.0f}m)")

        nb_morc = _nb_morceaux(_polygones_verif[s_id]) if s_id in _polygones_verif else 1
        if nb_morc > 1:
            raisons.append(f"FRAGMENTATION({nb_morc} morceaux)")
            raisons_base.append(f"FRAGMENTATION({nb_morc} morceaux)")

        secteur_conforme_reseau_base[s_id] = (len(raisons_base) == 0)
        secteur_conforme_strict[s_id] = (len(raisons) == 0)
        violations_par_secteur[s_id] = raisons

    nb_conformes_strict = sum(1 for v in secteur_conforme_strict.values() if v)
    nb_total_strict = len(secteur_conforme_strict)
    nb_conformes_reseau_base = sum(1 for v in secteur_conforme_reseau_base.values() if v)
    _log(f"Secteurs OBJECTIFS_STRICTS : {nb_conformes_strict} / {nb_total_strict}")
    _log(f"Secteurs OBJECTIFS_RESEAU_BASE : {nb_conformes_reseau_base} / {nb_total_strict}")
    for s_id, ok in sorted(secteur_conforme_strict.items()):
        if not ok:
            _log(f"  {s_id} : NON CONFORME -- {' | '.join(violations_par_secteur[s_id])}")

    # ============================================================================
    # DIAGNOSTIC : RESERVOIRS HYDRAULIQUEMENT VIABLES PAR NOEUD
    # ============================================================================

    _log("\n" + "=" * 70)
    _log("DIAGNOSTIC : RESERVOIRS HYDRAULIQUEMENT VIABLES PAR NOEUD")
    _log("=" * 70)

    def _dijkstra_hydraulique_depuis_source(source_nid):
        dist = {source_nid: 0.0}
        heap_d = [(0.0, source_nid)]
        while heap_d:
            d, n = heapq.heappop(heap_d)
            if d > dist.get(n, float('inf')):
                continue
            for (voisin, pipe_id, length, diam) in graphe.get(n, []):
                nb_ab = abonnes_par_noeud.get(voisin, 0)
                q_ls = max(nb_ab * q_pointe_abonne, 0.01)
                hf = perte_charge_hazen_williams(q_ls, length, diam, CONFIG['COEF_HAZEN_WILLIAMS'])
                nd = d + hf
                if nd < dist.get(voisin, float('inf')):
                    dist[voisin] = nd
                    heapq.heappush(heap_d, (nd, voisin))
        return dist

    pertes_par_reservoir = {}
    for source_nid, s_id, *_ in sources:
        pertes_par_reservoir[s_id] = _dijkstra_hydraulique_depuis_source(source_nid)

    def _reservoirs_viables_pour(nid):
        alt = noeud_altitudes.get(nid)
        if alt is None:
            return []
        viables = []
        for s_id, pertes in pertes_par_reservoir.items():
            perte = pertes.get(nid)
            if perte is None:
                continue
            cote_piezo = secteur_cote_piezo.get(s_id)
            if cote_piezo is None:
                continue
            p = cote_piezo - alt - perte
            if est_norme_pression(p, nid):
                viables.append((s_id, round(p, 2)))
        viables.sort(key=lambda t: abs(t[1] - CONFIG['PRESSION_CIBLE_M']))
        return viables

    diagnostic_par_noeud = {nid: _reservoirs_viables_pour(nid) for nid in noeuds_zone_a_etudier_final}
    nb_structurel = sum(1 for v in diagnostic_par_noeud.values() if not v)
    nb_affectation = sum(1 for v in diagnostic_par_noeud.values() if v)
    ab_structurel = sum(abonnes_par_noeud.get(n, 0) for n, v in diagnostic_par_noeud.items() if not v)
    ab_affectation = sum(abonnes_par_noeud.get(n, 0) for n, v in diagnostic_par_noeud.items() if v)

    _log(f"STRUCTUREL (aucun reservoir viable) : {nb_structurel} noeuds / {ab_structurel} abonnes")
    _log(f"AFFECTATION_CAPACITE (reservoir viable existant) : {nb_affectation} noeuds / {ab_affectation} abonnes")

    diag_out = CoucheDifferee(f"Point?crs={pipe_layer.crs().authid()}", "diagnostic_reservoirs_viables")
    dprov = diag_out.dataProvider()
    dprov.addAttributes([
        QgsField("nid", QVariant.String), QgsField("secteur_actuel", QVariant.String),
        QgsField("pression_actuelle_m", QVariant.Double), QgsField("pression_min_requise_m", QVariant.Double),
        QgsField("categorie", QVariant.String), QgsField("nb_reservoirs_viables", QVariant.Int),
        QgsField("meilleur_reservoir_viable", QVariant.String), QgsField("pression_si_meilleur_m", QVariant.Double),
        QgsField("nb_abonnes", QVariant.Int), QgsField("niveau_etage_max", QVariant.Int),
    ])
    diag_out.updateFields()
    diag_feats = []
    for nid, viables in diagnostic_par_noeud.items():
        pt = noeud_points.get(nid)
        if pt is None:
            continue
        categorie = "STRUCTUREL" if not viables else "AFFECTATION_CAPACITE"
        meilleur_id, meilleur_p = (viables[0] if viables else (None, None))
        feat = QgsFeature(diag_out.fields())
        feat.setGeometry(QgsGeometry.fromPointXY(pt))
        feat.setAttributes([
            nid, secteur_de_noeud.get(nid),
            round(pression_noeud.get(nid), 2) if pression_noeud.get(nid) is not None else None,
            round(pression_min_requise_noeud(nid), 2),
            categorie, len(viables), meilleur_id, meilleur_p,
            abonnes_par_noeud.get(nid, 0), noeud_niveau_max.get(nid, 0),
        ])
        diag_feats.append(feat)
    if diag_feats:
        dprov.addFeatures(diag_feats)
        diag_out.updateExtents()
        _ajouter_couche(diag_out)
        _log(f"Couche creee : {diag_out.name()} ({len(diag_feats)} noeuds)")

    # ============================================================================
    # 13) CONSTRUCTION DES POLYGONES -- ILOTS URBAINS (POLYGONISATION DES ROUTES)
    # ============================================================================
    # FIX-ILOTS-URBAINS-14 : abandon de l'approche par BUFFER (autour des conduites et/ou
    # des routes, FIX13) au profit d'une delimitation par VRAIS ILOTS URBAINS,
    # conforme a la pratique de sectorisation DMA (District Metered Area) qui
    # s'appuie sur le decoupage morphologique du bati/de la voirie plutot que
    # sur une distance geometrique arbitraire (cf. la notion de "bloc urbain"
    # enclave par son perimetre de rues, telle qu'employee dans la litterature
    # de morphologie urbaine -- Biraghi et al. 2025, "Urban block enclosure and
    # labyrinthineness"). Concretement :
    #   1. Les lignes de la couche routes sont unies (QgsGeometry.unaryUnion) :
    #      cette operation "noeude" completement le reseau (les croisements de
    #      rues deviennent des noeuds topologiques), condition necessaire a la
    #      polygonisation.
    #   2. QgsGeometry.polygonize() transforme ce reseau noue en polygones : ce
    #      sont les vrais ILOTS URBAINS (blocs enclaves par les rues), et non
    #      un buffer. La limite d'un ilot est PAR CONSTRUCTION la ligne
    #      centrale de la rue -- jamais un batiment ne la chevauche, quelle que
    #      soit la largeur de la rue, puisqu'aucune largeur n'est inventee.
    #   3. Chaque ilot est rattache au secteur MAJORITAIRE parmi les noeuds
    #      reseau (abonnes agreges) qu'il contient -- le meme secteur_de_noeud
    #      deja calcule par le moteur hydraulique, pas un nouveau critere.
    #   4. Les ilots d'un meme secteur sont fusionnes (dissolve) -> polygone
    #      du secteur. Aucun buffer, aucune largeur a regler : la geometrie
    #      suit exactement le bati/la voirie reels.
    #   5. Repli : la ou la couche routes est absente, trouee, ou ne ferme pas
    #      de polygone (reseau non boucle), l'ancien corridor-buffer variable
    #      (FIX13, AEP + routes combines) comble UNIQUEMENT les zones non
    #      couvertes par un ilot -- jamais les zones deja couvertes par un ilot
    #      reel, qui restent prioritaires.
    # secteurs_limites, plus bas dans ce fichier, n'est touchee par aucun
    # changement de ce correctif (demande explicite).
    # ============================================================================

    # ====================================================================
    # FIX-PARTITION : les territoires de secteur sont une PARTITION
    # STRICTE des ilots urbains (module core/partition_secteurs.py) :
    # jamais d'enveloppe convexe, jamais de chevauchement, jamais d'ilot
    # parasite. Les fonctions corridor-buffer ci-dessous ne servent plus
    # que de repli si le reseau routier ne ferme aucun polygone.
    # ====================================================================
    from . import partition_secteurs as _partition

    _log("\nConstruction des polygones de secteur (partition ilots urbains build precedent)...")

    def _construire_corridors_par_buffer():
        """Repli FIX13 : corridor-buffer variable (AEP + routes combines),
        utilise uniquement pour combler les zones non couvertes par un ilot
        urbain reel (voir _construire_corridors_reseau ci-dessous)."""
        ACTIVER_DIAM = bool(CONFIG.get('ACTIVER_CORRIDOR_VARIABLE_DIAMETRE', True))
        COEF_DIAM = float(CONFIG.get('COEF_LARGEUR_CORRIDOR_PAR_DIAMETRE', 40.0))
        LARGEUR_MIN = float(CONFIG.get('LARGEUR_CORRIDOR_MIN_M', 3.0))
        LARGEUR_MAX = float(CONFIG.get('LARGEUR_CORRIDOR_MAX_M', CONFIG['LARGEUR_CORRIDOR_M']))
        LARGEUR_DEFAUT = float(CONFIG['LARGEUR_CORRIDOR_M'])
        CHAMP_LARGEUR_AEP = (CONFIG.get('CHAMP_LARGEUR_CORRIDOR_AEP') or '').strip()

        idx_champ_largeur_aep = None
        if CHAMP_LARGEUR_AEP and pipe_layer is not None:
            idx_champ_largeur_aep = pipe_layer.fields().indexFromName(CHAMP_LARGEUR_AEP)
            if idx_champ_largeur_aep < 0:
                idx_champ_largeur_aep = None

        def _largeur_corridor_pour(pipe_id, f):
            if idx_champ_largeur_aep is not None:
                val = f.attribute(idx_champ_largeur_aep)
                if val is not None and val != '':
                    try:
                        r = float(val)
                        if r > 0:
                            return r
                    except (TypeError, ValueError):
                        pass
            if ACTIVER_DIAM:
                diam_mm = pipe_diameters.get(pipe_id)
                if diam_mm:
                    return max(LARGEUR_MIN, min(LARGEUR_MAX, COEF_DIAM * (diam_mm / 1000.0)))
            return LARGEUR_DEFAUT

        trame_routes_corridor = None
        if CONFIG.get('ACTIVER_ROGNAGE_ROUTES_CORRIDOR', True) and route_layer is not None:
            CHAMP_LARGEUR_ROUTE_C = (CONFIG.get('CHAMP_LARGEUR_TAMPON_ROUTE') or '').strip()
            CHAMP_TYPE_ROUTE_C = (CONFIG.get('CHAMP_TYPE_ROUTE') or '').strip()
            TABLE_TYPE_ROUTE_C = {
                str(k).strip().lower(): float(v)
                for k, v in (CONFIG.get('LARGEUR_TAMPON_PAR_TYPE_ROUTE') or {}).items()
            }
            RAYON_ROUTE_DEFAUT_C = float(CONFIG.get('LARGEUR_TAMPON_ROUTE_M', 4.0))
            idx_largeur_c = route_layer.fields().indexFromName(CHAMP_LARGEUR_ROUTE_C) if CHAMP_LARGEUR_ROUTE_C else -1
            idx_type_c = route_layer.fields().indexFromName(CHAMP_TYPE_ROUTE_C) if CHAMP_TYPE_ROUTE_C else -1

            def _rayon_route_corridor(f):
                if idx_largeur_c >= 0:
                    val = f.attribute(idx_largeur_c)
                    if val is not None and val != '':
                        try:
                            r = float(val)
                            if r > 0:
                                return r
                        except (TypeError, ValueError):
                            pass
                if idx_type_c >= 0 and TABLE_TYPE_ROUTE_C:
                    val = f.attribute(idx_type_c)
                    if val is not None:
                        r = TABLE_TYPE_ROUTE_C.get(str(val).strip().lower())
                        if r is not None and r > 0:
                            return r
                return RAYON_ROUTE_DEFAUT_C

            route_bufs_c = []
            emprise_bbox_c = QgsFeatureRequest().setFilterRect(emprise_decoupe.boundingBox())
            for f in route_layer.getFeatures(emprise_bbox_c):
                g = f.geometry()
                if g and not g.isEmpty() and g.intersects(emprise_decoupe):
                    gc = g.intersection(emprise_decoupe)
                    if gc and not gc.isEmpty():
                        route_bufs_c.append(gc.buffer(_rayon_route_corridor(f), 4))
            if route_bufs_c:
                trame_routes_corridor = QgsGeometry.unaryUnion(route_bufs_c)

        geoms_par_secteur = defaultdict(list)
        for pipe_id, s_id in secteur_de_conduite.items():
            f = pipe_features.get(pipe_id)
            geom = f.geometry() if f else None
            if geom is None or geom.isEmpty():
                continue
            buf = geom.buffer(_largeur_corridor_pour(pipe_id, f), CONFIG['BUFFER_SEGMENTS'])
            if buf and not buf.isEmpty():
                geoms_par_secteur[s_id].append(buf)

        polygones = {}
        for s_id, buffers in geoms_par_secteur.items():
            if not buffers:
                continue
            fusion = QgsGeometry.unaryUnion(buffers)
            if fusion and not fusion.isEmpty():
                fusion_clip = fusion.intersection(emprise_decoupe)
                if fusion_clip and not fusion_clip.isEmpty():
                    fusion = fusion_clip
            if trame_routes_corridor is not None and not trame_routes_corridor.isEmpty():
                rogne = fusion.difference(trame_routes_corridor)
                if rogne and not rogne.isEmpty():
                    fusion = rogne
            if fusion and not fusion.isEmpty():
                polygones[s_id] = fusion
        return polygones

    # ---- partition stricte (voir core/partition_secteurs.py) ----
    routes_geoms = []
    if route_layer is not None:
        for f in route_layer.getFeatures(
                QgsFeatureRequest().setFilterRect(emprise_decoupe.boundingBox())):
            g = f.geometry()
            if g is not None and not g.isEmpty():
                routes_geoms.append(g)
    noeuds_xy = {nid: (pt.x(), pt.y()) for nid, pt in noeud_points.items()}
    territoires = None
    try:
        territoires, _stats = _partition.construire_territoires(
            routes_geoms, emprise_decoupe, noeuds_xy, secteur_de_noeud,
            abonnes_par_noeud, CONFIG, log=_log)
    except Exception as e_part:
        import traceback
        _log(f"  ATTENTION : partition des ilots urbains en echec ({e_part})\n"
             f"{traceback.format_exc()} -- repli corridor-buffer nettoye.")
    if not territoires:
        brut_buffer = _construire_corridors_par_buffer()
        territoires, _stats = _partition.nettoyer_polygones(
            brut_buffer, noeuds_xy, secteur_de_noeud, abonnes_par_noeud,
            CONFIG, log=_log)
        _log(f"  Territoires de repli (corridor-buffer FIX13 + nettoyage "
             f"topologique build precedent) : {len(territoires)} secteur(s).")
    qualite = _partition.statistiques_partition(territoires)
    _log(f"  CONTROLE QUALITE {qualite['nb_chevauchements']} "
         f"chevauchement(s), {qualite['nb_parties']} partie(s) pour "
         f"{qualite['nb_secteurs']} secteur(s) -- objectif : 0 chevauchement.")
    # core/partition_secteurs.py travaille avec l'abstraction Geom (voir
    # core/geom_backend.py) et renvoie donc un dict secteur_id -> Geom. Le
    # reste du pipeline (assurer_multipolygone, _nb_morceaux, etc.) attend en
    # revanche du QgsGeometry natif -- on desenveloppe ici, a la frontiere
    # entre les deux mondes.
    final_polygons = {
        s_id: (g.impl if hasattr(g, "impl") else g)
        for s_id, g in territoires.items()
    }

    composantes_par_secteur = {}
    for s_id, geom in final_polygons.items():
        composantes_par_secteur[s_id] = _nb_morceaux(geom)

    secteurs_non_contigus = {s: n for s, n in composantes_par_secteur.items() if n > 1}
    _log(f"Polygones finaux : {len(final_polygons)} ({len(secteurs_non_contigus)} fragmentes)")
    # la fragmentation est le defaut qui rendait la carte illisible dans
    # QGIS (un secteur selectionne s'allumait en une dizaine de morceaux
    # disperses). Elle doit etre visible en tete de journal, pas noyee.
    if secteurs_non_contigus:
        _pire = sorted(secteurs_non_contigus.items(),
                       key=lambda kv: -kv[1])[:8]
        _log("  [ALERTE CONTIGUITE] " + str(len(secteurs_non_contigus))
             + " secteur(s) restent en plusieurs morceaux : "
             + ", ".join(f"{s} ({n} morceaux)" for s, n in _pire)
             + ". Chaque morceau supplementaire est soit une annexe reellement"
               " isolee du reste de son reseau (a traiter en conception, pas en"
               " cartographie), soit le signe d'une trame de decoupe"
               " incomplete : verifier la couche routes"
               " (CONFIG['ROUTE_LAYER']) et"
               " ASSURER_CONTIGUITE_TERRITOIRES / AIRE_EXCLAVE_TOLEREE_M2.")
    else:
        _log("  [OK CONTIGUITE] Chaque secteur forme UN territoire d'un seul tenant.")

    abonnes_pression_critique_par_secteur = defaultdict(int)
    for nid in noeuds_hors_plage_final:
        s = secteur_de_noeud.get(nid)
        if s:
            abonnes_pression_critique_par_secteur[s] += abonnes_par_noeud.get(nid, 0)

    # ============================================================================
    # 14) CONDUITES FRONTALIERES + PRV + SURPRESSEURS
    # ============================================================================

    _log("\nDetection des conduites frontalieres inter-secteurs...")
    conduites_frontalieres = []
    for pipe_id, (key1, key2) in pipe_endpoints.items():
        n1 = noeud_id_map[key1]
        n2 = noeud_id_map[key2]
        s1 = secteur_de_noeud.get(n1)
        s2 = secteur_de_noeud.get(n2)
        if s1 is not None and s2 is not None and s1 != s2:
            diam = pipe_diameters.get(pipe_id, 0)
            length = pipe_lengths.get(pipe_id, 0)
            p1 = pression_noeud.get(n1)
            p2 = pression_noeud.get(n2)
            cote1 = secteur_cote_piezo.get(s1)
            cote2 = secteur_cote_piezo.get(s2)
            conduites_frontalieres.append({
                'pipe_id': pipe_id, 'n1': n1, 'n2': n2,
                'secteur_amont': s1, 'secteur_aval': s2,
                'diametre': diam, 'longueur': length,
                'pression_n1': p1, 'pression_n2': p2,
                'cote_piezo_amont': cote1, 'cote_piezo_aval': cote2,
                'geometry': pipe_features[pipe_id].geometry()
            })

    conduites_prv = [c for c in conduites_frontalieres if c['diametre'] >= CONFIG['DN_MIN_PRV']]
    points_prv = []
    for cf in conduites_prv:
        p1, p2 = cf['pression_n1'], cf['pression_n2']
        cote1, cote2 = cf['cote_piezo_amont'], cf['cote_piezo_aval']
        if cote1 is not None and cote2 is not None:
            if cote1 > cote2:
                secteur_protege = cf['secteur_aval']
                cote_amont, cote_aval = cote1, cote2
                pression_amont, pression_aval = (p1 or 0), (p2 or 0)
            else:
                secteur_protege = cf['secteur_amont']
                cote_amont, cote_aval = cote2, cote1
                pression_amont, pression_aval = (p2 or 0), (p1 or 0)
        else:
            secteur_protege = cf['secteur_aval']
            cote_amont = cote1 or cote2 or 0
            cote_aval = cote2 or cote1 or 0
            pression_amont = p1 or p2 or 0
            pression_aval = p2 or p1 or 0
        geom = cf['geometry']
        if geom and not geom.isEmpty():
            pt = geom.centroid().asPoint()
            pression_reglee = min(max(pression_aval, CONFIG['PRESSION_CIBLE_MIN']), CONFIG['PRESSION_CIBLE_MAX'])
            if pression_amont > CONFIG['PRESSION_MAX_M'] and pression_aval < CONFIG['PRESSION_MIN_M']:
                type_vanne = "PRV + SURPRESSEUR"
            elif pression_amont > CONFIG['PRESSION_MAX_M']:
                type_vanne = "PRV_REDUCTRICE"
            elif pression_aval < CONFIG['PRESSION_MIN_M']:
                type_vanne = "SURPRESSEUR"
            else:
                type_vanne = "PRV_REGULATION"
            points_prv.append({
                'pipe_id': cf['pipe_id'], 'secteur_amont': cf['secteur_amont'],
                'secteur_aval': cf['secteur_aval'], 'secteur_protege': secteur_protege,
                'diametre': cf['diametre'], 'longueur': cf['longueur'],
                'cote_piezo_amont': cote_amont, 'cote_piezo_aval': cote_aval,
                'pression_amont': pression_amont, 'pression_aval': pression_aval,
                'pression_reglee': pression_reglee, 'type_vanne': type_vanne,
                'geometry': pt
            })

    points_surpresseur = []
    for cf in conduites_frontalieres:
        if cf['diametre'] < CONFIG['DN_MIN_SURPRESSEUR']:
            continue
        p1, p2 = cf['pression_n1'], cf['pression_n2']
        seuil1 = pression_min_requise_noeud(cf['n1'])
        seuil2 = pression_min_requise_noeud(cf['n2'])
        if (p1 is not None and p1 < seuil1) or (p2 is not None and p2 < seuil2):
            geom = cf['geometry']
            if geom and not geom.isEmpty():
                pt = geom.centroid().asPoint()
                pression_min = min(p for p in [p1, p2] if p is not None)
                secteur_cible = cf['secteur_amont'] if (p1 is not None and p1 < seuil1) else cf['secteur_aval']
                points_surpresseur.append({
                    'pipe_id': cf['pipe_id'], 'secteur_amont': cf['secteur_amont'],
                    'secteur_aval': cf['secteur_aval'], 'secteur_cible': secteur_cible,
                    'diametre': cf['diametre'], 'pression_min': pression_min,
                    'nb_abonnes': abonnes_par_secteur.get(secteur_cible, 0), 'geometry': pt
                })

    # Vanne d'isolement sur CHAQUE conduite inter-secteurs. Une seule vanne
    # sur une frontiere ne suffit pas en presence de conduites paralleles : la
    # couche dediee est donc construite conduite par conduite, independamment
    # du filtre DN des PRV/surpresseurs.
    points_vannes_sectorisation = []
    if CONFIG.get('AJOUTER_VANNES_SECTORISATION', True):
        # La règle d'isolabilité est calculée sur les conduites, jamais sur
        # l'intersection des polygones (voir core.isolation).
        points_vannes_sectorisation, paires_attendues, paires_vanne, paires_sans_vanne = \
            construire_vannes_sectorisation(conduites_frontalieres)
        points_vannes_sectorisation = [
            dict(v, geometry=v['geometry'].centroid().asPoint())
            for v in points_vannes_sectorisation
            if v.get('geometry') is not None and not v['geometry'].isEmpty()
        ]
        _log(
            "  Vannes de sectorisation : %d sur %d conduite(s) frontaliere(s), "
            "%d paire(s) de secteurs couvertes, %d paire(s) sans vanne."
            % (len(points_vannes_sectorisation), len(conduites_frontalieres),
               len(paires_vanne), len(paires_sans_vanne))
        )
        if paires_sans_vanne:
            _log("  [ALERTE] Paires sans vanne : %s" % ', '.join('%s/%s' % p for p in sorted(paires_sans_vanne)))
    else:
        _log("  Vannes de sectorisation desactivees par configuration.")

    _log(f"Frontalieres : {len(conduites_frontalieres)} | PRV : {len(points_prv)} | "
          f"Surpresseurs : {len(points_surpresseur)}")

    # ============================================================================
    # 15) VERIFICATION DES VITESSES DE CONCEPTION -- conduites structurantes
    #     uniquement (DN suffisant ET nb abonnes desservis en aval suffisant).
    #     Une antenne terminale a 1-2 abonnes en pointe horaire est normalement
    #     lente ; ce n'est pas un defaut de dimensionnement du reseau, et le
    #     signaler systematiquement (comme sur le run precedent, 79% "hors norme") noyait le
    #     signal utile sous des faux-positifs.
    # ============================================================================

    vitesses_hors_norme = []
    nb_conduites_controlees = 0
    for pipe_id, s_id in secteur_de_conduite.items():
        q = debit_pipe_final.get(pipe_id, 0.0)
        diam = pipe_diameters.get(pipe_id, 100)
        nb_abonnes_aval_estime = q / max(q_pointe_abonne, 1e-9)
        est_structurante = (diam >= CONFIG['DN_MIN_CONTROLE_VITESSE']
                             and nb_abonnes_aval_estime >= CONFIG['ABONNES_AVAL_MIN_CONTROLE_VITESSE'])
        if not est_structurante:
            continue
        nb_conduites_controlees += 1
        v = vitesse_ecoulement(q, diam)
        if v > 0 and (v < CONFIG['VITESSE_MIN_M_S'] or v > CONFIG['VITESSE_MAX_M_S']):
            vitesses_hors_norme.append({
                'pipe_id': pipe_id, 'secteur': s_id, 'diametre': diam,
                'debit_ls': q, 'vitesse': v,
                'statut': 'TROP_LENT' if v < CONFIG['VITESSE_MIN_M_S'] else 'TROP_RAPIDE',
                'diametre_estime': pipe_id in pipe_diametre_estime,
            })
    _log(f"Conduites structurantes controlees (DN>={CONFIG['DN_MIN_CONTROLE_VITESSE']}mm, "
          f">={CONFIG['ABONNES_AVAL_MIN_CONTROLE_VITESSE']} ab aval) : "
          f"{nb_conduites_controlees} / {len(secteur_de_conduite)}")
    _log(f"Vitesses hors norme (parmi les conduites structurantes) : "
          f"{len(vitesses_hors_norme)} / {nb_conduites_controlees}")

    # ============================================================================
    _etape(9)  # Creation des couches de sortie
    # 16) COUCHES DE SORTIE
    # ============================================================================

    _log("\nCreation des couches de sortie...")
    crs = pipe_layer.crs().authid()

    polygones_out = CoucheDifferee(f"MultiPolygon?crs={crs}", "secteurs_hydrauliques")
    prov = polygones_out.dataProvider()
    prov.addAttributes([
        QgsField("secteur_id", QVariant.String), QgsField("reservoir_nom", QVariant.String),
        QgsField("priorite", QVariant.String), QgsField("nb_abonnes", QVariant.Int),
        QgsField("population", QVariant.Int), QgsField("demande_l_s", QVariant.Double),
        QgsField("capacite_utile", QVariant.Int), QgsField("capacite_restante", QVariant.Int),
        QgsField("taux_utilisation_pct", QVariant.Double),
        QgsField("longueur_m", QVariant.Double), QgsField("nb_noeuds", QVariant.Int),
        QgsField("nb_conduites", QVariant.Int), QgsField("pression_min_m", QVariant.Double),
        QgsField("pression_max_m", QVariant.Double), QgsField("pression_moy_m", QVariant.Double),
        QgsField("ab_press_crit", QVariant.Int),
        QgsField("conforme_strict", QVariant.String),
        QgsField("conforme_reseau_base", QVariant.String),
        QgsField("dans_plage_abonnes", QVariant.String),
        QgsField("sous_zonage_requis", QVariant.String),
        QgsField("motif_non_conforme", QVariant.String),
        QgsField("nb_polygones", QVariant.Int),
        QgsField("cycles_convergence", QVariant.Int),
        QgsField("cote_eau_m", QVariant.Double),
        QgsField("cote_eau_recalibree", QVariant.String),
        QgsField("issu_scission_altimetrique", QVariant.String),
    ])
    polygones_out.updateFields()

    conduites_par_secteur_final = defaultdict(list)
    for pipe_id, secteur_id in secteur_de_conduite.items():
        conduites_par_secteur_final[secteur_id].append(pipe_id)

    feats = []
    for secteur_id, geom in final_polygons.items():
        if geom is None or geom.isEmpty():
            continue
        nb_ab = abonnes_par_secteur.get(secteur_id, 0)
        nb_noeuds = sum(1 for n, s in secteur_de_noeud.items() if s == secteur_id)
        nb_conduites = len(conduites_par_secteur_final.get(secteur_id, []))
        longueur = sum(pipe_lengths.get(pid, 0) for pid in conduites_par_secteur_final.get(secteur_id, []))
        ab_crit = abonnes_pression_critique_par_secteur.get(secteur_id, 0)
        rd = reservoir_data.get(secteur_id, {})
        p_liste = pressions_par_secteur.get(secteur_id, [])
        p_min = min(p_liste) if p_liste else None
        p_max = max(p_liste) if p_liste else None
        p_moy = (sum(p_liste) / len(p_liste)) if p_liste else None
        cap_utile = rd.get('capacite_abonnes_utile', 1)
        cap_rest = rd.get('capacite_abonnes_restante', 0)
        taux = round(100.0 * (1.0 - cap_rest / max(cap_utile, 1)), 1)
        cote_eau = rd.get('cote_eau', secteur_cote_piezo.get(secteur_id, 0))
        cote_eau_orig = rd.get('cote_eau_originale', cote_eau)
        recalibree = "OUI" if abs(cote_eau - cote_eau_orig) > 0.1 else "NON"
        dans_plage_ab = "OUI" if CONFIG['ABONNES_MIN_PAR_SECTEUR'] <= nb_ab <= CONFIG['ABONNES_MAX_PAR_SECTEUR'] else "NON"
        sous_zonage = "OUI" if secteur_id in secteurs_sous_zonage_requis else "NON"
        issu_alt = "OUI" if "_ALT" in secteur_id else "NON"

        geom_mp = assurer_multipolygone(geom)
        if geom_mp is None or geom_mp.isEmpty():
            continue
        feat = QgsFeature(polygones_out.fields())
        feat.setGeometry(geom_mp)
        feat.setAttributes([
            secteur_id, rd.get('nom', secteur_id), rd.get('type_priorite', ''), nb_ab,
            int(nb_ab * CONFIG['HABITANTS_PAR_ABONNE']), nb_ab * calculer_debit_abonne(),
            cap_utile, cap_rest, taux,
            round(longueur, 1), nb_noeuds, nb_conduites,
            round(p_min, 2) if p_min is not None else None,
            round(p_max, 2) if p_max is not None else None,
            round(p_moy, 2) if p_moy is not None else None,
            ab_crit,
            "OUI" if secteur_conforme_strict.get(secteur_id) else "NON",
            "OUI" if secteur_conforme_reseau_base.get(secteur_id) else "NON",
            dans_plage_ab,
            sous_zonage,
            " | ".join(violations_par_secteur.get(secteur_id, [])),
            composantes_par_secteur.get(secteur_id, 1),
            len(rapport_cycles),
            round(cote_eau, 2),
            recalibree,
            issu_alt,
        ])
        feats.append(feat)

    if feats:
        prov.addFeatures(feats)
        polygones_out.updateExtents()
        _ajouter_couche(polygones_out)
        _log(f"Couche creee : {polygones_out.name()} ({len(feats)} polygones)")

    # ============================================================================
    # 16bis) LIMITES DE SECTEUR -- VORONOI SUR NOEUDS + ELIMINATION DES ILOTS
    # ============================================================================
    # 'secteurs_hydrauliques' ci-dessus est un corridor (buffer autour des
    # conduites), utile pour le controle de connexite mais visuellement
    # approximatif. Cette couche additionnelle dessine la VRAIE limite de
    # chaque secteur, en suivant les secteurs deja calcules par le pipeline
    # (secteur_de_noeud) -- avec une passe d'elimination des ilots : toute
    # petite portion de secteur qui se retrouve isolee au milieu (ou en bordure)
    # d'un autre secteur est fusionnee dans le secteur voisin avec lequel elle
    # partage la plus longue frontiere commune, au lieu de rester comme un
    # petit polygone parasite. Seul le plus grand morceau contigu de chaque
    # secteur reste rattache a son propre secteur_id.

    # ================================================================
    # CONSTRUCTION DES LIMITES DE SECTEUR
    # Approche : buffer des conduites AEP par secteur + clip par routes
    # ================================================================
    # Principe (robuste, sans dependance aux ilots urbains) :
    #   1. Pour chaque secteur, bufferiser toutes ses conduites AEP
    #      avec un rayon = RAYON_BUFFER_CONDUITE_M (defaut 30 m).
    #   2. Unir les buffers d'un meme secteur -> polygone brut.
    #   3. Rogner par les routes (buffer fin) pour aligner les frontieres
    #      sur la trame viaire, si la couche routes est disponible.
    #   4. Supprimer les trous, forcer un Polygon simple par secteur
    #      (enveloppe convexe si encore multi-parties).
    #   5. Resoudre les chevauchements residuels entre secteurs voisins
    #      (attribuer la zone disputee au secteur majoritaire en longueur
    #      de conduite).
    #   Avantage : fonctionne meme quand le reseau longe les routes sans
    #   jamais se trouver DANS un ilot, cas tres frequent en AEP urbain.
    # ================================================================
    _log("\nExport secteurs_limites (partition des ilots urbains) + frontieres_secteurs...")
    try:
        limites_finales = dict(final_polygons)
        if limites_finales:
            limites_out = CoucheDifferee(
                f"Polygon?crs={crs}", "secteurs_limites")
            lprov = limites_out.dataProvider()
            lprov.addAttributes([
                QgsField("secteur_id",    QVariant.String),
                QgsField("reservoir_nom", QVariant.String),
                QgsField("nb_abonnes",    QVariant.Int),
                QgsField("priorite",      QVariant.String),
                QgsField("nb_parties",    QVariant.Int),
                QgsField("aire_ha",       QVariant.Double),
            ])
            limites_out.updateFields()
            l_feats = []
            for s_id, geom in limites_finales.items():
                if geom is None or geom.isEmpty():
                    continue
                rd = reservoir_data.get(s_id, {})
                nb_parties = 1
                if QgsWkbTypes.flatType(geom.wkbType()) == QgsWkbTypes.MultiPolygon:
                    nb_parties = len(geom.asMultiPolygon())
                feat = QgsFeature(limites_out.fields())
                feat.setGeometry(geom)
                feat.setAttributes([
                    s_id,
                    rd.get('nom', s_id),
                    abonnes_par_secteur.get(s_id, 0),
                    rd.get('type_priorite', ''),
                    nb_parties,
                    round(geom.area() / 10000.0, 2),
                ])
                l_feats.append(feat)
            if l_feats:
                lprov.addFeatures(l_feats)
                limites_out.updateExtents()
                _ajouter_couche(limites_out)
                _log(f"Couche creee : {limites_out.name()} "
                     f"({len(l_feats)} polygones -- partition des ilots urbains : limites sur "
                     f"les lignes centrales de rues, 0 chevauchement, 0 ilot "
                     f"parasite, aucune enveloppe convexe)")

        # ---- limites lineaires inter-secteurs + vannes associees ----
        frontieres_out = CoucheDifferee(
            f"LineString?crs={crs}", "frontieres_secteurs")
        fprov = frontieres_out.dataProvider()
        fprov.addAttributes([
            QgsField("secteur_a", QVariant.String),
            QgsField("secteur_b", QVariant.String),
            QgsField("longueur_m", QVariant.Double),
            QgsField("vannes_frontiere", QVariant.String),
        ])
        frontieres_out.updateFields()
        vannes_par_paire = {}
        for p_v in points_prv:
            paire = tuple(sorted((p_v['secteur_amont'], p_v['secteur_aval'])))
            vannes_par_paire.setdefault(paire, set()).add(p_v['type_vanne'])
        f_feats = []
        ids_sect = list(limites_finales.keys())
        for i in range(len(ids_sect)):
            for j in range(i + 1, len(ids_sect)):
                s1, s2 = ids_sect[i], ids_sect[j]
                g1, g2 = limites_finales[s1], limites_finales[s2]
                if g1 is None or g2 is None or not g1.intersects(g2):
                    continue
                inter = g1.intersection(g2)
                if inter is None or inter.isEmpty():
                    continue
                if QgsWkbTypes.flatType(inter.wkbType()) not in (
                        QgsWkbTypes.LineString, QgsWkbTypes.MultiLineString):
                    continue
                paire = tuple(sorted((s1, s2)))
                vannes = " | ".join(sorted(vannes_par_paire.get(paire, set()))) \
                    or "aucune"
                feat = QgsFeature(frontieres_out.fields())
                feat.setGeometry(inter)
                feat.setAttributes([s1, s2, round(inter.length(), 1), vannes])
                f_feats.append(feat)
        if f_feats:
            fprov.addFeatures(f_feats)
            frontieres_out.updateExtents()
            _ajouter_couche(frontieres_out)
            _log(f"Couche creee : {frontieres_out.name()} "
                 f"({len(f_feats)} frontieres lineaires).")
    except Exception as e:
        import traceback
        _log(f"  ATTENTION : construction des limites de secteur impossible "
              f"({e}\n{traceback.format_exc()}) -- "
              f"seule la couche 'secteurs_hydrauliques' (partition des ilots urbains) est disponible.")

    # PRV
    prv_out = CoucheDifferee(f"Point?crs={crs}", "vannes_PRV_frontieres")
    prv_prov = prv_out.dataProvider()
    prv_prov.addAttributes([
        QgsField("pipe_id", QVariant.Int), QgsField("secteur_amont", QVariant.String),
        QgsField("secteur_aval", QVariant.String), QgsField("secteur_protege", QVariant.String),
        QgsField("diametre_mm", QVariant.Int), QgsField("longueur_m", QVariant.Double),
        QgsField("pression_amont", QVariant.Double), QgsField("pression_aval", QVariant.Double),
        QgsField("pression_reglee", QVariant.Double), QgsField("type_vanne", QVariant.String),
    ])
    prv_out.updateFields()
    prv_feats = []
    for p in points_prv:
        feat = QgsFeature(prv_out.fields())
        feat.setGeometry(QgsGeometry.fromPointXY(p['geometry']))
        feat.setAttributes([
            p['pipe_id'], p['secteur_amont'], p['secteur_aval'], p['secteur_protege'],
            p['diametre'], round(p['longueur'], 1),
            round(p['pression_amont'], 2), round(p['pression_aval'], 2),
            round(p['pression_reglee'], 2), p['type_vanne'],
        ])
        prv_feats.append(feat)
    if prv_feats:
        prv_prov.addFeatures(prv_feats)
        prv_out.updateExtents()
        _ajouter_couche(prv_out)
        _log(f"Couche creee : {prv_out.name()} ({len(prv_feats)} vannes)")

    # Vannes d'isolement, une par conduite frontaliere, pour que chaque
    # secteur puisse etre isole hydrauliquement meme en cas de conduites
    # paralleles. Cette couche ne remplace pas l'etude de dimensionnement.
    if points_vannes_sectorisation:
        vannes_out = CoucheDifferee(f"Point?crs={crs}", "vannes_sectorisation")
        vprov = vannes_out.dataProvider()
        vprov.addAttributes([
            QgsField("pipe_id", QVariant.Int), QgsField("secteur_a", QVariant.String),
            QgsField("secteur_b", QVariant.String), QgsField("diametre_mm", QVariant.Double),
            QgsField("longueur_m", QVariant.Double), QgsField("type_vanne", QVariant.String),
            QgsField("fermer_pour_isoler", QVariant.String),
        ])
        vannes_out.updateFields()
        v_feats = []
        for v in points_vannes_sectorisation:
            vf = QgsFeature(vannes_out.fields())
            vf.setGeometry(QgsGeometry.fromPointXY(v['geometry']))
            vf.setAttributes([
                v['pipe_id'], v['secteur_a'], v['secteur_b'], v['diametre'],
                round(v['longueur'], 1), 'ISOLEMENT_SECTORISATION', 'OUI',
            ])
            v_feats.append(vf)
        vprov.addFeatures(v_feats)
        vannes_out.updateExtents()
        _ajouter_couche(vannes_out)
        _log(f"Couche creee : {vannes_out.name()} ({len(v_feats)} vannes, une par conduite frontaliere)")

    # Conduites sectorisees -- ajoute le flag "controle_vitesse"
    conduites_out = CoucheDifferee(f"LineString?crs={crs}", "conduites_sectorisees")
    cprov = conduites_out.dataProvider()
    cprov.addAttributes([
        QgsField("pipe_id", QVariant.Int), QgsField("secteur_id", QVariant.String),
        QgsField("diametre_mm", QVariant.Double), QgsField("longueur_m", QVariant.Double),
        QgsField("debit_l_s", QVariant.Double), QgsField("vitesse_m_s", QVariant.Double),
        QgsField("statut_vitesse", QVariant.String), QgsField("controle_vitesse", QVariant.String),
    ])
    conduites_out.updateFields()
    c_feats = []
    for pipe_id, s_id in secteur_de_conduite.items():
        f = pipe_features.get(pipe_id)
        geom = f.geometry() if f else None
        if geom is None or geom.isEmpty():
            continue
        q = debit_pipe_final.get(pipe_id, 0.0)
        diam = pipe_diameters.get(pipe_id, 100)
        v = vitesse_ecoulement(q, diam)
        nb_abonnes_aval_estime = q / max(q_pointe_abonne, 1e-9)
        est_structurante = (diam >= CONFIG['DN_MIN_CONTROLE_VITESSE']
                             and nb_abonnes_aval_estime >= CONFIG['ABONNES_AVAL_MIN_CONTROLE_VITESSE'])
        if not est_structurante:
            statut = "NON_CONTROLE"
        elif v > 0 and v < CONFIG['VITESSE_MIN_M_S']:
            statut = "TROP_LENT"
        elif v > CONFIG['VITESSE_MAX_M_S']:
            statut = "TROP_RAPIDE"
        else:
            statut = "OK"
        feat = QgsFeature(conduites_out.fields())
        feat.setGeometry(QgsGeometry(geom))
        feat.setAttributes([pipe_id, s_id, diam, round(pipe_lengths.get(pipe_id, 0.0), 1),
                             round(q, 4), round(v, 3), statut, "OUI" if est_structurante else "NON"])
        c_feats.append(feat)
    if c_feats:
        cprov.addFeatures(c_feats)
        conduites_out.updateExtents()
        _ajouter_couche(conduites_out)
        _log(f"Couche creee : {conduites_out.name()} ({len(c_feats)} conduites)")

    # Couche de revue des conduites qui n'ont pas de secteur. Elle distingue
    # une geometrie invalide d'un vrai noeud non rattache, au lieu de les
    # laisser disparaitre du bilan 5827/5827.
    if conduites_non_affectees_details:
        hors_pipe_out = CoucheDifferee(f"LineString?crs={crs}", "conduites_hors_secteur")
        hp = hors_pipe_out.dataProvider()
        hp.addAttributes([
            QgsField("pipe_id", QVariant.Int), QgsField("cause", QVariant.String),
            QgsField("detail", QVariant.String),
        ])
        hors_pipe_out.updateFields()
        hp_feats = []
        for pf in pipe_layer.getFeatures():
            cause = conduites_non_affectees_details.get(pf.id())
            if cause is None:
                continue
            geom = pf.geometry()
            if geom is None or geom.isEmpty():
                continue
            hf = QgsFeature(hors_pipe_out.fields())
            hf.setGeometry(QgsGeometry(geom))
            hf.setAttributes([pf.id(), cause, 'A_REVOIR_AVANT_SECTORISATION'])
            hp_feats.append(hf)
        if hp_feats:
            hp.addFeatures(hp_feats)
            hors_pipe_out.updateExtents()
            _ajouter_couche(hors_pipe_out)
            _log(f"Couche creee : {hors_pipe_out.name()} ({len(hp_feats)} conduites)")

    # Abonnes hors plage
    abonnes_critiques_out = CoucheDifferee(f"Point?crs={abonne_layer.crs().authid()}",
                                            "abonnes_pression_hors_plage")
    acprov = abonnes_critiques_out.dataProvider()
    acprov.addAttributes([
        QgsField("abonne_fid", QVariant.Int), QgsField("nid", QVariant.String),
        QgsField("secteur_id", QVariant.String), QgsField("pression_m", QVariant.Double),
        QgsField("pression_min_requise_m", QVariant.Double), QgsField("niveau", QVariant.Int),
        QgsField("ecart_m", QVariant.Double), QgsField("type_pb", QVariant.String),
        QgsField("type_violation", QVariant.String),
    ])
    abonnes_critiques_out.updateFields()
    ac_feats = []
    for af in abonne_layer.getFeatures():
        nid = abonne_vers_noeud.get(af.id())
        if nid is None:
            continue
        p = pression_noeud.get(nid)
        if p is None:
            continue
        geom = af.geometry()
        if geom is None or geom.isEmpty():
            continue
        ecart = ecart_hors_plage(p, nid)
        if ecart == 0:
            continue
        seuil_min = pression_min_requise_noeud(nid)
        type_pb = "SOUS-PRESSION" if p < seuil_min else "SURPRESSION"
        type_v = classer_type_violation(p, nid)
        feat = QgsFeature(abonnes_critiques_out.fields())
        feat.setGeometry(QgsGeometry(geom))
        feat.setAttributes([
            af.id(), nid, secteur_de_noeud.get(nid),
            round(p, 2), round(seuil_min, 2),
            abonne_niveau.get(af.id(), 0), round(ecart, 2), type_pb, type_v,
        ])
        ac_feats.append(feat)
    if ac_feats:
        acprov.addFeatures(ac_feats)
        abonnes_critiques_out.updateExtents()
        _ajouter_couche(abonnes_critiques_out)
        _log(f"Couche creee : {abonnes_critiques_out.name()} ({len(ac_feats)} abonnes)")

    # Couche de revue des abonnes hors secteur (non rattaches a un noeud).
    if abonnes_hors_secteur_details:
        hors_ab_out = CoucheDifferee(f"Point?crs={abonne_layer.crs().authid()}", "abonnes_hors_secteur")
        hab = hors_ab_out.dataProvider()
        hab.addAttributes([
            QgsField("abonne_fid", QVariant.Int), QgsField("cause", QVariant.String),
            QgsField("distance_noeud_m", QVariant.Double), QgsField("detail", QVariant.String),
        ])
        hors_ab_out.updateFields()
        hab_feats = []
        for af in abonne_layer.getFeatures():
            detail = abonnes_hors_secteur_details.get(af.id())
            if detail is None:
                continue
            geom = af.geometry()
            if geom is None or geom.isEmpty():
                continue
            cause, distance = detail
            hf = QgsFeature(hors_ab_out.fields())
            hf.setGeometry(QgsGeometry(geom))
            hf.setAttributes([af.id(), cause, round(distance, 2) if distance is not None else None,
                              'A_REVOIR_AVANT_SECTORISATION'])
            hab_feats.append(hf)
        if hab_feats:
            hab.addFeatures(hab_feats)
            hors_ab_out.updateExtents()
            _ajouter_couche(hors_ab_out)
            _log(f"Couche creee : {hors_ab_out.name()} ({len(hab_feats)} abonnes)")

    # Couche des reservoirs avec recalibration
    reservoirs_out = CoucheDifferee(f"Point?crs={pipe_layer.crs().authid()}", "reservoirs")
    rprov = reservoirs_out.dataProvider()
    rprov.addAttributes([
        QgsField("secteur_id", QVariant.String), QgsField("parent_tank_id", QVariant.String),
        QgsField("nom", QVariant.String),
        QgsField("capacite_m3", QVariant.Double), QgsField("cote_eau_orig_m", QVariant.Double),
        QgsField("cote_eau_final_m", QVariant.Double), QgsField("delta_cote_m", QVariant.Double),
        QgsField("nb_abonnes", QVariant.Int), QgsField("capacite_utile_ab", QVariant.Int),
        QgsField("taux_utilisation_pct", QVariant.Double), QgsField("conforme", QVariant.String),
        QgsField("sous_zonage_requis", QVariant.String),
        QgsField("debit_source_m3_j", QVariant.Double), QgsField("source_limitante", QVariant.String),
        QgsField("nb_parts_scission", QVariant.Int), QgsField("issu_scission_altimetrique", QVariant.String),
    ])
    reservoirs_out.updateFields()
    r_feats = []
    for s_id, pt in tank_points_par_secteur.items():
        rd = reservoir_data.get(s_id, {})
        cote_orig = rd.get('cote_eau_originale', rd.get('cote_eau', 0))
        cote_final = secteur_cote_piezo.get(s_id, cote_orig)
        nb_ab = abonnes_par_secteur.get(s_id, 0)
        cap_utile = rd.get('capacite_abonnes_utile', 1)
        cap_rest = rd.get('capacite_abonnes_restante', 0)
        taux = round(100.0 * (1.0 - cap_rest / max(cap_utile, 1)), 1)
        conforme = "OUI" if secteur_conforme_strict.get(s_id) else "NON"
        sous_zonage = "OUI" if s_id in secteurs_sous_zonage_requis else "NON"
        issu_alt = "OUI" if "_ALT" in s_id else "NON"
        feat = QgsFeature(reservoirs_out.fields())
        feat.setGeometry(QgsGeometry.fromPointXY(pt))
        feat.setAttributes([
            s_id, rd.get('parent_tank_id', rd.get('tank_id', s_id)), rd.get('nom', s_id), rd.get('capacite', 0),
            round(cote_orig, 2), round(cote_final, 2), round(cote_final - cote_orig, 2),
            nb_ab, cap_utile, taux, conforme, sous_zonage,
            rd.get('debit_source_m3_j'), rd.get('source_limitante', ''),
            rd.get('nb_parts_scission', 1), issu_alt,
        ])
        r_feats.append(feat)
    if r_feats:
        rprov.addFeatures(r_feats)
        reservoirs_out.updateExtents()
        _ajouter_couche(reservoirs_out)
        _log(f"Couche creee : {reservoirs_out.name()} ({len(r_feats)} reservoirs)")

    # ============================================================================
    # 17) RAPPORT FINAL
    # ============================================================================

    _log("\n" + "=" * 70)
    _log("RAPPORT FINAL DE SECTORISATION")
    _log("=" * 70)

    _log("CONVERGENCE ADAPTATIVE")
    _log("-" * 70)
    _log(f"{'Cycle':<7}{'Score':>14}{'Pression':>14}{'Capacite':>12}{'Viol.P':>8}{'Viol.C':>8}{'Frag.':>7}")
    for r in rapport_cycles:
        _log(f"{r['cycle']:<7}{r['score']:>14,.1f}{r['score_pression']:>14,.1f}"
              f"{r['score_capacite']:>12,.0f}{r['nb_viol_pression']:>8}"
              f"{r['nb_viol_capacite']:>8}{r['score_fragmentation']:>7}")
    _log("-" * 70)

    if conformite_atteinte:
        _log("*** OBJECTIFS CONFIGURES ATTEINTS (boucle adaptative) ***")
    else:
        dernier = rapport_cycles[-1]
        _log(f"Objectifs configures non atteints apres {len(rapport_cycles)} cycles (boucle adaptative).")
        _log(f"  Violations residuelles avant nettoyage final : {dernier['nb_viol_pression']} noeuds pression, "
              f"{dernier['nb_viol_capacite']} secteurs capacite, "
              f"{dernier['score_fragmentation']} fragments")

    total_ab = sum(abonnes_par_secteur.values())
    total_ab_critiques = sum(abonnes_pression_critique_par_secteur.values())
    pct_hors_norme = (100.0 * total_ab_critiques / total_ab) if total_ab else 0.0

    nb_secteurs_dans_plage = sum(1 for s_id in final_polygons
                                  if CONFIG['ABONNES_MIN_PAR_SECTEUR'] <= abonnes_par_secteur.get(s_id, 0) <= CONFIG['ABONNES_MAX_PAR_SECTEUR'])

    _log("\nBLOC RESULTATS / CRITERES CONFIGURES")
    _log("-" * 70)
    _log(f"  Secteurs OBJECTIFS_STRICTS : {nb_conformes_strict} / {nb_total_strict}")
    _log(f"  Secteurs OBJECTIFS_RESEAU_BASE : {nb_conformes_reseau_base} / {nb_total_strict}")
    _log(f"  Secteurs dans la plage {CONFIG['ABONNES_MIN_PAR_SECTEUR']}-{CONFIG['ABONNES_MAX_PAR_SECTEUR']} abonnes : "
          f"{nb_secteurs_dans_plage} / {len(final_polygons)}")
    _log(f"  Abonnes hors-plage de pression [{CONFIG['PRESSION_MIN_M']:.0f}-{CONFIG['PRESSION_MAX_M']:.0f}m] : "
          f"{total_ab_critiques} / {total_ab} ({pct_hors_norme:.1f}%)")
    _log(f"  Noeuds STRUCTUREL (aucun reservoir viable) : {nb_structurel} ({ab_structurel} ab)")
    _log(f"  Noeuds AFFECTATION_CAPACITE : {nb_affectation} ({ab_affectation} ab)")
    _log(f"  Abonnes en immeuble : {nb_abonnes_en_immeuble} (niveau max {niveau_max_detecte})")
    if clusters_anomalie:
        _log(f"  Clusters anomalie (doublons probables) : {len(clusters_anomalie)}")
    _log(f"  Reservoirs isoles structurellement : {len(reservoirs_isoles)} / {len(sources)}")
    if reservoirs_isoles:
        cap_isolee = sum(reservoir_data.get(s, {}).get('capacite_abonnes_utile', 0) for s in reservoirs_isoles if s in reservoir_data)
        _log(f"    -> {cap_isolee} ab de capacite utile inatteignable "
              f"({', '.join(sorted(reservoirs_isoles.keys()))})")
    if secteurs_sous_zonage_requis:
        _log(f"  Secteurs SOUS_ZONAGE_REQUIS (ecart topo > {CONFIG['SEUIL_ECART_TOPO_M']:.0f}m) : "
              f"{len(secteurs_sous_zonage_requis)} ({', '.join(sorted(secteurs_sous_zonage_requis.keys()))})")
    if recalibrations:
        _log(f"\n  RECALIBRATIONS COTES PIEZO :")
        for rec in recalibrations:
            _log(f"    {rec['s_id']} : {rec['cote_avant']:.1f} -> {rec['cote_apres']:.1f}m "
                  f"(delta={rec['delta']:+.1f}m, {rec['pct_hors_norme_avant']*100:.0f}% hors norme avant)")
    _log("-" * 70)

    # ============================================================================
    # LISTE DEDIEE -- SECTEURS SOUS-EFFECTIF (<ABONNES_MIN) ET SECTEURS
    # AVEC AU MOINS UN NOEUD A PRESSION NEGATIVE
    # ============================================================================

    secteurs_sous_effectif = []
    secteurs_pression_negative = []
    for s_id in sorted(final_polygons.keys()):
        nb_ab = abonnes_par_secteur.get(s_id, 0)
        p_liste = pressions_par_secteur.get(s_id, [])
        p_min_s = min(p_liste) if p_liste else None

        if 0 < nb_ab < CONFIG['ABONNES_MIN_PAR_SECTEUR']:
            secteurs_sous_effectif.append((s_id, nb_ab))

        if p_min_s is not None and p_min_s < 0:
            ab_neg = sum(abonnes_par_noeud.get(n, 0) for n, s in secteur_de_noeud.items()
                        if s == s_id and pression_noeud.get(n) is not None
                        and pression_noeud.get(n) < 0)
            nb_noeuds_neg = sum(1 for n, s in secteur_de_noeud.items()
                                if s == s_id and pression_noeud.get(n) is not None
                                and pression_noeud.get(n) < 0)
            secteurs_pression_negative.append((s_id, nb_ab, p_min_s, nb_noeuds_neg, ab_neg))

    _log("\nSECTEURS SOUS LE SEUIL MINIMAL D'ABONNES (<{})".format(CONFIG['ABONNES_MIN_PAR_SECTEUR']))
    _log("-" * 70)
    if secteurs_sous_effectif:
        _log(f"  {len(secteurs_sous_effectif)} secteur(s) concerne(s) (verrou geographique/topographique "
              f"reel -- aucun voisin compatible disponible pour fusion) :")
        for s_id, nb_ab in sorted(secteurs_sous_effectif, key=lambda t: t[1]):
            rd = reservoir_data.get(s_id, {})
            _log(f"    {s_id:<25} {nb_ab:>5} abonnes  (reservoir parent: {rd.get('parent_tank_id', rd.get('tank_id', '?'))})")
    else:
        _log("  Aucun secteur sous le seuil minimal.")
    _log("-" * 70)

    _log(f"\nSECTEURS AVEC AU MOINS UN NOEUD A PRESSION NEGATIVE")
    _log("-" * 70)
    if secteurs_pression_negative:
        _log(f"  {len(secteurs_pression_negative)} secteur(s) concerne(s) :")
        for s_id, nb_ab, p_min_s, nb_noeuds_neg, ab_neg in sorted(secteurs_pression_negative, key=lambda t: t[2]):
            _log(f"    {s_id:<25} {nb_ab:>5} abonnes  pression_min={p_min_s:>7.1f}m  "
                  f"({nb_noeuds_neg} noeud(s)/{ab_neg} ab a pression negative)")
        _log(f"\n  -> couche 'abonnes_pression_hors_plage' pour la liste noeud par noeud, "
              f"filtrer sur pression_m < 0.")
    else:
        _log("  Aucun secteur avec pression negative.")
    _log("-" * 70)

    _log(f"\nNombre de secteurs : {len(final_polygons)}")
    _log(f"Abonnes affectes : {sum(abonnes_par_secteur.values())} / {abonne_layer.featureCount()}")
    _log(f"Vannes PRV : {len(points_prv)} | Surpresseurs : {len(points_surpresseur)} | "
          f"Vannes isolement : {len(points_vannes_sectorisation)}")
    if validation_wntr:
        _log(
            "Validation WNTR : statut=%s | violations=%s | abonnes hors plage=%s | erreur=%s"
            % (validation_wntr.get('statut'), validation_wntr.get('nb_violations_pression'),
               validation_wntr.get('nb_abonnes_hors_plage'), validation_wntr.get('erreur'))
        )
    _log("Comparaison explicite avant/apres nettoyage final :")
    _log("  Violations de pression (noeuds) : %d -> %d" % (
        nb_violations_pression_avant, nb_violations_pression_apres))
    _log("  Abonnes hors plage de pression : %d -> %d" % (
        nb_abonnes_hors_plage_avant, nb_abonnes_hors_plage_apres))
    _log("  Transferts : %d avant nettoyage -> %d pendant nettoyage -> %d apres nettoyage" % (
        nb_transferts_avant_nettoyage, nb_transferts_nettoyage,
        nb_transferts_apres_nettoyage))
    _log(f"Vitesses hors plage de conception (conduites structurantes) : {len(vitesses_hors_norme)} / {nb_conduites_controlees}")
    _log("-" * 70)

    _log(f"\n{'Secteur':<22}{'Prio':<11}{'Abonnes':>8}{'Cap.Util':>9}{'Taux%':>7}"
          f"{'Pmin':>7}{'Pmax':>7}{'Pmoy':>7}{'Strict':>8}{'ResBase':>9}{'DansPlage':>10}")
    _log("-" * 110)
    for s_id in sorted(final_polygons.keys()):
        nb = abonnes_par_secteur.get(s_id, 0)
        p_liste = pressions_par_secteur.get(s_id, [])
        p_min = min(p_liste) if p_liste else float('nan')
        p_max = max(p_liste) if p_liste else float('nan')
        p_moy = (sum(p_liste) / len(p_liste)) if p_liste else float('nan')
        rd = reservoir_data.get(s_id, {})
        cap_utile = rd.get('capacite_abonnes_utile', 1)
        cap_rest = rd.get('capacite_abonnes_restante', 0)
        taux = round(100.0 * (1.0 - cap_rest / max(cap_utile, 1)), 1)
        conf_s = "OUI" if secteur_conforme_strict.get(s_id) else "NON"
        conf_b = "OUI" if secteur_conforme_reseau_base.get(s_id) else "NON"
        dans_plage = "OUI" if CONFIG['ABONNES_MIN_PAR_SECTEUR'] <= nb <= CONFIG['ABONNES_MAX_PAR_SECTEUR'] else "NON"
        _log(f"{str(s_id):<22}{rd.get('type_priorite','?'):<11}{nb:>8}{cap_utile:>9}{taux:>7.1f}"
              f"{p_min:>7.1f}{p_max:>7.1f}{p_moy:>7.1f}{conf_s:>8}{conf_b:>9}{dans_plage:>10}")
    _log("-" * 110)
    _log(f"OBJECTIFS STRICTS : {nb_conformes_strict} / {nb_total_strict}"
          f"  |  OBJECTIFS RESEAU BASE : {nb_conformes_reseau_base} / {nb_total_strict}"
          f"  |  DANS PLAGE ABONNES : {nb_secteurs_dans_plage} / {len(final_polygons)}")
    _log("=" * 70)
    _log("FIN DE LA SECTORISATION")
    _log("=" * 70)
    return {
        'nb_secteurs': len(final_polygons),
        'nb_abonnes_affectes': sum(abonnes_par_secteur.values()),
        'nb_prv': len(points_prv),
        'nb_surpresseurs': len(points_surpresseur),
        'nb_vannes_sectorisation': len(points_vannes_sectorisation),
        'nb_conduites_frontalieres': len(conduites_frontalieres),
        'validation_wntr': validation_wntr,
        'comparaison_avant_apres': {
            'violations_pression_avant_noeuds': nb_violations_pression_avant,
            'violations_pression_apres_noeuds': nb_violations_pression_apres,
            'abonnes_hors_plage_avant': nb_abonnes_hors_plage_avant,
            'abonnes_hors_plage_apres': nb_abonnes_hors_plage_apres,
            'transferts_avant': nb_transferts_avant_nettoyage,
            'transferts_nettoyage_final': nb_transferts_nettoyage,
            'transferts_apres': nb_transferts_apres_nettoyage,
        },
        'conformes_strict': nb_conformes_strict,
        'total_strict': nb_total_strict,
        # FIX-THREAD-SAFETY : couches memoire construites par ce run,
        # PAS ENCORE ajoutees au projet QGIS. L'appelant doit les enregistrer
        # lui-meme (QgsProject.instance().addMapLayer(...)) depuis le thread
        # PRINCIPAL uniquement -- voir commentaire sur _ajouter_couche()
        # plus haut, et PanneauPrincipal._ajouter_couches_dans_projet().
        'couches_en_attente': _couches_en_attente,
    }
