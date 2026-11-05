# -*- coding: utf-8 -*-
"""
extraire_osm_obstacles.py
==========================
Script autonome (a executer dans la console Python de QGIS, PAS un plugin)
pour extraire automatiquement depuis OpenStreetMap (via l'API Overpass) les
couches "obstacles" utilisees par le decoupage par routes de la sectorisation
hydraulique :

    - batiment    (polygones "building=*")
    - stade       (polygones "leisure=stadium")
    - hopital     (polygones "amenity=hospital")
    - chemin_fer  (lignes "railway=rail")

Ces 4 couches sont ensuite chargees dans le projet QGIS courant avec les noms
par defaut attendus par config_defaut.json (COUCHES.BATIMENT_LAYER, etc.).
Si vous avez deja renomme ces cles dans votre configuration, adaptez les noms
de sortie tout en bas du script (variable NOMS_SORTIE).

PREREQUIS :
  - Une connexion internet (l'API Overpass est un service public, gratuit,
    mais peut etre lent ou momentanement indisponible sous forte charge).
  - Une couche presente dans le projet pour definir l'emprise a interroger
    (par defaut : la couche des conduites AEP, nommee "AEP" ou "routes" en
    repli -- modifiable ci-dessous).

UTILISATION :
  1. Ouvrez votre projet QGIS (avec la couche AEP/routes deja chargee).
  2. Menu "Extensions" -> "Console Python".
  3. Dans la console : exec(open(r"CHEMIN\\COMPLET\\vers\\extraire_osm_obstacles.py", encoding="utf-8").read())
  4. Patientez (peut prendre de 10 secondes a quelques minutes selon la
     taille de la zone et la charge du serveur Overpass).

ATTENTION PERFORMANCE / FIABILITE :
  - Sur une grande ville (comme votre projet : ~42 000 abonnes), le nombre
    de batiments OSM peut se compter en dizaines de milliers. C'est normal
    et gere par ce script (l'ecriture se fait en un seul lot), mais le
    telechargement peut prendre du temps.
  - Si la zone n'est pas bien cartographiee sur OSM (frequent hors grandes
    villes), la couche "batiment" peut etre incomplete ou vide -- ce n'est
    pas une erreur du script, juste un manque de donnees source. Le
    pipeline gere deja cette absence normalement (couche optionnelle).
  - Si Overpass renvoie une erreur 429 (trop de requetes) ou 504 (timeout),
    relancez apres quelques minutes, ou reduisez l'emprise (EXTENT_BUFFER_M).
"""

import json
import urllib.request
import urllib.parse
import urllib.error

from qgis.core import (
    QgsProject, QgsVectorLayer, QgsFeature, QgsGeometry, QgsPointXY,
    QgsField, QgsFields, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
)
from qgis.PyQt.QtCore import QVariant

# ============================================================================
# PARAMETRES A ADAPTER SI BESOIN
# ============================================================================

# Nom de la couche du projet a utiliser pour definir l'emprise a interroger.
# Le script essaie ces noms dans l'ordre et prend le premier trouve.
COUCHES_EMPRISE_CANDIDATES = ["AEP", "routes", "route", "commune"]

# Marge (m) ajoutee autour de l'emprise de la couche ci-dessus, dans son CRS
# d'origine (habituellement un CRS projete en metres, ex. UTM).
EXTENT_BUFFER_M = 300.0

# Serveur Overpass. Le serveur principal peut etre sature ; decommentez un
# miroir si besoin.
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
# OVERPASS_URL = "https://overpass.kumi.systems/api/interpreter"

# Noms des couches de sortie dans le projet QGIS (doivent correspondre a
# COUCHES.BATIMENT_LAYER / STADE_LAYER / HOPITAL_LAYER / CHEMIN_FER_LAYER
# dans config_defaut.json -- ou a ce que vous y avez mis).
NOMS_SORTIE = {
    "batiment": "batiment",
    "stade": "stade",
    "hopital": "hopital",
    "chemin_fer": "chemin_fer",
}

TIMEOUT_S = 180

# ============================================================================
# 1) DETERMINATION DE L'EMPRISE (bbox WGS84 pour Overpass)
# ============================================================================

def _trouver_couche_emprise():
    proj = QgsProject.instance()
    for nom in COUCHES_EMPRISE_CANDIDATES:
        couches = proj.mapLayersByName(nom)
        if couches:
            return couches[0]
    raise RuntimeError(
        "Aucune couche d'emprise trouvee parmi " + str(COUCHES_EMPRISE_CANDIDATES) +
        ". Chargez d'abord votre couche de conduites AEP (ou routes), ou "
        "ajoutez son nom dans COUCHES_EMPRISE_CANDIDATES en haut du script."
    )


def _bbox_wgs84():
    couche = _trouver_couche_emprise()
    print(f"  Emprise calculee a partir de la couche : {couche.name()}")
    rect = couche.extent()
    rect.grow(EXTENT_BUFFER_M)
    crs_source = couche.crs()
    crs_wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
    transform = QgsCoordinateTransform(crs_source, crs_wgs84, QgsProject.instance())
    rect_wgs84 = transform.transformBoundingBox(rect)
    # Overpass attend : (south, west, north, east)
    return (rect_wgs84.yMinimum(), rect_wgs84.xMinimum(),
            rect_wgs84.yMaximum(), rect_wgs84.xMaximum()), crs_source


# ============================================================================
# 2) REQUETE OVERPASS (format "out geom" : geometrie directement incluse,
#    pas besoin de recomposer les polygones a partir des noeuds)
# ============================================================================

def _requete_overpass(bbox):
    s, w, n, e = bbox
    bbox_str = f"{s},{w},{n},{e}"
    query = f"""
[out:json][timeout:{TIMEOUT_S}];
(
  way["building"]({bbox_str});
  way["amenity"="hospital"]({bbox_str});
  way["leisure"="stadium"]({bbox_str});
  way["railway"="rail"]({bbox_str});
);
out geom;
""".strip()

    print("  Envoi de la requete a Overpass API (peut prendre du temps)...")
    body = ("data=" + urllib.parse.quote(query)).encode("utf-8")
    req = urllib.request.Request(OVERPASS_URL, data=body, method="POST")
    try:
        if urllib.parse.urlparse(OVERPASS_URL).scheme.lower() != "https":
            raise ValueError("OVERPASS_URL doit utiliser https")
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:  # nosec B310 - https verifie
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"Overpass a repondu une erreur HTTP {e.code}. "
            f"Si c'est un 429/504, reessayez dans quelques minutes ou "
            f"reduisez l'emprise (EXTENT_BUFFER_M) / la zone de la couche."
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"Impossible de contacter Overpass ({e}). Verifiez votre "
            f"connexion internet / pare-feu."
        ) from e

    return json.loads(raw.decode("utf-8"))


# ============================================================================
# 3) CONSTRUCTION DES COUCHES QGIS A PARTIR DE LA REPONSE OVERPASS
# ============================================================================

def _classer_element(tags):
    """Determine dans quelle couche de sortie ranger un element OSM."""
    if tags.get("railway") == "rail":
        return "chemin_fer"
    if tags.get("amenity") == "hospital":
        return "hopital"
    if tags.get("leisure") == "stadium":
        return "stade"
    if "building" in tags:
        return "batiment"
    return None


def _construire_couches(reponse_json, crs_cible):
    """Construit 4 couches memoire (batiment/stade/hopital en polygones,
    chemin_fer en lignes), dans le CRS de la couche d'emprise (metrique)."""
    crs_wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
    transform_inverse = QgsCoordinateTransform(crs_wgs84, crs_cible, QgsProject.instance())

    fields = QgsFields()
    fields.append(QgsField("osm_id", QVariant.LongLong))
    fields.append(QgsField("nom", QVariant.String))

    couches = {
        "batiment": QgsVectorLayer(f"Polygon?crs={crs_cible.authid()}", NOMS_SORTIE["batiment"], "memory"),
        "stade": QgsVectorLayer(f"Polygon?crs={crs_cible.authid()}", NOMS_SORTIE["stade"], "memory"),
        "hopital": QgsVectorLayer(f"Polygon?crs={crs_cible.authid()}", NOMS_SORTIE["hopital"], "memory"),
        "chemin_fer": QgsVectorLayer(f"LineString?crs={crs_cible.authid()}", NOMS_SORTIE["chemin_fer"], "memory"),
    }
    prov = {}
    for cle, couche in couches.items():
        couche.dataProvider().addAttributes(fields)
        couche.updateFields()
        prov[cle] = couche.dataProvider()

    feats_par_couche = {cle: [] for cle in couches}
    nb_ignores_geom_invalide = 0

    for el in reponse_json.get("elements", []):
        if el.get("type") != "way" or "geometry" not in el:
            continue
        tags = el.get("tags", {})
        cible = _classer_element(tags)
        if cible is None:
            continue

        pts_wgs84 = [QgsPointXY(pt["lon"], pt["lat"]) for pt in el["geometry"]]
        if len(pts_wgs84) < 2:
            continue
        pts_proj = [transform_inverse.transform(p) for p in pts_wgs84]

        if cible == "chemin_fer":
            geom = QgsGeometry.fromPolylineXY(pts_proj)
        else:
            # Polygone : s'assurer que l'anneau est ferme.
            if pts_proj[0] != pts_proj[-1]:
                pts_proj.append(pts_proj[0])
            if len(pts_proj) < 4:
                nb_ignores_geom_invalide += 1
                continue
            geom = QgsGeometry.fromPolygonXY([pts_proj])

        if geom is None or geom.isEmpty():
            nb_ignores_geom_invalide += 1
            continue

        f = QgsFeature(fields)
        f.setGeometry(geom)
        f.setAttributes([el.get("id", 0), tags.get("name", "")])
        feats_par_couche[cible].append(f)

    for cle, feats in feats_par_couche.items():
        if feats:
            prov[cle].addFeatures(feats)
        couches[cle].updateExtents()

    if nb_ignores_geom_invalide:
        print(f"  {nb_ignores_geom_invalide} element(s) OSM ignore(s) (geometrie invalide/trop simple).")

    return couches, feats_par_couche


# ============================================================================
# 4) POINT D'ENTREE
# ============================================================================

def executer():
    print("=" * 70)
    print("Extraction des obstacles OSM (batiments / stades / hopitaux / voie ferree)")
    print("=" * 70)

    bbox, crs_cible = _bbox_wgs84()
    reponse = _requete_overpass(bbox)
    couches, feats_par_couche = _construire_couches(reponse, crs_cible)

    proj = QgsProject.instance()
    for cle, couche in couches.items():
        nb = len(feats_par_couche[cle])
        print(f"  {NOMS_SORTIE[cle]:12s} : {nb} entite(s)")
        if nb == 0:
            print(f"    (aucune donnee OSM trouvee pour cette categorie dans l'emprise -- "
                  f"couche non ajoutee au projet, ce n'est pas une erreur)")
            continue
        # Si une couche du meme nom existe deja dans le projet, on la retire
        # d'abord pour eviter les doublons lors de relances successives.
        existantes = proj.mapLayersByName(couche.name())
        for c in existantes:
            proj.removeMapLayer(c.id())
        proj.addMapLayer(couche)

    print("=" * 70)
    print("Termine. Ces couches sont en memoire (non enregistrees sur disque) :")
    print("  si vous fermez QGIS sans les exporter, elles seront perdues -- pensez")
    print("  a faire clic droit > Exporter > Enregistrer les entites sous... (GeoPackage)")
    print("  si vous voulez les conserver pour les prochains lancements du plugin.")
    print("=" * 70)


executer()
