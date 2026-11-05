# -*- coding: utf-8 -*-
"""
Panneau latéral principal - Sectorisation Hydraulique
Dock panel QGIS : résumé config, lancement, journal de sortie.
"""

import os
import sys
import traceback
from datetime import datetime

from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout,
    QPushButton, QLabel, QTextEdit, QProgressBar,
    QGroupBox, QFrame
)
from qgis.PyQt.QtCore import Qt, QThread, pyqtSignal, QObject
from qgis.PyQt.QtGui import QFont, QTextCursor

from qgis.core import Qgis, QgsProject, QgsVectorLayer
from qgis.utils import iface

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from ..core.config_manager import json_vers_plat
from .dialog_config import DialogueConfig


def _separateur():
    line = QFrame()
    line.setFrameShape(QFrame.HLine)
    line.setFrameShadow(QFrame.Sunken)
    return line


# ---------------------------------------------------------------------------
# Worker thread  (exécute le pipeline sans bloquer l'UI)
# ---------------------------------------------------------------------------

class WorkerSignals(QObject):
    log       = pyqtSignal(str)        # message de log
    progres   = pyqtSignal(int, str)   # (valeur 0-100, étape)
    termine   = pyqtSignal(bool, str)  # (succès, message_final)
    # FIX-THREAD-SAFETY : liste de CoucheDifferee (cf.
    # core/couche_differee.py -- 'couches_en_attente' retourne par
    # pipeline.executer / executer_v68_topologique.executer), a
    # materialiser en vrais QgsVectorLayer PUIS enregistrer dans le projet
    # QGIS depuis le thread PRINCIPAL uniquement -- jamais depuis ce
    # thread de calcul. Voir PanneauPrincipal._materialiser_couche /
    # _ajouter_couches_dans_projet pour le detail du crash que ceci
    # corrige.
    couches_pretes = pyqtSignal(list)


class PipelineWorker(QThread):
    def __init__(self, config_plat):
        super().__init__()
        self.config_plat = config_plat
        self.signals = WorkerSignals()
        self._annuler = False

    def run(self):
        try:
            self._executer()
        except Exception as e:
            self.signals.log.emit(f"\n[ERREUR CRITIQUE] {e}\n{traceback.format_exc()}")
            self.signals.termine.emit(False, str(e))

    def annuler(self):
        self._annuler = True

    def _executer(self):
        """
        Importe et exécute le pipeline complet (moteur hydraulique).
        Le pipeline est importé dynamiquement depuis core/pipeline.py.
        """
        self.signals.log.emit("=" * 60)
        self.signals.log.emit("Sectorisation Hydraulique - développée par Kouidri Sofiane")
        self.signals.log.emit(f"Démarrage : {datetime.now().strftime('%H:%M:%S')}")
        self.signals.log.emit("=" * 60)

        self.signals.progres.emit(2, "Chargement des couches…")
        self.signals.log.emit("\nChargement des couches QGIS…")


        # Redirection du print() vers le signal log
        import builtins
        _print_orig = builtins.print
        def _print_redirige(*args, **kwargs):
            msg = " ".join(str(a) for a in args)
            self.signals.log.emit(msg)
            _print_orig(*args, **kwargs)
        builtins.print = _print_redirige

        try:
            import importlib
            from ..core import pipeline as pipeline_module
            importlib.reload(pipeline_module)

            etapes = [
                (5,  "Reconstruction du réseau…"),
                (15, "Rattachement des abonnés…"),
                (20, "Rattachement des réservoirs…"),
                (25, "Voronoï…"),
                (30, "Boucle adaptative…"),
                (70, "Scission altimétrique…"),
                (80, "Fusion sous-effectifs…"),
                (85, "Redistribution finale…"),
                (90, "Calcul des pressions…"),
                (95, "Création des couches de sortie…"),
            ]

            def on_etape(idx):
                if idx < len(etapes):
                    pct, msg = etapes[idx]
                    self.signals.progres.emit(pct, msg)

            resultat = pipeline_module.executer(
                self.config_plat, on_etape, lambda: self._annuler)

            # FIX-THREAD-SAFETY : les couches de sortie ont ete
            # construites en memoire par le pipeline mais PAS encore
            # ajoutees au projet (cf. core/pipeline.py::_ajouter_couche).
            # On les fait enregistrer par le thread principal AVANT
            # d'annoncer la fin du run, pour garantir qu'elles sont bien
            # presentes dans le projet des que l'enchainement automatique
            # vers le socle topologique  demarre.
            couches = (resultat or {}).get('couches_en_attente') or []
            if couches:
                self.signals.couches_pretes.emit(couches)

            self.signals.progres.emit(100, "Terminé")
            self.signals.termine.emit(True, "Pipeline terminé avec succès.")

        except ImportError as e:
            # Le module pipeline.py n'existe pas encore —
            # on signale clairement à l'utilisateur
            self.signals.log.emit(
                f"\n[INFO] Le module core/pipeline.py n'est pas présent.\n"
                f"Vérifiez que le dossier plugin complet a bien été copié "
                f"(le fichier core/pipeline.py doit exister).\n"
                f"(erreur : {e})"
            )
            self.signals.termine.emit(False,
                "core/pipeline.py manquant — voir le journal.")
        finally:
            builtins.print = _print_orig


class TopoWorkerSignals(QObject):
    log       = pyqtSignal(str)
    termine   = pyqtSignal(bool, str)
    # FIX-THREAD-SAFETY : voir WorkerSignals.couches_pretes ci-dessus.
    couches_pretes = pyqtSignal(list)


class TopoWorker(QThread):
    """Exécute le NOUVEAU socle topologique (core/graphe_topologique.py +
    core/polygones_topologiques.py, orchestrés par
    core/executer_topologique.py), indépendant de pipeline.py.
    Ne fait volontairement PAS pression/capacité/altimétrie -- socle de
    connectivité pure, cf. échanges precedents."""

    def __init__(self, config_plat):
        super().__init__()
        self.config_plat = config_plat
        self.signals = TopoWorkerSignals()

    def run(self):
        try:
            self._executer()
        except Exception as e:
            self.signals.log.emit(f"\n[ERREUR CRITIQUE] {e}\n{traceback.format_exc()}")
            self.signals.termine.emit(False, str(e))

    def _executer(self):
        import builtins
        _print_orig = builtins.print
        def _print_redirige(*args, **kwargs):
            msg = " ".join(str(a) for a in args)
            self.signals.log.emit(msg)
            _print_orig(*args, **kwargs)
        builtins.print = _print_redirige

        try:
            import importlib
            from ..core import graphe_topologique as gt_module
            from ..core import polygones_topologiques as pt_module
            from ..core import executer_topologique as topo_module
            importlib.reload(gt_module)
            importlib.reload(pt_module)
            importlib.reload(topo_module)

            resultat = topo_module.executer(self.config_plat)

            # FIX-THREAD-SAFETY : cf. PipelineWorker._executer plus
            # haut -- meme principe, meme raison.
            couches = (resultat or {}).get('couches_en_attente') or []
            if couches:
                self.signals.couches_pretes.emit(couches)

            self.signals.termine.emit(True, "Socle topologique  terminé avec succès.")
        except ImportError as e:
            self.signals.log.emit(
                f"\n[ERREUR] Import impossible (networkx manquant ?) : {e}\n"
                f"Installez networkx dans l'environnement Python de QGIS "
                f"(OSGeo4W Shell : python -m pip install networkx), puis "
                f"redémarrez QGIS."
            )
            self.signals.termine.emit(False, "Import échoué — voir le journal.")
        finally:
            builtins.print = _print_orig


# ---------------------------------------------------------------------------
# Panneau principal (DockWidget)
# ---------------------------------------------------------------------------

class PanneauPrincipal(QDockWidget):
    def __init__(self, iface_ref, config_json, parent=None):
        super().__init__("Sectorisation Hydraulique", parent)
        self.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self.setFeatures(
            QDockWidget.DockWidgetMovable |
            QDockWidget.DockWidgetFloatable |
            QDockWidget.DockWidgetClosable
        )
        self._iface = iface_ref
        self._config_json = config_json
        self._worker = None
        self._construire()

    # ── Construction UI ──────────────────────────────────────────────────────

    def _construire(self):
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # Titre
        titre = QLabel("🔵  Sectorisation Hydraulique")
        titre.setStyleSheet(
            "font-size: 13px; font-weight: bold; color: #1a5276; padding: 4px;"
        )
        layout.addWidget(titre)

        subtitle = QLabel("Paramètres de conception hydrauliques configurables")
        subtitle.setStyleSheet("color: #7f8c8d; font-size: 10px; padding: 0 4px 4px 4px;")
        layout.addWidget(subtitle)
        layout.addWidget(_separateur())

        # Bloc résumé config
        self._grp_config = QGroupBox("Configuration active")
        self._grp_config.setStyleSheet("QGroupBox { font-weight: bold; font-size: 10px; }")
        gl = QVBoxLayout()
        self._lbl_couche_aep    = QLabel()
        self._lbl_couche_ab     = QLabel()
        self._lbl_couche_tank   = QLabel()
        self._lbl_pression      = QLabel()
        self._lbl_abonnes       = QLabel()
        self._lbl_fichier_conf  = QLabel()
        for lbl in (self._lbl_couche_aep, self._lbl_couche_ab, self._lbl_couche_tank,
                    self._lbl_pression, self._lbl_abonnes, self._lbl_fichier_conf):
            lbl.setStyleSheet("font-size: 10px; color: #2c3e50; padding: 1px 4px;")
            lbl.setWordWrap(True)
            gl.addWidget(lbl)
        self._grp_config.setLayout(gl)
        layout.addWidget(self._grp_config)
        self._maj_resume_config()

        # Bouton Configuration
        btn_config = QPushButton("⚙️  Modifier la configuration…")
        btn_config.setToolTip("Ouvrir le dialogue de configuration (couches, paramètres)")
        btn_config.setStyleSheet("padding: 6px; font-size: 11px;")
        btn_config.clicked.connect(self._ouvrir_config)
        layout.addWidget(btn_config)

        layout.addWidget(_separateur())

        # Boutons lancement
        self._btn_lancer = QPushButton("▶  Lancer la sectorisation")
        self._btn_lancer.setToolTip(
            "Exécute le pipeline complet (pression, capacité, "
            "rattachement abonnés, scission altimétrique), PUIS enchaîne "
            "automatiquement le socle topologique dès que la "
            "sectorisation se termine avec succès."
        )
        self._btn_lancer.setStyleSheet(
            "QPushButton { background-color: #1a5276; color: white; "
            "font-weight: bold; font-size: 12px; padding: 8px; border-radius: 4px; }"
            "QPushButton:hover { background-color: #2471a3; }"
            "QPushButton:disabled { background-color: #aab7b8; }"
        )
        self._btn_lancer.clicked.connect(self._lancer_pipeline)
        layout.addWidget(self._btn_lancer)

        self._btn_annuler = QPushButton("⛔  Annuler")
        self._btn_annuler.setEnabled(False)
        self._btn_annuler.setStyleSheet("padding: 5px; color: #922b21;")
        self._btn_annuler.clicked.connect(self._annuler_pipeline)
        layout.addWidget(self._btn_annuler)

        layout.addWidget(_separateur())

        lbl_topo = QLabel(
            "Socle topologique  -- enchaîné automatiquement après "
            "'Lancer la sectorisation'. Le bouton ci-dessous ne sert qu'à le "
            "RELANCER SEUL (ex. après un ajustement de "
            "SEUIL_DN_TRANSPORT_MM), sans refaire tourner tout le calcul "
            "hydraulique. Depuis l'introduction de la consolidation, il se recale sur "
            "'secteurs_hydrauliques' : 'secteurs_hydrauliques_topo' "
            "contient exactement le même nombre de secteurs (il doit donc "
            "être relancé APRÈS la sectorisation, jamais avant)."
        )
        lbl_topo.setStyleSheet("font-size: 10px; color: #7f8c8d; padding: 2px;")
        lbl_topo.setWordWrap(True)
        layout.addWidget(lbl_topo)

        self._btn_lancer_topo = QPushButton("🔗  Relancer le socle topologique seul ")
        self._btn_lancer_topo.setToolTip(
            "Partitionnement par graphe (sans Voronoi/routes/convex hull). "
            "Ne calcule PAS pression/capacité/altimétrie -- socle de "
            "connectivité pure. Crée la couche 'secteurs_hydrauliques_topo'. "
            "Normalement enchaîné automatiquement après 'Lancer la "
            "sectorisation' -- ce bouton permet de le relancer isolément."
        )
        self._btn_lancer_topo.setStyleSheet(
            "QPushButton { background-color: #117864; color: white; "
            "font-weight: bold; font-size: 11px; padding: 7px; border-radius: 4px; }"
            "QPushButton:hover { background-color: #148f77; }"
            "QPushButton:disabled { background-color: #aab7b8; }"
        )
        self._btn_lancer_topo.clicked.connect(self._lancer_topo)
        layout.addWidget(self._btn_lancer_topo)

        # Barre de progression
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setTextVisible(True)
        self._progress.setFormat("%p%")
        self._progress.setStyleSheet(
            "QProgressBar { border: 1px solid #aaa; border-radius: 3px; height: 18px; }"
            "QProgressBar::chunk { background-color: #1a5276; border-radius: 2px; }"
        )
        layout.addWidget(self._progress)

        self._lbl_etape = QLabel("")
        self._lbl_etape.setStyleSheet("font-size: 10px; color: #555; padding: 2px;")
        self._lbl_etape.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._lbl_etape)

        layout.addWidget(_separateur())

        # Journal
        lbl_journal = QLabel("Journal d'exécution :")
        lbl_journal.setStyleSheet("font-weight: bold; font-size: 10px;")
        layout.addWidget(lbl_journal)

        self._journal = QTextEdit()
        self._journal.setReadOnly(True)
        self._journal.setFont(QFont("Courier New", 8))
        self._journal.setStyleSheet(
            "background-color: #1c2833; color: #d5d8dc; border-radius: 4px; padding: 4px;"
        )
        self._journal.setMinimumHeight(200)
        layout.addWidget(self._journal, stretch=1)

        # Bouton effacer journal
        btn_effacer = QPushButton("🗑 Effacer le journal")
        btn_effacer.setStyleSheet("font-size: 10px; padding: 3px;")
        btn_effacer.clicked.connect(self._journal.clear)
        layout.addWidget(btn_effacer)

        self.setWidget(root)

    # ── Mise à jour résumé config ────────────────────────────────────────────

    def _maj_resume_config(self):
        c = self._config_json
        couches = c.get('COUCHES', {})
        hyd = c.get('HYDRAULIQUE', {})
        ps  = c.get('PLAGES_SECTEUR', {})
        self._lbl_couche_aep.setText(f"🔹 Conduites : {couches.get('AEP_LAYER', '?')}")
        self._lbl_couche_ab.setText( f"🔹 Abonnés   : {couches.get('ABONNE_LAYER', '?')}")
        self._lbl_couche_tank.setText(f"🔹 Réservoirs : {couches.get('TANK_LAYER', '?')}")
        self._lbl_pression.setText(
            f"📊 Pression : [{hyd.get('PRESSION_MIN_M', '?')} – "
            f"{hyd.get('PRESSION_MAX_M', '?')}] m  (cible {hyd.get('PRESSION_CIBLE_M', '?')} m)"
        )
        self._lbl_abonnes.setText(
            f"👥 Abonnés/secteur : [{ps.get('ABONNES_MIN_PAR_SECTEUR', '?')} – "
            f"{ps.get('ABONNES_MAX_PAR_SECTEUR', '?')}]  idéal {ps.get('ABONNES_IDEAL_PAR_SECTEUR', '?')}"
        )
        alt = c.get('ALTIMETRIQUE', {})
        actif = "✅" if alt.get('ACTIVER_SCISSION_ALTIMETRIQUE', True) else "❌"
        self._lbl_fichier_conf.setText(
            f"{actif} Scission altimétrique  |  "
            f"Bande {alt.get('BANDE_PRESSION_UTILE_M', '?')} m"
        )

    # ── Ouverture config ─────────────────────────────────────────────────────

    def _ouvrir_config(self):
        dlg = DialogueConfig(self._config_json, parent=self)
        if dlg.exec_() == DialogueConfig.Accepted:
            self._config_json = dlg.config_json_resultat()
            self._maj_resume_config()
            self._log("Configuration mise à jour.")

    # ── Pipeline ─────────────────────────────────────────────────────────────

    def _lancer_pipeline(self):
        """Lance le pipeline hydraulique complet (pression, capacité,
        rattachement des abonnés, scission altimétrique). Le socle
        topologique  est TOUJOURS enchaîné automatiquement juste après,
        dès que ce pipeline se termine avec succès -- cf. _pipeline_termine.
        Le bouton dédié 'Lancer le socle topologique' reste disponible pour
        le relancer seul (ex. après avoir ajusté SEUIL_DN_TRANSPORT_MM) sans
        refaire tourner tout le calcul hydraulique."""
        self._journal.clear()
        self._progress.setValue(0)
        self._lbl_etape.setText("")
        self._btn_lancer.setEnabled(False)
        self._btn_lancer_topo.setEnabled(False)
        self._btn_annuler.setEnabled(True)

        config_plat = json_vers_plat(self._config_json)
        self._worker = PipelineWorker(config_plat)
        self._worker.signals.log.connect(self._log)
        self._worker.signals.progres.connect(self._maj_progres)
        self._worker.signals.couches_pretes.connect(self._ajouter_couches_dans_projet)
        self._worker.signals.termine.connect(self._pipeline_termine)
        self._worker.start()

    def _annuler_pipeline(self):
        if self._worker and self._worker.isRunning():
            self._worker.annuler()
            self._log("\n[Annulation demandée — en attente de la fin de l'étape courante…]")
            self._btn_annuler.setEnabled(False)

    def _pipeline_termine(self, succes, message):
        self._btn_annuler.setEnabled(False)
        if succes:
            self._progress.setValue(100)
            self._lbl_etape.setText("✅ Sectorisation terminée")
            self._log(f"\n✅ {message}")
            iface.messageBar().pushMessage(
                "Sectorisation", message, level=Qgis.Success, duration=5)
            # Enchaînement AUTOMATIQUE et TOUJOURS effectué : le socle
            # topologique  démarre dès que la sectorisation (moteur hydraulique, avec
            # ses calculs hydrauliques) s'est terminée avec succès. On ne
            # réactive pas les boutons entre les deux -- l'utilisateur voit
            # une seule séquence continue dans le journal.
            self._log(
                "\n" + "=" * 60 +
                "\nEnchaînement automatique : lancement du socle "
                "topologique …\n" + "=" * 60
            )
            self._lancer_topo(chaine_depuis_pipeline=True)
        else:
            self._btn_lancer.setEnabled(True)
            self._btn_lancer_topo.setEnabled(True)
            self._lbl_etape.setText("❌ Erreur")
            self._log(f"\n❌ {message}")
            self._log(
                "\n[INFO] Le socle topologique  n'est PAS enchaîné "
                "automatiquement car la sectorisation a échoué. Corrigez "
                "l'erreur ci-dessus puis relancez."
            )
            iface.messageBar().pushMessage(
                "Sectorisation", f"Erreur : {message}", level=Qgis.Critical, duration=8)

    def _lancer_topo(self, chaine_depuis_pipeline=False):
        """Lance le socle topologique . Si chaine_depuis_pipeline=True
        (cas normal, appelé automatiquement depuis _pipeline_termine), le
        journal existant de la sectorisation est conservé -- on ajoute la
        suite à la même trace au lieu de l'effacer. Si appelé directement
        par clic sur le bouton dédié (chaine_depuis_pipeline=False), le
        socle topologique peut être relancé seul (ex. après un ajustement
        de SEUIL_DN_TRANSPORT_MM / ZONE_MIN_NOEUDS) sans refaire tourner le
        calcul hydraulique complet."""
        self._chaine_depuis_pipeline = chaine_depuis_pipeline
        if not chaine_depuis_pipeline:
            self._journal.clear()
        self._lbl_etape.setText("")
        self._btn_lancer.setEnabled(False)
        self._btn_lancer_topo.setEnabled(False)

        config_plat = json_vers_plat(self._config_json)
        self._worker_topo = TopoWorker(config_plat)
        self._worker_topo.signals.log.connect(self._log)
        self._worker_topo.signals.couches_pretes.connect(self._ajouter_couches_dans_projet)
        self._worker_topo.signals.termine.connect(self._topo_termine)
        self._worker_topo.start()

    # ── Enregistrement des couches (thread principal UNIQUEMENT) ───────────

    def _materialiser_couche(self, spec):
        """Construit le VRAI QgsVectorLayer a partir d'un CoucheDifferee
        (cf. core/couche_differee.py) produit par le pipeline. Doit
        s'executer UNIQUEMENT dans le thread principal.

        FIX-THREAD-SAFETY : un build precedent differait deja l'ajout au projet
        au thread principal, mais construisait encore le QgsVectorLayer
        lui-meme (un QObject) dans le thread de calcul. Comme un
        QgsVectorLayer garde l'affinite du thread qui l'a cree, et que ce
        QThread se termine presque aussitot apres avoir emis le signal
        'couches_pretes', l'objet devenait invalide des que le thread
        principal cherchait a le manipuler (access violation observee sur
        couche.name(), dans cette meme fonction). Desormais AUCUN
        QgsVectorLayer n'est jamais construit ailleurs qu'ici : le thread
        de calcul ne manipule que des types-valeur QGIS (QgsField,
        QgsFeature, QgsGeometry), qui n'ont pas d'affinite de thread et
        peuvent circuler librement.
        """
        uri = f"{spec.type_geom}?crs={spec.crs}"
        couche = QgsVectorLayer(uri, spec.nom, "memory")
        provider = couche.dataProvider()
        provider.addAttributes(spec.champs)
        couche.updateFields()
        if spec.feats:
            provider.addFeatures(spec.feats)
        couche.updateExtents()
        return couche

    def _ajouter_couches_dans_projet(self, couches):
        """Enregistre dans le projet QGIS les couches produites par le
        pipeline (thread de calcul), en les materialisant d'abord (cf.
        _materialiser_couche). Appelee via le signal 'couches_pretes',
        donc toujours executee ICI, dans le thread principal -- c'est le
        seul endroit du plugin qui doit toucher QgsVectorLayer/QgsProject
        pour ces couches de sortie. Voir _materialiser_couche pour le
        detail du bug que ceci corrige.
        """
        project = QgsProject.instance()
        for spec in couches:
            couche = self._materialiser_couche(spec)
            nom = couche.name()
            anciennes = project.mapLayersByName(nom)
            if anciennes:
                try:
                    snap_config = project.snappingConfig()
                    snap_config.removeLayers(anciennes)
                    project.setSnappingConfig(snap_config)
                except Exception as e:
                    self._log(
                        f"  (avertissement : nettoyage snapping impossible "
                        f"pour '{nom}' : {e})")
                project.removeMapLayers([l.id() for l in anciennes])
            project.addMapLayer(couche)

    def _topo_termine(self, succes, message):
        self._btn_lancer.setEnabled(True)
        self._btn_lancer_topo.setEnabled(True)
        if succes:
            self._lbl_etape.setText("✅ Sectorisation + socle topologique terminés")
            self._log(f"\n✅ {message}")
            iface.messageBar().pushMessage(
                "Socle topologique", message, level=Qgis.Success, duration=5)
        else:
            self._lbl_etape.setText("❌ Erreur (socle topologique)")
            self._log(f"\n❌ {message}")
            iface.messageBar().pushMessage(
                "Socle topologique", f"Erreur : {message}", level=Qgis.Critical, duration=8)

    def _maj_progres(self, valeur, etape):
        self._progress.setValue(valeur)
        self._lbl_etape.setText(etape)

    def _log(self, message):
        curseur = self._journal.textCursor()
        curseur.movePosition(QTextCursor.End)
        self._journal.setTextCursor(curseur)
        self._journal.insertPlainText(message + "\n")
        self._journal.ensureCursorVisible()

    # ── Accesseurs ───────────────────────────────────────────────────────────

    def set_config_json(self, config_json):
        self._config_json = config_json
        self._maj_resume_config()
