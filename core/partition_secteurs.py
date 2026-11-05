# -*- coding: utf-8 -*-
"""
Partition topologique des territoires de secteur — build actuel
(FIX-PARTITION + FIX-CONTIGUITE-TERRITOIRES)
================================================================================

OBJECTIF
--------
Produire des polygones de secteur qui :
  1. suivent la trame routière (limite = ligne centrale de rue, jamais un
     bâtiment, un stade ou un quelconque ouvrage) ;
  2. ne se CHEVAUCHENT JAMAIS deux à deux (partition stricte de l'emprise
     urbanisée) ;
  3. ne contiennent AUCUN petit îlot parasite (toute partie plus petite que
     ``SEUIL_AIRE_ILOT_M2`` ou dépourvue de nœud du secteur est réaffectée au
     secteur voisin avec lequel elle partage la plus longue frontière) ;
  3bis. forment CHACUN UN SEUL TENANT : un secteur = un territoire
     connexe, jamais un patchwork de morceaux éparpillés et entremêlés avec
     ses voisins (cause du rendu constaté sur le run de validation : un
     secteur identifié dans QGIS s'allumait en dizaines de rubans dispersés
     sur toute la ville, y compris en plein désert). Chaque morceau
     secondaire est absorbé par le secteur voisin avec lequel il partage la
     plus longue frontière (``ASSURER_CONTIGUITE_TERRITOIRES``) ;
  3ter. ne couvrent JAMAIS de terrain hors tissu desservi : un îlot
     routier qui ne contient aucun nœud du réseau ET qui touche la frange
     EXTÉRIEURE de l'emprise (désert, terres agricoles au bord de la ville)
     n'est rattaché que si un nœud se trouve à moins de
     ``DIST_MAX_ILOT_SANS_NOEUD_M`` (sinon il est exclu et journalisé).
     **complement au build actuel** — un îlot sans nœud propre mais INTÉRIEUR (entouré par le
     territoire déjà tranché d'un secteur -- campus, stade, grand ensemble
     alimenté par une seule antenne périmétrale non modélisée comme jonction)
     est rattaché par ADJACENCE à ce secteur, sans condition de distance :
     c'est un morceau du même tissu urbain desservi, pas une zone non
     desservie, même si aucune jonction AEP n'est modélisée en son centre.
     Seuls les îlots qui touchent réellement le bord extérieur de l'emprise
     restent soumis à la borne de distance ;
  4. n'utilisent JAMAIS d'enveloppe convexe (convex hull), cause directe des
     grands polygones triangulaires chevauchants observés sur les builds
     précédents (cf. README, images du run de validation) ;
  5. respectent l'appartenance hydraulique déjà tranchée par le moteur
     (``secteur_de_noeud`` : pression 10-60 m, capacité réservoir, plages
     d'abonnés, connexité) — ce module ne décide JAMAIS d'une appartenance,
     il ne fait que l'habiller géométriquement. L'absorption d'un morceau
     secondaire (point 3bis) ne change AUCUNE appartenance hydraulique : elle
     ne déplace que du TERRITOIRE cartographique, et le journal indique
     exactement combien de surface et combien d'abonnés changent ainsi de
     territoire ;
  6. sont TOUS représentés : aucun secteur de la partition
     hydraulique ne peut disparaître de la couche parce que ses îlots ont été
     remportés par des voisins plus peuplés (il reçoit alors l'îlot où il
     pèse le plus, retiré à ce voisin).

MÉTHODE
-------
Étape 1 — polygonisation du réseau routier noué (unaryUnion + polygonize) :
          ce sont les vrais îlots urbains ; deux îlots voisins partagent la
          ligne centrale de la rue qui les sépare.
Étape 2 — vote majoritaire pondéré par les abonnés : chaque îlot prend le
          secteur majoritaire parmi les nœuds réseau qu'il contient (repli :
          secteur du nœud le plus proche).
Étape 3 — résidus (poches non fermées par le réseau routier) : toute poche
          INTÉRIEURE (ne touchant pas le bord de l'emprise) et de taille
          raisonnable est rattachée au secteur de l'îlot voisin avec lequel
          elle partage la plus longue frontière ; les zones externes (désert,
          franges) sont exclues.
Étape 4 — dissolution par secteur, puis nettoyage topologique
          (``nettoyer_polygones``) : réaffectation des îlots/exclaves,
          résolution itérative des chevauchements, MISE EN UN SEUL TENANT
          , comblement des petits trous vides. Aucune inflation
          géométrique.

Le même ``nettoyer_polygones`` est appliqué au repli « corridor-buffer »
(lorsque le réseau routier ne ferme aucun polygone) afin que, même en mode
dégradé, les secteurs produits restent sans chevauchement ni îlot.

Backend géométrique : ``core.geom_backend`` (PyQGIS dans QGIS, Shapely hors
QGIS pour les tests) — le code exécuté dans QGIS est exactement celui testé.
"""

from __future__ import annotations

from collections import defaultdict

from . import geom_backend as gb

# ---------------------------------------------------------------------------
# Index spatiaux légers (grille régulière) — indépendants du backend
# ---------------------------------------------------------------------------


class GridIndex:
    """Index grille sur les emprises (bounds) d'objets Geom."""

    def __init__(self, geoms, cell=500.0):
        self.cell = float(cell)
        self.bounds = {}
        self.cells = defaultdict(list)
        for key, g in geoms.items():
            b = g.bounds
            self.bounds[key] = b
            self._insert(key, b)

    def _insert(self, key, b):
        x0, y0, x1, y1 = b
        c = self.cell
        ix0, ix1 = int(x0 // c), int(x1 // c)
        iy0, iy1 = int(y0 // c), int(y1 // c)
        for ix in range(ix0, ix1 + 1):
            for iy in range(iy0, iy1 + 1):
                self.cells[(ix, iy)].append(key)

    def query_bounds(self, bounds, pad=0.0):
        x0, y0, x1, y1 = bounds
        x0 -= pad
        y0 -= pad
        x1 += pad
        y1 += pad
        c = self.cell
        out = set()
        for ix in range(int(x0 // c), int(x1 // c) + 1):
            for iy in range(int(y0 // c), int(y1 // c) + 1):
                out.update(self.cells[(ix, iy)])
        return [k for k in out if _bounds_intersect(self.bounds[k], bounds, pad)]


class PointIndex:
    """Index grille sur des points {id: (x, y)}."""

    def __init__(self, points, cell=500.0):
        self.cell = float(cell)
        self.points = dict(points)
        self.cells = defaultdict(list)
        for key, (x, y) in self.points.items():
            self.cells[(int(x // self.cell), int(y // self.cell))].append(key)

    def query_bounds(self, bounds, pad=0.0):
        x0, y0, x1, y1 = bounds
        x0 -= pad
        y0 -= pad
        x1 += pad
        y1 += pad
        c = self.cell
        out = []
        for ix in range(int(x0 // c), int(x1 // c) + 1):
            for iy in range(int(y0 // c), int(y1 // c) + 1):
                for k in self.cells[(ix, iy)]:
                    x, y = self.points[k]
                    if x0 <= x <= x1 and y0 <= y <= y1:
                        out.append(k)
        return out

    def nearest(self, x, y):
        return self.nearest_matching(x, y, lambda _k: True)[0]

    def nearest_matching(self, x, y, predicate):
        """Nœud le plus proche de (x, y) parmi ceux qui verifient
        ``predicate(id_noeud)`` (typiquement : appartenir a tel secteur).

        Retourne ``(id_noeud, distance_carree)`` ou ``(None, None)`` si aucun
        nœud ne verifie le predicat. Sert de brique commune a toute
        resolution "distance reseau" (le plus proche nœud DEJA affecte a un
        secteur, jamais un decompte d'abonnes) -- cf. FIX-CHEVAUCHEMENT-DISTANCE
        dans nettoyer_polygones() et polygones_topologiques.
        """
        c = self.cell
        cx, cy = int(x // c), int(y // c)
        for ring in range(0, 64):
            best, bd = None, None
            for ix in range(cx - ring, cx + ring + 1):
                for iy in range(cy - ring, cy + ring + 1):
                    if ring and not (ix in (cx - ring, cx + ring)
                                     or iy in (cy - ring, cy + ring)):
                        continue
                    for k in self.cells[(ix, iy)]:
                        if not predicate(k):
                            continue
                        px, py = self.points[k]
                        d = (px - x) ** 2 + (py - y) ** 2
                        if bd is None or d < bd:
                            bd, best = d, k
            if best is not None:
                return best, bd
        return None, None


def _bounds_intersect(b, other, pad=0.0):
    ax0, ay0, ax1, ay1 = b
    bx0, by0, bx1, by1 = other
    return not (ax1 < bx0 - pad or bx1 < ax0 - pad
                or ay1 < by0 - pad or by1 < ay0 - pad)


# ---------------------------------------------------------------------------
# Lectures de configuration (clés plates, valeurs par défaut sûres)
# ---------------------------------------------------------------------------

def _cfg(cfg):
    return {
        'activer': bool(cfg.get('ACTIVER_PARTITION_ILOTS', True)),
        'seuil_ilot': float(cfg.get('SEUIL_AIRE_ILOT_M2', 2000.0)),
        'largeur_max_voie': float(cfg.get('LARGEUR_MAX_VOIE_M', 30.0)),
        'aire_max_rattachement': float(cfg.get('AIRE_MAX_RATTACHEMENT_M2', 300000.0)),
        'max_passes': int(cfg.get('MAX_PASSES_NETTOYAGE', 4)),
        'snap': float(cfg.get('SNAP_LIMITE_M', 0.5)),
        # ---- contiguïté et emprise réellement desservie ----
        'contiguite': bool(cfg.get('ASSURER_CONTIGUITE_TERRITOIRES', True)),
        'aire_exclave_toleree': float(cfg.get('AIRE_EXCLAVE_TOLEREE_M2', 0.0)),
        'max_passes_contiguite': int(cfg.get('MAX_PASSES_CONTIGUITE', 60)),
        'dist_max_ilot_sans_noeud': float(
            cfg.get('DIST_MAX_ILOT_SANS_NOEUD_M', 300.0)),
    }


# ---------------------------------------------------------------------------
# Outils emprise réellement desservie + représentation de tout secteur
# ---------------------------------------------------------------------------

def _secteur_du_noeud_le_plus_proche(ilot, idx_noeuds, pts, secteur_de_noeud,
                                     dist_max):
    """Secteur du nœud sectorisé le plus proche de l'îlot, et sa distance.

    Retourne ``(None, None)`` si aucun nœud sectorisé ne se trouve à moins de
    ``dist_max`` mètres de l'îlot : l'îlot est alors HORS du tissu desservi
    (désert, terres agricoles, carrière enclavée entre deux routes) et ne doit
    entrer dans AUCUN territoire de secteur. ``dist_max <= 0`` désactive la
    borne (comportement des builds precedents, conservé pour compatibilité).
    """
    if dist_max is None or dist_max <= 0:
        nid = idx_noeuds.nearest(*ilot.centroid_xy())
        s = secteur_de_noeud.get(nid) if nid is not None else None
        return (s, None) if s is not None else (None, None)
    meilleur, meilleure_d = None, None
    for nid in idx_noeuds.query_bounds(ilot.bounds, pad=dist_max):
        s = secteur_de_noeud.get(nid)
        if s is None:
            continue
        x, y = pts[nid]
        d = ilot.distance_xy(x, y)
        if d > dist_max:
            continue
        if meilleure_d is None or d < meilleure_d:
            meilleure_d, meilleur = d, s
    if meilleur is None:
        return None, None
    return meilleur, meilleure_d


def _garantir_un_ilot_par_secteur(ilots, secteur_par_ilot, votes_par_ilot,
                                  secteur_de_noeud, stats, log):
    """tout secteur qui possède des nœuds possède au moins un îlot.

    Le vote majoritaire pondéré peut faire disparaître un petit secteur dont
    tous les îlots sont partagés avec un voisin plus peuplé. Le secteur
    absent récupère alors l'îlot où SON propre poids est le plus fort, retiré
    à ce voisin (qui garde forcément au moins un autre îlot). C'est la seule
    règle qui garantisse « autant de polygones que de secteurs » sans
    fabriquer de géométrie artificielle (ni tampon, ni enveloppe convexe).
    """
    attendus = sorted({s for s in secteur_de_noeud.values() if s is not None},
                      key=str)
    presents = set(secteur_par_ilot.values())
    manquants = [s for s in attendus if s not in presents]
    if not manquants:
        return
    compte = defaultdict(int)
    for s in secteur_par_ilot.values():
        compte[s] += 1
    for s in manquants:
        meilleur_i, meilleure_cle = None, None
        for i, votes in votes_par_ilot.items():
            poids = votes.get(s, 0.0)
            if poids <= 0:
                continue
            proprio = secteur_par_ilot.get(i)
            if proprio is None or compte[proprio] <= 1:
                continue
            cle = (poids, -ilots[i].area)
            if meilleure_cle is None or cle > meilleure_cle:
                meilleure_cle, meilleur_i = cle, i
        if meilleur_i is None:
            log(f"  [ALERTE] Secteur {s} : aucun îlot urbain ne peut lui être "
                f"attribué sans vider un voisin (ses nœuds sont hors de tout "
                f"îlot fermé par les routes). Secteur absent de la couche de "
                f"territoires -- vérifier la couche routes à cet endroit.")
            continue
        proprio = secteur_par_ilot[meilleur_i]
        secteur_par_ilot[meilleur_i] = s
        compte[proprio] -= 1
        compte[s] += 1
        stats['secteurs_restitues'] += 1
        log(f"  Secteur {s} sans îlot majoritaire (îlots remportés par des "
            f"voisins plus peuplés) : l'îlot où il pèse le plus "
            f"({ilots[meilleur_i].area:,.0f} m2) lui est restitué, retiré à "
            f"{proprio} -- partition toujours stricte.")


# ---------------------------------------------------------------------------
# Construction des territoires
# ---------------------------------------------------------------------------

def construire_territoires(routes_lignes, emprise, noeud_points,
                           secteur_de_noeud, poids_noeud, cfg, log=print):
    """Construit la partition stricte des territoires de secteur.

    Paramètres
    ----------
    routes_lignes   : géométries linéaires (QgsGeometry ou shapely) de la
                      couche routes ;
    emprise         : géométrie polygonale d'emprise de travail ;
    noeud_points    : dict nid -> (x, y) des nœuds du réseau ;
    secteur_de_noeud: dict nid -> secteur_id (résultat du moteur hydraulique) ;
    poids_noeud     : dict nid -> poids (nb d'abonnés agrégés) ;
    cfg             : configuration plate du plugin ;
    log             : fonction de journalisation.

    Retourne (territoires, stats) où territoires = dict secteur_id -> Geom,
    ou (None, stats) si la partition est impossible (repli appelant).
    """
    c = _cfg(cfg)
    stats = {
        'mode': 'aucun', 'nb_ilots': 0, 'ilots_par_vote': 0,
        'ilots_par_plus_proche': 0, 'poches_rattachees': 0,
        'poches_exclues': 0, 'aire_exclue_m2': 0.0,
        # tissu desservi (emprise reelle)
        'ilots_hors_tissu': 0, 'aire_hors_tissu_m2': 0.0,
        'secteurs_restitues': 0,
        # FIX-ILOTS-INTERIEURS-SANS-NOEUD
        'ilots_par_voisinage': 0,
    }
    if not c['activer']:
        log("  Partition par îlots désactivée (ACTIVER_PARTITION_ILOTS=false).")
        return None, stats

    emprise = gb.Geom.wrap(emprise)
    if emprise is None or emprise.is_empty:
        return None, stats

    lignes = []
    for g in routes_lignes:
        gg = gb.Geom.wrap(g)
        if gg is None or gg.is_empty:
            continue
        clip = gg.intersection(emprise)
        if clip is not None and not clip.is_empty:
            lignes.append(clip)
    if not lignes:
        log("  Partition : aucune route dans l'emprise -> repli.")
        return None, stats

    ilots = [b.make_valid() for b in gb.polygonize(lignes)]
    ilots = [b for b in ilots if b.area > 1.0]
    if not ilots:
        log("  Partition : polygonisation des routes sans résultat "
            "(réseau non bouclé) -> repli.")
        return None, stats
    stats['nb_ilots'] = len(ilots)

    idx_ilots = GridIndex({i: b for i, b in enumerate(ilots)}, cell=500.0)
    pts = {nid: (xy[0], xy[1]) for nid, xy in noeud_points.items()}
    idx_noeuds = PointIndex(pts, cell=500.0)

    def _poids(nid):
        try:
            return max(1.0, float(poids_noeud.get(nid, 1) or 1))
        except (TypeError, ValueError):
            return 1.0

    morceaux_par_secteur = defaultdict(list)
    secteur_par_ilot = {}
    votes_par_ilot = {}
    sans_noeud = []
    # Les nœuds réseau (carrefours) sont PAR CONSTRUCTION sur les limites
    # d'îlots : un nœud vote donc pour tous les îlots qu'il touche (à la
    # tolérance snap près). En cas d'égalité de poids, la continuité avec les
    # îlots VOISINS déjà affectés départage (frontière lisse, pas de zigzag),
    # puis l'ordre lexicographique des identifiants (déterminisme).
    tol = c['snap']
    ordre = sorted(range(len(ilots)),
                   key=lambda i: (round(ilots[i].centroid_xy()[0], 1),
                                  round(ilots[i].centroid_xy()[1], 1)))
    for i in ordre:
        ilot = ilots[i]
        votes = defaultdict(float)
        for nid in idx_noeuds.query_bounds(ilot.bounds, pad=tol):
            s = secteur_de_noeud.get(nid)
            if s is None:
                continue
            x, y = pts[nid]
            if ilot.contains_xy(x, y) or ilot.distance_xy(x, y) <= tol:
                votes[s] += _poids(nid)
        if votes:
            meilleur = max(votes.values())
            ex_aequo = sorted(s for s, w in votes.items() if w == meilleur)
            if len(ex_aequo) == 1:
                s = ex_aequo[0]
            else:
                voisins = defaultdict(float)
                for j in idx_ilots.query_bounds(ilot.bounds, pad=tol):
                    if j == i or j not in secteur_par_ilot:
                        continue
                    sl = ilot.shared_length(ilots[j])
                    if sl > 0.01:
                        voisins[secteur_par_ilot[j]] += sl
                s = min(ex_aequo,
                        key=lambda ss: (-voisins.get(ss, 0.0), ss))
            stats['ilots_par_vote'] += 1
            votes_par_ilot[i] = dict(votes)
            secteur_par_ilot[i] = s
        else:
            # FIX-ILOTS-INTERIEURS-SANS-NOEUD : un îlot SANS aucun
            # nœud du réseau (aucune jonction AEP modélisée à l'intérieur --
            # cas frequent d'un campus, d'un stade ou d'un grand ensemble
            # alimenté par une seule antenne perimetrale non representee
            # comme jonction) n'est PLUS exclu par principe. Il est traite
            # plus bas, une fois tous les votes connus : priorite a
            # l'ADJACENCE avec les ilots deja affectes (comme les "poches"
            # residuelles, cf. plus bas), la distance au nœud le plus proche
            # ne tranchant que pour les ilots qui touchent le bord EXTERIEUR
            # de l'emprise (vraie frange non desservie -- desert, terres
            # agricoles).
            sans_noeud.append(i)

    # ---- complement au build actuel : ilots sans aucun nœud -- adjacence d'abord, distance ----
    # uniquement en frange exterieure de l'emprise -------------------------
    if sans_noeud:
        restants = list(sans_noeud)
        for _passe in range(20):
            encore, progres = [], False
            for i in restants:
                ilot = ilots[i]
                if ilot.touches_bounds_of(emprise):
                    # frange reelle de l'emprise desservie : ne jamais
                    # deduire l'appartenance de la seule adjacence (c'est
                    # exactement le cas "desert/terres agricoles enclaves"
                    # que DIST_MAX_ILOT_SANS_NOEUD_M doit continuer a
                    # ecarter) -- tranche par distance plus bas.
                    encore.append(i)
                    continue
                voisins = defaultdict(float)
                for j in idx_ilots.query_bounds(ilot.bounds, pad=tol):
                    if j == i or j not in secteur_par_ilot:
                        continue
                    sl = ilot.shared_length(ilots[j])
                    if sl > 0.01:
                        voisins[secteur_par_ilot[j]] += sl
                if voisins:
                    s = max(voisins.items(), key=lambda kv: (kv[1], kv[0]))[0]
                    secteur_par_ilot[i] = s
                    stats['ilots_par_voisinage'] += 1
                    progres = True
                else:
                    encore.append(i)
            restants = encore
            if not progres:
                break
        for i in restants:
            s, dist = _secteur_du_noeud_le_plus_proche(
                ilots[i], idx_noeuds, pts, secteur_de_noeud,
                c['dist_max_ilot_sans_noeud'])
            if s is None:
                stats['ilots_hors_tissu'] += 1
                stats['aire_hors_tissu_m2'] += ilots[i].area
                continue
            secteur_par_ilot[i] = s
            stats['ilots_par_plus_proche'] += 1

    # ---- aucun secteur de la partition hydraulique ne disparait ----
    _garantir_un_ilot_par_secteur(ilots, secteur_par_ilot, votes_par_ilot,
                                  secteur_de_noeud, stats, log)

    for i, s in secteur_par_ilot.items():
        morceaux_par_secteur[s].append(ilots[i])

    # ---- résidus : poches non fermées par le réseau routier ----
    union_ilots = gb.unary_union(ilots)
    reste = emprise.difference(union_ilots)
    if reste is not None:
        for poche in reste.parts():
            if poche.area < 0.5:
                continue
            candidats = defaultdict(float)
            for i in idx_ilots.query_bounds(poche.bounds, pad=c['snap']):
                if i not in secteur_par_ilot:
                    continue
                if poche.intersects(ilots[i]):
                    sl = poche.shared_length(ilots[i])
                    if sl > 0.01:
                        candidats[secteur_par_ilot[i]] += sl
            externe = poche.touches_bounds_of(emprise)
            fine = (poche.buffer(-c['largeur_max_voie'] / 2.0) is None)
            if candidats and not externe and poche.area <= c['aire_max_rattachement']:
                s = max(candidats.items(), key=lambda kv: kv[1])[0]
                morceaux_par_secteur[s].append(poche)
                stats['poches_rattachees'] += 1
            elif candidats and fine:
                s = max(candidats.items(), key=lambda kv: kv[1])[0]
                morceaux_par_secteur[s].append(poche)
                stats['poches_rattachees'] += 1
            else:
                stats['poches_exclues'] += 1
                stats['aire_exclue_m2'] += poche.area

    territoires = {}
    for s, geoms in morceaux_par_secteur.items():
        u = gb.unary_union(geoms)
        if u is not None and not u.is_empty:
            territoires[s] = u

    stats['mode'] = 'ilots'
    log(f"  Partition {stats['nb_ilots']} îlots urbains "
        f"({stats['ilots_par_vote']} par vote abonnés, "
        f"{stats['ilots_par_voisinage']} par adjacence (sans nœud propre, "
        f"interieurs), {stats['ilots_par_plus_proche']} par plus proche "
        f"nœud (frange exterieure)), "
        f"{stats['poches_rattachees']} poche(s) rattachée(s), "
        f"{stats['poches_exclues']} zone(s) externe(s) exclue(s) "
        f"({stats['aire_exclue_m2']:,.0f} m2), "
        f"{stats['ilots_hors_tissu']} îlot(s) hors tissu desservi exclu(s) "
        f"({stats['aire_hors_tissu_m2']:,.0f} m2, aucun nœud à moins de "
        f"{c['dist_max_ilot_sans_noeud']:,.0f} m).")

    territoires, stats_net = nettoyer_polygones(
        territoires, noeud_points, secteur_de_noeud, poids_noeud, cfg, log)
    stats.update(stats_net)
    return territoires, stats


# ---------------------------------------------------------------------------
# Nettoyage topologique : îlots, exclaves, chevauchements, trous
# ---------------------------------------------------------------------------

def _poids_noeuds_dans(part, secteur, idx_noeuds, pts, secteur_de_noeud,
                       poids_noeud, tol=0.5):
    tot = 0.0
    for nid in idx_noeuds.query_bounds(part.bounds, pad=tol):
        if secteur_de_noeud.get(nid) != secteur:
            continue
        x, y = pts[nid]
        if part.contains_xy(x, y) or part.distance_xy(x, y) <= tol:
            try:
                tot += max(1.0, float(poids_noeud.get(nid, 1) or 1))
            except (TypeError, ValueError):
                tot += 1.0
    return tot


def nettoyer_polygones(polygones, noeud_points, secteur_de_noeud,
                       poids_noeud, cfg, log=print):
    """Post-traitement topologique garanti sans inflation géométrique.

    1. Réaffecte chaque partie < SEUIL_AIRE_ILOT_M2 (ou sans nœud du secteur)
       au secteur voisin partageant la plus longue frontière ;
    2. Résout itérativement TOUS les chevauchements restants -- FIX-
       CHEVAUCHEMENT-DISTANCE : la zone disputée va au secteur dont le nœud
       hydraulique déjà affecté est le plus proche (distance réseau/
       spatiale), jamais à celui qui compte le plus d'abonnés dans la zone
       (ce critère dépend de l'ordre de traitement des paires et produit des
       inversions en cascade -- secteurs emboîtés les uns dans les autres,
       "poupées russes"). Repli sur le poids d'abonnés uniquement si aucun
       nœud des deux secteurs n'est trouvable à proximité ;
    2bis. MET CHAQUE SECTEUR EN UN SEUL TENANT
       (``ASSURER_CONTIGUITE_TERRITOIRES``) : le morceau principal reste au
       secteur, tous les autres sont absorbés par le voisin avec lequel ils
       partagent la plus longue frontière. Fini le patchwork de rubans
       dispersés sur toute la ville ;
    3. Comble les petits trous vides (< seuil et non occupés par un autre
       secteur) ;
    4. Ne produit jamais d'enveloppe convexe.
    """
    c = _cfg(cfg)
    stats = {'ilots_reaffectes': 0, 'ilots_supprimes': 0,
             'chevauchements_resolus': 0, 'aire_chevauchements_m2': 0.0,
             'trous_combles': 0, 'exclaves_conservees': 0,
             'fragments_absorbes': 0, 'aire_fragments_absorbee_m2': 0.0,
             'poids_territoire_transfere': 0.0,
             'secteurs_remis_en_un_tenant': 0, 'exclaves_isolees': 0,
             'nb_parties_finales': 0}
    geoms = {}
    for s, g in polygones.items():
        gg = gb.Geom.wrap(g)
        if gg is None or gg.is_empty:
            continue
        gg = gg.make_valid()
        if gg is not None and not gg.is_empty:
            geoms[s] = gg
    if not geoms:
        return geoms, stats

    pts = {nid: (xy[0], xy[1]) for nid, xy in noeud_points.items()}
    idx_noeuds = PointIndex(pts, cell=500.0)
    seuil = c['seuil_ilot']

    def poids_dans(part, s):
        return _poids_noeuds_dans(part, s, idx_noeuds, pts, secteur_de_noeud,
                                  poids_noeud, tol=c['snap'])

    # ------------------------------------------------ 1) îlots / exclaves
    for _passe in range(c['max_passes']):
        deplacements = []   # (src, part, dst)
        suppressions = []   # (src, part)
        # recompté à chaque passe : seul l'état final compte (sinon la même
        # exclave était additionnée autant de fois qu'il y a eu de passes)
        exclaves_passe = 0
        index = GridIndex({s: g for s, g in geoms.items()}, cell=1000.0)
        for s, g in list(geoms.items()):
            parts = g.parts()
            if len(parts) <= 1:
                continue
            scores = []
            for q in parts:
                scores.append((poids_dans(q, s), q.area, q))
            scores.sort(key=lambda t: (t[0], t[1]), reverse=True)
            for w, a, q in scores[1:]:
                if c['contiguite']:
                    # plus aucune exclave n'est conservée par défaut --
                    # elles sont toutes ramenées à un seul tenant par l'étape
                    # 2bis. AIRE_EXCLAVE_TOLEREE_M2 > 0 permet de garder les
                    # grandes annexes réelles (choix explicite de l'exploitant).
                    garder = (c['aire_exclave_toleree'] > 0
                              and a >= c['aire_exclave_toleree'])
                else:
                    garder = (a >= seuil and w > 0)
                if garder:
                    exclaves_passe += 1
                    continue
                cands = {}
                for t in index.query_bounds(q.bounds, pad=c['snap']):
                    if t == s:
                        continue
                    if q.intersects(geoms[t]):
                        sl = q.shared_length(geoms[t])
                        if sl > 0.01:
                            cands[t] = sl
                if cands:
                    dst = max(cands.items(), key=lambda kv: kv[1])[0]
                    deplacements.append((s, q, dst))
                    stats['ilots_reaffectes'] += 1
                elif a < seuil:
                    suppressions.append((s, q))
                    stats['ilots_supprimes'] += 1
                else:
                    exclaves_passe += 1
        stats['exclaves_conservees'] = exclaves_passe
        if not deplacements and not suppressions:
            break
        for s, q, dst in deplacements:
            rest = geoms[s].difference(q)
            geoms[dst] = gb.unary_union([geoms[dst], q]) or geoms[dst]
            if rest is None or rest.is_empty:
                geoms.pop(s, None)
            else:
                geoms[s] = rest
        for s, q in suppressions:
            rest = geoms[s].difference(q)
            if rest is None or rest.is_empty:
                geoms.pop(s, None)
            else:
                geoms[s] = rest

    # ------------------------------------------------ 2) chevauchements
    for _passe in range(c['max_passes']):
        index = GridIndex({s: g for s, g in geoms.items()}, cell=1000.0)
        resolus = 0
        vus = set()
        for s in list(geoms.keys()):
            if s not in geoms:
                continue
            for t in index.query_bounds(geoms[s].bounds):
                paire = tuple(sorted((s, t)))
                if t == s or paire in vus:
                    continue
                vus.add(paire)
                if t not in geoms:
                    continue
                inter = geoms[s].intersection(geoms[t])
                if inter is None or inter.area <= 1.0:
                    continue
                # FIX-CHEVAUCHEMENT-DISTANCE : la zone en litige va au secteur
                # dont le NŒUD HYDRAULIQUE DEJA AFFECTE est le plus proche
                # (distance reseau/spatiale), jamais a celui qui compte le
                # plus d'abonnes dans la zone -- ce dernier critere depend de
                # l'ordre de traitement des paires et produit des inversions
                # en cascade (un secteur qui "gagne" localement contre un
                # voisin peut ensuite en "perdre" contre un troisieme,
                # emboitant des morceaux les uns dans les autres). Le plus
                # proche nœud est un fait geometrique stable, coherent avec la
                # regle deja utilisee pour les ilots sans aucun nœud
                # (_secteur_du_noeud_le_plus_proche) et avec l'affectation
                # d'origine du moteur hydraulique (plus court chemin reseau).
                cx, cy = inter.centroid_xy()
                nid_s, ds = idx_noeuds.nearest_matching(
                    cx, cy, lambda k: secteur_de_noeud.get(k) == s)
                nid_t, dt = idx_noeuds.nearest_matching(
                    cx, cy, lambda k: secteur_de_noeud.get(k) == t)
                if nid_s is None and nid_t is None:
                    # aucun nœud des deux secteurs a proximite (rare, ilots
                    # residuels sans nœud propre) : repli documente sur le
                    # poids d'abonnes, seul critere encore disponible ici.
                    ws = poids_dans(inter, s)
                    wt = poids_dans(inter, t)
                    perdant = t if ws >= wt else s
                elif nid_s is None:
                    perdant = s
                elif nid_t is None:
                    perdant = t
                else:
                    perdant = t if ds <= dt else s
                rest = geoms[perdant].difference(inter)
                if rest is None or rest.is_empty:
                    geoms.pop(perdant, None)
                else:
                    geoms[perdant] = rest
                resolus += 1
                stats['chevauchements_resolus'] += 1
                stats['aire_chevauchements_m2'] += inter.area
        if not resolus:
            break

    # --------------------------------- 2bis) un secteur = un seul tenant
    if c['contiguite']:
        _assurer_contiguite(geoms, poids_dans, c, stats, log)

    # ------------------------------------------------ 3) petits trous vides
    index = GridIndex({s: g for s, g in geoms.items()}, cell=1000.0)
    for s in list(geoms.keys()):
        def _garder(trou, _s=s):
            if trou.area >= seuil:
                return True
            for t in index.query_bounds(trou.bounds, pad=c['snap']):
                if t == _s:
                    continue
                inter = trou.intersection(geoms[t])
                if inter is not None and inter.area > 0.5 * trou.area:
                    return True   # enclave d'un autre secteur : legitime
            stats['trous_combles'] += 1
            return False
        geoms[s] = geoms[s].drop_inner_rings(keep_predicate=_garder)

    # ------------------------------------------------ 4) finalisation
    finals = {}
    for s, g in geoms.items():
        parts = [p for p in g.parts() if p.area > 1.0]
        if not parts:
            continue
        gg = gb.multi(parts)
        if gg is not None and not gg.is_empty:
            finals[s] = gg.make_valid()
    if stats['ilots_reaffectes'] or stats['ilots_supprimes'] \
            or stats['chevauchements_resolus'] or stats['trous_combles']:
        log(f"  Nettoyage topologique {stats['ilots_reaffectes']} îlot(s) "
            f"réaffecté(s), {stats['ilots_supprimes']} supprimé(s), "
            f"{stats['chevauchements_resolus']} chevauchement(s) résolu(s) "
            f"({stats['aire_chevauchements_m2']:,.0f} m2), "
            f"{stats['trous_combles']} trou(s) comblé(s), "
            f"{stats['exclaves_conservees']} exclave(s) réseau conservée(s).")
    stats['nb_parties_finales'] = sum(len(g.parts()) for g in finals.values())
    nb_multi = sum(1 for g in finals.values() if len(g.parts()) > 1)
    log(f"  CONTROLE CONTIGUITE {len(finals)} secteur(s), "
        f"{stats['nb_parties_finales']} partie(s) au total, "
        f"{nb_multi} secteur(s) encore en plusieurs morceaux "
        f"(objectif : 0 -- 1 secteur = 1 territoire d'un seul tenant).")
    return finals, stats


# ---------------------------------------------------------------------------
# mise en un seul tenant des territoires
# ---------------------------------------------------------------------------

def _assurer_contiguite(geoms, poids_dans, c, stats, log):
    """Ramène CHAQUE secteur à un territoire d'un seul tenant.

    Pour un secteur en plusieurs morceaux : le morceau PRINCIPAL (celui qui
    porte le plus d'abonnés, puis la plus grande surface) reste au secteur ;
    chaque autre morceau est absorbé par le secteur voisin avec lequel il
    partage la plus longue frontière -- exactement la règle déjà employée
    ailleurs dans le plugin pour réaffecter un îlot parasite ou fusionner un
    secteur sous-effectif.

    Propriétés garanties :
      * aucun secteur n'est vidé (son morceau principal ne bouge jamais), donc
        le nombre de polygones reste égal au nombre de secteurs ;
      * aucune surface n'est créée ni perdue : les morceaux changent de
        propriétaire, la partition reste stricte (0 chevauchement, 0 trou
        nouveau) ;
      * aucune enveloppe convexe, aucun tampon, aucune inflation ;
      * la boucle termine : chaque absorption réduit d'au moins une unité le
        nombre total de morceaux.

    Un morceau qui ne touche AUCUN autre secteur (annexe reliée au réseau par
    une conduite traversant une zone non bâtie, réservoir isolé en plein
    désert) ne peut être absorbé par personne : il est conservé et journalisé
    comme exclave physiquement isolée -- le supprimer effacerait du réseau
    réellement exploité, ce que ce module ne fait jamais en silence.
    """
    tol = c['snap']
    secteurs_traites = set()
    isolees = []
    for _passe in range(max(1, c['max_passes_contiguite'])):
        index = GridIndex({s: g for s, g in geoms.items()}, cell=1000.0)
        mouvements = 0
        for s in sorted(geoms.keys(), key=str):
            g = geoms.get(s)
            if g is None or g.is_empty:
                continue
            parts = g.parts()
            if len(parts) <= 1:
                continue
            classes = sorted(
                ((poids_dans(q, s), q.area, k) for k, q in enumerate(parts)),
                key=lambda t: (t[0], t[1]), reverse=True)
            for w, a, k in classes[1:]:
                q = parts[k]
                if c['aire_exclave_toleree'] > 0 \
                        and a >= c['aire_exclave_toleree']:
                    continue   # annexe volontairement tolérée (config)
                candidats = {}
                for t in index.query_bounds(q.bounds, pad=tol):
                    if t == s or t not in geoms:
                        continue
                    sl = q.shared_length(geoms[t])
                    if sl > 0.01:
                        candidats[t] = sl
                if not candidats:
                    isolees.append((s, a, w))
                    continue
                dst = max(candidats.items(), key=lambda kv: (kv[1], str(kv[0])))[0]
                fusion = gb.unary_union([geoms[dst], q])
                if fusion is None or fusion.is_empty:
                    continue
                reste = geoms[s].difference(q)
                if reste is None or reste.is_empty:
                    continue   # garde-fou : ne jamais vider un secteur
                geoms[s] = reste
                geoms[dst] = fusion.make_valid()
                mouvements += 1
                secteurs_traites.add(s)
                stats['fragments_absorbes'] += 1
                stats['aire_fragments_absorbee_m2'] += a
                stats['poids_territoire_transfere'] += w
        if not mouvements:
            break

    stats['secteurs_remis_en_un_tenant'] = len(secteurs_traites)
    stats['exclaves_isolees'] = len(isolees)
    if stats['fragments_absorbes']:
        log(f"  Contiguïté territoriale {stats['fragments_absorbes']} "
            f"morceau(x) éparpillé(s) absorbé(s) par le secteur voisin le plus "
            f"frontalier ({stats['aire_fragments_absorbee_m2']:,.0f} m2, "
            f"~{stats['poids_territoire_transfere']:,.0f} abonné(s)/nœud(s) "
            f"changeant de TERRITOIRE cartographique -- aucune appartenance "
            f"hydraulique modifiée), {stats['secteurs_remis_en_un_tenant']} "
            f"secteur(s) ramené(s) à une zone d'un seul tenant.")
    if isolees:
        detail = ", ".join(f"{s} ({a:,.0f} m2)"
                           for s, a, _w in sorted(isolees, key=lambda t: -t[1])[:8])
        log(f"  [INFO] {len(isolees)} exclave(s) physiquement isolée(s) "
            f"conservée(s) (aucun secteur voisin en contact : annexe alimentée "
            f"par une conduite traversant une zone non bâtie, ou réservoir "
            f"isolé) : {detail}. Les supprimer effacerait du réseau réellement "
            f"exploité -- à traiter en conception (conduite de raccordement) et "
            f"non en cartographie.")


# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------

def statistiques_partition(territoires):
    """Contrôle qualité : chevauchements restants, trous, fragmentation."""
    geoms = {s: gb.Geom.wrap(g) for s, g in territoires.items()}
    index = GridIndex(geoms, cell=1000.0)
    nb_chev, aire_chev = 0, 0.0
    vus = set()
    for s in geoms:
        for t in index.query_bounds(geoms[s].bounds):
            paire = tuple(sorted((s, t)))
            if t == s or paire in vus:
                continue
            vus.add(paire)
            inter = geoms[s].intersection(geoms[t])
            if inter is not None and inter.area > 1.0:
                nb_chev += 1
                aire_chev += inter.area
    nb_parts = sum(len(g.parts()) for g in geoms.values())
    return {'nb_secteurs': len(geoms), 'nb_chevauchements': nb_chev,
            'aire_chevauchements_m2': aire_chev, 'nb_parties': nb_parts}
