# -*- coding: utf-8 -*-
"""
core/couche_differee.py
========================
FIX-THREAD-SAFETY.

Historique du bug (cf. README / discussions precedentes) :

  - Builds precedents : le pipeline construisait un vrai QgsVectorLayer("memory") ET
    l'ajoutait au projet (QgsProject.instance().addMapLayer(...)) depuis le
    QThread de calcul. PyQGIS n'etant pas thread-safe, ceci provoquait un
    access violation aleatoire (deux threads touchant a la fois le registre
    de providers QGIS).

  - Premier correctif : l'ajout au projet a ete deplace dans le thread
    principal (cf. ui/panneau_principal.py::_ajouter_couches_dans_projet),
    mais le QgsVectorLayer lui-meme restait construit dans le thread de
    calcul, puis simplement transmis au thread principal via un signal Qt.
    INSUFFISANT : QgsVectorLayer est un QObject, dont l'affinite de thread
    reste celle du QThread qui l'a construit. Ce QThread se termine presque
    immediatement apres avoir emis le signal (il n'a plus rien a faire) ;
    quand le thread principal traite l'evenement en file d'attente et
    manipule enfin l'objet (ex. couche.name()), le thread proprietaire de
    l'objet a deja fini de tourner -> l'objet est dans un etat invalide ->
    access violation (observe dans _ajouter_couches_dans_projet).

  - Build actuel (ce module) : plus AUCUN QgsVectorLayer n'est jamais construit en
    dehors du thread principal. Le pipeline utilise a la place
    `CoucheDifferee`, un objet Python pur (aucun QObject, donc aucune
    affinite de thread) qui imite le tout petit sous-ensemble de l'API de
    QgsVectorLayer utilise par le code de construction des couches de
    sortie (`dataProvider()`, `fields()`, `updateFields()`,
    `updateExtents()`, `name()`). Les QgsField/QgsFeature/QgsGeometry qu'il
    accumule sont des types-valeur QGIS (pas des QObject) : ils n'ont pas
    d'affinite de thread et peuvent circuler librement entre threads via un
    signal Qt, comme n'importe quelle donnee Python.

    Le vrai QgsVectorLayer n'est construit qu'une seule fois, par le thread
    principal, a partir de ces donnees -- cf.
    ui/panneau_principal.py::_materialiser_couche.
"""

from qgis.core import QgsFields


class CoucheDifferee:
    """Remplace un QgsVectorLayer pendant le calcul en arriere-plan.

    uri_type_geom : chaine comme "LineString?crs=EPSG:2154" (le format
                    habituellement passe au constructeur QgsVectorLayer
                    pour une couche memoire).
    nom           : nom de la couche.
    """

    def __init__(self, uri_type_geom, nom):
        type_geom, _, crs = uri_type_geom.partition("?crs=")
        self.type_geom = type_geom
        self.crs = crs
        self.nom = nom
        self.champs = []   # list[QgsField]
        self.feats = []    # list[QgsFeature]

    # --- sous-ensemble de l'API QgsVectorLayer / QgsVectorDataProvider ---

    def dataProvider(self):
        return self

    def addAttributes(self, champs):
        self.champs.extend(champs)

    def addFeatures(self, feats):
        self.feats.extend(feats)

    def fields(self):
        flds = QgsFields()
        for champ in self.champs:
            flds.append(champ)
        return flds

    def getFeatures(self, request=None):
        # `request` ignore : cet objet n'existe que le temps du calcul, ses
        # seuls consommateurs (verifier_non_regression_geometrie.py) ne
        # filtrent pas. Suffisant pour l'usage actuel ; a etendre si un
        # futur appelant a besoin d'un vrai QgsFeatureRequest.
        return iter(self.feats)

    def updateFields(self):
        pass  # rien a faire : self.champs deja a jour

    def updateExtents(self):
        pass  # rien a faire : calcule par le vrai QgsVectorLayer plus tard

    def name(self):
        return self.nom
