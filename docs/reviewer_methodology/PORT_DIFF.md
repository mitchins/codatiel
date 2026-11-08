# Port diff: original guidebook → M9.2 arm-B/C instructions

`docs/reviewer_methodology/archive/REVIEWER_INVESTIGATION_GUIDEBOOK.md`
→ `docs/reviewer_methodology/guidebook_port.md`.
One-line justification per hunk; any hunk without one is a defect (none).

1. **Header + role/scope/evidence/tool preamble (new).** The rules-of-engagement companion is not sent to the model; the port must be self-contained, so scope, advisory posture, the evidence-ID grounding rule, and the 7 tool names are stated up front.
2. **§1: "changed hunks" → "with `diff_window`"; "surrounding code" → "with `read_file`".** Allowed change: legacy/editor tool vocabulary → the 7 I2 tools.
3. **§2: added "fetch both revisions … before concluding anything about either side".** P2 L10-evidenceTier lesson (M8 Luna cited the new field without grounding the old format); makes the compare-BASE-and-HEAD demand operational.
4. **§2 constant/policy bullet rewritten (predicate/access-framing → read-values/migration-framing).** Original text was auth-specific ("who gains and who loses access"); the port generalizes to all five in-scope detectors while keeping the 005 example.
5. **§3: tool names → `find_callers`, `lookup_docs`, `related_files`, `search_code`.** Allowed tool-vocabulary change; relationship-map discipline unchanged.
6. **§4: added direction check ("BASE must produce the old outcome and HEAD the new one, never the reverse").** P2 01E O-09 lesson (inverted counterfactual filed as a false accusation).
7. **§4 template: "cite file:line" → "file:line from a tool response, ev_...".** Allowed change: old citation scheme → M7.1 evidence IDs.
8. **§6 intentional-change bullet extended ("read the authorizing document itself…").** P2 O-07 lesson (treated intended behaviour as the defect) and L10-tier-trap lesson; raises the bar for the benign explanation without changing the check.
9. **§8: added blast-radius stop guard.** P2 O-15 lesson (stopped on the highest-blast-radius change); stopping now requires the largest change read.
10. **§9 table: outputs reworded to file-a-finding / silence / record.** Allowed change: legacy delivery wording ("public comment", "triage record/log") → the M7.1 shadow-only schema the service enforces; no claim-ladder terms imported (superseded, not ported).
11. **§9: added "a claim without tool evidence is not a finding".** Allowed change: output → existing M7.1 JSON + evidence-ID schema (matches the minimal prompt's final-message rule).
12. **New §10 "Prioritise on large PRs" (the single allowed addition).** P2-driven: NEVER_EXAMINED/BUDGET_EXHAUSTED_BEFORE are addressed by reading order (blast-radius first, authority skim, abandon-low-yield, report-unread). Generic: no repo, file, symbol, or defect names; no case-specific rules.
13. **Final-message schema appended.** Allowed change: the port must produce the M7.1 JSON the service parses; schema copied from the minimal prompt, not invented.
14. **Examples and §§5, 7 kept verbatim.** No tool, contract, or policy text in those sections needed porting; the FJ02/051/036/003/005/033/034 examples are synthetic and detector-agnostic.

Not imported: PR #6 impact-path content (nothing from `CHALLENGER_INSTRUCTIONS` appears above); claim ladder, publication policy, anti-gaming rules (superseded per RECOVERY.md §4).
