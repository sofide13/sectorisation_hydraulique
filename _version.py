# -*- coding: utf-8 -*-
"""Version unique de l'extension : lue depuis metadata.txt (source de verite)."""
import os


def lire_version():
    chemin = os.path.join(os.path.dirname(os.path.abspath(__file__)), "metadata.txt")
    try:
        with open(chemin, encoding="utf-8") as f:
            for ligne in f:
                if ligne.strip().lower().startswith("version="):
                    return ligne.split("=", 1)[1].strip()
    except OSError:
        pass
    return "?"


__version__ = lire_version()
