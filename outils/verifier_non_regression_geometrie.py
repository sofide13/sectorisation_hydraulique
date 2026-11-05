# -*- coding: utf-8 -*-
"""
verifier_non_regression_geometrie.py
=====================================
Contrôle de non-régression géométrique à lancer dans la CONSOLE PYTHON DE
QGIS, une fois que ``secteurs_hydrauliques`` (pipeline.py) ET
``secteurs_hydrauliques_topo`` (executer_topologique.py) existent dans le
projet.

Ce script attrape exactement les deux défauts remontés par l'utilisateur :

  1. Sigma(aire) doit être identique entre les deux couches, à une tolérance
     topologique près (défaut 1%) -- sinon, un second moteur de partition
     indépendant a été relancé pour produire secteurs_hydrauliques_topo (cf.
     FIX-DOUBLE-MOTEUR-ILOTS, core/polygones_topologiques.
     _reutiliser_polygones_reference).
  2. AUCUN polygone de secteur ne doit être contenu à ~100% dans un autre
     ("poupée russe") -- test all-pairs, dans CHAQUE couche séparément puis
     entre les deux couches.

Ce contrôle aurait détecté le problème avant tout export de couche.

USAGE (console Python de QGIS) :

    import sys
    sys.path.insert(0, r"<chemin vers ce plugin>")
    from outils import verifier_non_regression_geometrie as vng
    vng.executer()

Peut aussi être appelé automatiquement en fin de
core/executer_topologique.py (voir son point d'entrée) en passant
``CONFIG['VERIFIER_NON_REGRESSION_GEOMETRIE'] = True``.
"""

from qgis.core import QgsProject

NOM_REFERENCE = "secteurs_hydrauliques"
NOM_TOPO = "secteurs_hydrauliques_topo"
TOLERANCE_AIRE = 0.01          # 1%
SEUIL_EMBOITEMENT = 0.99       # 99% de l'aire d'un secteur recouverte


def _lire_polygones(nom_couche, log, couche_directe=None):
    """Lit les polygones de `nom_couche`. Si `couche_directe` est fourni
    (objet QgsVectorLayer OU CoucheDifferee -- cf. core/couche_differee.py
    -- deja construit en memoire), on l'utilise directement au lieu
    d'interroger QgsProject -- indispensable pour la couche
    'secteurs_hydrauliques_topo' quand ce controle est appele pendant le
    run de core/executer_topologique.py : depuis le build actuel aucune vraie
    couche QGIS n'existe encore a ce stade (cf. FIX-THREAD-SAFETY,
    core/couche_differee.py), donc une recherche par nom dans QgsProject
    ne la trouverait jamais."""
    if couche_directe is not None:
        couche = couche_directe
    else:
        couches = QgsProject.instance().mapLayersByName(nom_couche)
        if not couches:
            log(f"[ALERTE] Couche '{nom_couche}' introuvable : contrôle ignoré.")
            return None
        couche = couches[0]
    champs = [f.name() for f in couche.fields()]
    if 'secteur_id' not in champs:
        log(f"[ALERTE] Couche '{nom_couche}' sans champ 'secteur_id' : "
            f"contrôle impossible.")
        return None
    polys = {}
    for feat in couche.getFeatures():
        s = feat['secteur_id']
        if s in (None, ''):
            continue
        geom = feat.geometry()
        if geom is None or geom.isEmpty():
            continue
        polys[str(s)] = geom
    return polys


def _verifier_aires(aires_ref, aires_topo, log):
    total_ref = sum(aires_ref.values())
    total_topo = sum(aires_topo.values())
    if total_ref <= 0:
        log("[ALERTE] Aire de référence nulle : contrôle ignoré.")
        return True
    ecart = abs(total_topo - total_ref) / total_ref
    ok = ecart <= TOLERANCE_AIRE
    statut = "CONFORME" if ok else "ECHEC"
    log(f"[{statut}] Sigma(aire) : reference={total_ref:,.0f} m2, "
        f"topo={total_topo:,.0f} m2, ecart={ecart:.2%} "
        f"(tolerance={TOLERANCE_AIRE:.2%}).")
    if not ok:
        # detail secteur par secteur pour localiser la derive
        tous = sorted(set(aires_ref) | set(aires_topo))
        for s in tous:
            a_ref = aires_ref.get(s)
            a_topo = aires_topo.get(s)
            if a_ref is None:
                log(f"    {s} : absent de la reference, present en topo "
                    f"({a_topo:,.0f} m2).")
            elif a_topo is None:
                log(f"    {s} : present en reference ({a_ref:,.0f} m2), "
                    f"absent du topo.")
            else:
                d = abs(a_topo - a_ref) / a_ref if a_ref > 0 else 0.0
                if d > TOLERANCE_AIRE:
                    log(f"    {s} : reference={a_ref:,.0f} m2, "
                        f"topo={a_topo:,.0f} m2, ecart={d:.2%}.")
    return ok


def _verifier_emboitements(polys, nom_couche, log):
    fautifs = []
    ids = sorted(polys.keys())
    for a in ids:
        ga = polys[a]
        aire_a = ga.area()
        if aire_a <= 0:
            continue
        for b in ids:
            if a == b:
                continue
            gb_ = polys[b]
            if gb_.area() <= 0 or not ga.intersects(gb_):
                continue
            inter = ga.intersection(gb_)
            if inter is None or inter.isEmpty():
                continue
            part = inter.area() / aire_a
            if part >= SEUIL_EMBOITEMENT:
                fautifs.append((a, b, part))
    if fautifs:
        log(f"[ECHEC] {len(fautifs)} secteur(s) emboite(s) ('poupee russe') "
            f"dans '{nom_couche}' :")
        for a, b, p in fautifs:
            log(f"    {a} est contenu a {p:.1%} dans {b}")
    else:
        log(f"[CONFORME] Aucun secteur emboite dans '{nom_couche}' "
            f"({len(ids)} secteur(s) testes, {len(ids) * (len(ids) - 1)} "
            f"paire(s)).")
    return not fautifs


def executer(log=print, couche_topo=None):
    """couche_topo : objet QgsVectorLayer 'secteurs_hydrauliques_topo' deja
    construit en memoire, a utiliser directement si fourni (cf.
    _lire_polygones). Sert a l'appel automatique depuis
    core/executer_topologique.py, ou cette couche n'est pas encore dans
    le projet QGIS au moment de ce controle ."""
    log("=" * 70)
    log("CONTROLE DE NON-REGRESSION GEOMETRIQUE "
        f"('{NOM_REFERENCE}' vs '{NOM_TOPO}')")
    log("=" * 70)

    polys_ref = _lire_polygones(NOM_REFERENCE, log)
    polys_topo = _lire_polygones(NOM_TOPO, log, couche_directe=couche_topo)
    if polys_ref is None or polys_topo is None:
        log("Controle incomplet (couche(s) manquante(s)).")
        return False

    aires_ref = {s: g.area() for s, g in polys_ref.items()}
    aires_topo = {s: g.area() for s, g in polys_topo.items()}

    ok_aire = _verifier_aires(aires_ref, aires_topo, log)
    ok_emboitement_ref = _verifier_emboitements(polys_ref, NOM_REFERENCE, log)
    ok_emboitement_topo = _verifier_emboitements(polys_topo, NOM_TOPO, log)

    ok = ok_aire and ok_emboitement_ref and ok_emboitement_topo
    log("-" * 70)
    log("RESULTAT GLOBAL : " + ("CONFORME" if ok else "ECHEC -- voir details ci-dessus."))
    log("=" * 70)
    return ok


if __name__ == "__main__":
    executer()
