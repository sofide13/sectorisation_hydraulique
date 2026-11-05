# Références méthodologiques

## Détection de communautés et partitionnement

- **Blondel, V. D.; Guillaume, J.-L.; Lambiotte, R.; Lefebvre, E. (2008).** *Fast unfolding of communities in large networks*. **Journal of Statistical Mechanics: Theory and Experiment**, 2008(10), P10008. DOI : [10.1088/1742-5468/2008/10/P10008](https://doi.org/10.1088/1742-5468/2008/10/P10008).
  - Usage dans le projet : l'appel `networkx.algorithms.community.louvain_communities` propose une première partition des sous-graphes surdimensionnés. Cette partition est ensuite soumise aux contraintes hydrauliques, topologiques et de taille des abonnés ; elle ne constitue pas à elle seule la sectorisation finale.

## Conception des DMA

- **Alvisi, S. (2015).** *A New Procedure for Optimal Design of District Metered Areas Based on the Multilevel Balancing and Refinement Algorithm*. **Water Resources Management**, 29(12), 4397–4409. DOI : [10.1007/s11269-015-1066-z](https://doi.org/10.1007/s11269-015-1066-z).
  - Fiche complétée : procédure de conception automatique de DMA fondée sur le partitionnement multiniveau et le raffinement, avec simulation hydraulique pilotée par la pression pour évaluer les solutions ; résolution simultanée de l'affectation des nœuds, du nombre de districts et du choix des liaisons inter-districts à maintenir ou à fermer avec compteurs/vannes. Dans ce projet, cette logique motive la séparation entre partition candidate, raffinement, frontières et validation hydraulique indépendante par WNTR.

- **Morrison, J.; Tooms, S.; Rogers, D. (2007).** *District Metered Areas: Guidance Notes*. IWA Water Loss Task Force.
- **AWWA (2016).** *M36 Water Audits and Loss Control Programs*, 4th ed. American Water Works Association.
- **Perelman, L.; Ostfeld, A. (2011).** *Topological clustering for water distribution systems analysis*. Environmental Modelling & Software.
- **Diao, K.; Zhou, Y.; Rauch, W. (2013).** *Automated creation of district metered area boundaries in water distribution systems*. Journal of Water Resources Planning and Management.

## Validation hydraulique

- La validation finale est réalisée, lorsque le paquet est installé, avec `WNTRSimulator`. Les conduites inter-secteurs sont fermées dans le modèle lorsque `FERMER_CONDUITES_FRONTIERES_WNTR=true`, puis les pressions WNTR sont comparées aux bornes de conception configurées.
- Les plages par défaut du projet sont **10–60 m** pour la pression, **0,3–2 m/s** pour la vitesse et **500–3000 abonnés** par secteur. Ce sont des **paramètres de conception configurables**, pas une déclaration de conformité normative.
