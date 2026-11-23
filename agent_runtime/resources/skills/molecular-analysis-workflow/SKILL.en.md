---
name: molecular-analysis-workflow
description: Analyze evidence for solubility and toxicity, ring structures, aromatic C–H borylation candidates, and symmetry-equivalent sites in the loaded molecular graph. Use for these properties or candidate-site highlighting; distinguish structural descriptors, measurements, and unverified hypotheses.
---

# From molecular structure to site analysis

## Scope and evidence
First verify the active graph and the backend-validated `graph_background`. For the bundled BO-01 case, use `borylation_case_analysis`, which reads the current graph. Do not apply this case's numbers or descriptors to other molecules, or carry old evidence across graph changes. Answer in the current interface language: start with a one-sentence conclusion, then give evidence and explanation, followed by a summary.

Call the corresponding step and read its successful result. If structure reading fails, do not fabricate an answer from history. Stored descriptors were calculated offline with RDKit; deployment does not require RDKit. The structural fingerprint must match the current graph.

```json
{"tool":"borylation_case_analysis","arguments":{"step":"properties"}}
```
```json
{"tool":"borylation_case_analysis","arguments":{"step":"rings"}}
```
```json
{"tool":"borylation_case_analysis","arguments":{"step":"structure"}}
```
```json
{"tool":"borylation_case_analysis","arguments":{"step":"symmetry"}}
```

## 1. Solubility and toxicity
Present evidence in layers: original measurements → predictions from a specified model → qualitative structural hypotheses → current unknowns.
Give the method, units, and scope for each descriptor. Molecular weight, hydrogen-bond donors/acceptors, and TPSA describe structure. cLogP is a calculated octanol/water partition property, not logS in arbitrary organic solvents or measured solubility. Whether TPSA includes S/P depends on the tool's method.

Solubility depends on the specific solute, solvent, temperature, concentration, physical state, and equilibrium conditions. A 2D graph does not fully describe crystal packing or other solid-state factors. Aromatic scaffolds, polar groups, and hydrogen bonding can support explicit solvent-screening hypotheses, such as ethers like THF or polar aprotic solvents like DMF/DMSO/NMP; the last three are not low-polarity solvents. Explain the rationale and label it as requiring measurement. Do not invent g/L values or unsupported rankings of soluble/insoluble. A solvent in a reaction record, nominal loading concentration, or conversion rate does not prove complete dissolution; a failed reaction does not prove insolubility.

Toxicity conclusions require an endpoint, dose, exposure route, and test system. Without measurements or validated model results within their applicability domain, state that current evidence cannot establish whether the compound is toxic or nontoxic. Aromatic rings, halogens, heterocycles, cLogP, or drug-likeness are not empirical toxicity evidence for this compound. Do not label the mere presence of a halogen or aromatic ring as a confirmed structural toxicity alert; do not force an alert list without a specific rule, endpoint, and applicability domain. Do not claim that an amide bond has limited toxicity at normal doses or that a functional group is low-toxicity: dose and toxicity concern the complete compound and a specified endpoint, not an individual bond. Structural alerts are hypotheses for validation. Do not claim to have run an unexecuted QSAR model or that no information exists anywhere. Suggest checking literature/SDS and endpoint data for the verified identity without inventing compound names or CAS numbers.

## 2. Ring counts, types, and properties
Count chemical bonds rather than bidirectional storage edges. The two directions of one undirected bond do not form a two-membered ring. Distinguish ring count, number of ring types, an independent ring basis, and fused ring systems; state the counting convention for complex fused/bridged structures. In this case, rings are grouped by size, elemental composition, and aromaticity. Rings of the same type may have different substitution environments.

For every ring, list its display number, size, composition, and aromaticity. Ring numbers are not atom numbers. Aromaticity comes from chemical perception and atom attributes, not merely a drawn polygon or alternating bonds. An S/N-containing heterocycle is not an all-carbon benzene ring or a triazole. Explain aromatic conjugation, heteroatom effects on electron distribution, and substituent effects on the local environment. Uncalculated/unmeasured pKa, quantitative reactivity rankings, and mechanisms remain hypotheses.

## 3. Borylation candidates and actual highlighting
Candidates must be aromatic C atoms bearing H. Exclude fully substituted aromatic C, N–H, and other elements. This is a structural screen, not proof that all candidates are synthetically accessible or equally reactive. Ortho/meta/para relationships and substituent adjacency must use actual bonds (`bonded_neighbors` / `adjacent_ring_substituents`), not atom-number order or drawing positions. Two-dimensional topological distance is not three-dimensional steric hindrance. Without appropriate calculation or experiment, do not force site-specific rankings of crowding or reactivity. Discuss steric, electronic, and catalyst effects as hypotheses; distinguish reported sites from predictions.

When asked to mark sites on the graph, call `highlight_nodes` or `visualize_subgraph`. Map returned `atom_index` values to real current node IDs and explain using `display_number`. Read the tool schema instead of guessing fields. A text table does not replace actual highlighting. Claim sites are marked only after a successful receipt. Keep the current molecular structure in the workbench; do not switch to a reaction-record relationship graph.

## 4. Symmetry and nonequivalent positions
Group candidates using `symmetry_class` for the complete labeled molecular graph, accounting for elements, bonds, all substitution environments, and encoded stereochemistry/isotopes. Do not infer equivalence from drawing distances or a local ring mirror image. RDKit `CanonicalRankAtoms` with `breakTies=False` returns symmetry classes; internal class IDs are not displayed atom numbers. List each group's `display_numbers`, choose a representative, and verify that their union covers all candidates exactly once.

Different substituents do not automatically break every symmetry, and visually similar rings need not be equivalent. Topological equivalence does not imply equivalence in every chiral environment or dynamic conformation. Explain boundaries when the graph lacks relevant information. Distinguish an individual site from an equivalence class.

## Further experimental data
Use the `products`, `conditions`, and `plan` steps when needed. Product and condition tables are linked only through the substrate, so do not assign a condition to a particular product. Conversion is neither site selectivity nor isolated yield; a value below a threshold does not mean zero conversion.

## Method references
- [RDKit molecular descriptors](https://www.rdkit.org/docs/GettingStartedInPython.html) and [chemical perception and TPSA](https://www.rdkit.org/docs/RDKit_Book.html)
- [RDKit CanonicalRankAtoms](https://www.rdkit.org/docs/source/rdkit.Chem.rdmolfiles.html)
- [IUPAC definition of solubility](https://old.goldbook.iupac.org/html/S/S05740.html)
- [OECD QSAR assessment and applicability domains](https://www.oecd.org/en/topics/sub-issues/assessment-of-chemicals/quantitative-structure-activity-relationships-project.html)
