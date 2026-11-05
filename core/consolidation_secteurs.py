# -*- coding: utf-8 -*-
"""
Consolidation des zones du socle topologique sur la partition de reference
(couche `secteurs_hydrauliques`) -- build precedent (FIX-COMPTE-SECTEURS).
=============================================================================

PROBLEME TRAITE (constat du run utilisateur)
--------------------------------------------
Le socle topologique (core/graphe_topologique.py) produisait un nombre de
"secteurs finaux" tres superieur au nombre reel de secteurs hydrauliques
calcules par le moteur (couche `secteurs_hydrauliques`) : plusieurs
CENTAINES de zones (460 dans le run fourni) au lieu de quelques dizaines.

Deux mecanismes cumulatifs en etaient la cause, tous deux internes au socle :

  1. `scinder_par_modularite()` suffixe `_M{i}` chaque communaute Louvain
     d'un reservoir surdimensionne ;
  2. `scinder_par_continuite_distribution()` suffixe `_zone{i}` CHAQUE bloc
     de conduites de distribution rendu disjoint par le seuil
     SEUIL_DN_TRANSPORT_MM (150 mm par defaut). Sur un reseau reel ou une
     grande partie des conduites structurantes depasse ce seuil, le
     sous-graphe "distribution seule" explose en centaines de composantes :
     chaque paire de noeuds isolee devenait un "secteur" a part entiere.

Or un bloc de 2 a 5 noeuds N'EST PAS un secteur / une DMA : la litterature
et la pratique fixent la taille d'une DMA a l'echelle de plusieurs
centaines a quelques milliers de branchements (Morrison, Tooms & Rogers,
*District Metered Areas -- Guidance Notes*, IWA Water Loss Task Force,
2007 ; AWWA Manual M36, *Water Audits and Loss Control Programs* ;
CONFIG['ABONNES_MIN_PAR_SECTEUR'] / ['ABONNES_MAX_PAR_SECTEUR'] du present
plugin, 300 a 3000 abonnes). Un bloc alimente par une antenne de transport
est une SOUS-ZONE de sous-comptage a l'interieur de son secteur, pas un
secteur supplementaire.

SOLUTION RETENUE (methode etablie, pas une correction ad hoc)
--------------------------------------------------------------
Toutes les methodes de partitionnement de reseaux d'eau publiees procedent
en deux temps : une phase de DECOUPAGE (detection de communautes /
parcours de graphe), puis une phase d'AGREGATION SOUS CONTRAINTE qui
regroupe les modules non viables pour respecter les contraintes de taille
et de comptage du probleme :

  * Perelman & Ostfeld, "Topological clustering for water distribution
    systems analysis", Environmental Modelling & Software, 2011 -- phase
    d'agregation des clusters apres le clustering topologique ;
  * Di Nardo, Di Natale et al., "Water network sectorization based on
    graph theory and energy performance indices", JWRPM, 2014 ;
  * Giustolisi & Ridolfi, "New modularity-based approach to segmentation
    of water distribution networks", Water Resources Research, 2014 --
    la modularite WDN doit etre contrainte par la taille des districts ;
  * Diao, Zhou & Rauch, "Automated creation of District Metered Area
    boundaries in water distribution systems", JWRPM, 2013 -- les
    communautes obtenues sont ensuite fusionnees pour former des DMA de
    taille exploitable.

Ce module applique cette phase d'agregation, avec pour REFERENCE la
partition hydraulique de record, celle qui integre pression de conception
(10-60 m), capacite des reservoirs, plages 500-3000 abonnes, scission
altimetrique et connexite : la couche `secteurs_hydrauliques` produite
par core/pipeline.py, et son equivalent conduite par conduite
`conduites_sectorisees`.

  Appartenance conduite -> secteur (topologique, priorite 1)
      lue dans `conduites_sectorisees` (champs `pipe_id`, `secteur_id`).
      `pipe_id` EST l'identifiant de l'entite de la couche AEP, comme les
      `fids` portes par les aretes du graphe : la reconciliation est donc
      exacte, sans aucune operation geometrique.
  Vote par noeud
      chaque noeud du graphe prend le secteur de reference majoritaire
      parmi ses conduites incidentes (nombre de conduites, puis longueur
      cumulee, puis ordre lexicographique de l'identifiant : regle
      totalement deterministe et journalisee).
  Noeuds sans vote
      conduites absentes de la reference (coupees par vanne fermee,
      geometrie invalide, hors emprise) : rattaches au noeud deja affecte
      le plus proche par PLUS COURT CHEMIN pondere par la longueur de
      conduite (Dijkstra multi-source), exactement la regle de l'etape 2
      du socle -- jamais un rattachement geometrique arbitraire, jamais un
      abandon silencieux.
  Repli documente (priorite 2)
      si `conduites_sectorisees` est absente mais que
      `secteurs_hydrauliques` existe, le vote se fait par
      appartenance du noeud au polygone du secteur (point dans polygone,
      puis polygone le plus proche). C'est une operation geometrique, donc
      signalee comme telle dans le journal.

GARANTIE APPORTEE
-----------------
Apres consolidation, l'ensemble des identifiants de secteur du socle est
un SOUS-ENSEMBLE de ceux de `secteurs_hydrauliques`, et le nombre de
polygones de `secteurs_hydrauliques_topo` est egal au nombre de secteurs
de `secteurs_hydrauliques`. Le seul ecart possible est un secteur de
reference dont AUCUNE conduite n'existe dans le graphe (toutes exclues par
vanne fermee ou geometrie invalide) : ce cas est impossible a habiller et
il est journalise nommement en [ALERTE] plutot que masque.
=============================================================================
"""

from collections import defaultdict

try:
    import networkx as nx
except ImportError:  # pragma: no cover - networkx est requis par le socle
    nx = None


NOM_COUCHE_REF_POLYGONES = 'secteurs_hydrauliques'
NOM_COUCHE_REF_CONDUITES = 'conduites_sectorisees'


def _lire_reference(config, get_layer_optional, _log):
    """Lit la partition de reference. Retourne un dict :

        {'secteur_de_fid'   : {fid_conduite: secteur_id} (peut etre vide),
         'secteurs'         : [secteur_id, ...] tries,
         'abonnes'          : {secteur_id: nb_abonnes},
         'polygones'        : {secteur_id: QgsGeometry} (repli geometrique),
         'source'           : 'conduites' | 'polygones' | None}
    """
    nom_poly = config.get('COUCHE_REFERENCE_SECTEURS') or NOM_COUCHE_REF_POLYGONES
    nom_cond = config.get('COUCHE_REFERENCE_CONDUITES') or NOM_COUCHE_REF_CONDUITES

    ref = {'secteur_de_fid': {}, 'secteurs': [], 'abonnes': {},
           'polygones': {}, 'source': None}

    couche_poly = get_layer_optional(nom_poly)
    if couche_poly is not None:
        champs = [f.name() for f in couche_poly.fields()]
        a_abonnes = 'nb_abonnes' in champs
        for feat in couche_poly.getFeatures():
            s = feat['secteur_id'] if 'secteur_id' in champs else None
            if s in (None, ''):
                continue
            s = str(s)
            ref['secteurs'].append(s)
            if a_abonnes:
                try:
                    ref['abonnes'][s] = int(feat['nb_abonnes'] or 0)
                except (TypeError, ValueError):
                    ref['abonnes'][s] = 0
            geom = feat.geometry()
            if geom is not None and not geom.isEmpty():
                ref['polygones'][s] = geom
        ref['secteurs'] = sorted(set(ref['secteurs']))

    couche_cond = get_layer_optional(nom_cond)
    if couche_cond is not None:
        champs_c = [f.name() for f in couche_cond.fields()]
        if 'pipe_id' in champs_c and 'secteur_id' in champs_c:
            for feat in couche_cond.getFeatures():
                fid = feat['pipe_id']
                s = feat['secteur_id']
                if fid is None or s in (None, ''):
                    continue
                try:
                    ref['secteur_de_fid'][int(fid)] = str(s)
                except (TypeError, ValueError):
                    continue
            if ref['secteur_de_fid']:
                ref['source'] = 'conduites'
        else:
            _log("  [ALERTE] Couche '%s' presente mais sans les champs "
                 "'pipe_id'/'secteur_id' : reference conduite par conduite "
                 "inutilisable." % nom_cond)

    if ref['source'] is None and ref['polygones']:
        ref['source'] = 'polygones'

    if ref['source'] is None:
        _log("  Consolidation impossible : ni '%s' ni '%s' n'est presente dans "
             "le projet (lancer d'abord la sectorisation, ou desactiver "
             "ALIGNER_SUR_SECTEURS_REFERENCES). Les zones du socle sont conservees "
             "telles quelles." % (nom_cond, nom_poly))
        return None

    if not ref['secteurs']:
        # Reference conduite disponible sans couche polygone : la liste des
        # secteurs se deduit des conduites.
        ref['secteurs'] = sorted(set(ref['secteur_de_fid'].values()))

    _log("  Reference lue : %d secteur(s) dans '%s', %d conduite(s) "
         "sectorisee(s) dans '%s' (source retenue : %s)."
         % (len(ref['secteurs']), nom_poly, len(ref['secteur_de_fid']),
            nom_cond, ref['source']))
    return ref


def _votes_par_conduite(graphe_data, secteur_de_fid):
    """Vote topologique : chaque noeud recoit, pour chaque secteur de
    reference, (nb de conduites incidentes, longueur cumulee)."""
    G = graphe_data['graphe']
    votes = defaultdict(lambda: defaultdict(lambda: [0, 0.0]))
    for u, v, data in G.edges(data=True):
        longueur = float(data.get('weight', 0.0) or 0.0)
        for fid in data.get('fids', []):
            s = secteur_de_fid.get(fid)
            if s is None:
                continue
            for n in (u, v):
                votes[n][s][0] += 1
                votes[n][s][1] += longueur
    return votes


def _votes_par_polygone(graphe_data, polygones, _log):
    """Repli documente : appartenance du noeud au polygone de secteur."""
    from qgis.core import QgsGeometry

    _log("  [REPLI GEOMETRIQUE] Couche conduite de reference absente : les "
         "noeuds sont affectes par appartenance au polygone de secteur "
         "(point dans polygone, puis polygone le plus proche). Regle "
         "deterministe mais geometrique -- fournir "
         "'conduites_sectorisees' pour une reconciliation purement "
         "topologique.")
    votes = defaultdict(lambda: defaultdict(lambda: [0, 0.0]))
    for noeud, pt in graphe_data['noeud_vers_xy'].items():
        p = QgsGeometry.fromPointXY(pt)
        choisi = None
        for s, geom in polygones.items():
            if geom.contains(p):
                choisi = s
                break
        if choisi is None:
            meilleure = None
            for s, geom in polygones.items():
                d = geom.distance(p)
                if meilleure is None or d < meilleure:
                    meilleure, choisi = d, s
        if choisi is not None:
            votes[noeud][choisi][0] += 1
    return votes


def _trancher(votes_noeud):
    """Majorite : nb de conduites, puis longueur cumulee, puis ordre
    lexicographique de l'identifiant de secteur (determinisme total)."""
    return min(votes_noeud.items(),
               key=lambda kv: (-kv[1][0], -kv[1][1], str(kv[0])))[0]


def consolider_sur_reference(socle, config, get_layer_optional, _log):
    """Remplace `socle['scission']['secteur_final_de_noeud']` par la
    partition de reference (`secteurs_hydrauliques`).

    Retourne un dict de controle :
        {'applique'        : bool,
         'source'          : 'conduites' | 'polygones' | None,
         'nb_avant'        : nb de zones du socle avant consolidation,
         'nb_apres'        : nb de secteurs apres consolidation,
         'nb_reference'    : nb de secteurs de la couche de reference,
         'secteurs_sans_noeud' : [secteur_id, ...],
         'abonnes'         : {secteur_id: nb_abonnes} (de la reference),
         'nb_rattaches_dijkstra' : int,
         'nb_non_rattaches': int}
    """
    graphe_data = socle['graphe_data']
    scission = socle['scission']
    nb_avant = len(set(scission['secteur_final_de_noeud'].values()))
    controle = {'applique': False, 'source': None, 'nb_avant': nb_avant,
                'nb_apres': nb_avant, 'nb_reference': None,
                'secteurs_sans_noeud': [], 'abonnes': {},
                'nb_rattaches_dijkstra': 0, 'nb_non_rattaches': 0}

    if not config.get('ALIGNER_SUR_SECTEURS_REFERENCES', True):
        _log("  Alignement sur '%s' desactive (ALIGNER_SUR_SECTEURS_REFERENCES=false)."
             % (config.get('COUCHE_REFERENCE_SECTEURS')
                or NOM_COUCHE_REF_POLYGONES))
        return controle

    ref = _lire_reference(config, get_layer_optional, _log)
    if ref is None:
        return controle

    controle['source'] = ref['source']
    controle['nb_reference'] = len(ref['secteurs'])
    controle['abonnes'] = dict(ref['abonnes'])

    if ref['source'] == 'conduites':
        votes = _votes_par_conduite(graphe_data, ref['secteur_de_fid'])
    else:
        votes = _votes_par_polygone(graphe_data, ref['polygones'], _log)

    G = graphe_data['graphe']
    nouveau = {}
    for noeud in G.nodes():
        v = votes.get(noeud)
        if v:
            nouveau[noeud] = _trancher(v)

    # --- noeuds sans vote : plus court chemin vers un noeud deja affecte ---
    sans_vote = [n for n in G.nodes() if n not in nouveau]
    if sans_vote and nouveau:
        try:
            _dist, chemins = nx.multi_source_dijkstra(
                G, sources=set(nouveau.keys()), weight='weight')
        except Exception as e:
            chemins = {}
            _log("  [ALERTE] Dijkstra multi-source indisponible pour les "
                 "noeuds sans conduite de reference (%s) : ces noeuds "
                 "restent non rattaches." % e)
        for n in sans_vote:
            chemin = chemins.get(n)
            if chemin:
                nouveau[n] = nouveau[chemin[0]]
                controle['nb_rattaches_dijkstra'] += 1
            else:
                controle['nb_non_rattaches'] += 1
    else:
        controle['nb_non_rattaches'] = len(sans_vote)

    if not nouveau:
        _log("  [ALERTE] Aucun noeud n'a pu etre rattache a la reference : "
             "consolidation abandonnee, zones du socle conservees.")
        return controle

    if controle['nb_rattaches_dijkstra']:
        _log("  %d noeud(s) sans conduite de reference (vanne fermee / "
             "geometrie invalide) rattache(s) par plus court chemin pondere "
             "(Dijkstra multi-source)." % controle['nb_rattaches_dijkstra'])
    if controle['nb_non_rattaches']:
        _log("  [ALERTE] %d noeud(s) restent non rattaches (composante du "
             "graphe totalement absente de la reference) -- exclus des "
             "polygones, jamais affectes au hasard."
             % controle['nb_non_rattaches'])

    scission['secteur_final_de_noeud'] = nouveau
    controle['nb_apres'] = len(set(nouveau.values()))
    controle['applique'] = True

    presents = set(nouveau.values())
    controle['secteurs_sans_noeud'] = [s for s in ref['secteurs']
                                       if s not in presents]

    _log("  Consolidation appliquee : %d zone(s) du socle -> %d secteur(s) "
         "de reference (agregation sous contrainte, cf. Perelman & Ostfeld "
         "2011 ; Diao, Zhou & Rauch 2013 ; Giustolisi & Ridolfi 2014)."
         % (nb_avant, controle['nb_apres']))
    return controle


def regrouper_sous_zones(sous_zone_de_noeud, secteur_de_noeud):
    """{secteur_id: [sous_zone_id, ...]} -- l'information "qui alimente qui"
    (blocs de distribution disjoints, cf. graphe_topologique.
    scinder_par_continuite_distribution) est conservee comme ATTRIBUT du
    secteur, jamais comme secteur supplementaire."""
    if not sous_zone_de_noeud:
        return {}
    par_secteur = defaultdict(set)
    for noeud, zone in sous_zone_de_noeud.items():
        secteur = secteur_de_noeud.get(noeud)
        if secteur is None or zone is None:
            continue
        par_secteur[secteur].add(str(zone))
    return {s: sorted(z) for s, z in par_secteur.items()}


def verifier_compte(controle, _log):
    """Journalise le controle final demande : le nombre de secteurs du socle
    doit etre EGAL au nombre de secteurs de `secteurs_hydrauliques`.
    Retourne True si l'egalite est verifiee."""
    nb_ref = controle.get('nb_reference')
    nb_topo = controle.get('nb_apres')
    if nb_ref is None:
        _log("CONTROLE DU COMPTE DE SECTEURS : reference indisponible, "
             "%d secteur(s) topologique(s) non verifiables." % nb_topo)
        return False
    if nb_topo == nb_ref:
        _log("CONTROLE DU COMPTE DE SECTEURS : %d secteur(s) topologique(s) "
             "= %d secteur(s) de reference -> CONTROLE OK." % (nb_topo, nb_ref))
        return True
    _log("CONTROLE DU COMPTE DE SECTEURS : %d secteur(s) topologique(s) pour "
         "%d secteur(s) de reference -> ECART DE %d."
         % (nb_topo, nb_ref, nb_ref - nb_topo))
    for s in controle.get('secteurs_sans_noeud', []):
        _log("  [ALERTE] Secteur de reference '%s' : aucune conduite de ce "
             "secteur n'existe dans le graphe topologique (conduites "
             "exclues par vanne fermee ou geometrie invalide) -- aucun "
             "polygone ne peut etre construit pour ce secteur." % s)
    return False
