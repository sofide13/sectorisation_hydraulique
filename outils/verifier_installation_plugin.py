# -*- coding: utf-8 -*-
"""
verifier_installation_plugin.py
================================
Script autonome (Python pur, AUCUNE dependance a qgis.core) qui verifie que
le plugin reellement installe dans le profil QGIS correspond bien a CETTE
livraison (sectorisation_hydraulique.zip), et non a une ancienne copie
laissee en place par une mise a jour precedente.

POURQUOI CE SCRIPT EXISTE
--------------------------
Symptome observe : le pipeline produit des secteurs_hydrauliques /
secteurs_limites en grands polygones "enveloppe convexe" qui debordent
tres largement hors du bati (cf. images de run), alors que le code source
livre (core/partition_secteurs.py) ne calcule plus JAMAIS de convex hull
depuis le correctif FIX-PARTITION -- et que le code mort qui produisait
ces enveloppes a ete entierement SUPPRIME du build precedent. Un log qui en porte
encore la signature prouve qu'un pipeline.py different de celui de ce zip
a ete execute par QGIS. Cause typique : le dossier du plugin dans
le profil QGIS (ou son cache __pycache__) n'a pas ete entierement remplace,
ou QGIS n'a pas ete redemarre completement apres la mise a jour (un simple
"recharger le plugin" ne reimporte pas forcement tous les sous-modules
core.pipeline / core.partition_secteurs / core.geom_backend deja
presents dans sys.modules).

CE QUE FAIT CE SCRIPT
----------------------
1. Calcule l'empreinte sha256 de chaque fichier-cle de CETTE livraison
   (valeurs figees ci-dessous, recalculees a la generation de ce script).
2. Localise le(s) dossier(s) plugin QGIS plausible(s) sur la machine (ou
   utilise le chemin fourni en argument).
3. Compare fichier par fichier : identique / different / absent.
4. Recherche aussi un marqueur fonctionnel independant du hash (presence de
   "construire_territoires" dans partition_secteurs.py) pour detecter une
   version fonctionnellement perimee meme apres une modification mineure.
5. Signale toute trace de cache Python (*.pyc, __pycache__) plus recente que
   le .py source correspondant, cause frequente de code perime execute.

UTILISATION
-----------
En dehors de QGIS (recommande, terminal / invite de commandes classique) :
    python verifier_installation_plugin.py
    python verifier_installation_plugin.py --plugin-dir "C:\\chemin\\vers\\sectorisation_hydraulique"

Dans la console Python de QGIS (Extensions -> Console Python) :
    exec(open(r"CHEMIN\\COMPLET\\vers\\verifier_installation_plugin.py", encoding="utf-8").read())
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import sys
import time

# ---------------------------------------------------------------------------
# 1) Fichiers-cles compares. L'empreinte de reference n'est PLUS figee dans ce
#    script : elle est recalculee a chaque lancement depuis les fichiers de
#    CETTE livraison (dossier parent de outils/). Une table d'empreintes ecrite
#    a la main devenait fausse a chaque correctif et faisait passer une
#    installation correcte pour une installation perimee.
# ---------------------------------------------------------------------------
FICHIERS_CLES = (
    "core/pipeline.py",
    "core/partition_secteurs.py",
    "core/geom_backend.py",
    "core/polygones_topologiques.py",
    "core/graphe_topologique.py",
    "core/consolidation_secteurs.py",
    "core/executer_topologique.py",
    "core/config_manager.py",
    "plugin_main.py",
    "metadata.txt",
)

# Marqueur fonctionnel : chaine qui DOIT etre presente (preuve que le code a
# jour est bien celui-la).
MARQUEUR_ATTENDU = ("core/partition_secteurs.py", "def construire_territoires(")


def _sha256(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _repertoire_livraison():
    """Racine du plugin dans CE zip (dossier parent de outils/)."""
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _candidats_dossiers_qgis():
    """Emplacements usuels du profil QGIS par plateforme (best-effort)."""
    candidats = []
    home = os.path.expanduser("~")
    system = platform.system()
    if system == "Windows":
        appdata = os.environ.get("APPDATA", os.path.join(home, "AppData", "Roaming"))
        candidats.append(os.path.join(appdata, "QGIS", "QGIS3", "profiles"))
    elif system == "Darwin":
        candidats.append(os.path.join(home, "Library", "Application Support",
                                       "QGIS", "QGIS3", "profiles"))
    else:  # Linux et variantes
        candidats.append(os.path.join(home, ".local", "share", "QGIS", "QGIS3", "profiles"))
    trouves = []
    for base in candidats:
        if not os.path.isdir(base):
            continue
        for profil in os.listdir(base):
            plug = os.path.join(base, profil, "python", "plugins", "sectorisation_hydraulique")
            if os.path.isdir(plug):
                trouves.append(plug)
    return trouves


def _verifier_cache_python(dossier_plugin, log):
    """Signale tout .pyc plus recent restant alors que son .py source a ete
    remplace (ou l'inverse : .pyc plus ancien -> pas suspect en soi, mais
    on liste systematiquement les __pycache__ presents pour information)."""
    alertes = 0
    for racine, _dirs, fichiers in os.walk(dossier_plugin):
        if "__pycache__" not in racine:
            continue
        for f in fichiers:
            if not f.endswith(".pyc"):
                continue
            pyc_path = os.path.join(racine, f)
            # nom_module.cpython-3XX.pyc -> nom_module.py probable a cote du
            # dossier __pycache__ (un niveau au-dessus)
            nom_module = f.split(".")[0] + ".py"
            py_path = os.path.join(os.path.dirname(racine), nom_module)
            if os.path.isfile(py_path):
                if os.path.getmtime(pyc_path) > os.path.getmtime(py_path) + 1:
                    log(f"    [ATTENTION] cache compile plus recent que la source : {pyc_path}")
                    alertes += 1
    if alertes == 0:
        log("    aucune anomalie de cache __pycache__ detectee.")
    return alertes


def verifier(dossier_plugin, log=print):
    log("=" * 78)
    log("VERIFICATION DE L'INSTALLATION DU PLUGIN vs sectorisation_hydraulique.zip")
    log("=" * 78)
    log(f"Dossier plugin analyse : {dossier_plugin}")
    if not os.path.isdir(dossier_plugin):
        log("  [ECHEC] ce dossier n'existe pas -- verifier le chemin.")
        return False
    log("")

    livraison = _repertoire_livraison()
    if os.path.abspath(dossier_plugin) == livraison:
        log("  (note : le dossier analyse EST le dossier de la livraison -- "
            "la comparaison est donc trivialement conforme. Lancer avec "
            "--plugin-dir pour verifier le plugin reellement installe.)")
    log(f"Livraison de reference : {livraison}")
    log("")

    tout_ok = True
    for rel in FICHIERS_CLES:
        chemin_ref = os.path.join(livraison, *rel.split("/"))
        if not os.path.isfile(chemin_ref):
            log(f"  [?]          {rel} absent de la livraison -- non verifiable.")
            continue
        empreinte_attendue = _sha256(chemin_ref)
        chemin = os.path.join(dossier_plugin, *rel.split("/"))
        if not os.path.isfile(chemin):
            log(f"  [MANQUANT]   {rel}")
            tout_ok = False
            continue
        empreinte = _sha256(chemin)
        mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(chemin)))
        if empreinte == empreinte_attendue:
            log(f"  [IDENTIQUE]  {rel}  (modifie {mtime})")
        else:
            log(f"  [DIFFERENT]  {rel}  (modifie {mtime}) -- NE correspond PAS "
                f"a la livraison livree ici : une autre version tourne.")
            tout_ok = False
    log("")

    # ---- marqueurs fonctionnels ----
    log("Marqueurs fonctionnels :")
    rel, motif = MARQUEUR_ATTENDU
    chemin = os.path.join(dossier_plugin, *rel.split("/"))
    if os.path.isfile(chemin) and motif in open(chemin, encoding="utf-8", errors="ignore").read():
        log(f"  [OK] {motif.strip('(')} present dans {rel} "
            f"-- le moteur de partition des ilots urbains (sans convex hull) est bien la.")
    else:
        log(f"  [ALERTE] {motif.strip('(')} INTROUVABLE dans {rel} "
            f"-- ce n'est PAS la version secteurs_hydrauliques et "
            f"secteurs_limites seront construits par l'ancien mecanisme "
            f"(enveloppe convexe) responsable des gros polygones triangulaires.")
        tout_ok = False

    log("")
    log("Cache Python (__pycache__) :")
    _verifier_cache_python(dossier_plugin, log)

    log("")
    log("=" * 78)
    if tout_ok:
        log("RESULTAT : installation conforme a sectorisation_hydraulique.zip.")
        log("Si le probleme visuel persiste malgre tout, relancer le pipeline")
        log("et verifier dans le log la presence du bloc 'AUTO-DIAGNOSTIC")
        log("INSTALLATION' (ajoute en tete d'execution) ainsi que la ligne")
        log("'Partition des ilots urbains : X ilots urbains (...)'.")
    else:
        log("RESULTAT : installation NON conforme -- remplacer entierement le")
        log("dossier plugin par le contenu de sectorisation_hydraulique.zip,")
        log("supprimer tout dossier __pycache__ residuel, puis REDEMARRER QGIS")
        log("completement (pas seulement 'recharger le plugin').")
    log("=" * 78)
    return tout_ok


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-dir", default=None,
                         help="Chemin du dossier plugin installe a verifier. "
                              "Si omis, recherche automatique dans les profils QGIS usuels.")
    args = parser.parse_args()

    if args.plugin_dir:
        dossiers = [args.plugin_dir]
    else:
        dossiers = _candidats_dossiers_qgis()
        if not dossiers:
            print("Aucun dossier plugin 'sectorisation_hydraulique' trouve "
                  "automatiquement dans les profils QGIS usuels.")
            print("Relancer avec --plugin-dir \"<chemin exact>\".")
            sys.exit(2)

    ok_global = True
    for d in dossiers:
        ok_global = verifier(d) and ok_global
        print()

    sys.exit(0 if ok_global else 1)


if __name__ == "__main__":
    main()
