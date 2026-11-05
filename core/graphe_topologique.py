# -*- coding: utf-8 -*-
"""
Sectorisation hydraulique - socle TOPOLOGIQUE (graphe), en remplacement de
l'approche geometrique de core/pipeline.py.
=============================================================================

PRINCIPE (cf. litterature : Diao/Zhou/Rauch 2013 JWRPM ; Giustolisi &
Ridolfi 2014 WRR ; approche "Improved Girvan-Newman" JWRPM 2025 ;
Alvisi 2015 ; Blondel et al. 2008) :

    Un secteur = l'ensemble des noeuds du reseau EFFECTIVEMENT ATTEIGNABLES
    depuis un reservoir donne, determine par un parcours du graphe
    (noeuds = jonctions, aretes = conduites), jamais par une operation
    geometrique sur des polygones (Voronoi, buffers de routes, distances a
    vol d'oiseau).

Ce module NE PRODUIT AUCUN POLYGONE. Il tranche uniquement l'appartenance
noeud -> secteur, de facon deterministe et sans chevauchement possible par
construction (chaque noeud du graphe appartient a exactement un secteur, ou
est explicitement journalise comme ambigu si non tranchable sans regle
arbitraire). L'habillage cartographique (polygones) est un post-traitement
separe, en aval, qui peut reutiliser les briques deja robustes de
pipeline.py (decoupe par ilots/routes, micro-soudure) mais SANS jamais
faire intervenir la geometrie dans la decision d'appartenance elle-meme.

DEPENDANCE EXTERNE : networkx (module pur Python, aucune dependance
compilee). A installer dans l'environnement Python de QGIS si absent :
    - OSGeo4W (Windows) : "OSGeo4W Shell" -> `pip install networkx`
    - Linux/Mac (QGIS via apt/homebrew) : utiliser le python-pip de QGIS,
      p.ex. `/usr/bin/python3 -m pip install networkx` si QGIS partage le
      python systeme, sinon cibler l'interpreteur indique par
      QgsApplication.pythonPath() / la console Python de QGIS.
Si networkx est introuvable, ce module leve ImportError explicitement au
lieu de retomber silencieusement sur une heuristique degradee.
=============================================================================
"""

import logging
from collections import defaultdict

try:
    import networkx as nx
except ImportError as _e:
    nx = None
    _NETWORKX_IMPORT_ERROR = _e


# -----------------------------------------------------------------------
# Exceptions
# -----------------------------------------------------------------------

class GrapheTopologiqueErreur(Exception):
    """Erreur structurelle du socle topologique (a ne PAS masquer par un
    correctif geometrique de contournement -- cf. contrainte explicite du
    projet)."""
    pass


# -----------------------------------------------------------------------
# Etape 1 : construction du graphe depuis les couches QGIS
# -----------------------------------------------------------------------

def construire_graphe(config, get_layer, get_layer_optional, snap_key,
                       extremites_ligne, _log):
    """Construit le graphe topologique du reseau AEP.

    Retourne un dict :
        {
          'graphe'        : nx.Graph  (noeuds = cle de snapping (x,y) ;
                                        attribut 'xy' = coordonnees reelles),
          'sources'       : {noeud_source: feature_id_reservoir},
          'noeud_vers_xy' : {cle_snap: QgsPointXY},
          'edge_vers_fid' : {(u, v) tries: fid conduite},
          'nb_conduites_ignorees': int,
        }

    Regles :
      - Noeuds = extremites des conduites de la couche AEP, apres snapping
        (tolerance CONFIG['SNAP_TOLERANCE'], comme le reste du pipeline).
      - Aretes ponderees par la LONGUEUR (CONFIG['PIPE_LENGTH_FIELD'] si
        renseigne et > 0, sinon geom.length()). Le diametre
        (CONFIG['PIPE_DIAMETER_FIELD']) est stocke comme attribut d'arete
        pour un usage ulterieur (WDN-modularity, pas necessaire au socle
        Louvain de depart, selon Blondel et al. (2008).
      - Une conduite dont le champ de vanne fermee (CONFIG optionnel
        'VANNE_FERMEE_FIELD', valeur truthy) est active est EXCLUE du
        graphe (coupure), et journalisee.
      - Les reservoirs (couche TANK_LAYER) sont rattaches au noeud du
        graphe le plus proche (par distance euclidienne dans la tolerance
        CONFIG['TANK_RESEARCH_M'], sinon la plus proche connue) -> noeuds
        sources.
    """
    if nx is None:
        raise GrapheTopologiqueErreur(
            "Le module 'networkx' est requis pour le socle topologique et "
            "n'a pas pu etre importe (" + str(_NETWORKX_IMPORT_ERROR) + "). "
            "Installez-le dans l'environnement Python de QGIS avant de "
            "relancer la sectorisation."
        )

    tol = float(config.get('SNAP_TOLERANCE', 0.1))
    diam_field = config.get('PIPE_DIAMETER_FIELD')
    len_field = config.get('PIPE_LENGTH_FIELD')
    vanne_field = config.get('VANNE_FERMEE_FIELD')  # optionnel, absent par defaut

    aep_layer = get_layer(config['AEP_LAYER'])
    tank_layer = get_layer(config['TANK_LAYER'])

    G = nx.Graph()
    noeud_vers_xy = {}
    edge_vers_fid = {}
    nb_conduites_ignorees = 0
    nb_conduites_coupees_vanne = 0

    champs_aep = [f.name() for f in aep_layer.fields()]
    a_diam = diam_field in champs_aep
    a_len = len_field in champs_aep
    a_vanne = bool(vanne_field) and vanne_field in champs_aep

    for feat in aep_layer.getFeatures():
        geom = feat.geometry()
        if geom is None or geom.isEmpty():
            nb_conduites_ignorees += 1
            continue

        if a_vanne:
            try:
                if bool(feat[vanne_field]):
                    nb_conduites_coupees_vanne += 1
                    continue
            except Exception as exc:
                logging.getLogger(__name__).debug(
                    "Exception ignoree : %s", exc)

        p1, p2 = extremites_ligne(geom)
        if p1 is None or p2 is None:
            nb_conduites_ignorees += 1
            continue

        k1 = snap_key(p1.x(), p1.y(), tol)
        k2 = snap_key(p2.x(), p2.y(), tol)
        if k1 == k2:
            # conduite degeneree (boucle sur elle-meme apres snapping)
            nb_conduites_ignorees += 1
            continue

        longueur = None
        if a_len:
            try:
                v = feat[len_field]
                if v is not None and float(v) > 0:
                    longueur = float(v)
            except Exception:
                longueur = None
        if longueur is None:
            longueur = geom.length()
        longueur = max(longueur, 0.01)  # jamais 0 (poids Dijkstra/Louvain)

        diametre = None
        if a_diam:
            try:
                v = feat[diam_field]
                diametre = float(v) if v is not None else None
            except Exception:
                diametre = None

        noeud_vers_xy[k1] = p1
        noeud_vers_xy[k2] = p2

        if G.has_edge(k1, k2):
            # conduites paralleles : on garde l'arete la plus courte pour
            # le poids Dijkstra mais on journalise le cas (Giustolisi &
            # Ridolfi 2014 traitent explicitement les conduites paralleles
            # comme un cas particulier de la modularite WDN).
            if longueur < G[k1][k2]['weight']:
                G[k1][k2]['weight'] = longueur
                G[k1][k2]['diametre'] = diametre
            G[k1][k2]['fids'].append(feat.id())
        else:
            G.add_edge(k1, k2, weight=longueur, diametre=diametre,
                       fids=[feat.id()])
        edge_vers_fid[tuple(sorted((k1, k2)))] = feat.id()

    if G.number_of_nodes() == 0:
        raise GrapheTopologiqueErreur(
            "Graphe vide : aucune conduite exploitable trouvee dans la "
            "couche '%s'." % config['AEP_LAYER']
        )

    # --- Rattachement des reservoirs (noeuds sources) ---
    tank_research_m = float(config.get('TANK_RESEARCH_M', 2000.0))
    sources = {}
    reservoirs_non_rattaches = []

    noeuds_liste = list(noeud_vers_xy.items())
    for feat in tank_layer.getFeatures():
        geom = feat.geometry()
        if geom is None or geom.isEmpty():
            continue
        pt = geom.asPoint()
        meilleur_noeud = None
        meilleure_dist = None
        for k, xy in noeuds_liste:
            d = ((xy.x() - pt.x()) ** 2 + (xy.y() - pt.y()) ** 2) ** 0.5
            if meilleure_dist is None or d < meilleure_dist:
                meilleure_dist = d
                meilleur_noeud = k
        if meilleur_noeud is None:
            reservoirs_non_rattaches.append(feat.id())
            continue
        if meilleure_dist > tank_research_m:
            _log(
                "  [ALERTE] Reservoir fid=%s rattache au noeud le plus "
                "proche a %.1f m, au-dela de TANK_RESEARCH_M=%.1f m. "
                "Verifier la couche AEP / la position du reservoir."
                % (feat.id(), meilleure_dist, tank_research_m)
            )
        if meilleur_noeud in sources and sources[meilleur_noeud] != feat.id():
            _log(
                "  [ALERTE] Deux reservoirs (fid=%s et fid=%s) se "
                "rattachent au MEME noeud de graphe -> traites comme une "
                "source unique fusionnee (a corriger cote donnees si non "
                "voulu)." % (sources[meilleur_noeud], feat.id())
            )
        sources[meilleur_noeud] = feat.id()

    if reservoirs_non_rattaches:
        raise GrapheTopologiqueErreur(
            "Reservoir(s) fid=%s : impossible de trouver un noeud de "
            "graphe (couche AEP vide ou geometrie reservoir invalide)."
            % reservoirs_non_rattaches
        )
    if not sources:
        raise GrapheTopologiqueErreur(
            "Aucun reservoir rattache : la couche '%s' est vide ou "
            "aucun noeud AEP n'a pu lui etre associe." % config['TANK_LAYER']
        )

    nb_fids_exploitables = sum(len(data.get('fids', [])) for _, _, data in G.edges(data=True))
    nb_conduites_paralleles = max(0, nb_fids_exploitables - G.number_of_edges())
    _log(
        "  Graphe construit : %d noeuds, %d aretes uniques (%d conduites/fids exploitables, "
        "dont %d parallele(s)), %d source(s) (reservoirs), %d conduite(s) ignoree(s) "
        "(geometrie invalide), %d conduite(s) coupee(s) par vanne fermee."
        % (G.number_of_nodes(), G.number_of_edges(), nb_fids_exploitables,
           nb_conduites_paralleles, len(sources), nb_conduites_ignorees,
           nb_conduites_coupees_vanne)
    )

    return {
        'graphe': G,
        'sources': sources,
        'noeud_vers_xy': noeud_vers_xy,
        'edge_vers_fid': edge_vers_fid,
    }


# -----------------------------------------------------------------------
# Etape 2 : rattachement primaire par atteignabilite (Dijkstra multi-source)
# -----------------------------------------------------------------------

def rattacher_par_atteignabilite(graphe_data, _log, marge_ambiguite_pct=2.0):
    """Rattache chaque noeud au reservoir dont il est topologiquement le
    plus proche (plus court chemin pondere par longueur de conduite dans le
    graphe). C'est le coeur du nouveau socle : aucune geometrie, uniquement
    la connectivite reelle du reseau.

    Chaque noeud atteignable depuis au moins un reservoir recoit EXACTEMENT
    un secteur primaire -> chevauchement impossible par construction.

    Les composantes non connectees a AUCUN reservoir (reseau maille avec
    des tuyaux orphelins, erreurs de digitalisation) sont journalisees
    explicitement (jamais rattachees en silence).

    Les noeuds pour lesquels DEUX reservoirs ou plus sont a une distance
    quasi identique (ecart relatif < marge_ambiguite_pct) sont consignes
    comme AMBIGUS dans le journal, avec la regle appliquee (plus court
    chemin strict, premier reservoir par ordre de decouverte en cas
    d'egalite exacte). Ce sont les candidats naturels a une bouclage
    reseau maille / une future regle de repartition par pression.

    Retourne un dict :
        {
          'secteur_de_noeud'   : {noeud: fid_reservoir},
          'distance_de_noeud'  : {noeud: distance_m},
          'ambigus'            : [(noeud, [(fid_reservoir, distance), ...])],
          'non_atteignables'   : [noeud, ...],
        }
    """
    G = graphe_data['graphe']
    sources = graphe_data['sources']  # {noeud: fid_reservoir}

    # Dijkstra multi-source : on lance un Dijkstra depuis CHAQUE reservoir
    # separement (le nombre de reservoirs est petit -- dizaines, pas
    # milliers -- donc le cout est negligeable face a la clarte du
    # diagnostic par-reservoir), puis on compare noeud par noeud.
    distances_par_source = {}
    for noeud_source, fid in sources.items():
        try:
            dist = nx.single_source_dijkstra_path_length(
                G, noeud_source, weight='weight')
        except Exception as e:
            raise GrapheTopologiqueErreur(
                "Echec Dijkstra depuis le reservoir fid=%s : %s" % (fid, e))
        distances_par_source[fid] = dist

    tous_noeuds = set(G.nodes())
    secteur_de_noeud = {}
    distance_de_noeud = {}
    ambigus = []
    non_atteignables = []

    for noeud in tous_noeuds:
        candidats = []
        for fid, dist_map in distances_par_source.items():
            if noeud in dist_map:
                candidats.append((fid, dist_map[noeud]))
        if not candidats:
            non_atteignables.append(noeud)
            continue
        candidats.sort(key=lambda c: c[1])
        meilleur_fid, meilleure_dist = candidats[0]
        secteur_de_noeud[noeud] = meilleur_fid
        distance_de_noeud[noeud] = meilleure_dist

        if len(candidats) > 1:
            deuxieme_dist = candidats[1][1]
            if meilleure_dist == 0:
                ecart_pct = 0.0 if deuxieme_dist == 0 else 100.0
            else:
                ecart_pct = 100.0 * (deuxieme_dist - meilleure_dist) / meilleure_dist
            if ecart_pct < marge_ambiguite_pct:
                ambigus.append((noeud, candidats))

    if non_atteignables:
        _log(
            "  [ALERTE] %d noeud(s) du graphe ne sont atteignables depuis "
            "AUCUN reservoir (reseau non connexe / conduite(s) orpheline(s) "
            "/ vanne(s) sectionnant tout le sous-reseau). Ces noeuds ne "
            "sont PAS rattaches automatiquement -- a corriger cote donnees "
            "ou a traiter comme reseau isole avec sa propre source."
            % len(non_atteignables)
        )

    if ambigus:
        _log(
            "  [INFO] %d noeud(s) topologiquement ambigus (>=2 reservoirs "
            "a distance quasi egale, ecart < %.1f%%, reseau maille) -- "
            "regle appliquee : plus court chemin pondere par longueur de "
            "conduite ; en cas d'egalite stricte, premier reservoir par "
            "ordre d'iteration. Detail consigne pour revue manuelle."
            % (len(ambigus), marge_ambiguite_pct)
        )
        for noeud, candidats in ambigus[:20]:  # echantillon dans le journal
            detail = ", ".join(
                "reservoir_fid=%s (%.1fm)" % (fid, d) for fid, d in candidats)
            _log("      noeud %s : %s" % (str(noeud), detail))
        if len(ambigus) > 20:
            _log("      ... (%d autres, non affiches)" % (len(ambigus) - 20))

    return {
        'secteur_de_noeud': secteur_de_noeud,
        'distance_de_noeud': distance_de_noeud,
        'ambigus': ambigus,
        'non_atteignables': non_atteignables,
    }


# -----------------------------------------------------------------------
# Etape 3 : scission des arbres de reservoir trop volumineux (Louvain)
# -----------------------------------------------------------------------

def scinder_par_modularite(graphe_data, rattachement, config, _log):
    """Pour chaque reservoir dont le sous-graphe rattache depasse
    ABONNES_MAX_PAR_SECTEUR (proxy simple ici : nombre de noeuds -- a
    affiner en aval avec le nombre reel d'abonnes par noeud, cf.
    couche `abonne`), applique une detection de communautes Louvain
    ponderee pour proposer une scission en sous-modules plus petits.

    C'est une PREMIERE VERSION deliberement simple : Louvain optimise la
    modularite globale, pas directement la taille cible (500-3000
    abonnes/secteur). Chaque module obtenu est ensuite, si besoin, fusionne
    ou re-scinde par un post-traitement de taille (a faire cote appelant,
    en utilisant le nombre reel d'abonnes -- pas fait ici pour garder ce
    socle independant de la couche `abonne`).

    Retourne :
        {
          'secteur_final_de_noeud': {noeud: identifiant_secteur_str},
          'modules_par_reservoir' : {fid_reservoir: [set(noeuds), ...]},
        }
    """
    G = graphe_data['graphe']
    secteur_de_noeud = rattachement['secteur_de_noeud']

    if not hasattr(nx.algorithms.community, 'louvain_communities'):
        raise GrapheTopologiqueErreur(
            "La version de networkx installee ne fournit pas "
            "'louvain_communities' (disponible depuis networkx>=2.8.5). "
            "Mettez a jour networkx dans l'environnement Python de QGIS."
        )

    seuil_max = int(config.get('ABONNES_MAX_PAR_SECTEUR', 3000))

    # Regrouper les noeuds par reservoir
    noeuds_par_reservoir = defaultdict(set)
    for noeud, fid in secteur_de_noeud.items():
        noeuds_par_reservoir[fid].add(noeud)

    secteur_final_de_noeud = {}
    modules_par_reservoir = {}

    for fid, noeuds in noeuds_par_reservoir.items():
        sous_graphe = G.subgraph(noeuds)
        if sous_graphe.number_of_nodes() <= seuil_max:
            # Pas de scission necessaire : un seul module = tout le
            # sous-arbre du reservoir.
            secteur_id = "R%s" % fid
            for n in noeuds:
                secteur_final_de_noeud[n] = secteur_id
            modules_par_reservoir[fid] = [set(noeuds)]
            continue

        try:
            communautes = nx.algorithms.community.louvain_communities(
                sous_graphe, weight='weight', seed=42)
        except Exception as e:
            _log(
                "  [ALERTE] Louvain a echoue pour le reservoir fid=%s "
                "(%d noeuds) : %s -- secteur laisse NON scinde, a traiter "
                "manuellement." % (fid, sous_graphe.number_of_nodes(), e)
            )
            secteur_id = "R%s" % fid
            for n in noeuds:
                secteur_final_de_noeud[n] = secteur_id
            modules_par_reservoir[fid] = [set(noeuds)]
            continue

        _log(
            "  Reservoir fid=%s : %d noeuds > seuil %d -> scinde en %d "
            "module(s) par modularite (Louvain pondere)."
            % (fid, sous_graphe.number_of_nodes(), seuil_max, len(communautes))
        )
        modules_par_reservoir[fid] = communautes
        for i, module in enumerate(communautes):
            secteur_id = "R%s_M%d" % (fid, i)
            for n in module:
                secteur_final_de_noeud[n] = secteur_id

    return {
        'secteur_final_de_noeud': secteur_final_de_noeud,
        'modules_par_reservoir': modules_par_reservoir,
    }


# -----------------------------------------------------------------------
# Etape 3bis : scission des blocs relies UNIQUEMENT par du transport
# (continuite de distribution -- logique "qui alimente qui")
# -----------------------------------------------------------------------

def scinder_par_continuite_distribution(graphe_data, secteur_final_de_noeud,
                                         config, _log):
    """Au sein de CHAQUE secteur deja tranche (rattachement + Louvain),
    scinde en zones independantes tout bloc de noeuds qui n'est reliE au
    reste du secteur QUE par de la conduite de TRANSPORT (DN >=
    SEUIL_DN_TRANSPORT_MM) -- c'est-a-dire tout bloc dont la continuite de
    DISTRIBUTION (DN < seuil, le seul reseau qui delimite une DMA selon
    AWWA M36 / Grayman, Murray & Savic 2009) est rompue avec le reste du
    secteur.

    PRINCIPE ("qui alimente qui") : un secteur issu du rattachement
    topologique/Louvain peut tres bien rester connexe dans le graphe
    COMPLET (reservoir -> transport -> distribution) tout en etant, en
    pratique de terrain, compose de plusieurs quartiers qui ne se
    touchent QUE via une conduite d'adduction traversant une zone non
    batie. Ce module rend cette realite explicite : chaque bloc de
    distribution disjoint devient sa propre zone ("<secteur>_zone1",
    "_zone2", ...), avec sa propre conduite de transport amont comme
    antenne d'alimentation -- exactement la pratique de sous-comptage
    (un compteur en tete par zone, meme reservoir).

    Regle deterministe, journalisee, jamais silencieuse :
      - Construit, pour chaque secteur, le sous-graphe DISTRIBUTION SEULE
        (aretes de diametre < seuil ; une arete sans diametre connu est
        traitee comme distribution par defaut -- coherent avec l'alerte
        deja emise ailleurs si le champ diametre est introuvable).
      - Cherche les composantes connexes de ce sous-graphe. La PLUS
        GRANDE est toujours retenue comme zone a part entiere (coeur du
        secteur). Les autres composantes de taille >= ZONE_MIN_NOEUDS
        (config, defaut 2) deviennent aussi des zones independantes.
      - Les fragments plus petits (< ZONE_MIN_NOEUDS -- souvent un seul
        noeud isole, bruit de digitalisation) ne sont PAS promus zone a
        part entiere : ils sont rattaches a la zone la plus proche du
        MEME secteur par plus court chemin dans le graphe COMPLET
        (transport inclus), meme logique que le rattachement primaire
        (etape 2). Un fragment totalement injoignable meme ainsi est
        rattache par defaut a la plus grande zone, et journalise comme
        tel (a verifier manuellement).
      - Si un secteur ne contient qu'UNE SEULE composante de
        distribution, son identifiant reste INCHANGE (pas de suffixe
        "_zoneN" ajoute sans raison).
      - Desactivable via CONFIG['SCISSION_PAR_ANTENNE_TRANSPORT'] = False
        (retombe alors sur le comportement anterieur d'origine : secteurs
        potentiellement multi-parties, signale en aval par
        polygones_topologiques.py sans scission d'identifiant).

    ATTENTION -- ROLE EXACT DE CETTE SCISSION DEPUIS LA les zones
    produites ici sont des SOUS-ZONES de sous-comptage a l'interieur de leur
    secteur, PAS des secteurs supplementaires. Un bloc de quelques noeuds
    n'est pas une DMA : la taille d'une DMA se compte en centaines a
    quelques milliers de branchements (Morrison, Tooms & Rogers, *DMA
    Guidance Notes*, IWA 2007 ; AWWA M36 ; CONFIG['ABONNES_MIN_PAR_SECTEUR']
    = 500 ici). Promouvoir chaque bloc en secteur faisait exploser le nombre
    de secteurs du socle (460 zones observees pour quelques dizaines de
    secteurs reels dans `secteurs_hydrauliques`). Le decoupage reste
    calcule et journalise -- il est expose comme ATTRIBUT (`sous_zones` de la
    couche de sortie) -- mais il ne renomme les secteurs que si
    CONFIG['CONSOLIDER_ZONES_EN_SECTEURS'] est mis a False (comportement
    d'origine, conserve pour comparaison).

    Retourne :
        {
          'sous_zone_de_noeud'        : {noeud: identifiant_sous_zone_str},
          'zones_par_secteur_original': {secteur_id: [zone_id, ...]},
        }
    """
    G = graphe_data['graphe']
    seuil_transport = float(config.get('SEUIL_DN_TRANSPORT_MM', 150.0))
    zone_min_noeuds = int(config.get('ZONE_MIN_NOEUDS', 2))

    noeuds_par_secteur = defaultdict(set)
    for n, s in secteur_final_de_noeud.items():
        noeuds_par_secteur[s].add(n)

    nouveau_secteur_de_noeud = {}
    zones_par_secteur_original = {}
    nb_secteurs_scindes = 0

    for secteur, noeuds in noeuds_par_secteur.items():
        # Sous-graphe DISTRIBUTION SEULE, restreint aux noeuds du secteur.
        G_dist = nx.Graph()
        G_dist.add_nodes_from(noeuds)
        for u, v, data in G.subgraph(noeuds).edges(data=True):
            diam = data.get('diametre')
            if diam is None or diam < seuil_transport:
                G_dist.add_edge(u, v)

        composantes = sorted(
            nx.connected_components(G_dist), key=len, reverse=True)

        if len(composantes) <= 1:
            for n in noeuds:
                nouveau_secteur_de_noeud[n] = secteur
            zones_par_secteur_original[secteur] = [secteur]
            continue

        # La plus grande composante est TOUJOURS retenue comme zone
        # (garde-fou : garantit au moins une zone "coeur" meme si le
        # secteur est tres fragmente et qu'aucune composante n'atteint
        # ZONE_MIN_NOEUDS).
        grandes = [composantes[0]] + [
            c for c in composantes[1:] if len(c) >= zone_min_noeuds]
        petites = [c for c in composantes[1:] if len(c) < zone_min_noeuds]

        grande_id_de_noeud = {}
        if len(grandes) == 1:
            zone_ids = [secteur]
            for n in grandes[0]:
                grande_id_de_noeud[n] = secteur
        else:
            nb_secteurs_scindes += 1
            zone_ids = ["%s_zone%d" % (secteur, i + 1)
                        for i in range(len(grandes))]
            for zone_id, composante in zip(zone_ids, grandes):
                for n in composante:
                    grande_id_de_noeud[n] = zone_id
            _log(
                "  [INFO] Secteur %s : %d bloc(s) de DISTRIBUTION "
                "disjoint(s) (relies uniquement par du transport, DN >= "
                "%.0f mm) -> scinde en %d zone(s) independante(s) : %s "
                "(chacune son propre polygone, alimentee par sa propre "
                "antenne de transport)."
                % (secteur, len(composantes), seuil_transport, len(grandes),
                   ", ".join("%s (%dn)" % (z, len(c))
                             for z, c in zip(zone_ids, grandes)))
            )

        for n, zone in grande_id_de_noeud.items():
            nouveau_secteur_de_noeud[n] = zone

        if petites:
            sous_graphe_complet = G.subgraph(noeuds)
            try:
                _, paths = nx.multi_source_dijkstra(
                    sous_graphe_complet,
                    sources=list(grande_id_de_noeud.keys()), weight='weight')
            except Exception:
                paths = {}
            nb_rattaches = 0
            nb_orphelins = 0
            for composante in petites:
                for n in composante:
                    chemin = paths.get(n)
                    if chemin:
                        nouveau_secteur_de_noeud[n] = grande_id_de_noeud[chemin[0]]
                        nb_rattaches += 1
                    else:
                        # Injoignable meme via le graphe complet du
                        # secteur (cas limite) -- rattache par defaut a
                        # la plus grande zone, jamais laisse orphelin en
                        # silence.
                        nouveau_secteur_de_noeud[n] = zone_ids[0]
                        nb_orphelins += 1
            _log(
                "    %d noeud(s) en fragment(s) isole(s) (< %d noeuds) "
                "rattache(s) a la zone la plus proche par plus court "
                "chemin (transport inclus)%s."
                % (nb_rattaches, zone_min_noeuds,
                   "; %d noeud(s) totalement orphelin(s) rattache(s) par "
                   "defaut a %s (a verifier)" % (nb_orphelins, zone_ids[0])
                   if nb_orphelins else "")
            )

        zones_par_secteur_original[secteur] = zone_ids

    if nb_secteurs_scindes:
        _log(
            "  %d secteur(s) scinde(s) en zones independantes par "
            "continuite de distribution (logique \"qui alimente qui\", "
            "cf. AWWA M36)." % nb_secteurs_scindes
        )
    else:
        _log(
            "  Aucun secteur ne necessite de scission par continuite de "
            "distribution (chaque secteur reste un seul bloc connecte)."
        )

    return {
        'sous_zone_de_noeud': nouveau_secteur_de_noeud,
        'zones_par_secteur_original': zones_par_secteur_original,
    }


def calculer_antennes_transport(graphe_data, secteur_de_noeud, config):
    """Antennes d'alimentation : conduites de TRANSPORT (DN >=
    SEUIL_DN_TRANSPORT_MM) reliant deux secteurs differents -- ce sont les
    conduites qui alimentent un secteur depuis le reste du reseau.

    Calcule sur la partition REELLEMENT retenue (celle qui sert a produire
    les polygones), de sorte que les identifiants de `antennes_par_zone`
    correspondent toujours a ceux de la couche de sortie.

    Retourne {secteur_id: [fid_conduite, ...]}.
    """
    G = graphe_data['graphe']
    seuil_transport = float(config.get('SEUIL_DN_TRANSPORT_MM', 150.0))
    antennes = defaultdict(set)
    for u, v, data in G.edges(data=True):
        su = secteur_de_noeud.get(u)
        sv = secteur_de_noeud.get(v)
        if su is None or sv is None or su == sv:
            continue
        diam = data.get('diametre')
        if diam is not None and diam >= seuil_transport:
            for fid in data.get('fids', []):
                antennes[su].add(fid)
                antennes[sv].add(fid)
    return {s: sorted(fids) for s, fids in antennes.items()}


# -----------------------------------------------------------------------
# Etape 4 : detection des conduites frontalieres (candidates PRV/vannes)
# -----------------------------------------------------------------------

def detecter_conduites_frontalieres(graphe_data, secteur_final_de_noeud, _log):
    """Retourne la liste des conduites (fid) dont les deux extremites sont
    dans des secteurs finaux differents -> candidates PRV / vannes de
    sectionnement, exactement comme dans pipeline.py, mais deduites ici
    directement de la partition du graphe (pas d'une geometrie de secteur)."""
    G = graphe_data['graphe']
    frontalieres = []
    for u, v, data in G.edges(data=True):
        su = secteur_final_de_noeud.get(u)
        sv = secteur_final_de_noeud.get(v)
        if su is None or sv is None:
            continue
        if su != sv:
            for fid in data.get('fids', []):
                frontalieres.append({
                    'fid': fid,
                    'secteur_1': su,
                    'secteur_2': sv,
                    'diametre': data.get('diametre'),
                })
    _log("  %d conduite(s) frontaliere(s) detectee(s) (candidates PRV/vanne)."
         % len(frontalieres))
    return frontalieres


# -----------------------------------------------------------------------
# Point d'entree "socle" : orchestre les etapes 1 a 4
# -----------------------------------------------------------------------

def executer_socle_topologique(config, get_layer, get_layer_optional,
                                snap_key, extremites_ligne, _log,
                                _etape=None):
    """Orchestre la construction du socle topologique complet. Ne genere
    AUCUN polygone -- c'est le rappelant (pipeline / une future build precedent)
    qui doit prendre `secteur_final_de_noeud` et generer la cartographie en
    aval, en reutilisant si besoin les briques geometriques existantes,
    uniquement comme habillage.

    Retourne :
        {
          'graphe_data'     : ... (cf. construire_graphe),
          'rattachement'    : ... (cf. rattacher_par_atteignabilite),
          'scission'        : ... (cf. scinder_par_modularite),
          'frontalieres'    : ... (cf. detecter_conduites_frontalieres),
        }
    """
    if _etape:
        _etape(0)
    _log("-" * 70)
    _log("SOCLE TOPOLOGIQUE : construction du graphe reseau (noeuds = "
         "jonctions AEP, aretes = conduites).")
    graphe_data = construire_graphe(
        config, get_layer, get_layer_optional, snap_key, extremites_ligne, _log)

    if _etape:
        _etape(1)
    _log("SOCLE TOPOLOGIQUE : rattachement par atteignabilite depuis "
         "chaque reservoir (Dijkstra pondere, sans aucune geometrie).")
    rattachement = rattacher_par_atteignabilite(graphe_data, _log)

    if _etape:
        _etape(2)
    _log("SOCLE TOPOLOGIQUE : scission des reservoirs surdimensionnes par "
         "modularite (Louvain pondere).")
    scission = scinder_par_modularite(graphe_data, rattachement, config, _log)

    if config.get('SCISSION_PAR_ANTENNE_TRANSPORT', True):
        if _etape:
            _etape(3)
        _log(
            "SOCLE TOPOLOGIQUE : scission des blocs relies uniquement par "
            "du transport (continuite de distribution, logique \"qui "
            "alimente qui\")."
        )
        scission_zones = scinder_par_continuite_distribution(
            graphe_data, scission['secteur_final_de_noeud'], config, _log)
        scission['sous_zone_de_noeud'] = scission_zones['sous_zone_de_noeud']
        scission['zones_par_secteur_original'] = scission_zones['zones_par_secteur_original']
        if not config.get('CONSOLIDER_ZONES_EN_SECTEURS', True):
            _log(
                "  CONSOLIDER_ZONES_EN_SECTEURS=false : chaque sous-zone "
                "devient un secteur a part entiere (comportement anterieur, "
                "produit potentiellement des centaines de secteurs non "
                "viables au sens DMA)."
            )
            scission['secteur_final_de_noeud'] = scission_zones['sous_zone_de_noeud']
        else:
            _log(
                "  Sous-zones conservees comme ATTRIBUT du secteur "
                "(`sous_zones`), pas comme secteurs supplementaires : une "
                "DMA se compte en centaines a milliers de branchements "
                "(IWA DMA Guidance Notes 2007, AWWA M36), un bloc de "
                "quelques noeuds n'est pas un secteur."
            )

    scission['antennes_par_zone'] = calculer_antennes_transport(
        graphe_data, scission['secteur_final_de_noeud'], config)

    if _etape:
        _etape(4)
    _log("SOCLE TOPOLOGIQUE : detection des conduites frontalieres.")
    frontalieres = detecter_conduites_frontalieres(
        graphe_data, scission['secteur_final_de_noeud'], _log)

    _log("SOCLE TOPOLOGIQUE termine : %d secteur(s) final(aux)."
         % len(set(scission['secteur_final_de_noeud'].values())))
    _log("-" * 70)

    return {
        'graphe_data': graphe_data,
        'rattachement': rattachement,
        'scission': scission,
        'frontalieres': frontalieres,
    }
