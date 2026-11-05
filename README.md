# Sectorisation Hydraulique — Extension QGIS

> **Auteur : Kouidri Sofiane** (sofihydro@gmail.com) — © 2026 Kouidri Sofiane. Licence : GNU GPL v2 ou ultérieure (voir LICENSE). Toute redistribution doit conserver cette mention.

Extension QGIS pour la sectorisation automatique des réseaux AEP.  
Paramètres de conception hydrauliques configurables.

## Références et statut des seuils

La méthode Louvain est citée conformément à **Blondel et al. (2008)**, *Fast unfolding of communities in large networks*, Journal of Statistical Mechanics: Theory and Experiment, 2008(10), P10008, DOI `10.1088/1742-5468/2008/10/P10008`. La fiche **Alvisi (2015)** est complétée dans [`REFERENCES.md`](REFERENCES.md), avec le DOI `10.1007/s11269-015-1066-z` et le lien entre partitionnement multiniveau, raffinement et simulation hydraulique.

Les plages **10–60 m**, **0,3–2 m/s** et **500–3000 abonnés** sont affichées comme des **paramètres de conception configurables**. Elles ne sont pas présentées comme une conformité à une norme non consultée. La plage d'abonnés est réglée par défaut à 500–3000 pour rester cohérente avec une pratique DMA courante. Le champ reste configurable (l'interface accepte notamment 300–3000), mais une borne basse à 300 doit être justifiée et documentée pour le réseau concerné.

Le paquet `wntr` est requis pour la validation solveur finale :

```text
python -m pip install -r requirements.txt
```

Sous Windows, il faut installer WNTR dans **le même Python que QGIS**, et non dans un Python système distinct. Depuis l'invite OSGeo4W ou un terminal QGIS, utiliser par exemple :

```text
C:\\OSGeo4W\\bin\\python-qgis.bat -m pip install wntr
```

ou le fichier `python-qgis.bat` situé dans le répertoire `bin` de l'installation QGIS. Vérifier ensuite dans la console Python QGIS :

```python
import wntr
print(wntr.__version__)
```

Si WNTR n'est pas installé, le pipeline ne doit pas être considéré comme validé hydrauliquement : il conserve le statut explicite `INDISPONIBLE` et produit les diagnostics géométriques/topologiques sans présenter l'estimation comme une validation solveur.

La validation produit le journal de comparaison avant/après nettoyage, la couche `pressions_wntr` et les couches de revue `abonnes_hors_secteur` / `conduites_hors_secteur` lorsque des objets ne peuvent pas être rattachés. Elle utilise en priorité `WNTR/EpanetSimulator` ; `WNTRSimulator` est seulement utilisé en repli si le moteur EPANET n'est pas disponible.

## Changelog

### FIX-CONTIGUITE-TERRITOIRES (version 1.0.0)

**Constat (capture QGIS de l'utilisateur, couche `secteurs_hydrauliques_topo`,
37 entités)** : sélectionner **un** secteur allumait **une dizaine de morceaux
éparpillés dans toute la ville** — des rubans d'environ 60 m de large (soit
2 × le tampon de 30 m posé autour d'une conduite), entremêlés avec les morceaux
des secteurs voisins, plus une plage isolée en pleine zone non bâtie près d'un
réservoir. Une telle carte est inexploitable sur le terrain : un secteur / DMA
se lit comme **une zone continue** que l'on peut isoler par un jeu de vannes
(AWWA Manual M36 ; Morrison, Tooms & Rogers, *District Metered Areas — Guidance
Notes*, IWA, 2007).

**Causes, dans le code, et corrections apportées** :

| # | Cause | Correction |
|---|-------|-----------|
| 1 | L'habillage par **tampon de conduites** (`core/polygones_topologiques.py`) est par nature un faisceau de rubans disjoints ; il était publié tel quel dès que la couche `routes` manquait. | La **partition stricte des îlots** est désormais tentée même sans couche `routes`, en prenant le **réseau AEP de distribution comme trame de découpe** (les conduites suivent la voirie). Si le repli tampon reste inévitable, une **ALERTE** le dit explicitement, avec la marche à suivre. |
| 2 | La *garantie de représentation* du build précédent rendait à un secteur absent **tout le faisceau** de ses conduites. | Elle est ramenée à son **bloc principal** (`_partie_principale`), un secteur devant se lire comme une zone. |
| 3 | Un secteur pouvait rester en plusieurs morceaux à la sortie du nettoyage topologique (`core/partition_secteurs.py`) : les exclaves portant du réseau étaient conservées par conception. | Nouvelle étape **2bis `_assurer_contiguite`** : le morceau **principal** (celui qui porte le plus d'abonnés, puis la plus grande surface) reste au secteur ; **chaque autre morceau rejoint le secteur voisin avec lequel il partage la plus longue frontière** — la règle déjà utilisée dans le plugin pour réaffecter un îlot parasite. |
| 4 | Un îlot **sans aucun nœud de réseau** était attribué au secteur du nœud le plus proche, **sans limite de distance** : des territoires s'étendaient sur des kilomètres de terrain non desservi. | Nouveau paramètre **`DIST_MAX_ILOT_SANS_NOEUD_M`** (300 m par défaut) : au-delà, l'îlot n'est desservi par personne et n'entre dans aucun secteur. `0` restaure le comportement precedent. |
| 5 | La fragmentation n'apparaissait que dans une ligne discrète du journal. | **Alerte explicite** en fin de génération (`pipeline.py`) listant les secteurs encore fragmentés et le nombre de morceaux, et bannière `FIX-CONTIGUITE-TERRITOIRES` en tête de journal. |

**Invariants préservés** (exigences du projet, vérifiées par les tests) :

* **aucune enveloppe convexe**, aucun tampon d'inflation ajouté ;
* **aucune surface créée ni perdue** : un morceau change de propriétaire, la
  partition reste stricte — **0 chevauchement**, aucun trou nouveau ;
* **autant de polygones que de secteurs** : le morceau principal d'un secteur
  n'est jamais déplacé, donc aucun secteur ne peut être vidé ;
* **aucune appartenance hydraulique modifiée** : nœuds et conduites gardent le
  secteur décidé par le moteur hydraulique. Seul le **territoire
  cartographique** bouge, et chaque transfert est journalisé en m² et en
  abonnés concernés ;
* terminaison garantie : chaque absorption diminue d'au moins une unité le
  nombre total de morceaux.

**Ce qui n'est *pas* corrigé automatiquement, à dessein** : une annexe qui ne
touche **aucun** autre secteur (hameau alimenté par une conduite traversant une
zone non bâtie, réservoir isolé) ne peut être rendue à personne. Elle est
**conservée et journalisée** comme exclave physiquement isolée : la supprimer
effacerait du réseau réellement exploité. C'est un sujet de conception de
réseau (conduite de raccordement), pas de cartographie.

**Nouveaux paramètres** (section `PARTITION` du JSON, onglets *Simple* et
*Avancé* de la configuration) :

| Clé | Défaut | Rôle |
|-----|--------|------|
| `ASSURER_CONTIGUITE_TERRITOIRES` | `true` | Ramène chaque secteur à un territoire d'un seul tenant. `false` = comportement precedent. |
| `AIRE_EXCLAVE_TOLEREE_M2` | `0.0` | `> 0` : une annexe de cette surface ou plus est conservée au lieu d'être rendue au voisin (réseaux à vraies annexes détachées). |
| `MAX_PASSES_CONTIGUITE` | `60` | Itérations maximales ; le traitement s'arrête dès qu'il n'y a plus rien à recoller. |
| `DIST_MAX_ILOT_SANS_NOEUD_M` | `300.0` | Distance maximale de rattachement d'un îlot sans nœud de réseau. `0` = aucune borne . |

Une configuration enregistrée avec une version antérieure récupère
automatiquement ces clés au chargement (`core/config_manager.py`), donc les
correctifs ne restent pas désactivés en silence.

**Validation hors QGIS** : `python3 tests/test_partition_synthetic.py`
(backend *shapely*) — 8 contrôles, dont un patchwork reproduisant le défaut
(morceaux entremêlés + exclave lointaine), la conservation exacte des surfaces,
le bloc de « désert » laissé hors de tout secteur, la désactivation des deux
nouveaux comportements, et « autant de polygones que de secteurs, tous d'un
seul tenant ».

### FIX-COMPTE-SECTEURS (version 0.9.0)

**Constat (run utilisateur)** : le socle topologique annonçait **460
« secteurs »** alors que la couche de référence `secteurs_hydrauliques`
n'en contient que quelques dizaines. La couche `secteurs_hydrauliques_topo`
comptait donc 460 polygones, sans correspondance possible avec les secteurs
hydrauliques réellement calculés (pression, capacité, plages d'abonnés,
altimétrie).

**Cause racine, dans le code** — deux suffixages cumulés, internes au socle
`core/graphe_topologique.py` :

1. `scinder_par_modularite()` ajoute `_M{i}` par communauté Louvain d'un
   réservoir surdimensionné ;
2. `scinder_par_continuite_distribution()` ajoutait `_zone{i}` à **chaque**
   bloc de conduites de distribution rendu disjoint par le seuil
   `SEUIL_DN_TRANSPORT_MM` (150 mm). Sur un réseau réel où une grande part des
   conduites dépasse 150 mm, le sous-graphe « distribution seule » explose en
   centaines de composantes : **chaque paire de nœuds devenait un secteur**.

Or un bloc de 2 à 5 nœuds n'est pas un secteur : une DMA se dimensionne en
centaines à quelques milliers de branchements (Morrison, Tooms & Rogers,
*District Metered Areas — Guidance Notes*, IWA Water Loss Task Force, 2007 ;
AWWA Manual M36 ; ici `ABONNES_MIN/MAX_PAR_SECTEUR` = 500–3000). Un bloc
alimenté par une antenne de transport est une **sous-zone de sous-comptage**
à l'intérieur de son secteur.

**Correctif — nouveau module `core/consolidation_secteurs.py`**, qui applique
la **phase d'agrégation sous contrainte** systématique dans la littérature de
partitionnement des réseaux d'eau (découpage, *puis* regroupement pour
respecter les contraintes de taille et de comptage) :

* Perelman & Ostfeld, *Topological clustering for water distribution systems
  analysis*, Environmental Modelling & Software, 2011 ;
* Diao, Zhou & Rauch, *Automated creation of District Metered Area boundaries*,
  JWRPM, 2013 ;
* Di Nardo, Di Natale et al., *Water network sectorization based on graph
  theory and energy performance indices*, JWRPM, 2014 ;
* Giustolisi & Ridolfi, *New modularity-based approach to segmentation of water
  distribution networks*, Water Resources Research, 2014.

Concrètement, en deux garde-fous indépendants :

1. **`CONSOLIDER_ZONES_EN_SECTEURS` (défaut `true`)** — l'analyse « qui
   alimente qui » reste calculée et journalisée, mais les blocs deviennent des
   **sous-zones** reportées dans le nouveau champ `sous_zones` de
   `secteurs_hydrauliques_topo`, au lieu de renommer les secteurs. Le nombre de
   secteurs du socle redevient le nombre de réservoirs (± modules Louvain).
2. **`ALIGNER_SUR_SECTEURS_REFERENCES` (défaut `true`)** — le socle est ensuite
   recalé sur la partition de record : lecture de `conduites_sectorisees`
   (champs `pipe_id` / `secteur_id`, `pipe_id` étant l'identifiant d'entité de
   la couche AEP, exactement les `fids` portés par les arêtes du graphe →
   réconciliation **purement topologique, aucune opération géométrique**),
   vote majoritaire par nœud (nombre de conduites incidentes, puis longueur
   cumulée, puis ordre lexicographique — totalement déterministe), nœuds sans
   conduite de référence rattachés par **plus court chemin pondéré**
   (Dijkstra multi-source, la règle de l'étape 2 du socle). Repli documenté et
   signalé si seule la couche polygone existe (vote point-dans-polygone).
3. **Garantie de représentation** (`core/polygones_topologiques.py`) — un
   secteur qui perdrait tous ses îlots au profit d'un voisin plus peuplé
   récupère le tampon de **ses propres conduites de distribution** (territoire
   minimal au sens AWWA M36), surface retirée des voisins : la partition reste
   stricte, et aucun secteur ne peut disparaître de la couche.

**Vérification dans le journal** — chaque run affiche désormais :

```
CONTROLE DU COMPTE DE SECTEURS : 37 secteur(s) topologique(s) = 37 secteur(s) de reference -> CONTROLE OK.
```

Le seul écart possible est un secteur de référence dont **aucune** conduite
n'existe dans le graphe (toutes exclues par vanne fermée ou géométrie
invalide) : il est alors nommé en `[ALERTE]`, jamais masqué.

**Test** : `tests/test_consolidation_secteurs.py` (hors QGIS, doublures de
couches) — réseau où une conduite sur deux est du transport : 18 « secteurs »
en comportement anterieur, **2** après correctif, égal au nombre de secteurs de
la couche de référence ; sous-zones et antennes de transport conservées.

### NETTOYAGE-CODE-MORT (version 0.9.0)

Suppression de **toutes** les classes et fonctions jamais appelées, vérifiée
par analyse statique itérative (AST : construction du graphe d'appels, calcul
du point fixe depuis les points d'entrée QGIS `classFactory` / `initGui` /
`unload` / `executer` et les tests) puis contrôle `pyflakes` :

| Fichier | Supprimé |
| --- | --- |
| `core/pipeline.py` | `assurer_polygone_simple()`, `_corridor_polygone_simple()`, `_blocs_urbains_par_polygonisation()`, `_assigner_blocs_aux_secteurs()`, `_construire_corridors_reseau()`, `_altitude_range_secteur()` |
| `core/geom_backend.py` | `Geom.from_wkt()`, `Geom.clone()`, `Geom.wkt`, `Geom.is_valid`, `Geom.geom_kind()`, `Geom.union()`, `Geom.remove_holes()`, `Geom.simplify()`, `Geom.__repr__()`, `point()`, `from_polygon_xy()` |
| `core/partition_secteurs.py` | `filtrer_petites_parties()` |
| `core/config_manager.py` | `config_depuis_projet()` |
| `ui/dialog_config.py` | `DialogueConfig.config_plat_resultat()` |
| `outils/verifier_installation_plugin.py` | `MARQUEUR_CODE_MORT_A_VERIFIER` |

Les cinq premières fonctions de `pipeline.py` étaient précisément
l'ancienne chaîne **à enveloppe convexe** (`convexHull()`) responsable des
grands polygones chevauchants : elle n'est plus seulement contournée, elle
n'existe plus dans le code. Ont aussi été retirés les imports inutilisés
(9 fichiers) et trois variables locales assignées sans usage.
`outils/verifier_installation_plugin.py` ne contient plus de table
d'empreintes SHA-256 figée (elle devenait fausse à chaque correctif) : les
empreintes de référence sont recalculées à chaque lancement depuis les
fichiers de la livraison.

Aucune fonction utilisée n'a été touchée : `tests/test_partition_synthetic.py`
et `tests/test_consolidation_secteurs.py` passent tous les deux.

### FIX-PARTITION (version 0.8.0)

**Constat (images du run de validation fournies avec le projet)** :
- `secteurs_hydrauliques` et `secteurs_limites` présentaient de
  **grands polygones chevauchants** aux bords rectilignes (enveloppes
  convexes) recouvrant bâti, voies et zones non desservies ;
- `secteurs_hydrauliques_topo` (socle topologique) présentait des **îlots épars**
  (rubans de buffer de conduites disjoints) sans continuité territoriale ;
- des **petits îlots parasites** subsistaient à l'intérieur de secteurs
  d'autres secteurs.

**Cause racine identifiée dans le code** :
1. `_corridor_polygone_simple()` (pipeline) et le bloc `secteurs_limites`
   forçaient chaque secteur multi-parties en **enveloppe convexe**
   (`convexHull()`), y compris *après* la passe de résolution des
   chevauchements — donc les chevauchements résolus étaient **recréés** par
   le hull final ;
2. la résolution des chevauchements était **pair-à-pair en une seule passe**,
   sans contrôle global itératif ;
3. les micro-polygones issus de `polygonize()` (ronds-points, cours, poches)
   n'étaient jamais réaffectés : ils restaient des îlots parasites.

**Correctif — module `core/partition_secteurs.py` (+ `core/geom_backend.py`)** :
les territoires de secteur deviennent une **partition stricte** du tissu
urbanisé :
1. polygonisation du réseau routier noué → vrais îlots urbains ;
2. chaque îlot prend le secteur **majoritaire en abonnés** parmi les nœuds
   réseau qu'il touche (égalité départagée par continuité avec les îlots
   voisins déjà affectés) — l'appartenance hydraulique (pression 10-60 m,
   capacité, plages 500-3000 abonnés, connexité) reste celle du moteur, ce
   module ne décide jamais d'une appartenance ;
3. poches résiduelles internes (trous de trame routière) rattachées au secteur
   voisin à la plus longue frontière partagée ; zones externes (désert,
   franges) exclues ;
4. nettoyage topologique itératif : toute partie < `SEUIL_AIRE_ILOT_M2` ou
   sans nœud de son secteur est **réaffectée** au voisin à la plus longue
   frontière ; tous les chevauchements résiduels sont résolus jusqu'à
   convergence ; petits trous vides comblés ;
5. **aucune enveloppe convexe, nulle part** (le backend géométrique ne
   l'expose même plus).

Couches de sortie impactées :
- `secteurs_hydrauliques` = polygones de la partition (tous les attributs
  hydrauliques conservés) ;
- `secteurs_limites` = mêmes polygones (pavage complet, champs
  `nb_parties`, `aire_ha` ajoutés) ;
- **nouvelle** `frontieres_secteurs` = limites linéaires inter-secteurs
  avec le type de vanne (PRV / surpresseur) posé sur chaque frontière ;
- `secteurs_hydrauliques_topo` (socle topologique) = partition stricte dès que la
  couche routes existe (sinon repli documenté sur l'habillage buffer).

Paramètres (section `PARTITION` du JSON, onglet « Paramètres essentiels →
Partition des secteurs par îlots urbains  ») :
`ACTIVER_PARTITION_ILOTS`, `SEUIL_AIRE_ILOT_M2` (défaut 2000 m²),
`LARGEUR_MAX_VOIE_M` (30 m), `AIRE_MAX_RATTACHEMENT_M2` (300 000 m²),
`MAX_PASSES_NETTOYAGE` (4), `SNAP_LIMITE_M` (0,5 m).

**Validation** : `tests/test_partition_synthetic.py` (s'exécute hors QGIS,
backend Shapely) — ville synthétique 111 îlots, 3 secteurs, antenne enclavée :
0 chevauchement, 0 îlot parasite, couverture du tissu urbain à 100 %,
limites sur les lignes centrales de rues ; rendu comparatif AVANT/APRÈS dans
`tests/resultat_partition_synthetic.png`.

### FIX-ILOTS-URBAINS-14

**Constat du terrain (avec citation)** : même après le tampon variable
(FIX13), l'approche restait fondamentalement une méthode **géométrique par
buffer** — une approximation, pas une vraie délimitation morphologique.
Comme le souligne la littérature de morphologie urbaine sur la notion de
bloc/îlot enclavé par son réseau viaire (Biraghi et al., *"Urban block
enclosure and labyrinthineness: measuring morphological attributes that
matter"*, Journal of Urban Design, 2025), la bonne unité de découpage n'est
pas une distance arbitraire autour d'un tuyau ou d'une route, mais **l'îlot
urbain lui-même** — le polygone réellement enclavé par les rues.

**Correctif** : `secteurs_hydrauliques` est reconstruite par
**polygonisation du réseau de routes** :
1. Les lignes de la couche routes sont unies (`QgsGeometry.unaryUnion`), ce
   qui noue complètement le réseau aux croisements.
2. `QgsGeometry.polygonize()` transforme ce réseau noué en polygones : ce
   sont les **vrais îlots urbains**. La limite d'un îlot est par
   construction la ligne centrale de la rue — jamais un bâtiment, quelle
   que soit la largeur réelle de la rue, puisqu'aucune largeur n'est
   inventée (fini le réglage de tampon).
3. Chaque îlot est rattaché au secteur **majoritaire** parmi les nœuds
   réseau (abonnés agrégés) qu'il contient — même critère que le moteur
   hydraulique, pas un nouveau calcul arbitraire.
4. Les îlots d'un même secteur sont fusionnés (dissolve) → polygone du
   secteur.
5. **Repli automatique** : là où le réseau routier ne ferme pas de polygone
   (trous, tronçons manquants, réseau non bouclé), le corridor-buffer
   variable (FIX13) comble uniquement les zones non couvertes par un îlot —
   les îlots réels restent toujours prioritaires.

Contrôlable via `ACTIVER_ILOTS_URBAINS` (onglet **Corridor réseau**,
actif par défaut) ; désactiver revient à l'ancien corridor-buffer (FIX13)
pour toute l'emprise. Le journal indique, pour chaque run, le nombre
d'îlots obtenus, combien sont rattachés directement (nœud à l'intérieur) ou
par plus proche voisin, et la surface éventuellement comblée par le
corridor-buffer.

`secteurs_limites` n'est touchée par aucun changement de ce build.

### FIX-CORRIDOR-VARIABLE-13

**Demande** : les modifications de tampon variable devaient porter sur
`secteurs_hydrauliques` (pas sur `secteurs_limites`, volontairement
non modifiée à nouveau dans ce build).

**Problème** : `secteurs_hydrauliques` est construite en bufferisant
**chaque conduite AEP avec un rayon unique** (`LARGEUR_CORRIDOR_M`, 25 m par
défaut), quel que soit son diamètre, sans aucun lien avec les routes. Un
buffer de 25 m autour d'une petite conduite de distribution déborde
largement sur le bâti environnant.

**Correctif** : le corridor de `secteurs_hydrauliques` est maintenant :
1. **variable par tronçon de conduite AEP** — largeur résolue par
   priorité : `CHAMPS.CHAMP_LARGEUR_CORRIDOR_AEP` (champ numérique explicite)
   → sinon calcul automatique proportionnel au **diamètre** de la conduite
   (`COEF_LARGEUR_CORRIDOR_PAR_DIAMETRE × diamètre`, borné par
   `LARGEUR_CORRIDOR_MIN_M` / `LARGEUR_CORRIDOR_MAX_M`) → sinon repli sur
   `LARGEUR_CORRIDOR_M` (comportement d'origine) ;
2. **rogné par la trame routière** à largeur variable (même mécanisme que
   `CHAMP_LARGEUR_TAMPON_ROUTE` / `CHAMP_TYPE_ROUTE` déjà utilisés par
   `secteurs_limites`, calculé ici indépendamment pour ne pas toucher ce
   bloc) — la portion de corridor qui tombe sur l'emprise d'une route est
   retirée, ce qui empêche deux secteurs voisins de déborder l'un sur
   l'autre (et donc sur le bâti) de part et d'autre d'une rue.

Ces paramètres sont éditables dans l'onglet **Paramètres avancés → Corridor
réseau** (largeurs, coefficient, bornes) et **Paramètres essentiels →
Champs attributaires** (`CHAMP_LARGEUR_CORRIDOR_AEP`). Le journal indique,
pour chaque run, la répartition des tronçons entre champ / diamètre / valeur
par défaut, et si le rognage routier a été appliqué.

`secteurs_limites` n'est touchée par aucun changement de ce build.

### FIX-TAMPON-ROUTE-VARIABLE-11

**Problème constaté** : la coupure des limites de secteur le long des routes
(couche `secteurs_limites`) utilisait un **rayon de tampon unique**
(`LARGEUR_TAMPON_ROUTE_M`, ex: 4 m) appliqué à TOUTES les routes, quelle que
soit leur emprise réelle. Sur les petites voies (impasses, chemins ≈ 3-4 m
d'emprise), ce rayon fixe pouvait déborder sur le bâti voisin : d'où les
limites de secteur qui chevauchent des bâtiments constatées sur le terrain.

**Correctif** : le rayon de tampon est maintenant résolu **tronçon par
tronçon**, avec repli automatique sur l'ancien comportement (rayon unique) si
rien n'est configuré :
1. `CHAMPS.CHAMP_LARGEUR_TAMPON_ROUTE` — champ numérique sur la couche routes
   donnant directement, pour ce tronçon, le rayon (m) à utiliser (priorité la
   plus haute).
2. `CHAMPS.CHAMP_TYPE_ROUTE` + `DECOUPE_ROUTES.LARGEUR_TAMPON_PAR_TYPE_ROUTE`
   — champ catégoriel (type de voie) mappé vers un rayon par catégorie dans
   le JSON, ex :
   ```json
   "LARGEUR_TAMPON_PAR_TYPE_ROUTE": {
     "nationale": 1.0, "secondaire": 0.5, "chemin": 0.1
   }
   ```
3. Si aucun des deux n'est configuré/trouvé sur un tronçon : repli sur
   `LARGEUR_TAMPON_ROUTE_M` (comportement d'origine, inchangé par défaut).

Les deux champs se configurent dans l'onglet **Paramètres essentiels →
Champs attributaires** (ou directement dans `config_defaut.json`, section
`CHAMPS`). Le journal indique désormais, pour chaque run, combien de
tronçons ont été traités via le champ largeur, via la table de type, et via
le rayon par défaut, pour vérifier la configuration.

### FIX-FUSION-PRESSION-12

**Remplace FIX-FUSION-ALTITUDE-10**, qui s'est révélé insuffisant : le run
de validation a montré une légère RÉGRESSION (CONFORME_STRICT 11→10/37,
abonnés hors-plage de pression 27,2%→28,6%). Cause : FIX-10 choisissait le
voisin de secours par **écart d'altitude brut minimal**, un proxy trop
grossier — deux secteurs peuvent avoir des altitudes de terrain proches sans
que la cote piézométrique RÉGULÉE du secteur receveur donne une pression
correcte aux abonnés migrants (et inversement, un écart brut élevé peut
malgré tout donner une pression correcte selon la cote du receveur).

**Correctif** : le choix du voisin de secours se fait maintenant sur la
**pression réelle estimée** des abonnés qui migreraient, calculée comme
`cote_piezo(candidat) − altitude(abonné)` — exactement le calcul déjà
utilisé ailleurs dans le moteur (diagnostic de recalibration des cotes
piézo, redistribution forcée de capacité). Le journal affiche désormais,
pour chaque fusion de secours, le nombre de nœuds encore hors plage et la
violation cumulée (m) retenue.

Comme pour FIX-10, ce correctif ne résout pas les cas structurels réels déjà
signalés par le diagnostic (déficit de capacité, réservoirs isolés, écarts
topographiques non résorbables sans nouvelle infrastructure) : ceux-ci
restent des constats de terrain à traiter par des moyens physiques.

### ~~FIX-FUSION-ALTITUDE-10~~ (remplacé par FIX-12, historique)

**Problème constaté** : sur les projets avec un relief marqué, une grande
partie des fusions de secteurs sous-effectif (< `ABONNES_MIN_PAR_SECTEUR`)
tombaient dans le cas « aucun voisin altitude-compatible ». Dans ce cas,
`_fusionner_sous_effectifs_finale` (`core/pipeline.py`) retombait sur le
voisin **le plus connecté** (le plus de conduites frontalières), sans jamais
regarder si un *autre* voisin, moins connecté mais bien plus proche en
altitude, aurait donné un résultat moins mauvais. Sur le run de référence,
cela concernait 75 fusions sur 166, et corrélait directement avec les 13
secteurs à pression négative du rapport final (jusqu'à -77 m) et un taux de
conformité stricte de seulement 11/37.

**Correctif** : dans ce cas de secours (aucun voisin ≤
`ECART_ALT_MAX_FUSION_M`), le moteur choisit maintenant le voisin qui
**minimise l'écart d'altitude résultant** de la fusion, et non plus le plus
connecté. Le journal indique désormais l'écart retenu pour chaque fusion de
secours (`FUSION ... écart retenu=XXm`) afin de pouvoir vérifier l'effet sur
vos propres données.

Ce correctif ne change ni la géométrie/topologie des secteurs (déjà traitée
par FIX-TOPOLOGIE-09), ni les cas structurels réels signalés par le
diagnostic (déficit de capacité, réservoirs isolés, écarts topographiques
> `SEUIL_ECART_TOPO_M` non résorbables sans nouvelle infrastructure) : ceux-ci
restent des constats de terrain à traiter par des moyens physiques (nouvelle
conduite, PRV, réservoir relais), pas par l'algorithme.

### FIX-TOPOLOGIE-09

1 secteur = 1 polygone simple, anti-chevauchement structurel, anti-vide,
anneaux intérieurs supprimés.

## Installation

1. **Copier** le dossier `sectorisation_hydraulique/` dans votre répertoire d'extensions QGIS :
   - Windows : `C:\Users\<nom>\AppData\Roaming\QGIS\QGIS3\profiles\default\python\plugins\`
   - Linux : `~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/`

2. Le moteur de sectorisation (`core/pipeline.py`) et tous les modules `core/`
   sont **déjà inclus** dans l'archive — rien à ajouter séparément.

3. **Activer l'extension** dans QGIS : menu `Extensions → Installer/Gérer → Installées`.

4. **Créer une icône** (optionnel) : placez `icon.png` et `icon_config.png` dans `resources/`.

### Mise à jour depuis une version antérieure

Les versions antérieures portaient des suffixes de build dans les noms des
fichiers (`pipeline_v67.py`, `executer_v68_topologique.py`) et des couches de
sortie (`secteurs_hydrauliques_v67`, `secteurs_limites_v67`,
`frontieres_secteurs_v69`, `conduites_sectorisees_v67`, …). Ces suffixes ont
été supprimés au profit d'une version unique du plugin :

1. **Remplacer entièrement** le dossier `sectorisation_hydraulique` du profil
   QGIS par celui de l'archive, supprimer tout `__pycache__` résiduel et
   redémarrer QGIS complètement.
2. **Relancer la sectorisation** : les couches de sortie sont régénérées avec
   les nouveaux noms (`secteurs_hydrauliques`, `secteurs_limites`,
   `frontieres_secteurs`, `conduites_sectorisees`, `vannes_PRV_frontieres`,
   `reservoirs`, `diagnostic_reservoirs_viables`, `abonnes_pression_hors_plage`).
   Les couches anciennes peuvent être supprimées du projet après vérification.
3. **Configuration sauvegardée** : un fichier JSON enregistré avec une version
   antérieure est automatiquement complété au chargement (`core/config_manager.py`)
   — aucune clé n'est perdue, et la clé `ALIGNER_SUR_SECTEURS_V67` est
   renommée `ALIGNER_SUR_SECTEURS_REFERENCES` avec sa valeur conservée.
   Le socle topologique peut aussi pointer sur une couche de référence
   ancienne via les clés `COUCHE_REFERENCE_SECTEURS` /
   `COUCHE_REFERENCE_CONDUITES` (onglet Avancé) si vous ne souhaitez pas
   relancer la sectorisation.

## Structure des fichiers

```
sectorisation_hydraulique/
├── __init__.py               # Point d'entrée QGIS
├── plugin_main.py            # Classe principale, toolbar, menu
├── metadata.txt              # Métadonnées QGIS
├── config_defaut.json        # Configuration par défaut (tous les paramètres)
├── README.md
├── REFERENCES.md            # Références Louvain, Alvisi, DMA et validation
├── core/
│   ├── __init__.py
│   ├── config_manager.py     # Chargement / sauvegarde / validation JSON
│   ├── pipeline.py       # Moteur hydraulique (partition de référence)
│   ├── partition_secteurs.py # Partition stricte des îlots urbains 
│   ├── geom_backend.py       # Abstraction géométrique QGIS / Shapely
│   ├── graphe_topologique.py # Socle graphe (Dijkstra, Louvain, "qui alimente qui")
│   ├── validation_wntr.py    # Validation solveur indépendante des pressions finales
│   ├── consolidation_secteurs.py  # agrégation sur secteurs_hydrauliques
│   ├── polygones_topologiques.py  # Habillage cartographique du socle
│   └── executer_topologique.py # Orchestration du socle + consolidation
├── ui/
│   ├── __init__.py
│   ├── dialog_config.py      # Dialogue configuration (onglets Simple + Avancé)
│   └── panneau_principal.py  # Panneau latéral QGIS (dock panel)
├── outils/
│   ├── extraire_osm_obstacles.py   # Import routes/bâtiments OSM (optionnel)
│   └── verifier_installation_plugin.py  # Contrôle "est-ce bien CE zip qui tourne ?"
├── tests/
│   ├── test_partition_synthetic.py      # Partition des ilots urbains (hors QGIS)
│   └── test_consolidation_secteurs.py   # Compte de secteurs (hors QGIS)
└── resources/
    ├── icon.png              # Icône principale (optionnel)
    └── icon_config.png       # Icône configuration (optionnel)
```

## Utilisation

1. Ouvrez votre projet QGIS avec les couches AEP, abonnés, réservoirs.
2. Le **panneau latéral** s'ouvre automatiquement à droite.
3. Cliquez **⚙️ Modifier la configuration** pour :
   - Onglet **Paramètres essentiels** : noms des couches, pressions, plages abonnés, scission altimétrique.
   - Onglet **Avancé** : tous les paramètres du moteur (Hazen-Williams, Voronoï, redistribution…).
4. **Chargez / sauvegardez** votre configuration en JSON (bouton 📂 / 💾).
5. Cliquez **▶ Lancer la sectorisation** — le journal affiche la progression en temps réel.
6. Les couches de résultat apparaissent dans le projet QGIS :
   - `secteurs_hydrauliques`
   - `secteurs_limites` et `frontieres_secteurs`
   - `secteurs_hydrauliques_topo` (socle topologique — **même nombre de
     secteurs** que `secteurs_hydrauliques` depuis l'introduction de la consolidation, avec les champs
     `sous_zones` et `antennes_fid`)
   - `conduites_sectorisees`
   - `vannes_PRV_frontieres` et `vannes_sectorisation` (une vanne d'isolement par conduite frontière)
   - `reservoirs`
   - `diagnostic_reservoirs_viables`
   - `abonnes_pression_hors_plage`, `pressions_wntr`, `abonnes_hors_secteur` et `conduites_hors_secteur`

## Limites de secteur suivant les routes (couche `secteurs_limites`)

Par défaut (d'origine), la couche `secteurs_limites` — la vraie limite
visuelle de chaque secteur — était un simple **Voronoï sur les nœuds du
réseau**, sans aucun rapport avec les routes, bâtiments, stades, hôpitaux ou
voies ferrées réels. C'est ce qui explique des limites de secteur qui
« coupent » un bâtiment ou traversent un stade sans suivre aucune voie.

Cette étape a été refondue :

1. **Extraction des routes** : toutes les routes de la couche `ROUTE_LAYER`
   sont extraites dans l'emprise de travail (conduites AEP + réservoirs +
   abonnés, `INCLURE_TANK_ABONNE_DANS_EMPRISE`), puis bufferisées
   (`LARGEUR_TAMPON_ROUTE_M`) pour former une trame de coupure.
2. **Découpage en îlots** : l'emprise de travail, moins la trame de coupure,
   forme des « îlots » urbains dont le contour suit les routes.
3. **Affectation** : chaque îlot est rattaché au secteur hydraulique
   majoritaire parmi les nœuds du réseau qu'il contient (les îlots sans nœud
   sont rattachés au secteur voisin avec la plus longue frontière commune,
   comme pour l'ancienne élimination des îlots).
4. **Protection des obstacles** : les bâtiments (`BATIMENT_LAYER`), stades
   (`STADE_LAYER`), hôpitaux (`HOPITAL_LAYER`) et la voie ferrée
   (`CHEMIN_FER_LAYER`, en ligne ou en polygone) sont retirés de la trame de
   coupure et « recollés » entièrement à un seul îlot : ils ne peuvent donc
   jamais être scindés entre deux secteurs.

Toutes ces couches sont **optionnelles** (nom vide ou couche absente = étape
ignorée) :
- Si `ROUTE_LAYER` est introuvable ou `ACTIVER_DECOUPE_ROUTES` est à `false`,
  le plugin revient automatiquement à l'ancien comportement (Voronoï pur).
- `BATIMENT_LAYER` / `STADE_LAYER` / `HOPITAL_LAYER` / `CHEMIN_FER_LAYER`
  peuvent être renseignées indépendamment les unes des autres.

Paramètres dans `config_defaut.json`, section `DECOUPE_ROUTES` (aussi
disponibles dans l'onglet Avancé → « Découpe par les routes ») :

| Paramètre | Rôle |
|---|---|
| `ACTIVER_DECOUPE_ROUTES` | Active/désactive toute la fonctionnalité |
| `INCLURE_TANK_ABONNE_DANS_EMPRISE` | Étend l'emprise (et donc l'extraction des routes) aux réservoirs et abonnés, pas seulement aux conduites AEP |
| `LARGEUR_TAMPON_ROUTE_M` | Demi-largeur de route utilisée pour creuser la coupure |
| `LARGEUR_TAMPON_VOIE_FERREE_M` | Idem pour la voie ferrée, si elle est fournie en ligne |

**Important** : ceci ne change QUE la couche de dessin final
(`secteurs_limites`). Les calculs hydrauliques (pressions, débits,
affectation des abonnés aux secteurs) restent entièrement pilotés par
`secteur_de_noeud`, c'est-à-dire par le réseau et non par la géométrie des
routes — la découpe par routes habille géométriquement un résultat déjà
calculé, elle ne le modifie pas.

### Rebouchage des vides le long des rues

La bande retirée autour de chaque route reste, par construction, un vide
dans l'emprise de travail — y compris entre deux îlots voisins **du même
secteur** séparés par une rue interne, ce qui donnerait un vide permanent
dans `secteurs_limites` sans traitement supplémentaire.

Une première approche (fermeture morphologique par buffer symétrique,
secteur par secteur) s'est révélée peu robuste dès que les secteurs sont
très finement entremêlés — ce qui arrive typiquement avec la scission
altimétrique (des bâtiments voisins peuvent appartenir à deux secteurs
différents si l'écart de cote piézométrique l'exige). Avec des milliers de
morceaux entremêlés, les soustractions successives grignotaient une bonne
partie du gain de fermeture.

La méthode retenue est plus robuste : chaque petit morceau du vide (la
bande de route retirée, décomposée en fragments individuels) est
directement **rattaché au bloc final le plus proche** (par distance),
principe similaire à un diagramme de Voronoi localisé au vide lui-même.
Cela referme systématiquement les vides internes à un même secteur (le
bloc le plus proche est alors du même secteur des deux côtés) et crée une
frontière raisonnable entre secteurs différents, quelle que soit la
complexité du carrefour.

La délimitation de secteurs hydrauliques (DMA) par SIG à partir du réseau
viaire est documentée notamment dans Ciaponi et al., *« Geospatial and
Hydraulic Simulation to Design District Metered Areas for Large Water
Distribution Networks »*, ASCE Journal of Water Resources Planning and
Management, 146(7), 2020.

## Extraction automatique des bâtiments/stades/hôpitaux/voie ferrée (OSM)

Si vous n'avez pas déjà ces couches dans votre projet, `outils/extraire_osm_obstacles.py`
les télécharge automatiquement depuis OpenStreetMap (API Overpass, gratuite)
et les ajoute au projet QGIS avec les bons noms (`batiment`, `stade`,
`hopital`, `chemin_fer`). À lancer dans la Console Python de QGIS :

```python
exec(open(r"CHEMIN\COMPLET\vers\extraire_osm_obstacles.py", encoding="utf-8").read())
```

Nécessite une connexion internet et une couche AEP/routes déjà chargée dans
le projet (sert à déterminer la zone à interroger). Les couches créées sont
en mémoire : pensez à les exporter en GeoPackage si vous voulez les garder
d'une session à l'autre. Voir l'en-tête du script pour le détail et les
limites (couverture OSM variable selon la zone, temps de réponse Overpass).

## Configuration JSON

Le fichier `config_defaut.json` contient tous les paramètres organisés par section.  
Vous pouvez créer plusieurs fichiers JSON pour différents projets ou scénarios et les charger via le bouton 📂.

### Sections du JSON

| Section | Contenu |
|---|---|
| `COUCHES` | Noms des couches QGIS et chemin du MNT |
| `CHAMPS` | Noms des champs attributaires |
| `HYDRAULIQUE` | Pressions, Hazen-Williams, pertes de charge |
| `PLAGES_SECTEUR` | Min/max/idéal abonnés par secteur |
| `ALTIMETRIQUE` | Scission altimétrique PRV virtuels  |
| `VITESSES` | Contrôle des vitesses de conception conduites structurantes |
| `BOUCLE_ADAPTATIVE` | Cycles, pénalités, durcissement |
| `REDISTRIBUTION` | Redistribution par sous-arbre |
| `RAFFINEMENT` | Raffinement local |
| … | Voir `config_defaut.json` pour la liste complète |

## Dépannage

- **"core/pipeline.py manquant"** → le fichier est inclus dans l'archive ; vérifiez que le dossier plugin a bien été copié dans son intégralité (et redémarrez QGIS).
- **Couches introuvables** → Vérifiez les noms dans l'onglet "Paramètres essentiels".
- **Erreur CRS** → Assurez-vous que toutes les couches sont en EPSG:32632 (ou le même CRS).
