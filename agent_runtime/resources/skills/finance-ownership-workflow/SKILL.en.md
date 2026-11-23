---
name: finance-ownership-workflow
description: Inspect the current ownership snapshot and entities, then use dedicated finance tools for largest shareholders, control chains, ultimate control roots, group trees, and look-through economic interests. For corporate ownership and control analysis, not price forecasting, social-network communities, or general shortest paths.
metadata:
  version: 1.2.0
  domain: finance_ownership
  risk_level: low
  igraph:
    requires_tools: [finance_inspect_ownership, finance_first_holder, finance_control_chain, finance_group_tree, finance_economic_interest]
    requires_graph_state: loaded
    activates_on_intents: [first_holder, ownership_control, group_tree, economic_interest]
---

# Ownership analysis workflow

Create an ownership projection from the property graph loaded in the current session and calculate on the server. Explain only the facts, candidates, statuses, and evidence returned by the tools. Do not invent algorithms or rules.

## Default controller definition
In this project, requests for an actual or ultimate controller default to an equity-only interpretation. Use `policy.mode="equity_only"` consistently for inspection and calculation. Identify the equity-based control root from the strict voting-control chain returned by `finance_control_chain`. The largest shareholder, a single ownership-path product, and ordinary graph reachability cannot substitute for control determination. Check other control rules only when the user explicitly requests them.

By default, do not add GP, legal-representative, appointment, or contractual-control rules as extra requirements, or halt ownership analysis, group trees, or highlighting because those rules are missing. Preserve returned statuses and `rule_status`, and label results as equity-based controllers/control roots. This does not confirm an ultimate controller in the full business or legal sense. Still report ownership conflicts, insufficient coverage, cycles requiring review, and computation truncation; equity-only scope does not remove these limitations.

## Inspect data and entities first
1. After reading this skill, call `finance_inspect_ownership`. If a target is provided, search it with `query`; otherwise inspect the snapshot with empty arguments. To expand direct shareholder facts, inspect the resolved `target_id` specifically and check completeness. Do not filter a few bounded global facts and present them as all shareholders of a target. This tool reads the current session graph directly; do not pass guessed graph IDs, session IDs, or paths.
2. If the tool explicitly says no graph is available, use the dataset-status workflow to load the user-selected ready version, then inspect again. Do not reconstruct a graph for a finance query, retrieve the complete graph with `get_constructed_graph`, or substitute workspace Python, general reachability, or community tools for finance calculations.
3. From the quality report, check whether input can be projected, the snapshot, entity/ownership counts, ratio formats, duplicates, and ownership-total anomalies. Construction readiness does not establish finance data quality; algorithm completion does not establish complete business rules. Let dedicated tools determine each issue's effect on the current target. Do not ignore conflicts or invalidate every result because of an unrelated target's anomaly. An institution-only graph can have no personal records; do not invent people to satisfy a table count. If ownership records reference missing people, use the tool's missing-table or reference error.
4. Search the user's business ID or name with `query`. Read `entity_id`, `business_id`, `name`, and source identifiers from `result.entities`, preferring returned `entity_id` values for subsequent calls. Names are search terms, not grounds for merging people or inferring legal-representative identities. Narrow ambiguous matches or `AMBIGUOUS_ID`; if still unclear, ask the user to select an entity instead of choosing the first match.

Strings in these examples are argument placeholders. Replace them with the user's original query or entity IDs actually returned by the tools; they are not test data or preset answers.

```json
{"tool":"finance_inspect_ownership","arguments":{"query":"<user-provided entity ID or name>","display_limit":20}}
```

## Choose the calculation required by the question
Use the same snapshot and `policy`. After resolving IDs, execute only required branches. Independent branches may be batched; dependent branches must wait for actual results.

For every distinct `policy` branch, first call `finance_inspect_ownership` with the same policy as its calculation. Reinspect when changing or adding stop/exclude rules. A default-policy inspection or ordinary identity lookup does not replace that branch's policy inspection.

| Question | Tool and interpretation |
|---|---|
| Largest shareholder or candidates | `finance_first_holder`. Preserve all ties and distinguish direct-shareholder anchors from accumulated economic roots. Do not relabel largest-shareholder candidates as ultimate controllers. |
| Who controls a company, control chains, ultimate control roots | `finance_control_chain`. Explain intermediate entities, ultimate roots, and contribution chains from strict control results. Economic path products do not replace voting-control closure. |
| Controlled group or group tree of a root | `finance_group_tree`, with a user-specified and resolved `root_id`. If only a member company is given, first run `finance_control_chain`, then use its equity-based root for the group query, retaining quality and rule limits. If there is no unique root, present unresolved/multiple candidates rather than substituting a largest shareholder or community center. |
| Look-through percentages, indirect economic interests, economic-root ranking | `finance_economic_interest`. Without a specified upstream entity, explain final economic-root rankings. For a specified entity, pass optional `root_id` and read `requested_interest`. It may be an intermediate shareholder or control root, not necessarily an input-graph boundary root. For SCCs, use the equation solver's status. Do not delete cycles manually or replace complete accumulation with displayed simple-path sums. |

```json
{"tool":"finance_first_holder","arguments":{"target_id":"<resolved target entity_id>","evidence_top_k":5}}
```
```json
{"tool":"finance_control_chain","arguments":{"target_id":"<resolved target entity_id>","evidence_top_k":5}}
```
```json
{"tool":"finance_group_tree","arguments":{"root_id":"<user-specified or tool-confirmed root entity_id>","display_limit":100,"evidence_top_k":5}}
```
```json
{"tool":"finance_economic_interest","arguments":{"target_id":"<resolved target entity_id>","display_limit":100,"evidence_top_k":5}}
```

For the control root's look-through economic interest in a target, first obtain the corresponding root from `finance_control_chain`, then pass it unchanged as `root_id` to the economic tool. `equity_controller_id` denotes only the equity-based root; retain missing-business-rule information in `rule_status`. Query multiple roots separately or list them as candidates; do not choose one or add them arbitrarily. For an explicitly specified upstream entity, resolve it with the inspection tool without first requiring it to be a controller.

```json
{"tool":"finance_economic_interest","arguments":{"target_id":"<resolved target entity_id>","root_id":"<user-specified or returned upstream entity ID>","display_limit":100,"evidence_top_k":5}}
```

When chaining control results into group or economic calculations, prefer the `I:`/`P:` identifiers in `equity_controller_entity_id` or `ultimate_controller_entity_ids`. Match group edges using `source_entity_id` / `target_entity_id` and control proofs using `controller_entity_id` / `controlled_entity_id`. Do not merge institutions and people with identical raw business IDs or create false self-loops. Raw business IDs remain suitable for display.

Distinguish three quantities: `requested_interest` is the specified entity's complete accumulated economic interest in the target; a displayed path's `product` is that path's product; final economic-root rankings accumulate by input-graph boundary roots. Neither a single control-chain product nor a limited Top-K path sum replaces `requested_interest`. A control root need not appear in the final economic-root ranking.

`target_interest` is the target node's own solution in `(I-A)s=d; d[target]+=1`. It is the unit injection 1 without feedback and can exceed 1 with feedback. It is not paid-in capital, total direct ownership, shareholder coverage, or proof that all ownership has been identified. Use direct-ownership facts and corresponding quality fields to assess coverage.

`root_interests` is not a mutually exclusive partition of 100% ownership, especially when walks from boundary roots inside cycles overlap. Do not infer remaining interests by subtracting summed root interests from 1, or allocate that difference to intermediates, cycle members, or unrecorded shareholders. `expanded_states` counts search expansion states, not all paths. Without an explicit complete path count, report only displayed path count and `evidence.complete`.

Before explaining path contributions for a specified entity, verify `evidence.path_scope="requested_upstream"`, `requested_root_entity_id`, and path endpoints against this `root_id → target_id`. Unspecified `boundary_roots` use a different scope. If scope is absent or inconsistent, report only the returned total and explain that the paths do not support its decomposition. Do not mix `root_interests` or other roots' paths into this entity's contributions, sum them, or subtract them from `requested_interest`. Displayed paths are bounded evidence. Do not infer remaining-path contributions from total minus Top-K sums; with SCCs, simple paths especially cannot be subtracted from totals including cyclic walks. Use a decomposition only if the tool directly provides it for the same scope and calculation definition.

The group's main view uses only returned CONTROL members and relationships, with confirmation still constrained by quality and rule status. Prefer `finance_group_tree`'s `tree_edges` and `additional_edges` for hierarchical text, tables, or Mermaid, using returned business names and IDs. Tree edges are a display structure; control determination uses `control_dag`. Do not call every display edge independently confirmed control.

Under default `equity_only`, label the result equity-based members/group hierarchy. An empty `primary_faction_id` does not permit claiming a fully confirmed business group. CONTROL membership does not supply missing GP, legal-representative, or special-entity rules.

PATH is economic evidence and AFFINITY is an association candidate; neither belongs in confirmed primary membership. Do not remove cross-branch contributions, multiple parents, or SCCs to force a single tree. Text or Mermaid can explain hierarchy, but an explicit request to show/highlight a group tree, nodes, or paths on the graph requires `visualize_subgraph` after the dedicated calculation. Text, tables, or Mermaid alone do not establish that highlighting occurred.

For a repeated highlight request, reuse verified dedicated results only for the same graph and snapshot, and call `visualize_subgraph` again this turn. Historical calls do not replace current display. A repeated request does not prove a frontend rendering failure; do not invent a frontend fault, refuse to repeat the action, or switch to an image. Treat it as a failure only if the current tool actually fails.

After largest-shareholder analysis, highlight the target, returned candidates, and returned direct shareholders together using `visualize_subgraph`. Use this turn's `result.direct_holders`, not potentially incorrect historical IDs. If `direct_holders_truncated=true`, state that only a subset is shown. Put the target and direct holders in the same `highlights.subgraphs.nodes` group to include intermediate ownership-record nodes and actual edges automatically. Use `highlights.nodes` to distinguish target and candidates. Usually omit `node_names` to preserve the ordinary background. Labels follow the current interface language.

Finance graphs support exact node mapping for business IDs and `I:`/`P:` entity IDs, but prefer returned `source_node_ids`. If a node is absent, check this turn's source IDs. This does not establish that three-table finance graphs cannot be highlighted; do not invent that limitation.

Map dedicated-result entities via `source_node_ids` and ownership-fact source nodes/edges to actual IDs in the current original graph. A business `entity_id` is not necessarily a canvas node ID. Include ownership-record nodes in PK/FK connection paths and preserve original endpoints/directions rather than inventing direct entity-to-entity edges. Mark members with `highlights.nodes` and actual relationships with `highlights.paths` or `highlights.subgraphs`. Prefer a visible edge color such as orange `#f97316`; highlighted nodes default to stars. Obtain missing mappings when needed; highlight only verified portions and explain their scope. Overlay highlights on the original graph, retaining unhighlighted nodes and edges.

The canvas shows original-graph evidence corresponding to dedicated group results. Do not call the raw record graph or a generic spanning tree a finance group, or treat extra association edges as CONTROL conclusions. Ordinary non-ownership paths continue to use their existing visualization workflow.

### Circular ownership and SCC highlighting
Use strongly connected components of the ownership projection for business-level circular ownership; directed-cycle detection on the raw FK graph is not a substitute. Highlight each returned SCC separately, including source record nodes and real FK edges for all internal ownership facts. Preserve closing edges, reverse holdings, and multiple records; do not show only a spanning tree, shortest path, or partial neighborhood. Each SCC's real member nodes can be a `highlights.nodes` group; PK/FK visualization adds actual internal ownership records and edges. Keep cross-SCC associations as ordinary background instead of merging them into one cycle.

Use star highlights for nodes and a visible color such as orange for internal ownership edges, with ordinary nodes and edges still visible. Verify returned members, relationships, and truncation information. If source or server budgets limit the result, label it a partial display instead of claiming complete SCC highlighting. Raw FK arrows still represent record-to-entity references; business ownership direction comes from finance facts.

## Rules, completeness, and bounded results
- The default `policy` is `equity_only`, using input ownership facts only. If `RULE_DATA_MISSING` is returned, state that broader business rules are incomplete without requiring GP/legal-representative/special-entity rules for the current equity-only analysis. Report control roots or candidates within equity scope; do not promote them to full business confirmation. Do not infer these relationships from names or fabricate `rule_relations` / `rule_coverage`.
- Stopping at deregistered entities is disabled by default. Only when explicitly requested, pass the same policy to inspection and related calculations. For the Chinese source status meaning deregistered, use `policy: {"stop_statuses":["\u6ce8\u9500"]}` (JSON escapes preserve the stored value). Other stop/exclude lists use only user-provided or verified IDs. Otherwise retain defaults and explain the chosen policy. Do not silently change filters or raise `min_edge_ratio` to reduce work.
- Control uses strict `>0.5`. Exactly `0.5` means that edge alone does not establish control, not that the entire target has no controller. Count control contributions by controlled shareholder; do not count multiple paths to the same shareholder repeatedly as voting power. Largest-shareholder ties depend on returned candidates and equal accumulated values, not merely the absence of a direct majority. A unique largest economic-root candidate can exist without any direct majority holder.
- For each target, first read top-level `status`, the target-specific effect of `quality`, and `review_reasons` before drawing a definite conclusion. Algorithm diagnostics in `analysis_status` / `control_status` do not override this gate. `NO_SINGLE_CONTROLLER` under `DATA_CONFLICT` means the current calculation did not identify one controller but the conflict prevents confirmation, not confirmed absence of strict control. Upstream anomalies can affect the target; a normal direct total or an edge exactly at one-half does not override a reported upstream conflict.
- Do not turn `DATA_CONFLICT` into confirmed largest-shareholder/control/primary conclusions. Candidate IDs may be diagnostic only. Preserve all simultaneous review reasons. `SCC_REVIEW` does not mean a cycle has been merged into a controller; do not invent precise interests under `NON_CONVERGENT`.
  Quote largest-shareholder candidates only from `first_holder_ids`. If it is empty and `first_holder_id=null`, state that no largest-shareholder candidate was returned. Do not supply candidates from direct holdings or the control branch, even for a 100% direct shareholder. Control candidates and largest-shareholder candidates are distinct results.
  Limitations are independent: fixing ownership totals does not automatically resolve SCC_REVIEW or RULE_DATA_MISSING. Recommend verifying and rerunning all relevant checks, not promising that a corrected total will confirm a particular largest shareholder or controller.
- `evidence_top_k` limits displayed evidence and `display_limit` limits returned lists; neither should alter complete-calculation rankings. Start with few evidence paths and increase display parameters only when needed. Do not infer that a bounded list contains all shareholders or members.
- `max_nodes`, `max_edges`, and `max_seconds` are computation budgets. Distinguish budget truncation, unfinished calculation, incomplete data coverage, missing rules, and display pagination. HTTP success or candidate values alone do not establish completeness. Do not remove low-ratio edges, raise arbitrary thresholds, or replace full accumulation with Top-K paths to eliminate truncation.
- For unsupported input, rules, or scale, explain missing conditions and next steps from actual tool results. Do not repeat the same deterministic failure or switch to ad hoc Python to guess a successful answer.

## Answers and evidence
First answer the actual question about largest shareholders, control chains, group members, or economic interests. State each target's status and affected scope alongside its conclusion rather than appending only a generic warning. Then give the snapshot date, rule scope, key ratios/contributions, and source-edge evidence. Distinguish confirmed results, ties/candidates, quality conflicts, and incompleteness. Economic interests do not automatically establish ultimate beneficiaries. Display ratios as percentages if useful without changing calculation precision for thresholds or rankings.

Keep the following fields for each executed branch and check them against tool results before sending:

- For every entity, list top-level `status` separately from algorithm diagnostics `analysis_status` / `control_status`. Do not label diagnostics as overall status. Explicitly identify missing fields instead of inventing them. With `confirmed_id=null` or `RULE_DATA_MISSING`, use equity-based largest-shareholder candidate or equity-based control root. It is acceptable to say the equity calculation holds, but not to assert an unqualified confirmed ultimate controller or confirmable conclusion. `NO_SINGLE_CONTROLLER` means none was identified under the current input and policy; it does not rule out unprovided contractual or other arrangements.
- For largest-shareholder results, separately report `direct_ownership_total` and `direct_strict_majority_holder_count`. With `threshold_semantics="per_single_holder"`, majority applies to one entity. A combined total exceeding one-half does not establish a majority shareholder; absence of an individual majority does not mean the combined total is below one-half. Direct-shareholder tables or facts may be bounded. Compare `matched_fact_count` / `fact_count`, returned length, and truncation fields. Title incomplete tables "Displayed subset of direct shareholders". Quote the total only from `direct_ownership_total`, not a sum of limited facts. Economic-path evidence is not a direct-shareholder list.
- For group size, separately report `node_count` and `node_count_includes_root`, `controlled_member_count` excluding the root, and `tree_edge_count`, `additional_edge_count`, and `control_dag_edge_count`. Explain scope using `count_scope`. Do not substitute display-list lengths for full counts, conflate edge sets, or merge tree and additional edges and rename them the control DAG. Keep the equity-based members/group hierarchy qualifier whenever `primary_faction_id` is empty.
- For cycles, distinguish economic values from control judgments. Equation convergence supports numerical calculation only; it does not remove `SCC_REVIEW` or control-candidate confirmation requirements, or prove that cycles do not affect control determination. Even with a direct strict-majority holder, inspect `economic_status`, `component_solutions`, and every `review_reasons` entry. Under `NON_CONVERGENT`, do not present a direct-holder candidate as the definite largest shareholder, though original direct facts can still be listed. If complete economic rankings or values are needed and the largest-shareholder tool returns only direct evidence, call `finance_economic_interest` with the same target and policy. Do not infer convergence from direct ratios. An external economic root's largest-shareholder diagnostic may be `COMPLETE`; do not change it to `SCC_REVIEW` merely because a cycle exists elsewhere. Obtain control conclusions from the control tool.
- Correct source data only after verifying authoritative records. When total ownership exceeds 100%, do not normalize or proportionally scale it to hide anomalies or manufacture confirmation. For nonconvergence, do not suggest invented outside shareholders, cycle deletion, or stop/exclude rules to force solvability. Authoritative GP/legal-representative rules affect their corresponding business judgments; they do not alter the original ownership matrix or make economic equations converge. Recalculate only after verifying and correcting actual ownership sources. Explicitly requested scenario analysis must be labeled as different input/policy, not substituted for the original conclusion.
- Distinguish whole-snapshot statistics from the target's component. Changes in SCC counts across policies do not prove that a particular entity or relationship caused them. Without target-component and source-edge evidence, report each statistical scope without guessing causation or association. `coverage_gap_institutions` counts institutions whose direct ownership total is below 1, including institutions with no shareholder records and those with partial records. Do not describe all of them as having no recorded upstream shareholders or use this count to infer target-path or full-graph completeness.

Prefer returned business IDs, names, direct ratios, controlled voting power or economic interests, and bounded source chains. Do not print the complete input graph, internal session metadata, or server paths. Identify missing evidence rather than filling gaps with general knowledge or company names.

## Reference cases and ground truth
For verification, distinguish the case's declared `expected` values, the reference program's actual output, and the system tool output. Explicitly note differences between declarations and actual reference output. Do not treat old assertions as observed ground truth or modify original ratios or quality checks merely to match them.

Compare under the same ownership direction, entity types, snapshot, ratio units, and filtering policy. For control, compare final roots, intermediate controllers, and strict voting power. For economics, compare complete root rankings, accumulated values, SCC members, and convergence. Explain semantic differences in rule names first. Displayed path counts are bounded by Top-K and depth and are not a correctness standard for complete interests. State a tolerance when comparing floating-point reference values to Decimal strings, without premature rounding that changes rankings.

Report separately whether computation ran, whether numbers match, and whether business conclusions can be confirmed. A reference program that skips ownership-total checks may produce the same numbers while the system reports `DATA_CONFLICT`. List conflict sources, top-level statuses, and algorithm diagnostics by target instead of calling this a generic algorithm failure. Economic-path sums may exceed 100%; preserve actual tool values and explain quality and model scope rather than capping or normalizing. Preserve tied candidates; different evidence paths with equal ratios are not duplicates.
