# -*- coding: utf-8 -*-
"""
Backend géométrique unique pour les modules de sectorisation.
=================================================================

Ce module fournit une couche mince et uniforme autour des géométries, avec
DEUX backends interchangeables :

  * ``qgis``    : utilisé automatiquement quand le code tourne DANS QGIS
                  (``QgsGeometry``) -- c'est le cas normal du plugin ;
  * ``shapely`` : utilisé automatiquement hors QGIS (tests unitaires,
                  validation hors-ligne, CI) avec Shapely >= 2.0.

L'algorithme de partition (``core/partition_secteurs.py``) ne manipule JAMAIS
QgsGeometry ou shapely directement : il passe par la classe ``Geom`` ci-dessous,
ce qui permet de tester exactement le même code que celui exécuté dans QGIS.

Opérations exposées (strictement celles utilisées par le pipeline -- toute
méthode devenue inutile a été retirée, cf. changelog NETTOYAGE-CODE-MORT) :
  intersection / difference / buffer / area / length / bounds / centroid_xy /
  contains_xy / distance_xy / intersects / touches_bounds_of / boundary /
  shared_length / parts / drop_inner_rings / make_valid / is_empty, plus les
  fonctions module unary_union(), polygonize(), multi() et box().

AUCUNE enveloppe convexe (convex hull) n'est exposée ici : c'est un choix
délibéré du build precedent (voir README, changelog FIX-PARTITION), la convex
hull étant la cause directe des grands polygones chevauchants observés.
"""

from __future__ import annotations

try:  # ---------------------------------------------------------------- QGIS
    from qgis.core import QgsGeometry, QgsPointXY, QgsWkbTypes, QgsRectangle

    BACKEND = "qgis"
except Exception:  # ------------------------------------------------- Shapely
    BACKEND = "shapely"
    from shapely.geometry import (
        box as sh_box,
        MultiPolygon as ShMultiPolygon,
        Polygon as ShPolygon,
    )
    from shapely.ops import polygonize as sh_polygonize
    from shapely.ops import unary_union as sh_unary_union
    from shapely import make_valid as sh_make_valid


def _qgis_flat(geom):
    return QgsWkbTypes.flatType(geom.wkbType())


class Geom:
    """Enveloppe uniforme d'une géométrie (QgsGeometry ou objet shapely)."""

    __slots__ = ("impl",)

    def __init__(self, impl):
        if isinstance(impl, Geom):
            impl = impl.impl
        self.impl = impl

    # ------------------------------------------------------------ fabrique
    @staticmethod
    def wrap(obj):
        if obj is None:
            return None
        if isinstance(obj, Geom):
            return obj
        return Geom(obj)

    # ------------------------------------------------------------ propriétés
    @property
    def is_empty(self):
        if BACKEND == "qgis":
            return self.impl is None or self.impl.isEmpty()
        return self.impl is None or self.impl.is_empty

    @property
    def area(self):
        if self.is_empty:
            return 0.0
        if BACKEND == "qgis":
            return self.impl.area()
        return self.impl.area

    @property
    def length(self):
        if self.is_empty:
            return 0.0
        if BACKEND == "qgis":
            return self.impl.length()
        return self.impl.length

    @property
    def bounds(self):
        if self.is_empty:
            return (0.0, 0.0, 0.0, 0.0)
        if BACKEND == "qgis":
            r = self.impl.boundingBox()
            return (r.xMinimum(), r.yMinimum(), r.xMaximum(), r.yMaximum())
        return tuple(self.impl.bounds)

    def centroid_xy(self):
        if self.is_empty:
            return (0.0, 0.0)
        if BACKEND == "qgis":
            c = self.impl.centroid()
            p = c.asPoint()
            return (p.x(), p.y())
        c = self.impl.centroid
        return (c.x, c.y)

    # ------------------------------------------------------------ opérations
    def buffer(self, dist, segments=8):
        if self.is_empty:
            return None
        if BACKEND == "qgis":
            r = self.impl.buffer(dist, segments)
        else:
            r = self.impl.buffer(dist, quad_segs=max(1, int(segments)))
        return Geom(r) if r is not None and not Geom(r).is_empty else None

    def intersection(self, other):
        other = Geom.wrap(other)
        if self.is_empty or other is None or other.is_empty:
            return None
        if BACKEND == "qgis":
            r = self.impl.intersection(other.impl)
        else:
            r = self.impl.intersection(other.impl)
        g = Geom(r)
        return None if g.is_empty else g

    def difference(self, other):
        other = Geom.wrap(other)
        if self.is_empty:
            return None
        if other is None or other.is_empty:
            return self
        if BACKEND == "qgis":
            r = self.impl.difference(other.impl)
        else:
            r = self.impl.difference(other.impl)
        g = Geom(r)
        return None if g.is_empty else g

    def intersects(self, other):
        other = Geom.wrap(other)
        if self.is_empty or other is None or other.is_empty:
            return False
        if BACKEND == "qgis":
            return self.impl.intersects(other.impl)
        return self.impl.intersects(other.impl)

    def contains_xy(self, x, y):
        if self.is_empty:
            return False
        if BACKEND == "qgis":
            return self.impl.contains(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
        from shapely.geometry import Point as _P

        return self.impl.contains(_P(x, y))

    def distance_xy(self, x, y):
        """Distance (m) entre la géométrie et le point (x, y)."""
        if self.is_empty:
            return float("inf")
        if BACKEND == "qgis":
            return self.impl.distance(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
        from shapely.geometry import Point as _P

        return self.impl.distance(_P(x, y))

    def touches_bounds_of(self, emprise):
        """Vrai si la géométrie touche le bord de l'emprise (zone externe)."""
        emprise = Geom.wrap(emprise)
        if self.is_empty or emprise is None or emprise.is_empty:
            return False
        if BACKEND == "qgis":
            b_raw = self.impl.constGet().boundary()
            eb_raw = emprise.impl.constGet().boundary()
            if b_raw is None or eb_raw is None:
                return False
            b = QgsGeometry(b_raw)
            eb = QgsGeometry(eb_raw)
            return (not b.isEmpty()) and (not eb.isEmpty()) and b.intersects(eb)
        return self.impl.boundary.intersects(emprise.impl.boundary)

    @property
    def boundary(self):
        if self.is_empty:
            return None
        if BACKEND == "qgis":
            b_raw = self.impl.constGet().boundary()
            if b_raw is None:
                return None
            b = QgsGeometry(b_raw)
            return None if b.isEmpty() else Geom(b)
        b = self.impl.boundary
        return None if b.is_empty else Geom(b)

    def shared_length(self, other):
        """Longueur de frontière partagée avec ``other`` (m)."""
        b1, b2 = self.boundary, Geom.wrap(other).boundary
        if b1 is None or b2 is None:
            return 0.0
        inter = b1.intersection(b2)
        if inter is None:
            return 0.0
        return inter.length

    # ------------------------------------------------------------ polygones
    def parts(self):
        """Liste des parties polygonales (trous conservés)."""
        if self.is_empty:
            return []
        if BACKEND == "qgis":
            ft = _qgis_flat(self.impl)
            if ft == QgsWkbTypes.Polygon:
                return [Geom(QgsGeometry(self.impl))]
            if ft == QgsWkbTypes.MultiPolygon:
                return [Geom(QgsGeometry.fromPolygonXY(p))
                        for p in self.impl.asMultiPolygon() if p]
            # collection générique
            out = []
            for g in self.impl.asGeometryCollection():
                out.extend(Geom(g).parts())
            return out
        g = self.impl
        if g.geom_type == "Polygon":
            return [Geom(g)]
        if g.geom_type == "MultiPolygon":
            return [Geom(p) for p in g.geoms]
        if hasattr(g, "geoms"):
            out = []
            for sub in g.geoms:
                out.extend(Geom(sub).parts())
            return out
        return []

    def drop_inner_rings(self, keep_predicate=None):
        """Supprime les anneaux internes pour lesquels keep_predicate(hole_geom)
        est faux. Sans prédicat : supprime tous les trous."""
        if self.is_empty:
            return self
        if BACKEND == "qgis":
            ft = _qgis_flat(self.impl)
            if ft == QgsWkbTypes.Polygon:
                rings = self.impl.asPolygon()
                if not rings:
                    return self
                ext = rings[0]
                holes = [QgsGeometry.fromPolygonXY([r]) for r in rings[1:]]
                keep = [r for r, h in zip(rings[1:], holes)
                        if keep_predicate is not None and keep_predicate(Geom(h))]
                return Geom(QgsGeometry.fromPolygonXY([ext] + keep))
            if ft == QgsWkbTypes.MultiPolygon:
                polys = []
                for p in self.impl.asMultiPolygon():
                    if not p:
                        continue
                    ext = p[0]
                    keep = [r for r in p[1:]
                            if keep_predicate is not None
                            and keep_predicate(Geom(QgsGeometry.fromPolygonXY([r])))]
                    polys.append([ext] + keep)
                return Geom(QgsGeometry.fromMultiPolygonXY(polys)) if polys else self
            return self
        g = self.impl
        if g.geom_type == "Polygon":
            holes = [ShPolygon(r) for r in g.interiors]
            keep = [r for r, h in zip(g.interiors, holes)
                    if keep_predicate is not None and keep_predicate(Geom(h))]
            return Geom(ShPolygon(g.exterior, keep))
        if g.geom_type == "MultiPolygon":
            polys = []
            for p in g.geoms:
                keep = [r for r in p.interiors
                        if keep_predicate is not None
                        and keep_predicate(Geom(ShPolygon(r)))]
                polys.append(ShPolygon(p.exterior, keep))
            return Geom(ShMultiPolygon(polys))
        return self

    def make_valid(self):
        if self.is_empty:
            return self
        if BACKEND == "qgis":
            if self.impl.isGeosValid():
                return self
            g = QgsGeometry(self.impl)
            g.makeValid()
            return Geom(g)
        if self.impl.is_valid:
            return self
        return Geom(sh_make_valid(self.impl))


# ------------------------------------------------------------------ fabriques
def box(minx, miny, maxx, maxy):
    if BACKEND == "qgis":
        return Geom(QgsGeometry.fromRect(QgsRectangle(minx, miny, maxx, maxy)))
    return Geom(sh_box(minx, miny, maxx, maxy))


def unary_union(geoms):
    geoms = [Geom.wrap(g).impl for g in geoms
             if g is not None and not Geom.wrap(g).is_empty]
    if not geoms:
        return None
    if BACKEND == "qgis":
        r = QgsGeometry.unaryUnion(geoms)
        g = Geom(r)
        return None if g.is_empty else g
    r = sh_unary_union(geoms)
    g = Geom(r)
    return None if g.is_empty else g


def polygonize(lignes):
    """Transforme un réseau de lignes nouées en polygones (liste de Geom)."""
    lignes = [Geom.wrap(g).impl for g in lignes
              if g is not None and not Geom.wrap(g).is_empty]
    if not lignes:
        return []
    if BACKEND == "qgis":
        noue = QgsGeometry.unaryUnion(lignes)
        if noue is None or noue.isEmpty():
            return []
        poly = QgsGeometry.polygonize([noue])
        if poly is None or poly.isEmpty():
            return []
        return Geom(poly).parts()
    noue = sh_unary_union(lignes)
    polys = list(sh_polygonize(noue))
    return [Geom(p) for p in polys if not p.is_empty]


def multi(geoms):
    """MultiPolygon à partir d'une liste de Geom polygonaux."""
    geoms = [Geom.wrap(g) for g in geoms if g is not None and not g.is_empty]
    if not geoms:
        return None
    if len(geoms) == 1:
        return geoms[0]
    if BACKEND == "qgis":
        polys = []
        for g in geoms:
            ft = _qgis_flat(g.impl)
            if ft == QgsWkbTypes.Polygon:
                polys.append(g.impl.asPolygon())
            elif ft == QgsWkbTypes.MultiPolygon:
                polys.extend(g.impl.asMultiPolygon())
        return Geom(QgsGeometry.fromMultiPolygonXY(polys)) if polys else None
    from shapely.geometry import MultiPolygon as _MP

    polys = []
    for g in geoms:
        if g.impl.geom_type == "Polygon":
            polys.append(g.impl)
        elif g.impl.geom_type == "MultiPolygon":
            polys.extend(g.impl.geoms)
    return Geom(_MP(polys)) if polys else None
