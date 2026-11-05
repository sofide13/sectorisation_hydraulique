# -*- coding: utf-8 -*-
"""
Dialogue de configuration - Sectorisation Hydraulique
Deux onglets : Simple (paramètres clés) + Avancé (tous les paramètres).
Chargement / sauvegarde JSON.
"""

import os
import copy

from qgis.PyQt.QtWidgets import (
    QDialog, QDialogButtonBox, QTabWidget, QWidget,
    QVBoxLayout, QHBoxLayout, QGridLayout, QGroupBox,
    QLabel, QLineEdit, QDoubleSpinBox, QSpinBox,
    QCheckBox, QComboBox, QPushButton, QFileDialog,
    QScrollArea, QMessageBox, QFrame,
    QTextBrowser
)
from qgis.PyQt.QtGui import QFont
from qgis.core import QgsProject, QgsVectorLayer, QgsRasterLayer

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from ..core.config_manager import (
    charger_config_json, sauvegarder_config_json,
    json_vers_plat, plat_vers_json, valider_config,
)


# ---------------------------------------------------------------------------
# Helpers UI
# ---------------------------------------------------------------------------

def _separateur():
    line = QFrame()
    line.setFrameShape(QFrame.HLine)
    line.setFrameShadow(QFrame.Sunken)
    return line


def _titre_section(texte):
    lbl = QLabel(texte)
    font = QFont()
    font.setBold(True)
    font.setPointSize(9)
    lbl.setFont(font)
    lbl.setStyleSheet("color: #1a5276; margin-top: 6px;")
    return lbl


def _spinbox_double(val, minimum, maximum, decimals=1, suffix=""):
    sb = QDoubleSpinBox()
    sb.setMinimum(minimum)
    sb.setMaximum(maximum)
    sb.setDecimals(decimals)
    sb.setValue(float(val))
    sb.setSuffix(f" {suffix}" if suffix else "")
    sb.setMinimumWidth(110)
    return sb


def _spinbox_int(val, minimum, maximum, suffix=""):
    sb = QSpinBox()
    sb.setMinimum(minimum)
    sb.setMaximum(maximum)
    sb.setValue(int(val))
    sb.setSuffix(f" {suffix}" if suffix else "")
    sb.setMinimumWidth(110)
    return sb


def _champ_couche(valeur_defaut, layers_noms):
    cb = QComboBox()
    cb.setEditable(True)
    cb.addItems([""] + sorted(layers_noms))
    idx = cb.findText(valeur_defaut)
    if idx >= 0:
        cb.setCurrentIndex(idx)
    else:
        cb.setEditText(valeur_defaut)
    return cb


# ---------------------------------------------------------------------------
# Onglet SIMPLE
# ---------------------------------------------------------------------------

class OngletSimple(QWidget):
    def __init__(self, config_json, layers_noms, parent=None):
        super().__init__(parent)
        self._config = config_json
        self._widgets = {}
        self._construire(layers_noms)

    def _construire(self, layers_noms):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setSpacing(8)
        layout.setContentsMargins(12, 12, 12, 12)

        # ── Couches ──────────────────────────────────────────────────────────
        grp_couches = QGroupBox("Couches du projet")
        grp_couches.setStyleSheet("QGroupBox { font-weight: bold; }")
        gl = QGridLayout()
        gl.setColumnMinimumWidth(1, 220)

        couches_def = [
            ("AEP_LAYER",        "Conduites AEP",  "Couche des conduites du réseau"),
            ("ABONNE_LAYER",     "Abonnés",         "Couche des points d'abonnés"),
            ("TANK_LAYER",       "Réservoirs",      "Couche des réservoirs / châteaux d'eau"),
            ("COMMUNE_LAYER",    "Commune (optionnel)", "Couche commune pour découpe Voronoï"),
            ("ROUTE_LAYER",      "Routes (optionnel)",
             "Couche des routes : si renseignée, les limites de secteur (secteurs_limites) "
             "suivent les routes au lieu d'un pur Voronoï sur les nœuds"),
            ("BATIMENT_LAYER",   "Bâtiments (optionnel)",
             "Couche des emprises de bâtiments : jamais coupée par une limite de secteur"),
            ("STADE_LAYER",      "Stades (optionnel)",
             "Couche des emprises de stades : jamais coupée par une limite de secteur"),
            ("HOPITAL_LAYER",    "Hôpitaux (optionnel)",
             "Couche des emprises d'hôpitaux : jamais coupée par une limite de secteur"),
            ("CHEMIN_FER_LAYER", "Chemin de fer (optionnel)",
             "Couche de la voie ferrée (ligne ou emprise polygone) : utilisée comme coupure "
             "si c'est une ligne, ou protégée comme un bâtiment si c'est un polygone"),
        ]
        for row, (cle, label, tooltip) in enumerate(couches_def):
            val = self._config.get('COUCHES', {}).get(cle, '')
            cb = _champ_couche(val, layers_noms)
            cb.setToolTip(tooltip)
            lbl = QLabel(label + " :")
            lbl.setToolTip(tooltip)
            gl.addWidget(lbl, row, 0)
            gl.addWidget(cb, row, 1)
            self._widgets[cle] = cb

        # MNT
        row = len(couches_def)
        val_dem = self._config.get('COUCHES', {}).get('DEM_PATH', '')
        tooltip_dem = "Chemin vers le MNT (raster .tif en UTM), utilisé pour l'altitude des points sans côte connue"
        self._dem_edit = QLineEdit(val_dem)
        self._dem_edit.setToolTip(tooltip_dem)
        btn_dem = QPushButton("…")
        btn_dem.setMaximumWidth(30)
        btn_dem.clicked.connect(self._choisir_dem)
        hl = QHBoxLayout()
        hl.addWidget(self._dem_edit)
        hl.addWidget(btn_dem)
        lbl_dem = QLabel("MNT (raster .tif) :")
        lbl_dem.setToolTip(tooltip_dem)
        gl.addWidget(lbl_dem, row, 0)
        gl.addLayout(hl, row, 1)
        self._widgets['DEM_PATH'] = self._dem_edit

        grp_couches.setLayout(gl)
        layout.addWidget(grp_couches)

        # ── Champs attributaires ─────────────────────────────────────────────
        grp_champs = QGroupBox("Champs attributaires")
        grp_champs.setStyleSheet("QGroupBox { font-weight: bold; }")
        gl2 = QGridLayout()
        champs_def = [
            ("PIPE_DIAMETER_FIELD",     "Diamètre conduites",   "Nom du champ diamètre (mm) dans la couche AEP"),
            ("PIPE_LENGTH_FIELD",       "Longueur conduites",   "Nom du champ longueur (m) dans la couche AEP"),
            ("TANK_CAPACITY_FIELD",     "Capacité réservoirs",  "Nom du champ capacité (m³) dans la couche tank"),
            ("TANK_DEBIT_SOURCE_FIELD", "Débit source (m³/j)",  "Nom du champ débit source dans la couche tank"),
            ("CHAMP_BLOC_ABONNE",       "Bloc bâtiment (abonné)", "Nom du champ identifiant le bloc/bâtiment dans la couche abonné (laisser vide pour ignorer)"),
            ("CHAMP_ETAGE_ABONNE",      "Étage (abonné)",       "Nom du champ nombre d'étage/niveau dans la couche abonné (laisser vide pour ignorer)"),
            ("CHAMP_LARGEUR_TAMPON_ROUTE", "Largeur tampon (route)",
             "Nom du champ NUMÉRIQUE sur la couche routes donnant, tronçon par tronçon, le rayon de "
             "tampon (m) à utiliser (ex: 1 sur une nationale, 0.5 sur une voie secondaire, 0.1 sur un "
             "chemin étroit) — évite qu'un rayon unique ne déborde sur le bâti le long des petites voies. "
             "Laisser vide pour utiliser le rayon unique 'Largeur tampon route (m)' ci-dessous."),
            ("CHAMP_TYPE_ROUTE", "Type de voie (route)",
             "Nom du champ catégoriel sur la couche routes (type de voie, classe, etc.) utilisé avec la "
             "table 'LARGEUR_TAMPON_PAR_TYPE_ROUTE' du JSON pour attribuer un rayon de tampon différent "
             "par catégorie de route. Ignoré si le champ ci-dessus (numérique) est déjà renseigné et non "
             "vide pour le tronçon. Laisser vide pour utiliser le rayon unique."),
            ("CHAMP_LARGEUR_CORRIDOR_AEP", "Largeur corridor (AEP)",
             "Nom du champ NUMÉRIQUE sur la couche AEP donnant, tronçon par tronçon, le rayon de corridor "
             "(m) à utiliser pour la couche secteurs_hydrauliques. Si vide, le rayon est calculé "
             "automatiquement à partir du diamètre de la conduite (voir Corridor réseau), avec repli sur "
             "le rayon unique si le diamètre est inconnu."),
        ]
        for row, (cle, label, tooltip) in enumerate(champs_def):
            val = self._config.get('CHAMPS', {}).get(cle, '')
            le = QLineEdit(str(val))
            le.setToolTip(tooltip)
            lbl = QLabel(label + " :")
            lbl.setToolTip(tooltip)
            gl2.addWidget(lbl, row, 0)
            gl2.addWidget(le, row, 1)
            self._widgets[cle] = le
        grp_champs.setLayout(gl2)
        layout.addWidget(grp_champs)

        # ── Pressions ────────────────────────────────────────────────────────
        grp_press = QGroupBox("Plages de pression (m)")
        grp_press.setStyleSheet("QGroupBox { font-weight: bold; }")
        gl3 = QGridLayout()
        hyd = self._config.get('HYDRAULIQUE', {})
        press_def = [
            ("PRESSION_MIN_M",   "Pression minimale", hyd.get('PRESSION_MIN_M', 10), 0, 100, "m",
             "Pression minimale acceptable en tout point du réseau (10 m ≈ 1 bar). En dessous, l'abonné est mal desservi."),
            ("PRESSION_MAX_M",   "Pression maximale", hyd.get('PRESSION_MAX_M', 60), 0, 200, "m",
             "Pression maximale tolérée. Au-delà, risque de casse sur les installations privées des abonnés."),
            ("PRESSION_CIBLE_M", "Pression cible",    hyd.get('PRESSION_CIBLE_M', 30), 0, 150, "m",
             "Pression visée par défaut lors du réglage d'un point de régulation (PRV, réservoir relais)."),
        ]
        for row, (cle, label, val, mn, mx, sfx, tooltip) in enumerate(press_def):
            sb = _spinbox_double(val, mn, mx, 1, sfx)
            sb.setToolTip(tooltip)
            lbl = QLabel(label + " :")
            lbl.setToolTip(tooltip)
            gl3.addWidget(lbl, row, 0)
            gl3.addWidget(sb, row, 1)
            self._widgets[cle] = sb
        grp_press.setLayout(gl3)
        layout.addWidget(grp_press)

        # ── Plages secteur ───────────────────────────────────────────────────
        grp_sect = QGroupBox("Plages secteur (abonnés)")
        grp_sect.setStyleSheet("QGroupBox { font-weight: bold; }")
        gl4 = QGridLayout()
        ps = self._config.get('PLAGES_SECTEUR', {})
        sect_def = [
            ("ABONNES_MIN_PAR_SECTEUR",   "Minimum / secteur",  ps.get('ABONNES_MIN_PAR_SECTEUR', 500),   10, 5000, "ab",
             "Paramètre de conception : 500 par défaut pour une DMA ; une borne 300 peut être saisie si elle est justifiée et documentée pour le réseau. En dessous, fusion avec un secteur voisin."),
            ("ABONNES_MAX_PAR_SECTEUR",   "Maximum / secteur",  ps.get('ABONNES_MAX_PAR_SECTEUR', 3000),  100, 20000, "ab",
             "Nombre maximal d'abonnés qu'un secteur peut compter avant d'être scindé en deux."),
            ("ABONNES_IDEAL_PAR_SECTEUR", "Idéal / secteur",    ps.get('ABONNES_IDEAL_PAR_SECTEUR', 2000), 100, 10000, "ab",
             "Taille visée en priorité lors du découpage, entre le minimum et le maximum."),
        ]
        for row, (cle, label, val, mn, mx, sfx, tooltip) in enumerate(sect_def):
            sb = _spinbox_int(val, mn, mx, sfx)
            sb.setToolTip(tooltip)
            lbl = QLabel(label + " :")
            lbl.setToolTip(tooltip)
            gl4.addWidget(lbl, row, 0)
            gl4.addWidget(sb, row, 1)
            self._widgets[cle] = sb
        grp_sect.setLayout(gl4)
        layout.addWidget(grp_sect)

        # ── Altimétrique  ───────────────────────────────────────────────
        grp_alt = QGroupBox("Scission altimétrique (PRV virtuels)")
        grp_alt.setStyleSheet("QGroupBox { font-weight: bold; }")
        gl5 = QGridLayout()
        alt = self._config.get('ALTIMETRIQUE', {})

        cb_alt = QCheckBox("Activer la scission altimétrique")
        cb_alt.setChecked(bool(alt.get('ACTIVER_SCISSION_ALTIMETRIQUE', True)))
        cb_alt.setToolTip("Coupe chaque branche dès que la pression statique sort de la bande utile, "
                           "avant même le découpage par nombre d'abonnés")
        gl5.addWidget(cb_alt, 0, 0, 1, 2)
        self._widgets['ACTIVER_SCISSION_ALTIMETRIQUE'] = cb_alt

        alt_def = [
            ("BANDE_PRESSION_UTILE_M", "Bande de pression utile", alt.get('BANDE_PRESSION_UTILE_M', 45), 10, 100, "m",
             "Largeur de la fourchette de pression exploitable par palier avant de déclencher une coupure altimétrique."),
            ("ECART_ALT_MAX_FUSION_M", "Écart alt. max fusion",   alt.get('ECART_ALT_MAX_FUSION_M', 45), 5,  150, "m",
             "Écart d'altitude maximal accepté entre deux secteurs pour les fusionner sans recréer un problème de pression."),
        ]
        for row, (cle, label, val, mn, mx, sfx, tooltip) in enumerate(alt_def, start=1):
            sb = _spinbox_double(val, mn, mx, 1, sfx)
            sb.setToolTip(tooltip)
            lbl = QLabel(label + " :")
            lbl.setToolTip(tooltip)
            gl5.addWidget(lbl, row, 0)
            gl5.addWidget(sb, row, 1)
            self._widgets[cle] = sb
        grp_alt.setLayout(gl5)
        layout.addWidget(grp_alt)

        # ── Démographie ──────────────────────────────────────────────────────
        grp_demo = QGroupBox("Données démographiques")
        grp_demo.setStyleSheet("QGroupBox { font-weight: bold; }")
        gl6 = QGridLayout()
        demo = self._config.get('DEMOGRAPHIQUE', {})
        demo_def = [
            ("HABITANTS_PAR_ABONNE", "Habitants / abonné",    demo.get('HABITANTS_PAR_ABONNE', 5),   1, 20, "pers",
             "Nombre moyen d'habitants par branchement, pour convertir un nombre d'abonnés en population desservie."),
            ("DOTATION_L_HAB_JOUR",  "Dotation (L/hab/jour)", demo.get('DOTATION_L_HAB_JOUR', 150), 50, 500, "L/j",
             "Consommation moyenne par habitant et par jour, base du calcul des débits."),
            ("COEF_POINTE_HORAIRE",  "Coef. pointe horaire",  demo.get('COEF_POINTE_HORAIRE', 1.3),  1, 5, "",
             "Coefficient multiplicateur appliqué au débit moyen pour obtenir le débit de pointe horaire."),
        ]
        for row, (cle, label, val, mn, mx, sfx, tooltip) in enumerate(demo_def):
            sb = _spinbox_double(val, mn, mx, 2, sfx)
            sb.setToolTip(tooltip)
            lbl = QLabel(label + " :")
            lbl.setToolTip(tooltip)
            gl6.addWidget(lbl, row, 0)
            gl6.addWidget(sb, row, 1)
            self._widgets[cle] = sb
        grp_demo.setLayout(gl6)
        layout.addWidget(grp_demo)

        # ── Partition des ilots urbains ───────────────────────────────────────────────────
        grp_part = QGroupBox("Partition des secteurs par îlots urbains")
        grp_part.setStyleSheet("QGroupBox { font-weight: bold; }")
        gl7 = QGridLayout()
        part = self._config.get('PARTITION', {})
        cb_part = QCheckBox("Délimiter les secteurs par partition stricte des "
                            "îlots urbains (recommandé)")
        cb_part.setChecked(bool(part.get('ACTIVER_PARTITION_ILOTS', True)))
        cb_part.setToolTip(
            "les territoires de secteur (secteurs_hydrauliques, "
            "secteurs_limites, secteurs_hydrauliques_topo) pavent le tissu "
            "urbanisé polygonisé par les routes : limites sur les lignes "
            "centrales de rues, AUCUN chevauchement, AUCUN îlot parasite, "
            "jamais d'enveloppe convexe. Désactivé, ou si la couche routes est "
            "absente/non bouclée : repli sur le corridor-buffer nettoyé.")
        gl7.addWidget(cb_part, 0, 0, 1, 2)
        self._widgets['ACTIVER_PARTITION_ILOTS'] = cb_part
        part_def = [
            ("SEUIL_AIRE_ILOT_M2", "Seuil d'îlot parasite (m²)",
             part.get('SEUIL_AIRE_ILOT_M2', 2000), 100, 100000, 0, "m²",
             "En dessous de cette surface, une partie disjointe ou une poche "
             "résiduelle est réaffectée au secteur voisin avec lequel elle "
             "partage la plus longue frontière (fini les petits îlots au "
             "milieu des stades / parcels d'un autre secteur)."),
            ("LARGEUR_MAX_VOIE_M", "Largeur max d'une voie (m)",
             part.get('LARGEUR_MAX_VOIE_M', 30), 5, 80, 1, "m",
             "Largeur maximale d'un couloir résiduel (trou de la trame "
             "routière) rattachable au secteur adjacent."),
            ("MAX_PASSES_NETTOYAGE", "Passes de nettoyage topologique",
             part.get('MAX_PASSES_NETTOYAGE', 4), 1, 10, 0, "",
             "Itérations du nettoyage (îlots, chevauchements, trous) appliqué "
             "aux territoires de secteur."),
            ("DIST_MAX_ILOT_SANS_NOEUD_M",
             "Distance max de rattachement d'un îlot sans réseau (m)",
             part.get('DIST_MAX_ILOT_SANS_NOEUD_M', 300), 0, 5000, 0, "m",
             "un îlot sans aucun nœud de réseau est rattaché au secteur "
             "du nœud le plus proche, mais seulement jusqu'à cette distance. "
             "Au-delà, personne ne le desservant (terrain agricole, désert), "
             "il n'entre dans aucun secteur : fini les territoires qui "
             "débordent sur des kilomètres de vide. 0 = aucune borne."),
        ]
        for row, (cle, label, val, mn, mx, dec, sfx, tooltip) in enumerate(part_def):
            if dec == 0:
                sb = _spinbox_int(int(val), mn, mx, sfx)
            else:
                sb = _spinbox_double(val, mn, mx, dec, sfx)
            sb.setToolTip(tooltip)
            lbl = QLabel(label + " :")
            lbl.setToolTip(tooltip)
            gl7.addWidget(lbl, row + 1, 0)
            gl7.addWidget(sb, row + 1, 1)
            self._widgets[cle] = sb
        cb_contig = QCheckBox("Un secteur = un seul territoire, d'un seul tenant "
                              "(recommandé)")
        cb_contig.setChecked(bool(part.get('ASSURER_CONTIGUITE_TERRITOIRES',
                                           True)))
        cb_contig.setToolTip(
            "corrige le défaut où un secteur sélectionné dans QGIS "
            "s'allumait en une dizaine de morceaux éparpillés dans toute la "
            "ville, entremêlés avec ses voisins. Le morceau principal (celui "
            "qui porte le plus d'abonnés) reste au secteur ; chaque autre "
            "morceau est rendu au secteur voisin avec lequel il partage la plus "
            "longue frontière. Seule la CARTE change : les nœuds et conduites "
            "gardent exactement le secteur décidé par l'hydraulique, aucune "
            "surface n'est créée ni perdue, et jamais d'enveloppe convexe. "
            "Chaque transfert est journalisé (surface et abonnés concernés). "
            "Décoché = comportement de la version précédente.")
        gl7.addWidget(cb_contig, len(part_def) + 1, 0, 1, 2)
        self._widgets['ASSURER_CONTIGUITE_TERRITOIRES'] = cb_contig
        grp_part.setLayout(gl7)
        layout.addWidget(grp_part)

        layout.addStretch()
        scroll.setWidget(inner)
        main = QVBoxLayout(self)
        main.setContentsMargins(0, 0, 0, 0)
        main.addWidget(scroll)

    def _choisir_dem(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choisir le MNT", "", "Raster (*.tif *.tiff *.asc *.img)")
        if path:
            self._dem_edit.setText(path)

    def lire_valeurs(self):
        """Retourne un dict plat des valeurs lues depuis l'UI."""
        vals = {}
        for cle, widget in self._widgets.items():
            if isinstance(widget, (QDoubleSpinBox,)):
                vals[cle] = widget.value()
            elif isinstance(widget, QSpinBox):
                vals[cle] = widget.value()
            elif isinstance(widget, QCheckBox):
                vals[cle] = widget.isChecked()
            elif isinstance(widget, QComboBox):
                vals[cle] = widget.currentText().strip()
            elif isinstance(widget, QLineEdit):
                vals[cle] = widget.text().strip()
        return vals


# ---------------------------------------------------------------------------
# Onglet AVANCÉ  (tous les paramètres organisés par section)
# ---------------------------------------------------------------------------

# Valeur à afficher quand la clé est absente du JSON chargé (config d'une
# version antérieure du plugin). Sans cela, une case à cocher absente du
# fichier repartirait à « décochée » et désactiverait silencieusement un
# correctif actif par défaut dans le moteur.
_DEFAUT_SI_SECTION_ABSENTE = {
    'CONSOLIDER_ZONES_EN_SECTEURS': True,
    'ALIGNER_SUR_SECTEURS_REFERENCES': True,
    # correctifs actifs par defaut dans le moteur -- une config ancienne
    # rechargee ne doit pas les desactiver en silence.
    'ASSURER_CONTIGUITE_TERRITOIRES': True,
    'AIRE_EXCLAVE_TOLEREE_M2': 0.0,
    'MAX_PASSES_CONTIGUITE': 60,
    'DIST_MAX_ILOT_SANS_NOEUD_M': 300.0,
}


class OngletAvance(QWidget):
    def __init__(self, config_json, parent=None):
        super().__init__(parent)
        self._config = config_json
        self._widgets = {}
        self._construire()

    def _construire(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setSpacing(6)
        layout.setContentsMargins(12, 12, 12, 12)

        # Définition complète : (section_json, cle, label, type, min, max, decimals, suffix, tooltip)
        SECTIONS = [
            ("Hydraulique général", "HYDRAULIQUE", [
                ("COEF_HAZEN_WILLIAMS",       "Coef. Hazen-Williams C",       "float", 80,   160,  1,  "",   "Rugosité conduite (PVC neuf≈140, fonte≈100)"),
                ("PERTE_CHARGE_MAX_M",        "Perte charge max (m)",         "float", 10,   200,  1,  "m",  "Perte de charge cumulée maximale acceptée"),
                ("HAUTEUR_EAU_RESERVOIR_M",   "Hauteur eau réservoir (m)",    "float", 0,    20,   1,  "m",  "Hauteur d'eau dans les réservoirs non renseignés"),
                ("CORRECTION_COTE_PIEZO_M",   "Correction côte piézo (m)",    "float", -20,  20,   2,  "m",  "Ajustement global de toutes les côtes piézométriques"),
                ("SNAP_TOLERANCE",            "Tolérance snap (m)",           "float", 0.01, 10,   3,  "m",  "Tolérance de fusion des nœuds proches"),
                ("TANK_RESEARCH_M",           "Rayon recherche réservoir",    "float", 100,  10000,0,  "m",  "Rayon de recherche du nœud réseau le plus proche d'un réservoir"),
                ("VORONOI_CLIP_BUFFER_M",     "Buffer Voronoï (m)",           "float", 10,   500,  0,  "m",  "Buffer autour du réseau pour le découpage Voronoï"),
                ("MAX_ITER_CONSOLIDATION",    "Max itérations consolidation", "int",   10,   500,  0,  "",   "Nombre max d'itérations de consolidation"),
            ]),
            ("PRV et surpresseurs", "PRV_SURPRESSEUR", [
                ("DN_MIN_PRV",            "DN min PRV (mm)",          "int",   20,  2500, 0, "mm", "Diamètre nominal minimum pour poser un PRV"),
                ("DN_MIN_SURPRESSEUR",    "DN min surpresseur (mm)",  "int",   20,  2500, 0, "mm", "Diamètre nominal minimum pour poser un surpresseur"),
                ("PRESSION_CIBLE_MIN",    "Pression réglée min (m)",  "float", 0,   60,  1, "m",  "Pression minimale réglée par un PRV"),
                ("PRESSION_CIBLE_MAX",    "Pression réglée max (m)",  "float", 10,  120, 1, "m",  "Pression maximale réglée par un PRV"),
            ]),
            ("Vitesses de conception", "VITESSES", [
                ("VITESSE_MIN_M_S",                   "Vitesse min (m/s)",             "float", 0.01, 2,     2, "m/s", "Borne basse de conception, configurable (m/s)"),
                ("VITESSE_MAX_M_S",                   "Vitesse max (m/s)",             "float", 0.5,  5,     1, "m/s", "Borne haute de conception, configurable (m/s)"),
                ("DN_MIN_CONTROLE_VITESSE",           "DN min contrôle (mm)",          "int",   20,   2500,  0, "mm",  "Seules les conduites > ce DN sont contrôlées"),
                ("ABONNES_AVAL_MIN_CONTROLE_VITESSE", "Abonnés aval min pour contrôle","int",   1,    100,   0, "ab",  "Seules les conduites desservant > N abonnés sont contrôlées"),
            ]),
            ("Capacité réservoir", "CAPACITE_RESERVOIR", [
                ("JOURS_AUTONOMIE_RESERVOIR",   "Jours d'autonomie",          "float", 0.5, 7,    1, "j",   "Nombre de jours de stock requis"),
                ("MARGE_SECURITE_CAPACITE_PCT", "Marge sécurité capacité",    "float", 0,   0.5,  2, "",    "Part de la capacité mise en réserve (0.05 = 5%)"),
                ("CAPACITE_PRINCIPAL_MIN_M3",   "Seuil réservoir principal",  "float", 100, 50000,0, "m³",  "Capacité minimale pour classer en PRINCIPAL"),
                ("CAPACITE_SECONDAIRE_MIN_M3",  "Seuil réservoir secondaire", "float", 50,  10000,0, "m³",  "Capacité minimale pour classer en SECONDAIRE"),
            ]),
            ("Étages / immeubles", "ETAGES", [
                ("GRILLE_ETAGE_M",       "Grille clustering (m)",     "float", 0.5, 10,  1, "m",  "Taille de cellule pour le clustering d'étages"),
                ("HAUTEUR_ETAGE_M",      "Hauteur étage (m)",         "float", 2,   5,   2, "m",  "Hauteur d'un niveau (pression supplémentaire requise)"),
                ("NIVEAU_MAX_PLAUSIBLE", "Niveau max plausible",      "int",   1,   20,  0, "",   "Nombre max d'étages par immeuble"),
                ("BLOC_MAX_NIVEAUX",     "Abonnés max par bloc",      "int",   1,   20,  0, "",   "Nombre d'abonnés par niveaux de bloc"),
            ]),
            ("Voronoï", "VORONOI", [
                ("UTILISER_VORONOI_AMORCE", "Utiliser Voronoï amorce", "bool", 0, 1, 0, "", "Active le biais Voronoï pour l'amorçage"),
                ("VORONOI_BIAS_FAVORISE",   "Facteur favorisé",        "float", 0.1, 2, 2, "", "Facteur de coût pour un nœud dans sa cellule Voronoï"),
                ("VORONOI_BIAS_PENALISE",   "Facteur pénalisé",        "float", 1,   5, 2, "", "Facteur de coût pour un nœud hors de sa cellule Voronoï"),
            ]),
            ("Raffinement", "RAFFINEMENT", [
                ("MAX_ROUNDS_RAFFINEMENT",      "Rounds max raffinement",       "int",   10,  500, 0, "",   "Nombre max d'itérations de raffinement local"),
                ("MAX_TRANSFERTS_PAR_NOEUD",    "Max transferts / nœud",        "int",   1,   200, 0, "",   "Nombre max de transferts pour un même nœud"),
                ("PENALITE_CAPACITE_COEF",      "Coef pénalité capacité",       "float", 100, 10000,0,"",   "Coefficient de pénalité pour dépassement de capacité"),
                ("DEGRADATION_PRESSION_MAX_M",  "Dégradation pression max (m)", "float", 5,   200, 1, "m",  "Dégradation max acceptée lors d'un transfert"),
                ("MAX_SAUTS_RAFFINEMENT",       "Sauts max raffinement",        "int",   1,   10,  0, "",   "Nombre max de sauts BFS lors du raffinement"),
            ]),
            ("Habillage cartographique (polygones)", "POLYGONES_TOPOLOGIQUES", [
                ("SEUIL_DN_TRANSPORT_MM", "Seuil DN transport (mm)", "float", 20, 2500, 0, "mm",
                 "Diamètre à partir duquel une conduite est classée TRANSPORT/adduction : elle "
                 "n'est jamais remplie en polygone (traverse souvent des zones non desservies) et "
                 "reste affichée en ligne seule dans conduites_transport_topo. En dessous, elle "
                 "est DISTRIBUTION et participe au remplissage du secteur. Si des conduites "
                 "apparaissent hors de tout secteur alors qu'elles devraient être couvertes, "
                 "vérifier leur diamètre réel contre ce seuil"),
                ("LARGEUR_TAMPON_CONDUITE_M", "Largeur tampon conduite (m)", "float", 1, 200, 1, "m",
                 "Demi-largeur du buffer appliqué aux conduites de distribution pour matérialiser "
                 "le polygone du secteur"),
                ("ZONE_MIN_NOEUDS", "Taille min zone indépendante (nœuds)", "int", 1, 50, 0, "",
                 "Nombre de nœuds minimum pour qu'un bloc de distribution isolé par une conduite "
                 "de transport soit promu zone indépendante plutôt que rattaché à la zone la plus "
                 "proche"),
            ]),
            ("Découpe par les routes", "DECOUPE_ROUTES", [
                ("ACTIVER_DECOUPE_ROUTES",          "Activer découpe par routes", "bool", 0, 1, 0, "",
                 "Si actif et couche Routes renseignée : les limites de secteur suivent les routes "
                 "au lieu d'un Voronoï pur (repli automatique si couche absente)"),
                ("INCLURE_TANK_ABONNE_DANS_EMPRISE", "Emprise = AEP + Tank + abonnés", "bool", 0, 1, 0, "",
                 "Étend l'emprise de travail (extraction routes incluse) aux réservoirs et abonnés, "
                 "pas seulement aux conduites AEP"),
                ("LARGEUR_TAMPON_ROUTE_M",          "Largeur tampon route (m)",    "float", 0.5, 30, 1, "m",
                 "Demi-largeur de route utilisée pour creuser la coupure entre secteurs voisins"),
                ("LARGEUR_TAMPON_VOIE_FERREE_M",    "Largeur tampon voie ferrée (m)", "float", 0.5, 30, 1, "m",
                 "Demi-largeur utilisée si le chemin de fer est fourni comme une ligne (coupure), "
                 "sans effet s'il est fourni comme un polygone (traité comme un bâtiment)"),
            ]),
            ("Corridor réseau", "CORRIDOR_RESEAU", [
                ("ACTIVER_ILOTS_URBAINS", "Délimiter par îlots urbains (recommandé)", "bool", 0, 1, 0, "",
                 "Si actif : secteurs_hydrauliques suit les vrais îlots urbains obtenus en "
                 "polygonisant le réseau de routes (limite = ligne centrale de rue, jamais un "
                 "bâtiment). Si inactif, ou là où le réseau routier ne ferme pas de polygone : repli "
                 "sur le corridor-buffer variable ci-dessous"),
                ("LARGEUR_CORRIDOR_M", "Largeur corridor de repli (m)", "float", 5, 200, 1, "m",
                 "Largeur utilisée par le corridor-buffer de repli (zones non couvertes par un îlot, "
                 "ou si la délimitation par îlots est désactivée) quand ni le champ ni le calcul par "
                 "diamètre ne s'appliquent"),
                ("BUFFER_SEGMENTS",   "Segments buffer",       "int",   4, 32,  0, "",   "Nombre de segments pour approcher les cercles de buffer"),
                ("ACTIVER_CORRIDOR_VARIABLE_DIAMETRE", "Corridor de repli variable par diamètre", "bool", 0, 1, 0, "",
                 "Pour le corridor-buffer de repli uniquement : largeur calculée à partir du diamètre "
                 "de chaque conduite au lieu d'un rayon unique pour toutes"),
                ("COEF_LARGEUR_CORRIDOR_PAR_DIAMETRE", "Coefficient largeur/diamètre", "float", 1, 200, 1, "",
                 "Largeur (m) = coefficient × diamètre (m) de la conduite, avant application des bornes "
                 "min/max ci-dessous"),
                ("LARGEUR_CORRIDOR_MIN_M", "Largeur corridor min (m)", "float", 0.5, 50, 1, "m",
                 "Borne basse appliquée à la largeur calculée à partir du diamètre"),
                ("LARGEUR_CORRIDOR_MAX_M", "Largeur corridor max (m)", "float", 1, 200, 1, "m",
                 "Borne haute appliquée à la largeur calculée à partir du diamètre"),
                ("ACTIVER_ROGNAGE_ROUTES_CORRIDOR", "Rogner le corridor de repli par les routes", "bool", 0, 1, 0, "",
                 "Pour le corridor-buffer de repli uniquement : retire la trame routière (même largeur "
                 "variable que l'onglet 'Découpe par les routes') du corridor"),
            ]),
            ("Partition îlots", "PARTITION", [
                ("ACTIVER_PARTITION_ILOTS", "Partition stricte par îlots urbains", "bool", 0, 1, 0, "",
                 "territoires = partition stricte du tissu urbanisé (polygonisation des "
                 "routes) : 0 chevauchement, 0 îlot parasite, limites sur les lignes centrales "
                 "de rues, jamais d'enveloppe convexe. Repli automatique sur corridor-buffer "
                 "nettoyé si routes absentes/non bouclées"),
                ("SEUIL_AIRE_ILOT_M2", "Seuil îlot parasite (m²)", "float", 100, 200000, 0, "m²",
                 "Toute partie disjointe ou poche résiduelle plus petite est réaffectée au "
                 "secteur voisin partageant la plus longue frontière"),
                ("LARGEUR_MAX_VOIE_M", "Largeur max voie (m)", "float", 5, 80, 1, "m",
                 "Largeur maximale d'un couloir résiduel rattachable (trou de trame routière)"),
                ("AIRE_MAX_RATTACHEMENT_M2", "Aire max poche rattachable (m²)", "float", 10000, 3000000, 0, "m²",
                 "Au-delà, une poche interne non fermée est exclue et journalisée (anomalie de "
                 "la trame routière)"),
                ("MAX_PASSES_NETTOYAGE", "Passes de nettoyage", "int", 1, 10, 0, "",
                 "Itérations du nettoyage topologique (îlots, chevauchements, trous)"),
                ("SNAP_LIMITE_M", "Tolérance de contact (m)", "float", 0.05, 5, 2, "m",
                 "Tolérance pour considérer deux polygones comme voisins (frontière partagée)"),
                ("ASSURER_CONTIGUITE_TERRITOIRES",
                 "1 secteur = 1 territoire d'un seul tenant", "bool", 0, 1, 0, "",
                 "Recommandé. Corrige le défaut visible dans QGIS : un secteur "
                 "sélectionné s'allumait en une dizaine de morceaux éparpillés dans toute "
                 "la ville, entremêlés avec ses voisins. Le morceau principal (celui qui "
                 "porte le plus d'abonnés) reste au secteur, chaque autre rejoint le "
                 "secteur voisin avec lequel il partage la plus longue frontière. Seul le "
                 "TERRITOIRE cartographique change : aucune appartenance hydraulique de "
                 "nœud ou de conduite n'est modifiée, aucune surface n'est créée ni "
                 "perdue, jamais d'enveloppe convexe. Décoché = comportement precedent"),
                ("AIRE_EXCLAVE_TOLEREE_M2", "Annexe isolée tolérée dès (m²)",
                 "float", 0, 5000000, 0, "m²",
                 "0 = aucune annexe tolérée (recommandé : territoires d'un seul "
                 "tenant). Si votre réseau a de vraies annexes détachées (hameau relié "
                 "par une seule conduite), mettez ici leur surface minimale : au-delà "
                 "elles sont conservées telles quelles au lieu d'être données au voisin"),
                ("MAX_PASSES_CONTIGUITE", "Passes de mise en un seul tenant",
                 "int", 1, 200, 0, "",
                 "Itérations maximales de l'absorption des morceaux dispersés. Le "
                 "traitement s'arrête de lui-même dès qu'il n'y a plus rien à recoller"),
                ("DIST_MAX_ILOT_SANS_NOEUD_M", "Distance max de rattachement d'un îlot sans réseau (m)",
                 "float", 0, 5000, 0, "m",
                 "Un îlot qui ne contient AUCUN nœud du réseau est rattaché au "
                 "secteur du nœud le plus proche, mais seulement jusqu'à cette distance. "
                 "Au-delà, personne ne le desservant (terrain agricole, désert), il "
                 "n'entre dans aucun territoire : fini les secteurs qui débordent sur des "
                 "kilomètres de vide. 0 = aucune borne (comportement precedent)"),
            ]),
            ("Consolidation secteurs", "CONSOLIDATION_SECTEURS", [
                ("CONSOLIDER_ZONES_EN_SECTEURS",
                 "Sous-zones de transport = attribut, pas secteur", "bool", 0, 1, 0, "",
                 "Recommandé. Un bloc de conduites de distribution isolé du reste de son "
                 "secteur par une conduite de transport reste une SOUS-ZONE de sous-comptage "
                 "(champ 'sous_zones' de secteurs_hydrauliques_topo) au lieu de devenir un "
                 "secteur à part entière : une DMA se compte en centaines à milliers de "
                 "branchements (IWA DMA Guidance Notes 2007, AWWA M36), pas en 2 nœuds. "
                 "Décoché = comportement anterieur, qui produisait des centaines de secteurs"),
                ("ALIGNER_SUR_SECTEURS_REFERENCES",
                 "Aligner le socle topologique sur secteurs_hydrauliques", "bool", 0, 1, 0, "",
                 "Recommandé. Le socle reprend, conduite par conduite (couche "
                 "conduites_sectorisees, champs pipe_id/secteur_id — aucune opération "
                 "géométrique), la partition hydraulique de record calculée par le pipeline "
                 "moteur hydraulique (pression, capacité, plages d'abonnés, altimétrie) : "
                 "secteurs_hydrauliques_topo compte alors EXACTEMENT autant de secteurs que "
                 "secteurs_hydrauliques"),
            ]),
            ("Multi-itérations", "MULTI_ITERATIONS", [
                ("NB_ITERATIONS_INTERNES",    "Itérations internes / cycle",  "int",   1,  20,   0, "",  "Nombre d'itérations par cycle adaptatif"),
                ("SEED_BASE",                 "Graine aléatoire",             "int",   0,  9999, 0, "",  "Graine de base pour la reproductibilité"),
                ("VARIATION_PARAM_PCT",       "Variation paramètres (%)",     "float", 0,  0.5,  2, "",  "Amplitude de variation des paramètres entre itérations"),
                ("POIDS_SCORE_PRESSION",      "Poids score pression",         "float", 0.1,100, 1, "",  "Poids de la composante pression dans le score"),
                ("POIDS_SCORE_CAPACITE",      "Poids score capacité",         "float", 0.1,100, 1, "",  "Poids de la composante capacité dans le score"),
                ("POIDS_SCORE_FRAGMENTATION", "Poids score fragmentation",    "float", 0,  5000, 0, "",  "Poids de la composante fragmentation dans le score"),
            ]),
            ("Boucle adaptative", "BOUCLE_ADAPTATIVE", [
                ("MAX_CYCLES_CONFORMITE",          "Cycles max",                    "int",   1, 20,   0, "",  "Nombre max de cycles de la boucle adaptative"),
                ("FACTEUR_DURCISSEMENT_PENALITE",  "Facteur durcissement",          "float", 1, 3,    2, "",  "Facteur multiplicatif des pénalités en cas de stagnation"),
                ("SEUIL_REDUCTION_VIOLATIONS_PCT", "Seuil réduction violations (%)", "float",0, 20,   1, "%", "Seuil en dessous duquel on considère qu'il y a stagnation"),
                ("TAUX_CIBLE_CAPACITE_MAX",        "Taux cible capacité max",       "float", 0.5,1,  2, "",  "Taux d'utilisation au-delà duquel la redistribution s'active"),
                ("MAX_DURCISSEMENTS_CONSECUTIFS",  "Max durcissements consécutifs", "int",   1, 20,  0, "",  "Nombre max de durcissements avant arrêt anticipé"),
                ("ACTIVER_REDISTRIBUTION_FORCEE",  "Redistribution forcée",         "bool",  0, 1,   0, "",  "Active la redistribution forcée des capacités"),
                ("ACTIVER_CORRECTION_SURPRESSION", "Correction surpressions",       "bool",  0, 1,   0, "",  "Active la correction PRV des surpressions"),
            ]),
            ("Redistribution sous-arbre", "REDISTRIBUTION", [
                ("MAX_SAUTS_REDISTRIBUTION",              "Sauts max redistribution",     "int",   1, 10,   0, "",  "Nombre max de sauts pour chercher un secteur receveur"),
                ("TAILLE_MAX_SOUS_ARBRE_REDISTRIBUTION",  "Taille max sous-arbre",        "int",   10,5000, 0, "ab","Taille maximale d'un sous-arbre déplaçable d'un coup"),
                ("MAX_TRANSFERTS_REDIS",                  "Max transferts redistribution", "int",  10,10000,0, "",  "Nombre max total de transferts par passe de redistribution"),
                ("ACTIVER_REDISTRIBUTION_SOUS_ARBRE",     "Activer redistribution sous-arbre","bool",0,1,  0, "",  "Permet de déplacer des sous-arbres entiers (pas seulement les feuilles)"),
                ("ACTIVER_PLAFOND_ABONNES_MAX",           "Activer plafond abonnés max",  "bool",  0, 1,   0, "",  "Bloque l'affectation si le secteur dépasse ABONNES_MAX"),
            ]),
            ("Recalibration côtes piézo", "RECALIBRATION", [
                ("ACTIVER_RECALIBRATION_COTE",          "Activer recalibration",             "bool",  0,  1,   0, "",  "Recalibre automatiquement les côtes piézométriques"),
                ("MAX_AJUSTEMENT_COTE_M",               "Ajustement max côte (m)",           "float", 0,  50,  1, "m", "Amplitude maximale d'ajustement d'une côte"),
                ("SEUIL_PCT_VIOLATION_RECALIBRATION",   "Seuil % violations pour recalibrage","float",0,  1,   2, "",  "Part des nœuds hors plage qui déclenche une recalibration"),
                ("SEUIL_ECART_TOPO_M",                  "Seuil écart topo (m)",              "float", 10, 200, 1, "m", "Écart de pression au-delà duquel on signale SOUS_ZONAGE_REQUIS"),
            ]),
            ("Correction forcée", "CORRECTION_FORCEE", [
                ("ACTIVER_CORRECTION_FORCEE",   "Activer correction forcée", "bool", 0, 1,  0, "",  "Active les itérations de correction des violations résiduelles"),
                ("MAX_ITER_CORRECTION_FORCEE",  "Itérations max",            "int",  5, 200, 0, "",  "Nombre max d'itérations de correction forcée"),
            ]),
            ("Connexité", "CONNEXITE", [
                ("ACTIVER_GARANTIE_CONNEXITE",           "Garantir la connexité",       "bool", 0, 1, 0, "",  "Corrige les nœuds déconnectés de leur source"),
                ("MAX_PASSES_GARANTIE_CONNEXITE_FINALE", "Passes max connexité finale", "int",  1, 10,0, "",  "Nombre de passes de correction connexité en fin de pipeline"),
            ]),
            ("Altimétrique avancé", "ALTIMETRIQUE", [
                ("MARGE_BASSE_AVANT_COUPURE_M", "Marge basse avant coupure (m)", "float", 0, 20, 1, "m", "Marge en dessous de PRESSION_MIN avant de déclencher une coupure"),
            ]),
            ("Validation solveur WNTR", "WNTR_VALIDATION", [
                ("VALIDER_PRESSION_WNTR", "Valider les pressions finales avec WNTR", "bool", 0, 1, 0, "",
                 "Reconstruit le réseau final et compare les pressions avec un solveur WNTR indépendant. Nécessite le paquet wntr dans Python de QGIS."),
                ("FERMER_CONDUITES_FRONTIERES_WNTR", "Fermer les frontières dans WNTR", "bool", 0, 1, 0, "",
                 "Ferme les conduites inter-secteurs dans le modèle solveur, comme les vannes d'isolement proposées."),
                ("RUGOSITE_WNTR", "Rugosité WNTR", "float", 50, 200, 1, "", "Coefficient de rugosité utilisé uniquement par le modèle WNTR."),
            ]),
            ("Vannes de sectorisation", "VANNES_SECTORISATION", [
                ("AJOUTER_VANNES_SECTORISATION", "Vanne sur chaque conduite frontière", "bool", 0, 1, 0, "",
                 "Crée la couche vannes_sectorisation avec une vanne d'isolement par conduite reliant deux secteurs, y compris les conduites parallèles."),
            ]),
        ]

        for titre_section, section_key, params in SECTIONS:
            layout.addWidget(_titre_section(titre_section))
            layout.addWidget(_separateur())
            section_data = self._config.get(section_key, {})
            gl = QGridLayout()
            gl.setColumnMinimumWidth(1, 130)
            for row, param in enumerate(params):
                cle, label, typ, mn, mx, dec, sfx, tooltip = param
                val = section_data.get(cle, _DEFAUT_SI_SECTION_ABSENTE.get(cle, mn))
                if typ == "bool":
                    widget = QCheckBox()
                    widget.setChecked(bool(val))
                elif typ == "int":
                    widget = _spinbox_int(val, int(mn), int(mx), sfx)
                else:
                    widget = _spinbox_double(val, mn, mx, dec, sfx)
                widget.setToolTip(tooltip)
                lbl = QLabel(label + " :")
                lbl.setToolTip(tooltip)
                gl.addWidget(lbl, row, 0)
                gl.addWidget(widget, row, 1)
                self._widgets[cle] = widget
            layout.addLayout(gl)
            layout.addItem(__import__('qgis.PyQt.QtWidgets', fromlist=['QSpacerItem']).QSpacerItem(0, 6))

        layout.addStretch()
        scroll.setWidget(inner)
        main = QVBoxLayout(self)
        main.setContentsMargins(0, 0, 0, 0)
        main.addWidget(scroll)

    def lire_valeurs(self):
        vals = {}
        for cle, widget in self._widgets.items():
            if isinstance(widget, QDoubleSpinBox):
                vals[cle] = widget.value()
            elif isinstance(widget, QSpinBox):
                vals[cle] = widget.value()
            elif isinstance(widget, QCheckBox):
                vals[cle] = widget.isChecked()
        return vals


# ---------------------------------------------------------------------------
# Onglet AIDE (glossaire hydraulique)
# ---------------------------------------------------------------------------

_AIDE_HTML = """
<style>
  h2 { color:#1a5276; margin-top:14px; margin-bottom:4px; }
  h3 { color:#2471a3; margin-top:10px; margin-bottom:2px; }
  p, li { font-size:10.5pt; }
  table { border-collapse:collapse; margin:6px 0; }
  td, th { border:1px solid #ccc; padding:4px 8px; vertical-align:top; font-size:10pt; }
  th { background-color:#eaf2f8; text-align:left; }
  code { background-color:#f4f6f6; padding:1px 4px; border-radius:2px; }
</style>

<h2>Pression, altitude, côte piézométrique</h2>
<table>
<tr><th>Notion</th><th>Signification</th></tr>
<tr><td><b>Altitude (Z)</b></td><td>Cote du sol au point considéré (issue du MNT).</td></tr>
<tr><td><b>Côte piézométrique</b></td><td>Niveau auquel l'eau « monterait » naturellement dans une colonne
verticale ouverte à ce point du réseau. C'est la « ligne de charge » : elle dépend du niveau d'eau au
réservoir source, moins les pertes de charge accumulées jusqu'au point.</td></tr>
<tr><td><b>Pression statique (P)</b></td><td><code>P = côte piézométrique − altitude</code>, exprimée en
mètres de colonne d'eau (1 bar ≈ 10 m). C'est la pression ressentie par l'abonné.</td></tr>
<tr><td><b>Écart</b></td><td>Différence, en valeur absolue, entre une valeur calculée et un seuil de
référence (min/max, ou entre deux secteurs). Un « écart hors plage » = la pression sort de la fourchette
acceptable.</td></tr>
</table>

<h2>Seuils de pression (onglet Essentiels → « Plages de pression »)</h2>
<table>
<tr><th>Champ</th><th>Rôle</th></tr>
<tr><td><code>PRESSION_MIN_M</code></td><td>Pression minimale acceptable en tout point (ex. 10 m = 1 bar).</td></tr>
<tr><td><code>PRESSION_MAX_M</code></td><td>Pression maximale tolérée (ex. 60 m = 6 bar), au-delà : risque de casse.</td></tr>
<tr><td><code>PRESSION_CIBLE_M</code></td><td>Pression visée lors du réglage d'un point de régulation.</td></tr>
</table>

<h2>Scission altimétrique (le cœur de la build initial)</h2>
<p><b>Le problème :</b> découper un secteur uniquement par nombre d'abonnés peut produire une zone qui
s'étend sur un dénivelé de 70 à 180 m. Une seule côte piézométrique ne peut pas donner une pression
correcte à la fois en bas et en haut d'un tel dénivelé (pressions négatives observées : -47 m, -77 m).</p>
<p><b>Le principe :</b> avant même de découper par effectif, le pipeline coupe chaque branche dès qu'elle
sortirait de la bande de pression utile. Le point de coupure devient un point de régulation virtuel
(futur PRV ou réservoir relais).</p>
<table>
<tr><th>Champ</th><th>Rôle</th></tr>
<tr><td><code>ACTIVER_SCISSION_ALTIMETRIQUE</code></td><td>Active/désactive cette passe.</td></tr>
<tr><td><code>BANDE_PRESSION_UTILE_M</code></td><td>Largeur de la fourchette de pression exploitable par
palier (ex. 45 m) avant de déclencher une coupure.</td></tr>
<tr><td><code>MARGE_BASSE_AVANT_COUPURE_M</code></td><td>Tolérance sous PRESSION_MIN avant de couper.</td></tr>
<tr><td><code>ECART_ALT_MAX_FUSION_M</code></td><td>Écart d'altitude max toléré pour fusionner deux
secteurs sous-effectifs sans recréer le problème.</td></tr>
</table>

<h2>PRV et surpresseurs</h2>
<table>
<tr><th>Terme</th><th>Signification</th></tr>
<tr><td><b>PRV</b></td><td>Vanne réductrice de pression : réduit une pression amont trop élevée à une
valeur réglée en aval.</td></tr>
<tr><td><b>Surpresseur</b></td><td>Pompe qui augmente la pression quand elle est insuffisante en aval.</td></tr>
<tr><td><b>Conduite frontalière</b></td><td>Conduite reliant deux nœuds de secteurs différents — c'est là
qu'on évalue s'il faut poser un PRV ou un surpresseur.</td></tr>
<tr><td><code>DN_MIN_PRV</code> / <code>DN_MIN_SURPRESSEUR</code></td><td>Diamètre minimal de la conduite
pour y envisager un équipement.</td></tr>
</table>

<h2>Étages et bâtiments</h2>
<p>Deux façons de détecter le nombre de niveaux desservis par un abonné, dans l'ordre de priorité :</p>
<ol>
<li><b>Champs explicites</b> (onglet Essentiels → « Champs attributaires ») : si votre table
<i>abonné</i> a des colonnes <code>bloc</code> / <code>etage</code> renseignées, elles sont utilisées
directement.</li>
<li><b>Repli géométrique</b> : pour les abonnés sans valeur, les points superposés dans une même maille
(<code>GRILLE_ETAGE_M</code>) sont regroupés et numérotés en niveaux 0, 1, 2… jusqu'à
<code>BLOC_MAX_NIVEAUX</code>.</li>
</ol>
<p>La pression minimale exigée au nœud qui dessert un niveau N est relevée :
<code>PRESSION_MIN_M + N × HAUTEUR_ETAGE_M</code>.</p>

<h2>Découpage en secteurs</h2>
<table>
<tr><th>Champ</th><th>Rôle</th></tr>
<tr><td><code>ABONNES_MIN/MAX_PAR_SECTEUR</code></td><td>Bornes acceptables du nombre d'abonnés par secteur.</td></tr>
<tr><td><code>ABONNES_IDEAL_PAR_SECTEUR</code></td><td>Taille visée en priorité.</td></tr>
<tr><td><code>TAILLE_MIN_COUPURE_AB</code></td><td>Taille minimale pour qu'un morceau devienne un secteur
autonome lors d'une coupure.</td></tr>
</table>

<h2>Vitesses de conception (paramètres configurables)</h2>
<table>
<tr><th>Champ</th><th>Rôle</th></tr>
<tr><td><code>VITESSE_MIN/MAX_M_S</code></td><td>Fourchette de vitesse d'eau acceptable dans les conduites.
Trop lent → stagnation/dépôts ; trop rapide → usure, bruit, coups de bélier.</td></tr>
<tr><td><code>DN_MIN_CONTROLE_VITESSE</code></td><td>Diamètre à partir duquel on contrôle la vitesse
(les petites antennes terminales sont exclues, normalement lentes, ce n'est pas un défaut).</td></tr>
</table>

<h2>Habillage cartographique (polygones) : transport vs distribution</h2>
<table>
<tr><th>Champ</th><th>Rôle</th></tr>
<tr><td><code>SEUIL_DN_TRANSPORT_MM</code></td><td>Diamètre à partir duquel une conduite est classée
TRANSPORT/adduction plutôt que DISTRIBUTION. Seules les conduites de DISTRIBUTION sont bufferisées
pour former le polygone d'un secteur ; les conduites de TRANSPORT restent affichées en ligne seule
(couche <code>conduites_transport_topo</code>) car elles traversent souvent des zones non desservies.
<b>Symptôme si trop bas :</b> des conduites qui devraient desservir un quartier ressortent hors de
tout secteur sur la carte. Ajuster ce seuil (pas <code>DN_MIN_CONTROLE_VITESSE</code>, qui ne sert
qu'au contrôle de vitesse) pour englober la plage réelle de diamètres de votre réseau.</td></tr>
<tr><td><code>LARGEUR_TAMPON_CONDUITE_M</code></td><td>Demi-largeur du buffer appliqué aux conduites
de distribution retenues pour construire le polygone.</td></tr>
<tr><td><code>ZONE_MIN_NOEUDS</code></td><td>Taille minimale (en nœuds) pour qu'un bloc de distribution
isolé par une conduite de transport devienne sa propre zone plutôt que d'être rattaché à la zone la
plus proche.</td></tr>
</table>

<h2>Sorties : différence entre les deux couches de polygones</h2>
<table>
<tr><th>Couche</th><th>Ce qu'elle représente</th></tr>
<tr><td><code>secteurs_hydrauliques</code></td><td>Corridor : buffer de <code>LARGEUR_CORRIDOR_M</code>
autour des conduites du secteur. Sert au contrôle interne de connexité (fragmentation).</td></tr>
<tr><td><code>secteurs_limites</code></td><td>Vraie limite de secteur : tessellation de Voronoï sur
les nœuds du réseau, fusionnée par secteur. Les secteurs voisins se touchent exactement à la frontière,
sans marge artificielle.</td></tr>
</table>

<p style="color:#7f8c8d; font-size:9pt; margin-top:16px;">Astuce : chaque champ de configuration a aussi
une info-bulle — survolez son libellé ou son champ de saisie avec la souris pour un rappel rapide.</p>
"""


class OngletAide(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        visionneuse = QTextBrowser()
        visionneuse.setOpenExternalLinks(True)
        visionneuse.setHtml(_AIDE_HTML)
        layout.addWidget(visionneuse)


# ---------------------------------------------------------------------------
# Dialogue principal
# ---------------------------------------------------------------------------

class DialogueConfig(QDialog):
    def __init__(self, config_json_courant, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Configuration — Sectorisation Hydraulique")
        self.setMinimumSize(700, 620)
        self.resize(760, 680)
        self._config_json = copy.deepcopy(config_json_courant)
        self._chemin_json_courant = None
        self._construire()

    def _construire(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(12, 12, 12, 12)

        # Barre fichier JSON
        barre = QHBoxLayout()
        self._lbl_fichier = QLabel("Fichier : <défaut>")
        self._lbl_fichier.setStyleSheet("color: #555; font-style: italic;")
        btn_ouvrir = QPushButton("📂 Ouvrir JSON…")
        btn_sauver  = QPushButton("💾 Enregistrer JSON…")
        btn_reset   = QPushButton("↺ Réinitialiser")
        btn_ouvrir.setToolTip("Charger une configuration JSON existante")
        btn_sauver.setToolTip("Enregistrer la configuration actuelle dans un fichier JSON")
        btn_reset.setToolTip("Revenir à la configuration par défaut")
        btn_ouvrir.clicked.connect(self._ouvrir_json)
        btn_sauver.clicked.connect(self._sauver_json)
        btn_reset.clicked.connect(self._reset_defaut)
        barre.addWidget(self._lbl_fichier, stretch=1)
        barre.addWidget(btn_ouvrir)
        barre.addWidget(btn_sauver)
        barre.addWidget(btn_reset)
        layout.addLayout(barre)
        layout.addWidget(_separateur())

        # Onglets
        self._tabs = QTabWidget()
        layers_noms = [l.name() for l in QgsProject.instance().mapLayers().values()
                       if isinstance(l, (QgsVectorLayer, QgsRasterLayer))]
        self._onglet_simple  = OngletSimple(self._config_json, layers_noms)
        self._onglet_avance  = OngletAvance(self._config_json)
        self._onglet_aide    = OngletAide()
        self._tabs.addTab(self._onglet_simple, "⚙️  Paramètres essentiels")
        self._tabs.addTab(self._onglet_avance, "🔬  Avancé (tous les paramètres)")
        self._tabs.addTab(self._onglet_aide, "❓  Aide")
        layout.addWidget(self._tabs, stretch=1)

        # Boutons OK / Annuler
        layout.addWidget(_separateur())
        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_box.button(QDialogButtonBox.Ok).setText("Appliquer")
        btn_box.button(QDialogButtonBox.Cancel).setText("Annuler")
        btn_box.accepted.connect(self._valider_et_accepter)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

    # ── Slots fichier ────────────────────────────────────────────────────────

    def _ouvrir_json(self):
        chemin, _ = QFileDialog.getOpenFileName(
            self, "Ouvrir configuration JSON", "", "JSON (*.json)")
        if not chemin:
            return
        try:
            self._config_json = charger_config_json(chemin)
            self._chemin_json_courant = chemin
            self._lbl_fichier.setText(f"Fichier : {os.path.basename(chemin)}")
            self._rafraichir_onglets()
        except Exception as e:
            QMessageBox.critical(self, "Erreur", f"Impossible de charger le fichier :\n{e}")

    def _sauver_json(self):
        chemin, _ = QFileDialog.getSaveFileName(
            self, "Enregistrer configuration JSON",
            self._chemin_json_courant or "config_sectorisation.json",
            "JSON (*.json)")
        if not chemin:
            return
        try:
            config_a_sauver = self._lire_tous_widgets()
            sauvegarder_config_json(config_a_sauver, chemin)
            self._chemin_json_courant = chemin
            self._lbl_fichier.setText(f"Fichier : {os.path.basename(chemin)}")
            QMessageBox.information(self, "Sauvegardé", f"Configuration enregistrée dans :\n{chemin}")
        except Exception as e:
            QMessageBox.critical(self, "Erreur", f"Impossible de sauvegarder :\n{e}")

    def _reset_defaut(self):
        rep = QMessageBox.question(
            self, "Réinitialiser",
            "Revenir à la configuration par défaut ?\nLes modifications non sauvegardées seront perdues.",
            QMessageBox.Yes | QMessageBox.No)
        if rep == QMessageBox.Yes:
            try:
                self._config_json = charger_config_json()
                self._chemin_json_courant = None
                self._lbl_fichier.setText("Fichier : <défaut>")
                self._rafraichir_onglets()
            except Exception as e:
                QMessageBox.critical(self, "Erreur", str(e))

    def _rafraichir_onglets(self):
        """Recrée les onglets avec la nouvelle config."""
        layers_noms = [l.name() for l in QgsProject.instance().mapLayers().values()]
        idx_courant = self._tabs.currentIndex()
        self._tabs.clear()
        self._onglet_simple = OngletSimple(self._config_json, layers_noms)
        self._onglet_avance = OngletAvance(self._config_json)
        self._onglet_aide = OngletAide()
        self._tabs.addTab(self._onglet_simple, "⚙️  Paramètres essentiels")
        self._tabs.addTab(self._onglet_avance, "🔬  Avancé (tous les paramètres)")
        self._tabs.addTab(self._onglet_aide, "❓  Aide")
        self._tabs.setCurrentIndex(idx_courant)

    # ── Lecture / validation ─────────────────────────────────────────────────

    def _lire_tous_widgets(self):
        """Fusionne les valeurs des deux onglets et retourne le dict JSON imbriqué."""
        vals_simple  = self._onglet_simple.lire_valeurs()
        vals_avance  = self._onglet_avance.lire_valeurs()
        vals_combines = {**vals_avance, **vals_simple}  # simple écrase avancé si doublon
        return plat_vers_json(vals_combines, self._config_json)

    def _valider_et_accepter(self):
        config_json_new = self._lire_tous_widgets()
        config_plat = json_vers_plat(config_json_new)
        erreurs = valider_config(config_plat)
        if erreurs:
            msg = "Des paramètres semblent incorrects :\n\n" + "\n".join(f"• {e}" for e in erreurs)
            rep = QMessageBox.warning(
                self, "Paramètres suspects", msg + "\n\nAppliquer quand même ?",
                QMessageBox.Yes | QMessageBox.No)
            if rep == QMessageBox.No:
                return
        self._config_json = config_json_new
        self.accept()

    def config_json_resultat(self):
        """Retourne le dict JSON imbriqué après validation."""
        return self._config_json
