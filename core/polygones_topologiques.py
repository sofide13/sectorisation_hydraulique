# -*- coding: utf-8 -*-
"""
Sectorisation hydraulique - habillage cartographique EN AVAL du socle
topologique (core/graphe_topologique.py).
=============================================================================

ROLE STRICT DE CE MODULE : transformer une appartenance noeud/conduite deja
tranchee (secteur_final_de_noeud, cf. graphe_topologique.executer_socle_
topologique) en polygones QGIS. Ce module NE DECIDE JAMAIS de l'appartenance
-- il ne fait qu'habiller un resultat deja figé. Si un doute geometrique
survient (parties disjointes, chevauchement), il n'invente jamais
d'appartenance : il le journalise et applique une regle deterministe et
documentee ci-dessous, jamais une correction cachee.

CHOIX EXPLICITES (a la demande du projet) :
  - Aucune dependance a la couche `routes` ni a une largeur de route : ce
    plugin ne dispose que de l'itineraire des routes, pas d'un attribut de
    largeur fiable. Le seul parametre geometrique utilise est un buffer
    FIXE autour des conduites AEP (LARGEUR_TAMPON_CONDUITE_M), exactement
    comme le fait deja `secteurs_limites` dans pipeline.py (buffer
    30 m), mais sans jamais toucher aux routes.
  - AUCUNE enveloppe convexe (contrairement a assurer_polygone_simple() de
    pipeline.py, qui est la cause directe des grands polygones
    triangulaires chevauchants observes). Un secteur peut légitimement
    rester MultiPolygon si son reseau a des antennes isolees (une DMA reelle
    a parfois une annexe reliee par une seule conduite longue a travers une
    zone non batie) -- ce n'est pas un defaut a masquer par une inflation
    geometrique arbitraire.
  - Une fermeture morphologique BORNEE (buffer +r puis -r, avec r =
    SOUDURE_MAX_M) est proposee en option pour ressouder les parties
    proches (petites coutures dues au buffer), mais r reste petit
    (quelques dizaines de metres, jamais assez pour avaler du desert) --
    a la difference du convex hull qui n'a aucune borne.
  - Un tuyau frontalier (relie deux secteurs differents, cf.
    detecter_conduites_frontalieres) est BUFFER UNIQUEMENT DANS LE SECTEUR
    LE PLUS PROCHE DE LA SOURCE (celui dont l'extremite a la plus petite
    distance topologique au reservoir). Regle deterministe et journalisee
    -- jamais buffer des deux cotes (ce qui recreerait un chevauchement
    volontaire pres de chaque frontiere).
  - Une conduite de TRANSPORT (diametre >= SEUIL_DN_TRANSPORT_MM, cf.
    config) N'EST JAMAIS bufferisee dans le polygone de secteur, meme si le
    graphe l'attribue topologiquement a ce secteur. Raison : une conduite de
    transport/adduction relie souvent un reservoir a un quartier eloigne en
    traversant geographiquement le territoire physique d'un ou plusieurs
    autres secteurs -- la bufferiser produirait un chevauchement massif
    (centaines de milliers de m2, observe en pratique sur ce projet) et,
    pire, sa resolution automatique peut fragmenter le secteur perdant en
    plusieurs morceaux disjoints (violation du principe "un secteur ne peut
    jamais contenir un morceau d'un autre secteur, ni etre lui-meme
    fragmente ailleurs"). Conforme a la pratique AWWA M36 : une DMA se
    delimite par son reseau de DISTRIBUTION, pas par les artères de
    transport qui la traversent. Les conduites de transport restent
    affichees en couche ligne separee (conduites_transport_topo), jamais
    remplies en surface.
  - Chevauchement geometrique RESIDUEL possible malgre tout, une fois le
    transport exclu : deux conduites de DISTRIBUTION de deux secteurs
    DIFFERENTS mais physiquement proches dans l'espace (rues paralleles,
    sans etre connectees) peuvent voir leurs buffers se toucher. C'est
    purement local et borne par 2 x LARGEUR_TAMPON_CONDUITE_M (quelques
    dizaines de metres), sans commune mesure avec les recouvrements
    kilometriques du convex hull ou des conduites de transport bufferisees
    par erreur. Il est resolu par _resoudre_chevauchements_residuels()
    (priorite au secteur avec le plus d'abonnes, pas d'inflation ni de
    perte de couverture), et journalisee avec sa surface exacte pour
      verification.

CORRECTIF CONTIGUITE -- FIN DES SECTEURS EN CONFETTIS
  Constat terrain (capture QGIS, couche `secteurs_hydrauliques_topo`, 37
  secteurs) : selectionner UN secteur allumait une dizaine de morceaux
  eparpilles dans toute la ville -- rubans de ~60 m de large (= 2 x le buffer
  de 30 m autour d'une conduite) entremeles avec ceux des secteurs voisins, et
  jusqu'a un dome isole en plein desert. Une telle carte est inexploitable :
  un secteur / DMA se lit comme UNE zone continue (AWWA M36, IWA DMA Guidance
  Notes 2007). Trois causes, trois corrections, toutes journalisees :
    1. l'habillage par tampon de conduites EST par nature un faisceau de
       rubans disjoints. Il n'est plus publie tel quel : la partition stricte
       des ilots est desormais tentee meme sans couche `routes`, en prenant le
       reseau AEP de distribution comme trame de decoupe (les conduites
       suivent la voirie). S'il faut malgre tout retomber sur le tampon, une
       ALERTE explicite le dit, avec la marche a suivre ;
    2. la representation de secours d'un secteur absent  reprenait tout
       le faisceau de ses conduites : elle est ramenee a son BLOC PRINCIPAL ;
    3. filet de securite final `_mettre_en_un_seul_tenant()` : tout morceau
       secondaire subsistant rejoint le secteur voisin avec lequel il partage
       la plus longue frontiere (regle deja utilisee pour les exclaves).
  Invariants preserves : aucune enveloppe convexe, aucune surface inventee ni
  perdue (simple transfert de propriete du TERRITOIRE), 0 chevauchement,
  autant de polygones que de secteurs, et aucune appartenance hydraulique
  modifiee -- ce module continue de n'habiller qu'un resultat deja tranche.
  Une annexe qui ne touche AUCUN autre secteur reste conservee et signalee :
  la supprimer effacerait du reseau reellement exploite.
=============================================================================
"""

import logging
from collections import defaultdict

from qgis.core import (
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsProject,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QVariant

from .couche_differee import CoucheDifferee


def _assigner_conduites_aux_secteurs(graphe_data, secteur_final_de_noeud, _log):
    """Determine, pour CHAQUE conduite (fid), le secteur auquel son buffer
    doit etre attribue. Regle :
      - Les deux extremites dans le meme secteur -> ce secteur (cas
        largement majoritaire).
      - Extremites dans deux secteurs differents (conduite frontaliere) ->
        secteur du noeud le plus proche topologiquement d'un reservoir
        (distance_de_noeud la plus petite), journalise.
      - Une extremite (ou les deux) non rattachee a un secteur (noeud
        non-atteignable, cf. rattachement) -> conduite EXCLUE de tout
        polygone, journalisee explicitement (jamais rattachee au hasard).

    Retourne : {fid: secteur_id}, et le nb de conduites exclues.
    """
    G = graphe_data['graphe']
    fid_vers_secteur = {}
    nb_exclues = 0
    nb_frontalieres_tranchees = 0

    for u, v, data in G.edges(data=True):
        su = secteur_final_de_noeud.get(u)
        sv = secteur_final_de_noeud.get(v)
        fids = data.get('fids', [])

        if su is None or sv is None:
            nb_exclues += len(fids)
            continue

        if su == sv:
            secteur_choisi = su
        else:
            # Frontaliere : attribuee au cote le plus proche d'une source.
            # (Le tri se fait sur distance_de_noeud, fourni par l'appelant
            # via un attribut pose sur le graphe -- cf. generer_polygones.)
            du = G.nodes[u].get('_distance_source', float('inf'))
            dv = G.nodes[v].get('_distance_source', float('inf'))
            secteur_choisi = su if du <= dv else sv
            nb_frontalieres_tranchees += 1

        for fid in fids:
            fid_vers_secteur[fid] = secteur_choisi

    if nb_exclues:
        _log(
            "  [ALERTE] %d conduite(s) exclue(s) de la generation de "
            "polygones (extremite non rattachee a un secteur -- cf. "
            "'non_atteignables' du rattachement)." % nb_exclues
        )
    if nb_frontalieres_tranchees:
        _log(
            "  %d conduite(s) frontaliere(s) attribuee(s) au secteur cote "
            "source le plus proche (regle deterministe, pas de double "
            "buffer)." % nb_frontalieres_tranchees
        )

    return fid_vers_secteur


def _fermeture_morphologique_bornee(geom, rayon_m, buffer_segments):
    """Buffer +r puis -r : ressoude les parties proches de moins de 2*r,
    sans jamais depasser cette portee (contrairement au convex hull, qui
    n'a aucune limite). rayon_m = 0 desactive (aucune fermeture)."""
    if rayon_m <= 0 or geom is None or geom.isEmpty():
        return geom
    elargi = geom.buffer(rayon_m, buffer_segments)
    if elargi is None or elargi.isEmpty():
        return geom
    reduit = elargi.buffer(-rayon_m, buffer_segments)
    if reduit is None or reduit.isEmpty():
        return geom
    return reduit


def _nettoyer_trous_interieurs(geom):
    """Supprime les anneaux interieurs (trous) d'un polygone/multipolygone.
    Contrairement a assurer_polygone_simple() de pipeline.py, ceci NE
    fusionne PAS les parties disjointes entre elles -- un MultiPolygon reste
    un MultiPolygon, seuls les trous internes de CHAQUE partie sont retires
    (un trou en forme de rue a l'interieur d'un secteur n'a pas de sens
    hydraulique)."""
    if geom is None or geom.isEmpty():
        return geom
    flat_type = QgsWkbTypes.flatType(geom.wkbType())
    if flat_type == QgsWkbTypes.Type.Polygon:
        rings = geom.asPolygon()
        if not rings:
            return geom
        return QgsGeometry.fromPolygonXY([rings[0]])
    if flat_type == QgsWkbTypes.Type.MultiPolygon:
        parts = geom.asMultiPolygon()
        if not parts:
            return geom
        return QgsGeometry.fromMultiPolygonXY([[p[0]] for p in parts if p])
    return geom


def _parties_polygonales(geom):
    """Parties polygonales d'une geometrie, chacune en QgsGeometry simple."""
    if geom is None or geom.isEmpty():
        return []
    if QgsWkbTypes.flatType(geom.wkbType()) == QgsWkbTypes.Type.MultiPolygon:
        out = []
        for anneaux in (geom.asMultiPolygon() or []):
            if not anneaux:
                continue
            g = QgsGeometry.fromPolygonXY(anneaux)
            if g is not None and not g.isEmpty():
                out.append(g)
        return out
    return [geom]


def _partie_principale(geom):
    """(partie la plus vaste, nb de parties abandonnees, leur surface).

    sert a ne JAMAIS publier un territoire en confettis quand une
    representation de secours doit etre fabriquee -- un secteur est une zone,
    pas une collection de rubans disperses dans la ville.
    """
    parts = _parties_polygonales(geom)
    if len(parts) <= 1:
        return geom, 0, 0.0
    parts.sort(key=lambda g: g.area(), reverse=True)
    return parts[0], len(parts) - 1, sum(g.area() for g in parts[1:])


def _longueur_frontiere(a, b):
    """Longueur de frontiere commune entre deux polygones jointifs (0 s'ils
    ne se touchent pas). Si les geometries se recouvrent encore, l'aire
    d'intersection sert de mesure de proximite (cas residuel)."""
    try:
        if not a.intersects(b):
            return 0.0
        inter = a.intersection(b)
        if inter is None or inter.isEmpty():
            return 0.0
        if inter.area() > 0.5:
            return float(inter.area())
        return float(inter.length())
    except Exception:
        return 0.0


def _mettre_en_un_seul_tenant(polygones_par_secteur, config, _log):
    """FILET DE SECURITE CONTIGUITE : ramene chaque secteur de la
    couche de sortie a UN territoire d'un seul tenant.

    Defaut corrige : dans QGIS, selectionner un secteur allumait une dizaine
    de morceaux eparpilles dans toute la ville, entremeles avec ceux des
    secteurs voisins (couche secteurs_hydrauliques_topo). Un secteur / DMA se
    lit comme UNE zone continue (AWWA M36, IWA DMA Guidance Notes) : la
    fragmentation cartographique rend la carte inexploitable pour le
    terrain.

    Regle, identique a celle deja utilisee pour les exclaves : le morceau le
    plus vaste reste au secteur, chaque autre morceau rejoint le secteur
    voisin avec lequel il partage la plus longue frontiere. Aucune surface
    n'est creee ni supprimee (transfert de propriete), aucun secteur n'est
    vide, aucune enveloppe convexe. Un morceau qui ne touche aucun autre
    secteur (annexe reliee par une conduite en zone non batie) est conserve
    et journalise : le supprimer effacerait du reseau exploite.
    """
    if not bool(config.get('ASSURER_CONTIGUITE_TERRITOIRES', True)):
        return 0, 0
    tolere = float(config.get('AIRE_EXCLAVE_TOLEREE_M2', 0.0))
    max_passes = max(1, int(config.get('MAX_PASSES_CONTIGUITE', 60)))
    absorbes = 0
    aire_absorbee = 0.0
    isolees = []
    for _passe in range(max_passes):
        mouvements = 0
        isolees = []          # seul l'etat de la derniere passe compte
        for s in sorted(polygones_par_secteur.keys(), key=str):
            parts = _parties_polygonales(polygones_par_secteur.get(s))
            if len(parts) <= 1:
                continue
            parts.sort(key=lambda g: g.area(), reverse=True)
            for q in parts[1:]:
                aire = q.area()
                if tolere > 0 and aire >= tolere:
                    continue      # annexe volontairement conservee (config)
                meilleur, meilleure = None, 0.0
                for t, gt in polygones_par_secteur.items():
                    if t == s or gt is None or gt.isEmpty():
                        continue
                    lg = _longueur_frontiere(q, gt)
                    if lg > meilleure + 1e-9:
                        meilleur, meilleure = t, lg
                if meilleur is None or meilleure <= 0.01:
                    isolees.append((s, aire))
                    continue
                reste = polygones_par_secteur[s].difference(q)
                if reste is None or reste.isEmpty():
                    continue      # garde-fou : ne jamais vider un secteur
                fusion = QgsGeometry.unaryUnion(
                    [polygones_par_secteur[meilleur], q])
                if fusion is None or fusion.isEmpty():
                    continue
                polygones_par_secteur[s] = reste
                polygones_par_secteur[meilleur] = _nettoyer_trous_interieurs(
                    fusion)
                mouvements += 1
                absorbes += 1
                aire_absorbee += aire
        if not mouvements:
            break

    if absorbes:
        _log("  Contiguite %d morceau(x) eparpille(s) (%.0f m2) rendu(s) "
             "au secteur voisin le plus frontalier -- chaque secteur redevient "
             "une zone d'un seul tenant (aucune appartenance hydraulique "
             "modifiee, seul le TERRITOIRE cartographique change)."
             % (absorbes, aire_absorbee))
    if isolees:
        detail = ", ".join("%s (%.0f m2)" % (s, a)
                           for s, a in sorted(isolees, key=lambda t: -t[1])[:8])
        _log("  [INFO] %d exclave(s) physiquement isolee(s) conservee(s), "
             "aucun secteur voisin en contact (annexe alimentee par une "
             "conduite traversant une zone non batie, reservoir isole) : %s. "
             "A traiter en conception de reseau, pas en cartographie."
             % (len(isolees), detail))
    return absorbes, len(isolees)


def _resoudre_chevauchements_residuels(polygones_par_secteur, abonnes_par_secteur,
                                       _log, noeud_vers_xy=None,
                                       secteur_final_de_noeud=None):
    """Chevauchement geometrique residuel entre secteurs DIFFERENTS (buffers
    de conduites non connectees mais spatialement proches).

    FIX-CHEVAUCHEMENT-DISTANCE : la zone en litige est attribuee au secteur
    dont le NŒUD HYDRAULIQUE DEJA AFFECTE est le plus proche (distance
    reseau/spatiale) -- PAS au secteur qui compte le plus d'abonnes au total.
    L'ancien critere ("le secteur avec le plus d'abonnes garde la zone")
    comparait un total GLOBAL par secteur, invariant d'une paire a l'autre :
    un meme secteur tres peuple gagnait systematiquement contre tous ses
    voisins, y compris loin de son propre reseau, produisant des morceaux
    disjoints imbriques dans le territoire d'un secteur voisin plus petit
    ("poupees russes" constatees sur secteurs_hydrauliques_topo). Le nœud le
    plus proche est un fait local et geometrique, coherent avec la regle deja
    utilisee ailleurs dans ce module pour trancher une conduite frontaliere
    (secteur du nœud le plus proche d'une source) et dans
    partition_secteurs._secteur_du_noeud_le_plus_proche.

    noeud_vers_xy / secteur_final_de_noeud : fournis par l'appelant
    (graphe_data['noeud_vers_xy'], scission['secteur_final_de_noeud']) pour
    activer ce critere. S'ils sont absents (retro-compatibilite), on retombe
    sur l'ancien critere "plus d'abonnes", explicitement journalise comme
    degrade.

    Modifie `polygones_par_secteur` en place et retourne le nombre de
    chevauchements resolus + la surface totale corrigee.
    """
    index_par_secteur = None
    if noeud_vers_xy and secteur_final_de_noeud:
        from . import partition_secteurs as _ps
        pts_par_secteur = defaultdict(dict)
        for nid, xy in noeud_vers_xy.items():
            s = secteur_final_de_noeud.get(nid)
            if s is not None:
                pts_par_secteur[s][nid] = (xy.x(), xy.y())
        index_par_secteur = {
            s: _ps.PointIndex(pts) for s, pts in pts_par_secteur.items()}
    else:
        _log(
            "  [ALERTE] noeuds indisponibles pour la resolution des "
            "chevauchements : repli sur le critere 'plus d'abonnes' "
            "(degrade, cf. FIX-CHEVAUCHEMENT-DISTANCE)."
        )

    secteurs = sorted(polygones_par_secteur.keys())
    surface_totale_corrigee = 0.0
    nb_resolus = 0

    for i, s_a in enumerate(secteurs):
        geom_a = polygones_par_secteur.get(s_a)
        if geom_a is None or geom_a.isEmpty():
            continue
        for s_b in secteurs[i + 1:]:
            geom_b = polygones_par_secteur.get(s_b)
            if geom_b is None or geom_b.isEmpty():
                continue
            if not geom_a.intersects(geom_b):
                continue
            inter = geom_a.intersection(geom_b)
            if inter is None or inter.isEmpty():
                continue
            aire = inter.area()
            if aire < 0.5:  # bruit numerique, pas un vrai chevauchement
                continue

            gagnant, motif = None, None
            if index_par_secteur is not None:
                centre = inter.centroid().asPoint()
                idx_a = index_par_secteur.get(s_a)
                idx_b = index_par_secteur.get(s_b)
                nid_a, d_a = (idx_a.nearest_matching(
                    centre.x(), centre.y(), lambda _k: True)
                    if idx_a is not None else (None, None))
                nid_b, d_b = (idx_b.nearest_matching(
                    centre.x(), centre.y(), lambda _k: True)
                    if idx_b is not None else (None, None))
                if nid_a is None and nid_b is None:
                    pass
                elif nid_a is None:
                    gagnant, motif = s_b, "seul %s a un nœud a proximite" % s_b
                elif nid_b is None:
                    gagnant, motif = s_a, "seul %s a un nœud a proximite" % s_a
                else:
                    gagnant = s_a if d_a <= d_b else s_b
                    motif = "nœud le plus proche (%.0fm vs %.0fm)" % (
                        d_a ** 0.5, d_b ** 0.5)
            if gagnant is None:
                # repli : ancien critere, explicitement marque comme degrade
                w_a = abonnes_par_secteur.get(s_a, 0)
                w_b = abonnes_par_secteur.get(s_b, 0)
                gagnant = s_a if w_a >= w_b else s_b
                motif = "repli abonnes (%d vs %d)" % (w_a, w_b)

            perdant = s_b if gagnant == s_a else s_a
            polygones_par_secteur[perdant] = polygones_par_secteur[perdant].difference(
                polygones_par_secteur[gagnant])
            surface_totale_corrigee += aire
            nb_resolus += 1
            _log(
                "    Chevauchement residuel %s / %s : %.1f m2 -- attribue "
                "a %s (%s)." % (s_a, s_b, aire, gagnant, motif)
            )

    if nb_resolus:
        _log(
            "  %d chevauchement(s) residuel(s) resolu(s), %.1f m2 au total "
            "(a comparer aux dizaines de millions de m2 corriges par "
            "l'ancien convex hull -- cf. logs pipeline.py)."
            % (nb_resolus, surface_totale_corrigee)
        )
    else:
        _log("  Aucun chevauchement residuel detecte.")

    return nb_resolus, surface_totale_corrigee


def _garantir_representation(polygones_par_secteur, secteurs_attendus,
                              geoms_buffer_par_secteur, _log):
    """GARANTIE DE REPRESENTATION  : aucun secteur de la partition ne
    peut disparaitre de la couche de sortie.

    Un secteur peut se retrouver sans polygone pour deux raisons, toutes deux
    observees : (1) dans l'habillage par ilots urbains, tous ses ilots ont
    ete remportes par un secteur voisin plus peuple (vote majoritaire) ;
    (2) dans le nettoyage topologique, ses parties etaient toutes sous le
    seuil d'ilot. Or le nombre de polygones doit rester egal au nombre de
    secteurs de la partition (exigence du projet).

    Regle appliquee, deterministe et journalisee : le secteur manquant
    recupere le tampon de SES PROPRES conduites de distribution -- son
    territoire minimal au sens AWWA M36, une DMA etant delimitee par son
    reseau de distribution -- et cette surface est RETIREE des secteurs qui
    l'occupaient. La partition reste donc stricte (aucun chevauchement
    cree), et aucune surface n'est inventee au-dela du tampon deja utilise
    ailleurs dans ce module.
    """
    manquants = [s for s in secteurs_attendus if s not in polygones_par_secteur]
    if not manquants:
        return 0
    nb_rendus = 0
    for secteur in sorted(manquants, key=str):
        geoms = geoms_buffer_par_secteur.get(secteur)
        if not geoms:
            _log(
                "  [ALERTE] Secteur %s sans aucune conduite de DISTRIBUTION "
                "exploitable (toutes ses conduites sont du transport ou sans "
                "geometrie) : aucun polygone ne peut lui etre construit sans "
                "inventer de la surface. Secteur absent de la couche, a "
                "verifier cote donnees." % secteur
            )
            continue
        minimal = QgsGeometry.unaryUnion(geoms)
        if minimal is None or minimal.isEmpty():
            continue
        minimal = _nettoyer_trous_interieurs(minimal)
        # le tampon des conduites d'un secteur est un faisceau de rubans
        # disjoints. Publier ce faisceau tel quel, c'est exactement le defaut
        # constate dans QGIS (un secteur = 10 morceaux eparpilles). On ne garde
        # que le BLOC PRINCIPAL : une zone lisible, d'un seul tenant.
        minimal, nb_abandonnees, aire_abandonnee = _partie_principale(minimal)
        if nb_abandonnees:
            _log(
                "  Secteur %s : representation de secours ramenee a son bloc "
                "principal (%d ruban(s) de conduite isole(s), %.0f m2, laisses "
                "aux secteurs qui les entourent) -- un secteur doit se lire "
                "comme UNE zone." % (secteur, nb_abandonnees, aire_abandonnee)
            )
        for autre, geom_autre in list(polygones_par_secteur.items()):
            if geom_autre is None or geom_autre.isEmpty():
                continue
            if not geom_autre.intersects(minimal):
                continue
            reste = geom_autre.difference(minimal)
            if reste is not None and not reste.isEmpty():
                polygones_par_secteur[autre] = reste
        polygones_par_secteur[secteur] = minimal
        nb_rendus += 1
        _log(
            "  Secteur %s absent de l'habillage (ilots remportes par des "
            "secteurs voisins) : represente par le tampon de ses propres "
            "conduites de distribution (%.0f m2), surface retiree des "
            "secteurs voisins -- partition toujours sans chevauchement."
            % (secteur, minimal.area())
        )
    if nb_rendus:
        _log("  %d secteur(s) restitue(s) : le nombre de polygones est de "
             "nouveau egal au nombre de secteurs de la partition." % nb_rendus)
    return nb_rendus


def _reutiliser_polygones_reference(secteurs_attendus, config, _log):
    """FIX-DOUBLE-MOTEUR-ILOTS : si la partition de reference
    (``secteurs_hydrauliques``, produite par pipeline.py) est
    presente ET couvre TOUS les secteurs attendus, on adopte directement ses
    polygones au lieu de relancer un second calcul d'ilots ici.

    Pourquoi : consolidation_secteurs.consolider_sur_reference() a deja
    aligne `secteur_final_de_noeud` sur cette meme reference (vote par
    conduite + Dijkstra pour les noeuds sans vote). Le socle topologique n'a
    donc PLUS d'appartenance differente de celle de pipeline.py -- ce
    n'est que la meme sectorisation. Relancer `partition_secteurs.
    construire_territoires` une seconde fois ici, avec sa PROPRE emprise
    (recalculee sur les seuls noeuds atteignables) et ses PROPRES poids
    (1.0 par noeud, faute d'abonnes connus a ce stade), produit un ensemble
    d'ilots differents de celui utilise pour construire secteurs_hydrauliques --
    deux moteurs geometriques independants sur la meme appartenance
    peuvent legitimement decouper les ilots differemment pres des frontieres,
    d'ou les emboitements ("poupees russes") et l'ecart de surface totale
    observes entre secteurs_hydrauliques et secteurs_hydrauliques_topo.

    Reutiliser directement les polygones de reference garantit par
    construction : Sigma(aire) identique, zero nouvel emboitement (la
    reference elle-meme est deja verifiee a 0 chevauchement par
    pipeline.py), et un seul moteur de partition faisant autorite.

    Retourne {secteur_id: QgsGeometry} (clones) ou None si la reference est
    absente, incomplete, ou desactivee (REUTILISER_GEOMETRIE_REFERENCE=false).
    """
    if not bool(config.get('REUTILISER_GEOMETRIE_REFERENCE', True)):
        return None
    if not bool(config.get('ALIGNER_SUR_SECTEURS_REFERENCES', True)):
        return None
    nom_ref = config.get('COUCHE_REFERENCE_SECTEURS') or 'secteurs_hydrauliques'
    couches = QgsProject.instance().mapLayersByName(nom_ref)
    if not couches:
        _log(
            "  [INFO] Couche de reference '%s' introuvable : impossible de "
            "reutiliser sa geometrie, la partition par ilots sera "
            "(re)calculee ici." % nom_ref
        )
        return None
    couche_ref = couches[0]
    champs = [f.name() for f in couche_ref.fields()]
    if 'secteur_id' not in champs:
        _log(
            "  [ALERTE] Couche de reference '%s' sans champ 'secteur_id' : "
            "reutilisation impossible." % nom_ref
        )
        return None
    geoms_ref = {}
    for feat in couche_ref.getFeatures():
        s = feat['secteur_id']
        if s in (None, ''):
            continue
        geom = feat.geometry()
        if geom is None or geom.isEmpty():
            continue
        geoms_ref[str(s)] = QgsGeometry(geom)

    manquants = [s for s in secteurs_attendus if s not in geoms_ref]
    if manquants:
        _log(
            "  [INFO] Couche de reference '%s' ne couvre pas tous les "
            "secteurs attendus (%d manquant(s) : %s) : la partition par "
            "ilots sera (re)calculee ici plutot que de melanger deux "
            "sources geometriques." % (nom_ref, len(manquants),
                                       ", ".join(sorted(manquants)[:8]))
        )
        return None

    _log(
        "  Polygones REPRIS DIRECTEMENT de la partition de reference '%s' "
        "(aucun second moteur d'ilots relance ici) : Sigma(aire) et "
        "chevauchements restent strictement ceux, deja verifies, de "
        "pipeline.py." % nom_ref
    )
    return {s: geoms_ref[s] for s in secteurs_attendus}


def generer_polygones_secteurs(graphe_data, rattachement, scission, config,
                                get_layer, _ajouter_couche, _log,
                                abonnes_par_secteur=None,
                                antennes_par_zone=None,
                                sous_zones_par_secteur=None):
    """Point d'entree : construit la couche de polygones de secteur a partir
    de la partition DEJA TRANCHEE (scission['secteur_final_de_noeud']).

    Parametres de config utilises (nouveaux, tous SANS lien avec les
    routes) :
      - LARGEUR_TAMPON_CONDUITE_M (defaut 30.0) : rayon de buffer autour de
        chaque conduite, comme secteurs_limites dans pipeline.py.
      - BUFFER_SEGMENTS (defaut 8) : qualite du buffer (deja utilise
        ailleurs dans le projet).
      - SOUDURE_MAX_M (defaut 0.0, DESACTIVE) : rayon de fermeture
        morphologique bornee. Mettre par ex. 20-30 m si vous voulez
        ressouder les petites coutures sans jamais recourir au convex hull.

    abonnes_par_secteur : dict optionnel {secteur_id: nb_abonnes} pour
    prioriser la resolution des chevauchements residuels (a defaut, la
    resolution se fait par ordre alphabetique -- moins satisfaisant mais
    fonctionnel).

    sous_zones_par_secteur : dict optionnel {secteur_id: [sous_zone_id, ...]}
    (cf. graphe_topologique.scinder_par_continuite_distribution et
    consolidation_secteurs.regrouper_sous_zones) -- les blocs de distribution
    disjoints d'un secteur, reportes dans le champ 'sous_zones' de la couche
    de sortie. Purement informatif : une sous-zone de sous-comptage n'est pas
    un secteur (IWA DMA Guidance Notes 2007, AWWA M36), elle n'a donc pas son
    propre polygone.

    antennes_par_zone : dict optionnel {secteur_id: [fid_conduite, ...]}
    (cf. graphe_topologique.scinder_par_continuite_distribution) -- les
    conduites de TRANSPORT qui alimentent chaque zone depuis le reste du
    reseau. Reporte tel quel dans le champ 'antennes_fid' de la couche de
    sortie (liste de fids separes par des virgules, vide si aucune ou si
    non fourni) -- purement informatif, n'influence aucune geometrie.

    Retourne : {
        'couche'              : QgsVectorLayer des polygones crees,
        'polygones_par_secteur': {secteur_id: QgsGeometry},
        'nb_multipart'        : int (secteurs restes MultiPolygon, assumes),
        'nb_chevauchements_resolus': int,
    }
    """
    G = graphe_data['graphe']
    secteur_final_de_noeud = scission['secteur_final_de_noeud']
    distance_de_noeud = rattachement['distance_de_noeud']

    # Pose temporairement la distance-source sur les noeuds du graphe pour
    # que _assigner_conduites_aux_secteurs() puisse trancher les
    # frontalieres sans recalcul.
    for n, d in distance_de_noeud.items():
        G.nodes[n]['_distance_source'] = d

    _log("-" * 70)
    _log("GENERATION DES POLYGONES (aucune enveloppe convexe, aucune "
         "largeur de route -- buffer fixe sur conduites AEP uniquement).")

    fid_vers_secteur = _assigner_conduites_aux_secteurs(
        graphe_data, secteur_final_de_noeud, _log)

    secteurs_attendus = sorted(set(
        s for s in secteur_final_de_noeud.values() if s is not None))
    polygones_reference = _reutiliser_polygones_reference(
        secteurs_attendus, config, _log)

    aep_layer = get_layer(config['AEP_LAYER'])
    largeur = float(config.get('LARGEUR_TAMPON_CONDUITE_M', 30.0))
    segments = int(config.get('BUFFER_SEGMENTS', 8))
    soudure_max = float(config.get('SOUDURE_MAX_M', 0.0))
    seuil_transport = float(config.get('SEUIL_DN_TRANSPORT_MM', 150.0))
    diam_field = config.get('PIPE_DIAMETER_FIELD')
    champs_aep = [f.name() for f in aep_layer.fields()]
    a_diam = bool(diam_field) and diam_field in champs_aep

    if not a_diam:
        _log(
            "  [ALERTE] Champ diametre '%s' introuvable dans la couche AEP "
            "-- IMPOSSIBLE de distinguer transport/distribution. TOUTES les "
            "conduites seront bufferisees (risque de chevauchements massifs "
            "si des conduites de transport traversent d'autres secteurs). "
            "Verifiez CONFIG['PIPE_DIAMETER_FIELD'] (doit correspondre au "
            "nom exact du champ dans la couche AEP, ex. 'DIAMETRE')."
            % diam_field
        )

    geoms_par_secteur = defaultdict(list)
    lignes_distribution = []     # trame de repli si la couche routes
    feats_transport = []         #       est absente (les conduites suivent
    nb_transport = 0             #       la voirie)
    for feat in aep_layer.getFeatures():
        secteur = fid_vers_secteur.get(feat.id())
        if secteur is None:
            continue
        geom = feat.geometry()
        if geom is None or geom.isEmpty():
            continue

        est_transport = False
        if a_diam:
            try:
                v = feat[diam_field]
                if v is not None and float(v) >= seuil_transport:
                    est_transport = True
            except Exception as exc:
                logging.getLogger(__name__).debug(
                    "Exception ignoree : %s", exc)

        if est_transport:
            nb_transport += 1
            feats_transport.append((feat, secteur))
            continue  # jamais bufferise dans le polygone de secteur

        lignes_distribution.append(QgsGeometry(geom))
        tampon = geom.buffer(largeur, segments)
        if tampon is not None and not tampon.isEmpty():
            geoms_par_secteur[secteur].append(tampon)

    if a_diam:
        _log(
            "  %d conduite(s) de TRANSPORT (DN >= %.0f mm, champ '%s') "
            "exclue(s) du remplissage de polygone -- affichees en ligne "
            "seule (couche conduites_transport_topo)."
            % (nb_transport, seuil_transport, diam_field)
        )

    nb_multipart = 0
    nb_resolus, surface_corrigee = 0, 0.0
    partition_appliquee = False

    if polygones_reference is not None:
        # FIX-DOUBLE-MOTEUR-ILOTS : geometrie reprise telle quelle, aucun
        # second calcul d'ilots -- cf. _reutiliser_polygones_reference().
        polygones_par_secteur = dict(polygones_reference)
        partition_appliquee = 'reference'
        for secteur, geom in polygones_par_secteur.items():
            if QgsWkbTypes.flatType(geom.wkbType()) == QgsWkbTypes.Type.MultiPolygon \
                    and len(geom.asMultiPolygon()) > 1:
                nb_multipart += 1
    else:
        polygones_par_secteur = {}
        for secteur, geoms in geoms_par_secteur.items():
            union = QgsGeometry.unaryUnion(geoms)
            if union is None or union.isEmpty():
                continue
            union = _nettoyer_trous_interieurs(union)
            if soudure_max > 0:
                union = _fermeture_morphologique_bornee(union, soudure_max, segments)
                union = _nettoyer_trous_interieurs(union)
            if QgsWkbTypes.flatType(union.wkbType()) == QgsWkbTypes.Type.MultiPolygon:
                nb_parts = len(union.asMultiPolygon())
                if nb_parts > 1:
                    nb_multipart += 1
                    _log(
                        "  [INFO] Secteur %s reste en %d partie(s) disjointe(s) "
                        "apres buffer (antenne(s) isolee(s) du reseau, pas de "
                        "fusion forcee -- augmenter SOUDURE_MAX_M si ces "
                        "parties devraient etre ressoudees, ou verifier la "
                        "continuite reelle du reseau AEP a cet endroit)."
                        % (secteur, nb_parts)
                    )
            polygones_par_secteur[secteur] = union

        _log("  %d secteur(s) construits par buffer/dissolve (sans convex hull)."
             % len(polygones_par_secteur))

        nb_resolus, surface_corrigee = _resoudre_chevauchements_residuels(
            polygones_par_secteur, abonnes_par_secteur or {}, _log,
            noeud_vers_xy=graphe_data.get('noeud_vers_xy'),
            secteur_final_de_noeud=secteur_final_de_noeud)

        # ---- partition FIX-PARTITION ---------------------------------------------
        # Le buffer de conduites produit des rubans fragmentes (cf. images du
        # run de validation : « secteurs_hydrauliques_topo » en ilots epars).
        # Quand la couche routes est disponible, on remplace cet habillage par
        # la PARTITION STRICTE des ilots urbains (core/partition_secteurs.py) :
        # memes appartenances deja tranchees (secteur_final_de_noeud), mais des
        # polygones qui pavent le tissu urbain sans chevauchement ni ilot
        # parasite, limites sur les lignes centrales de rues.
        #
        # N'est atteint QUE si `_reutiliser_polygones_reference()` n'a rien pu
        # reutiliser (pas de couche de reference dans le projet, ou desactive) --
        # c'est le mode de test autonome du socle topologique, cf. docstring
        # de core/executer_topologique.py.
        if bool(config.get('ACTIVER_PARTITION_ILOTS', True)):
            route_layer69 = None
            try:
                _nom_route69 = (config.get('ROUTE_LAYER') or 'routes')
                _ls69 = QgsProject.instance().mapLayersByName(_nom_route69)
                route_layer69 = _ls69[0] if _ls69 else None
            except Exception:
                route_layer69 = None
            # la couche routes reste la meilleure trame (limites sur les
            # lignes centrales de rues). Si elle est absente, on ne retombe PLUS
            # sur le faisceau de rubans bufferises -- c'est le defaut visible
            # dans QGIS : on utilise le reseau AEP lui-meme comme trame, puisque
            # les conduites de distribution suivent la voirie. Le pavage reste
            # une partition stricte, sans enveloppe convexe.
            if route_layer69 is not None:
                trame71 = [f.geometry() for f in route_layer69.getFeatures()]
                origine71 = "couche routes '%s'" % (config.get('ROUTE_LAYER')
                                                    or 'routes')
            else:
                trame71 = lignes_distribution
                origine71 = ("reseau AEP de distribution (couche routes '%s' "
                             "introuvable)" % (config.get('ROUTE_LAYER')
                                               or 'routes'))
                _log(
                    "  [ALERTE] Couche routes '%s' introuvable : la trame de "
                    "decoupe est prise sur le reseau AEP de distribution lui-meme "
                    "(les conduites suivent la voirie). Ajoutez la couche routes "
                    "au projet, ou corrigez CONFIG['ROUTE_LAYER'], pour des "
                    "limites de secteur calees sur les lignes centrales de rues."
                    % (config.get('ROUTE_LAYER') or 'routes')
                )
            try:
                from . import geom_backend as _gb69
                from . import partition_secteurs as _part69
                noeud_vers_xy69 = graphe_data.get('noeud_vers_xy', {})
                noeuds_xy69 = {k: (xy.x(), xy.y())
                               for k, xy in noeud_vers_xy69.items()}
                secteur_noeuds69 = {k: v for k, v in
                                    secteur_final_de_noeud.items() if v}
                # poids par noeud : abonnes du secteur si connus (repartis
                # uniformement sur ses noeuds, a defaut d'un decompte par
                # noeud), sinon 1.0 -- coherent avec le vote pondere par
                # abonnes de pipeline.py plutot qu'un vote uniforme.
                _compte_noeuds_secteur = defaultdict(int)
                for k, v in secteur_noeuds69.items():
                    _compte_noeuds_secteur[v] += 1
                poids69 = {}
                for k, v in secteur_noeuds69.items():
                    ab = (abonnes_par_secteur or {}).get(v)
                    if ab:
                        poids69[k] = max(
                            1.0, float(ab) / _compte_noeuds_secteur[v])
                    else:
                        poids69[k] = 1.0
                xs69 = [x for x, _y in noeuds_xy69.values()]
                ys69 = [y for _x, y in noeuds_xy69.values()]
                if not xs69 or not trame71:
                    raise ValueError("aucun noeud localise ou trame vide")
                marge69 = 2.0 * largeur
                emprise69 = _gb69.box(min(xs69) - marge69, min(ys69) - marge69,
                                      max(xs69) + marge69, max(ys69) + marge69)
                territoires69, _st69 = _part69.construire_territoires(
                    trame71, emprise69, noeuds_xy69, secteur_noeuds69, poids69,
                    config, log=_log)
                if territoires69:
                    polygones_par_secteur = {s: g.impl
                                             for s, g in territoires69.items()}
                    partition_appliquee = True
                    _log("  habillage remplace par la PARTITION STRICTE des "
                         "ilots (trame : %s) -- 0 chevauchement, 0 ilot parasite, "
                         "1 secteur = 1 territoire." % origine71)
                else:
                    raise ValueError("partition vide")
            except Exception as e69:
                _log(
                    "  [ALERTE] Partition par ilots impossible ici (%s) : la "
                    "couche de secteurs reste le tampon des conduites, donc des "
                    "RUBANS FRAGMENTES (un secteur apparaitra en plusieurs "
                    "morceaux dans QGIS). Corrigez la trame de decoupe (couche "
                    "routes) puis relancez pour obtenir des territoires d'un seul "
                    "tenant." % e69
                )

    nb_restitues = _garantir_representation(
        polygones_par_secteur, secteurs_attendus, geoms_par_secteur, _log)

    # dernier filet -- plus aucun secteur en morceaux eparpilles.
    nb_absorbes, nb_isolees = _mettre_en_un_seul_tenant(
        polygones_par_secteur, config, _log)

    nb_multipart = sum(
        1 for g in polygones_par_secteur.values()
        if g is not None and not g.isEmpty()
        and QgsWkbTypes.flatType(g.wkbType()) == QgsWkbTypes.Type.MultiPolygon
        and len(g.asMultiPolygon()) > 1)
    _log("  %d polygone(s) pour %d secteur(s) de la partition."
         % (len(polygones_par_secteur), len(secteurs_attendus)))
    if nb_multipart == 0:
        _log("  CONTROLE chaque secteur est un territoire d'un seul "
             "tenant (0 secteur fragmente).")
    elif nb_multipart <= nb_isolees:
        _log("  CONTROLE %d secteur(s) en plusieurs morceaux, tous "
             "justifies par une annexe physiquement isolee du reste de leur "
             "reseau (detail ci-dessus)." % nb_multipart)
    else:
        _log(
            "  [ALERTE] %d secteur(s) restent en plusieurs morceaux alors que "
            "seules %d annexe(s) isolee(s) l'expliquent. La carte sera "
            "difficile a lire : verifiez la trame de decoupe (couche routes) "
            "et ASSURER_CONTIGUITE_TERRITOIRES dans la configuration."
            % (nb_multipart, nb_isolees)
        )

    # --- Couche de sortie ---
    crs = aep_layer.crs().authid()
    # FIX-THREAD-SAFETY : CoucheDifferee (pas QgsVectorLayer) -- cf.
    # core/couche_differee.py. Cette fonction est appelee depuis TopoWorker
    # (QThread, cf. ui/panneau_principal.py).
    couche = CoucheDifferee(f"MultiPolygon?crs={crs}", "secteurs_hydrauliques_topo")
    provider = couche.dataProvider()
    provider.addAttributes([
        QgsField("secteur_id", QVariant.String),
        QgsField("nb_abonnes", QVariant.Int),
        QgsField("multipart", QVariant.Int),
        QgsField("surface_m2", QVariant.Double),
        QgsField("antennes_fid", QVariant.String),
        QgsField("sous_zones", QVariant.String),
    ])
    couche.updateFields()

    feats_out = []
    for secteur, geom in polygones_par_secteur.items():
        if geom is None or geom.isEmpty():
            continue
        multipart_flag = 1 if QgsWkbTypes.flatType(geom.wkbType()) == QgsWkbTypes.Type.MultiPolygon \
            and len(geom.asMultiPolygon()) > 1 else 0
        f = QgsFeature(couche.fields())
        geom_out = geom
        if not geom_out.isMultipart():
            anneaux = geom_out.asPolygon()
            if anneaux:
                geom_out = QgsGeometry.fromMultiPolygonXY([anneaux])
        f.setGeometry(geom_out)
        f.setAttribute("secteur_id", str(secteur))
        f.setAttribute("nb_abonnes", int((abonnes_par_secteur or {}).get(secteur, 0)))
        f.setAttribute("multipart", multipart_flag)
        f.setAttribute("surface_m2", geom.area())
        fids_antennes = (antennes_par_zone or {}).get(secteur, [])
        f.setAttribute("antennes_fid", ",".join(str(x) for x in fids_antennes))
        sous_zones = (sous_zones_par_secteur or {}).get(secteur, [])
        f.setAttribute("sous_zones", ",".join(str(x) for x in sous_zones))
        feats_out.append(f)

    provider.addFeatures(feats_out)
    couche.updateExtents()
    _ajouter_couche(couche)
    _log("Couche creee : %s (%d polygones, buffer=%.1fm, soudure_max=%.1fm)"
         % (couche.name(), len(feats_out), largeur, soudure_max))

    if feats_transport:
        crs_t = aep_layer.crs().authid()
        couche_transport = CoucheDifferee(
            f"LineString?crs={crs_t}", "conduites_transport_topo")
        prov_t = couche_transport.dataProvider()
        prov_t.addAttributes([
            QgsField("secteur_id", QVariant.String),
            QgsField("diametre_mm", QVariant.Double),
        ])
        couche_transport.updateFields()
        out_t = []
        for feat, secteur in feats_transport:
            f = QgsFeature(couche_transport.fields())
            f.setGeometry(feat.geometry())
            f.setAttribute("secteur_id", str(secteur))
            try:
                f.setAttribute("diametre_mm", float(feat[diam_field]))
            except Exception:
                f.setAttribute("diametre_mm", None)
            out_t.append(f)
        prov_t.addFeatures(out_t)
        couche_transport.updateExtents()
        _ajouter_couche(couche_transport)
        _log("Couche creee : %s (%d conduites de transport, DN >= %.0f mm, "
             "jamais remplies en surface)"
             % (couche_transport.name(), len(out_t), seuil_transport))

    _log("-" * 70)

    return {
        'couche': couche,
        'polygones_par_secteur': polygones_par_secteur,
        'nb_multipart': nb_multipart,
        'nb_chevauchements_resolus': nb_resolus,
        'partition_appliquee': partition_appliquee,
        'nb_secteurs_restitues': nb_restitues,
        'nb_fragments_absorbes': nb_absorbes,
        'nb_exclaves_isolees': nb_isolees,
    }
