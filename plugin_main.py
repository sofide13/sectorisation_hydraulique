# -*- coding: utf-8 -*-
"""
Point d'entrée principal de l'extension QGIS - Sectorisation Hydraulique
"""

import os
import sys

from qgis.PyQt.QtWidgets import QAction, QToolBar, QFileDialog, QMessageBox
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtCore import Qt, QSettings

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))

__author__ = "Kouidri Sofiane"
__copyright__ = "Copyright (c) 2026 Kouidri Sofiane"

from .core.config_manager import charger_config_json, sauvegarder_config_json


class SectorisationHydrauliquePlugin:
    """Classe principale de l'extension QGIS."""

    def __init__(self, iface):
        self.iface = iface
        self._panneau = None
        self._config_json = None
        self._actions = []
        self._toolbar = None
        self._chemin_config_courant = None

    # ── Cycle de vie QGIS ────────────────────────────────────────────────────

    def initGui(self):
        """Appelé par QGIS au chargement de l'extension."""
        self._charger_config_initiale()
        self._creer_toolbar()
        self._creer_menu()
        self._creer_panneau()

    def unload(self):
        """Appelé par QGIS au déchargement de l'extension."""
        # Supprimer les actions du menu
        for action in self._actions:
            self.iface.removePluginMenu("Sectorisation Hydraulique", action)
            self.iface.removeToolBarIcon(action)

        # Supprimer la toolbar
        if self._toolbar:
            self.iface.mainWindow().removeToolBar(self._toolbar)
            self._toolbar = None

        # Fermer et supprimer le panneau
        if self._panneau:
            self.iface.removeDockWidget(self._panneau)
            self._panneau.deleteLater()
            self._panneau = None

    # ── Config initiale ──────────────────────────────────────────────────────

    def _charger_config_initiale(self):
        """
        Charge la configuration :
        1) Depuis le chemin sauvegardé dans QSettings (session précédente)
        2) Sinon depuis le fichier par défaut
        """
        settings = QSettings()
        chemin_sauvegarde = settings.value(
            "sectorisation_hydraulique/dernier_fichier_config", "")

        if chemin_sauvegarde and os.path.exists(chemin_sauvegarde):
            try:
                self._config_json = charger_config_json(chemin_sauvegarde)
                self._chemin_config_courant = chemin_sauvegarde
                return
            except Exception:
                pass  # On retombe sur le défaut

        self._config_json = charger_config_json()  # fichier par défaut
        self._chemin_config_courant = None

    # ── Toolbar ──────────────────────────────────────────────────────────────

    def _creer_toolbar(self):
        self._toolbar = QToolBar("Sectorisation Hydraulique", self.iface.mainWindow())
        self._toolbar.setObjectName("SectorisationHydrauliqueToolBar")
        self.iface.mainWindow().addToolBar(Qt.TopToolBarArea, self._toolbar)

        # Icône panneau
        icon_panneau = self._icone("icon.png")
        action_panneau = QAction(icon_panneau, "Ouvrir / fermer le panneau", self.iface.mainWindow())
        action_panneau.setToolTip("Afficher / masquer le panneau de sectorisation hydraulique")
        action_panneau.triggered.connect(self._toggle_panneau)
        self._toolbar.addAction(action_panneau)
        self._actions.append(action_panneau)

        self._toolbar.addSeparator()

        # Icône config
        icon_config = self._icone("icon_config.png")
        action_config = QAction(icon_config, "Configuration…", self.iface.mainWindow())
        action_config.setToolTip("Ouvrir le dialogue de configuration")
        action_config.triggered.connect(self._ouvrir_config)
        self._toolbar.addAction(action_config)
        self._actions.append(action_config)

        # Charger JSON
        action_charger = QAction("📂 Charger config JSON…", self.iface.mainWindow())
        action_charger.setToolTip("Charger un fichier de configuration JSON")
        action_charger.triggered.connect(self._charger_json)
        self._toolbar.addAction(action_charger)
        self._actions.append(action_charger)

        # Sauver JSON
        action_sauver = QAction("💾 Sauver config JSON…", self.iface.mainWindow())
        action_sauver.setToolTip("Sauvegarder la configuration courante dans un fichier JSON")
        action_sauver.triggered.connect(self._sauver_json)
        self._toolbar.addAction(action_sauver)
        self._actions.append(action_sauver)

        # À propos
        action_apropos = QAction("ℹ️ À propos…", self.iface.mainWindow())
        action_apropos.setToolTip("À propos de l'extension - Développée par Kouidri Sofiane")
        action_apropos.triggered.connect(self._a_propos)
        self._toolbar.addAction(action_apropos)
        self._actions.append(action_apropos)

    # ── Menu ─────────────────────────────────────────────────────────────────

    def _creer_menu(self):
        for action in self._actions:
            self.iface.addPluginToMenu("Sectorisation Hydraulique", action)

    # ── Panneau ──────────────────────────────────────────────────────────────

    def _creer_panneau(self):
        from .ui.panneau_principal import PanneauPrincipal
        self._panneau = PanneauPrincipal(
            self.iface, self._config_json,
            parent=self.iface.mainWindow()
        )
        self.iface.addDockWidget(Qt.RightDockWidgetArea, self._panneau)

    def _toggle_panneau(self):
        if self._panneau:
            self._panneau.setVisible(not self._panneau.isVisible())

    # ── Actions fichier ──────────────────────────────────────────────────────

    def _charger_json(self):
        chemin, _ = QFileDialog.getOpenFileName(
            self.iface.mainWindow(),
            "Charger configuration JSON",
            self._chemin_config_courant or "",
            "JSON (*.json)"
        )
        if not chemin:
            return
        try:
            self._config_json = charger_config_json(chemin)
            self._chemin_config_courant = chemin
            QSettings().setValue(
                "sectorisation_hydraulique/dernier_fichier_config", chemin)
            if self._panneau:
                self._panneau.set_config_json(self._config_json)
            self.iface.messageBar().pushMessage(
                "Config chargée", os.path.basename(chemin),
                level=0, duration=3)
        except Exception as e:
            QMessageBox.critical(
                self.iface.mainWindow(), "Erreur",
                f"Impossible de charger la configuration :\n{e}")

    def _sauver_json(self):
        chemin, _ = QFileDialog.getSaveFileName(
            self.iface.mainWindow(),
            "Sauvegarder configuration JSON",
            self._chemin_config_courant or "config_sectorisation.json",
            "JSON (*.json)"
        )
        if not chemin:
            return
        try:
            # Récupère la config la plus récente depuis le panneau si possible
            config_a_sauver = self._config_json
            if self._panneau:
                # Le panneau stocke _config_json à jour (modifié via le dialogue)
                config_a_sauver = self._panneau._config_json
            sauvegarder_config_json(config_a_sauver, chemin)
            self._chemin_config_courant = chemin
            QSettings().setValue(
                "sectorisation_hydraulique/dernier_fichier_config", chemin)
            self.iface.messageBar().pushMessage(
                "Config sauvegardée", chemin, level=0, duration=3)
        except Exception as e:
            QMessageBox.critical(
                self.iface.mainWindow(), "Erreur",
                f"Impossible de sauvegarder :\n{e}")

    def _ouvrir_config(self):
        if self._panneau:
            self._panneau._ouvrir_config()

    def _a_propos(self):
        QMessageBox.information(
            self.iface.mainWindow(),
            "À propos - Sectorisation Hydraulique",
            "<h3>Sectorisation Hydraulique</h3>"
            "<p>Sectorisation hydraulique automatisée des réseaux AEP.</p>"
            "<p><b>Développée par : Kouidri Sofiane</b><br>"
            "© 2026 Kouidri Sofiane<br>Contact : sofihydro@gmail.com</p>"
            "<p>Toute redistribution doit conserver cette mention d'auteur.</p>")

    # ── Utilitaires ──────────────────────────────────────────────────────────

    def _icone(self, nom_fichier):
        chemin = os.path.join(_PLUGIN_DIR, 'resources', nom_fichier)
        if os.path.exists(chemin):
            return QIcon(chemin)
        return QIcon()  # icône vide si fichier absent
