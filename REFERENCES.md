# Methodological references

## Community detection and partitioning

- **Blondel, V. D.; Guillaume, J.-L.; Lambiotte, R.; Lefebvre, E. (2008).** *Fast unfolding of communities in large networks*. Journal of Statistical Mechanics: Theory and Experiment, 2008(10), P10008. DOI: [10.1088/1742-5468/2008/10/P10008](https://doi.org/10.1088/1742-5468/2008/10/P10008).
  - Use in the project: `networkx.algorithms.community.louvain_communities` proposes a first partition of oversized subgraphs. That partition is then submitted to hydraulic, topological and subscriber-count constraints; it is not the final sectorisation by itself.

## DMA design

- **Alvisi, S. (2015).** *A New Procedure for Optimal Design of District Metered Areas Based on the Multilevel Balancing and Refinement Algorithm*. Water Resources Management, 29(12), 4397–4409. DOI: [10.1007/s11269-015-1066-z](https://doi.org/10.1007/s11269-015-1066-z).
  - Automatic DMA design based on multilevel partitioning and refinement, with pressure-driven hydraulic simulation to evaluate solutions. In this project, this logic motivates the separation between candidate partition, refinement, boundaries and independent hydraulic validation with WNTR.
- **Morrison, J.; Tooms, S.; Rogers, D. (2007).** *District Metered Areas: Guidance Notes*. IWA Water Loss Task Force.
- **AWWA (2016).** *M36 Water Audits and Loss Control Programs*, 4th ed. American Water Works Association.
- **Perelman, L.; Ostfeld, A. (2011).** *Topological clustering for water distribution systems analysis*. Environmental Modelling & Software.
- **Diao, K.; Zhou, Y.; Rauch, W. (2013).** *Automated creation of district metered area boundaries in water distribution systems*. Journal of Water Resources Planning and Management.
- **Ciaponi, C. et al. (2020).** *Geospatial and Hydraulic Simulation to Design District Metered Areas for Large Water Distribution Networks*. Journal of Water Resources Planning and Management, 146(7).

## Hydraulic validation

- Final validation is performed, when the package is installed, with WNTR. Inter-sector pipes are closed in the model when `FERMER_CONDUITES_FRONTIERES_WNTR=true`, then WNTR pressures are compared with the configured design bounds.
- Default ranges are **10–60 m** pressure, **0.3–2 m/s** velocity and **500–3000 subscribers** per sector. These are **configurable design parameters**, not a statement of normative compliance.
