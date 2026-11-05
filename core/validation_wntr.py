# -*- coding: utf-8 -*-
"""
Validation hydraulique optionnelle avec WNTR.

Le moteur de sectorisation utilise une estimation Hazen-Williams pour guider
la partition. Ce module reconstruit ensuite un modele hydraulique independant
avec WNTR, ferme les conduites frontieres (vannes de sectorisation) et
compare les pressions calculees aux seuils de conception configures.

WNTR n'est pas importe au chargement du plugin : QGIS peut donc demarrer
meme si la dependance n'est pas encore installee. Dans ce cas, l'appelant
obtient un statut explicite ``indisponible`` et ne doit pas presenter les
pressions estimees comme une validation solveur.
"""

from collections import defaultdict
import importlib
import os
import site
import sys


def _import_wntr():
    """Importe WNTR même si QGIS désactive le site utilisateur.

    ``pip show wntr`` peut pointer vers le site utilisateur Windows alors que
    QGIS démarre avec ``PYTHONNOUSERSITE`` ou une liste de chemins différente.
    On ajoute explicitement ce site seulement comme repli, sans modifier les
    paquets QGIS déjà chargés.
    """
    try:
        return importlib.import_module("wntr")
    except ModuleNotFoundError as premiere_erreur:
        chemins = []
        try:
            chemins.append(site.getusersitepackages())
        except Exception:
            pass
        try:
            chemins.extend(site.getsitepackages())
        except Exception:
            pass
        for chemin in chemins:
            if not chemin or chemin in sys.path:
                continue
            if os.path.isdir(os.path.join(chemin, "wntr")):
                sys.path.append(chemin)
                try:
                    return importlib.import_module("wntr")
                except ModuleNotFoundError:
                    continue
        raise premiere_erreur


def _log(log, message):
    if log is not None:
        log(message)


def _nom_piece(prefix, value):
    """Produit un nom EPANET/WNTR stable, ASCII et non vide."""
    texte = str(value)
    texte = "".join(c if c.isalnum() else "_" for c in texte)
    return (prefix + texte)[:80]


def valider_pressions_wntr(
    noeud_altitudes,
    pipe_edges,
    abonnes_par_noeud,
    q_pointe_abonne,
    secteur_de_noeud,
    source_par_secteur,
    cote_piezo_par_secteur,
    pression_min_par_noeud=None,
    pression_min_m=10.0,
    pression_max_m=60.0,
    rugosite=130.0,
    fermer_conduites_frontieres=True,
    log=None,
):
    """Construit et résout le réseau final avec WNTR.

    Paramètres
    ----------
    noeud_altitudes : dict ``nid -> altitude_m``
    pipe_edges : iterable de dicts ``pipe_id, n1, n2, longueur_m, diametre_mm``
    abonnes_par_noeud : demandes unitaires agrégées aux jonctions
    q_pointe_abonne : demande de pointe en m3/s par abonné
    secteur_de_noeud : partition finale des jonctions
    source_par_secteur : jonction source (réservoir ou PRV virtuel) par secteur
    cote_piezo_par_secteur : charge imposée par secteur, en m

    Les conduites reliant deux secteurs sont retirées du modèle lorsque
    ``fermer_conduites_frontieres`` est vrai. C'est la situation correspondant
    à l'installation des vannes d'isolement produites par le pipeline.

    Retourne toujours un dictionnaire sérialisable :
    ``statut`` vaut ``OK``, ``INDISPONIBLE`` ou ``ECHEC``.
    """
    resultat = {
        "statut": "INDISPONIBLE",
        "solveur": "WNTRSimulator",
        "nb_noeuds": 0,
        "nb_conduites_modelees": 0,
        "nb_conduites_frontieres_fermees": 0,
        "nb_noeuds_non_alimentes": 0,
        "noeuds_non_alimentes": [],
        "nb_noeuds_pression_inexploitable": 0,
        "nb_violations_pression": None,
        "nb_abonnes_hors_plage": None,
        "pression_min_m": None,
        "pression_max_m": None,
        "pression_moyenne_m": None,
        "noeuds": {},
        "erreur": None,
    }

    try:
        wntr = _import_wntr()
    except Exception as exc:  # pragma: no cover - dépend de l'environnement QGIS
        resultat["erreur"] = (
            "Paquet WNTR indisponible : %s | Python=%s | usersite=%s"
            % (exc, sys.executable, getattr(site, "getusersitepackages", lambda: "?")())
        )
        _log(log, "  [WNTR] Validation non executee : %s" % resultat["erreur"])
        return resultat

    _log(
        log,
        "  [WNTR] Paquet=%s | module=%s"
        % (getattr(wntr, "__version__", "?"), getattr(wntr, "__file__", "?")),
    )
    noeuds = sorted(noeud_altitudes.keys(), key=str)
    if not noeuds:
        resultat["erreur"] = "aucun noeud d'altitude fourni"
        _log(log, "  [WNTR] Echec : %s" % resultat["erreur"])
        resultat["statut"] = "ECHEC"
        return resultat

    try:
        wn = wntr.network.WaterNetworkModel()
        wn.options.hydraulic.demand_model = "DD"
        # EPANET est le solveur hydraulique embarqué par WNTR et est plus
        # robuste que le solveur Python WNTRSimulator sur les réseaux AEP
        # comportant des pressions négatives, des secteurs fermés et des
        # composantes peu alimentées. Il reste piloté par WNTR.
        wn.options.hydraulic.trials = 1000
        wn.options.hydraulic.accuracy = 0.001
        wn.options.time.duration = 0
        # Contrôle indépendant de l'alimentation après fermeture des
        # frontières. Une pression EPANET extrêmement négative peut être un
        # marqueur de jonction sans source, pas une pression hydraulique
        # exploitable : on doit le distinguer d'une vraie violation.
        graphe_modele = defaultdict(set)
        sources_modelees = []

        for nid in noeuds:
            altitude = float(noeud_altitudes[nid])
            demande = max(float(abonnes_par_noeud.get(nid, 0)) * float(q_pointe_abonne), 0.0)
            wn.add_junction(
                str(nid),
                base_demand=demande,
                elevation=altitude,
            )

        # Une source hydraulique est ajoutée par secteur final. Le choix d'un
        # noeud appartenant effectivement au secteur est obligatoire : après
        # une fusion, l'ancien noeud-source peut avoir changé de secteur.
        membres_par_secteur = defaultdict(list)
        for nid, secteur in secteur_de_noeud.items():
            if nid in noeud_altitudes:
                membres_par_secteur[secteur].append(nid)

        for secteur in sorted(membres_par_secteur, key=str):
            membres = membres_par_secteur[secteur]
            source = source_par_secteur.get(secteur)
            if source not in membres:
                source = sorted(membres, key=str)[0]
            head = float(cote_piezo_par_secteur.get(secteur, pression_min_m + float(noeud_altitudes[source])))
            reservoir_name = _nom_piece("RES_", secteur)
            pipe_name = _nom_piece("SRC_", secteur)
            wn.add_reservoir(reservoir_name, base_head=head)
            # Une conduite courte et de grand diamètre représente la liaison
            # source -> noeud de référence sans introduire une perte notable.
            wn.add_pipe(
                pipe_name,
                reservoir_name,
                str(source),
                length=0.1,
                diameter=1.0,
                roughness=float(rugosite),
                minor_loss=0.0,
                initial_status="Open",
            )
            sources_modelees.append(str(source))

        for edge in pipe_edges:
            n1 = edge.get("n1")
            n2 = edge.get("n2")
            if n1 not in noeud_altitudes or n2 not in noeud_altitudes or n1 == n2:
                continue
            s1 = secteur_de_noeud.get(n1)
            s2 = secteur_de_noeud.get(n2)
            if fermer_conduites_frontieres and s1 is not None and s2 is not None and s1 != s2:
                resultat["nb_conduites_frontieres_fermees"] += 1
                continue
            longueur = max(float(edge.get("longueur_m") or 0.0), 0.1)
            diametre_mm = max(float(edge.get("diametre_mm") or 0.0), 20.0)
            wn.add_pipe(
                _nom_piece("P_", edge.get("pipe_id")),
                str(n1),
                str(n2),
                length=longueur,
                diameter=diametre_mm / 1000.0,
                roughness=float(rugosite),
                minor_loss=0.0,
                initial_status="Open",
            )
            graphe_modele[str(n1)].add(str(n2))
            graphe_modele[str(n2)].add(str(n1))
            resultat["nb_conduites_modelees"] += 1

        # Les frontières fermées peuvent isoler une composante d'un secteur
        # sans lui laisser de source. Cette situation rend la validation
        # complète impossible et doit être signalée explicitement.
        alimentes = set()
        pile = list(dict.fromkeys(sources_modelees))
        while pile:
            courant = pile.pop()
            if courant in alimentes:
                continue
            alimentes.add(courant)
            pile.extend(graphe_modele.get(courant, ()))
        noeuds_non_alimentes = sorted(set(str(n) for n in noeuds) - alimentes)
        resultat["nb_noeuds_non_alimentes"] = len(noeuds_non_alimentes)
        resultat["noeuds_non_alimentes"] = noeuds_non_alimentes[:500]
        if noeuds_non_alimentes:
            par_secteur = defaultdict(int)
            for nid in noeuds_non_alimentes:
                par_secteur[secteur_de_noeud.get(nid, "?")] += 1
            _log(
                log,
                "  [WNTR] %d noeud(s) non alimenté(s) après fermeture des frontières "
                "(%d secteur(s)) : %s"
                % (len(noeuds_non_alimentes), len(par_secteur),
                   ", ".join("%s=%d" % (s, n) for s, n in sorted(par_secteur.items(), key=lambda x: str(x[0]))[:20]))
            )

        resultat["nb_noeuds"] = len(noeuds)
        # EpanetSimulator est fourni par WNTR et constitue ici le solveur
        # principal. Repli sur WNTRSimulator si le moteur EPANET local n'est
        # pas disponible dans l'installation QGIS.
        solveur_utilise = "WNTR/EpanetSimulator"
        try:
            simulation = wntr.sim.EpanetSimulator(wn)
            resultats_sim = simulation.run_sim()
        except Exception as erreur_epanet:
            _log(log, "  [WNTR] EPANET indisponible/echec (%s) ; repli WNTRSimulator." % erreur_epanet)
            solveur_utilise = "WNTR/WNTRSimulator"
            simulation = wntr.sim.WNTRSimulator(wn)
            resultats_sim = simulation.run_sim(convergence_error=True)
        resultat["solveur"] = solveur_utilise
        table_pression = resultats_sim.node["pressure"]
        ligne = table_pression.iloc[-1]

        pressions = []
        nb_violations = 0
        nb_abonnes_hors_plage = 0
        seuils = pression_min_par_noeud or {}
        non_alimentes_set = set(noeuds_non_alimentes)
        noeuds_pression_inexploitable = set()
        for nid in noeuds:
            pression = float(ligne.get(str(nid), float("nan")))
            if pression != pression:  # NaN
                noeuds_pression_inexploitable.add(str(nid))
                continue
            seuil_min = float(seuils.get(nid, pression_min_m))
            ecart_min = max(0.0, seuil_min - pression)
            ecart_max = max(0.0, pression - float(pression_max_m))
            # EPANET peut renvoyer une valeur finie gigantesque pour une
            # jonction sans alimentation. Elle ne doit jamais être comptée
            # comme une simple violation de pression.
            pression_inexploitable = abs(pression) > 1.0e6
            if pression_inexploitable:
                noeuds_pression_inexploitable.add(str(nid))
            violation = ecart_min > 0.0 or ecart_max > 0.0
            nb_ab = int(abonnes_par_noeud.get(nid, 0))
            if violation and not pression_inexploitable and str(nid) not in non_alimentes_set:
                nb_violations += 1
                nb_abonnes_hors_plage += nb_ab
            if not pression_inexploitable:
                pressions.append(pression)
            resultat["noeuds"][nid] = {
                "secteur_id": secteur_de_noeud.get(nid),
                "pression_wntr_m": pression,
                "pression_min_requise_m": seuil_min,
                "ecart_m": max(ecart_min, ecart_max),
                "statut": ("NON_ALIMENTE" if str(nid) in non_alimentes_set or pression_inexploitable
                            else ("VIOLATION" if violation else "OK")),
                "nb_abonnes": nb_ab,
            }

        resultat["nb_noeuds_pression_inexploitable"] = len(noeuds_pression_inexploitable)
        if noeuds_pression_inexploitable:
            extremes_par_secteur = defaultdict(list)
            for nid in sorted(noeuds_pression_inexploitable):
                extremes_par_secteur[secteur_de_noeud.get(nid, "?")].append(nid)
            resume_extremes = ", ".join(
                "%s=%d" % (secteur, len(ids))
                for secteur, ids in sorted(extremes_par_secteur.items(), key=lambda item: str(item[0]))
            )
            exemples = []
            for nid in sorted(noeuds_pression_inexploitable)[:10]:
                valeur = ligne.get(str(nid), float("nan"))
                exemples.append("%s=%s" % (nid, valeur))
            _log(
                log,
                "  [WNTR] Pressions extrêmes par secteur : %s | exemples : %s"
                % (resume_extremes, "; ".join(exemples)),
            )
        resultat["pression_min_m"] = min(pressions) if pressions else None
        resultat["pression_max_m"] = max(pressions) if pressions else None
        resultat["pression_moyenne_m"] = sum(pressions) / len(pressions) if pressions else None
        if noeuds_non_alimentes or noeuds_pression_inexploitable:
            resultat["statut"] = "ECHEC"
            resultat["nb_violations_pression"] = None
            resultat["nb_abonnes_hors_plage"] = None
            resultat["erreur"] = (
                "%d noeud(s) non alimente(s) ou pression inexploitable apres fermeture "
                "des frontieres (%d valeur(s) extreme(s))"
                % (len(noeuds_non_alimentes), len(noeuds_pression_inexploitable))
            )
            _log(log, "  [WNTR] Echec de validation : %s" % resultat["erreur"])
        else:
            resultat["statut"] = "OK"
            resultat["nb_violations_pression"] = nb_violations
            resultat["nb_abonnes_hors_plage"] = nb_abonnes_hors_plage
            _log(
                log,
                "  [WNTR] Validation solveur OK : %d noeuds, %d conduites, "
                "%d frontière(s) fermée(s), %d violation(s) pression / %d abonne(s) "
                "hors plage, P=[%.2f, %.2f]m."
                % (
                    resultat["nb_noeuds"], resultat["nb_conduites_modelees"],
                    resultat["nb_conduites_frontieres_fermees"],
                    resultat["nb_violations_pression"],
                    resultat["nb_abonnes_hors_plage"],
                    resultat["pression_min_m"] if resultat["pression_min_m"] is not None else float("nan"),
                    resultat["pression_max_m"] if resultat["pression_max_m"] is not None else float("nan"),
                ),
            )
    except Exception as exc:  # pragma: no cover - dépend du réseau fourni
        resultat["statut"] = "ECHEC"
        resultat["erreur"] = "%s: %s" % (type(exc).__name__, exc)
        _log(log, "  [WNTR] Echec de la validation solveur : %s" % resultat["erreur"])

    return resultat
