# -*- coding: utf-8 -*-
"""
Gestionnaire de configuration - Sectorisation Hydraulique
Charge, sauvegarde et valide la configuration JSON.
Convertit entre le format JSON imbriqué et le dict plat attendu par le moteur.
"""

import json
import os
import copy


# Chemin du fichier de config par défaut (dans le dossier de l'extension)
_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DEFAUT_PATH = os.path.join(_PLUGIN_DIR, 'config_defaut.json')


# Clés de config renommées lors de l'unification de la version (les anciens
# fichiers JSON sauvegardés avec un nom ancien restent exploitables : la
# valeur de l'ancienne clé est reportée sur la nouvelle, sans être perdue).
_CLES_RENOMMEES = {
    'ALIGNER_SUR_SECTEURS_V67': 'ALIGNER_SUR_SECTEURS_REFERENCES',
}


def charger_config_json(chemin=None):
    """
    Charge une config depuis un fichier JSON.
    Si chemin est None, charge le fichier par défaut.
    Retourne le dict JSON imbriqué.
    """
    path = chemin or CONFIG_DEFAUT_PATH
    if not os.path.exists(path):
        raise FileNotFoundError(f"Fichier de configuration introuvable : {path}")
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    # Renommage des cles devenues obsolètes (unification de la version) :
    # on reporte la valeur de l'ancienne cle sur la nouvelle, si l'utilisateur
    # l'avait modifiée.
    for section, valeurs in data.items():
        if not isinstance(valeurs, dict):
            continue
        for ancienne, nouvelle in _CLES_RENOMMEES.items():
            if ancienne in valeurs:
                valeurs.setdefault(nouvelle, valeurs[ancienne])
                del valeurs[ancienne]
    if path != CONFIG_DEFAUT_PATH:
        # Config enregistree par une version anterieure du plugin : on complete
        # les cles manquantes avec les valeurs par defaut de la version
        # courante (ex. les cles de contiguite des territoires). Les choix
        # de l'utilisateur sont conserves tels quels ; seules les cles absentes
        # sont ajoutees, sans quoi un nouveau parametre resterait invisible
        # dans l'interface et ne serait jamais reenregistre.
        try:
            with open(CONFIG_DEFAUT_PATH, 'r', encoding='utf-8') as fd:
                defauts = json.load(fd)
        except Exception:
            return data
        for section, valeurs in defauts.items():
            if section not in data:
                data[section] = copy.deepcopy(valeurs)
            elif isinstance(valeurs, dict) and isinstance(data[section], dict):
                for cle, val in valeurs.items():
                    data[section].setdefault(cle, copy.deepcopy(val))
    return data


def sauvegarder_config_json(config_json, chemin):
    """
    Sauvegarde la config imbriquée dans un fichier JSON.
    """
    with open(chemin, 'w', encoding='utf-8') as f:
        json.dump(config_json, f, ensure_ascii=False, indent=2)


def json_vers_plat(config_json):
    """
    Convertit le dict JSON imbriqué en dict plat attendu par le moteur.
    Fusionne toutes les sections (COUCHES, HYDRAULIQUE, etc.) en un seul dict.
    Les clés des COUCHES et CHAMPS sont gardées telles quelles.
    """
    plat = {}
    for section, valeurs in config_json.items():
        if section.startswith('_'):
            continue
        if isinstance(valeurs, dict):
            plat.update(valeurs)
        else:
            plat[section] = valeurs

    # Corrections de type (JSON ne distingue pas int/float)
    ENTIERS = {
        'ABONNES_MIN_PAR_SECTEUR', 'ABONNES_MAX_PAR_SECTEUR',
        'ABONNES_IDEAL_PAR_SECTEUR', 'ABONNES_IDEAL_CIBLE_SCISSION',
        'TAILLE_MIN_COUPURE_AB', 'MAX_ITER_CONSOLIDATION',
        'DN_MIN_PRV', 'DN_MIN_SURPRESSEUR',
        'DN_MIN_CONTROLE_VITESSE', 'ABONNES_AVAL_MIN_CONTROLE_VITESSE',
        'PRIORITE_SCORE_PRINCIPAL', 'PRIORITE_SCORE_SECONDAIRE',
        'PRIORITE_SCORE_SECOURS',
        'NIVEAU_MAX_PLAUSIBLE', 'BLOC_MAX_NIVEAUX', 'SEUIL_BLOCS_ANOMALIE',
        'NB_ITERATIONS_INTERNES', 'SEED_BASE', 'BUFFER_SEGMENTS',
        'MAX_CYCLES_CONFORMITE', 'MAX_DURCISSEMENTS_CONSECUTIFS',
        'SEUIL_DECLENCHEMENT_REDISTRIB',
        'MAX_ROUNDS_RAFFINEMENT', 'MAX_TRANSFERTS_PAR_NOEUD', 'MAX_SAUTS_RAFFINEMENT',
        'MAX_SAUTS_REDISTRIBUTION', 'TAILLE_MAX_SOUS_ARBRE_REDISTRIBUTION',
        'MAX_TRANSFERTS_REDIS', 'SEUIL_ABONNES_ISOLEMENT',
        'MAX_ITER_CORRECTION_FORCEE', 'MAX_PASSES_GARANTIE_CONNEXITE_FINALE',
        'MAX_PASSES_CONTIGUITE', 'ZONE_MIN_NOEUDS',
    }
    for cle in ENTIERS:
        if cle in plat and plat[cle] is not None:
            plat[cle] = int(plat[cle])

    return plat


def plat_vers_json(config_plat, config_json_ref=None):
    """
    Convertit un dict plat (modifié par l'UI) en dict JSON imbriqué.
    Utilise config_json_ref comme template de structure (sections).
    """
    if config_json_ref is None:
        config_json_ref = charger_config_json()

    result = copy.deepcopy(config_json_ref)
    for section, valeurs in result.items():
        if section.startswith('_'):
            continue
        if isinstance(valeurs, dict):
            for cle in valeurs:
                if cle in config_plat:
                    result[section][cle] = config_plat[cle]
    return result


def valider_config(config_plat):
    """
    Valide les valeurs critiques de la config.
    Retourne une liste de messages d'erreur (vide si tout est OK).
    """
    erreurs = []

    p_min = config_plat.get('PRESSION_MIN_M', 10)
    p_max = config_plat.get('PRESSION_MAX_M', 60)
    p_cible = config_plat.get('PRESSION_CIBLE_M', 30)

    if p_min >= p_max:
        erreurs.append(f"Pression min ({p_min}) doit être < pression max ({p_max})")
    if not (p_min <= p_cible <= p_max):
        erreurs.append(f"Pression cible ({p_cible}) doit être entre {p_min} et {p_max} m")

    ab_min = config_plat.get('ABONNES_MIN_PAR_SECTEUR', 500)
    ab_max = config_plat.get('ABONNES_MAX_PAR_SECTEUR', 3000)
    ab_ideal = config_plat.get('ABONNES_IDEAL_PAR_SECTEUR', 2000)
    if ab_min >= ab_max:
        erreurs.append(f"Abonnés min ({ab_min}) doit être < abonnés max ({ab_max})")
    if not (ab_min <= ab_ideal <= ab_max):
        erreurs.append(f"Abonnés idéal ({ab_ideal}) doit être entre {ab_min} et {ab_max}")

    v_min = config_plat.get('VITESSE_MIN_M_S', 0.3)
    v_max = config_plat.get('VITESSE_MAX_M_S', 2.0)
    if v_min >= v_max:
        erreurs.append(f"Vitesse min ({v_min}) doit être < vitesse max ({v_max} m/s)")

    bande = config_plat.get('BANDE_PRESSION_UTILE_M', 45)
    if bande > (p_max - p_min):
        erreurs.append(
            f"Bande de pression utile ({bande} m) > plage pression ({p_max - p_min} m) — "
            f"aucune coupure altimétrique ne sera déclenchée"
        )

    c = config_plat.get('COEF_HAZEN_WILLIAMS', 130)
    if not (80 <= c <= 160):
        erreurs.append(f"Coefficient Hazen-Williams ({c}) hors plage usuelle [80-160]")

    return erreurs
