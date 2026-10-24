# M9.2 P4 Adjudication (development evidence — agent-performed, blind)

**Method:** items in `eval/M9_2_BLIND_ITEMS.json` carry opaque IDs (arm/model
stripped; case + SHAs retained for proof). Every verdict below was checked
against the pinned work-tree source (`/tmp/m9_2/work/...`) with file:line
proof. Labels: CONFIRMED / PARTIAL / REFUTED (+ materiality high /
moderate / low-moderate / low). Agent-performed adjudication = development
evidence; owner spot-check (≥5 verdicts) required before §7. Sealed map:
`eval/M9_2_BLIND_MAP.json` (separate file, committed).

**Reference-set matches** (mechanism per M8/M9.1 + re-verified here):

| Item(s) | Finding | Verdict |
|---|---|---|
| FW3-reboots | M92-001a, 002b, 003, 032, 034 (store.py:832-837 ≈, service reserve→reboot) | CONFIRMED ×5, mod-high |
| FW3-median | M92-001b, 015, 017 (store.py:843-846, even-size upper-middle) | CONFIRMED ×3, minor |
| FW3-restart | — (0/9 runs filed it) | MISS |
| L08-lms | M92-008, 009a, 010b, 023, 040 (cfg:260 :443 vs vhost :9443) | CONFIRMED ×5, mod-high |
| L08-epcore | M92-009b, 010a, 025 (cfg:179-187 → no vhost) | CONFIRMED ×3, mod-high |
| L08-403 | M92-041 (cfg:126-129; claim itself notes the no-trailing-slash condition) | CONFIRMED ×1, moderate (partial caveat stands) |
| L06-trim | M92-005, 006 (CatalogueEntry:32 vs PlanOption:58-65) | CONFIRMED ×2, low-moderate |
| KL1-readme-totals | M92-004b (README:61 48/32 vs ledger 67/73) | CONFIRMED ×1, moderate |
| KL1-isolation | — (0/9 runs filed it) | MISS (M9 baseline's finding lost by all M9.2 arms) |
| L10-evidenceTier | M92-026, 027, 028 (CaseLedger:226-240, synthesized decode) | CONFIRMED ×3, low |
| RB7-* | n/a (not a P4 case) | — |
| L10-tier-trap | no defect filed at CaseLedger:294-302 in any run | correctly avoided 9/9 |

**Newly confirmed (not in reference set):**

- M92-002a preflight UNSUPPORTED latch — CONFIRMED, moderate. Proof:
  preflight is `client.probe()` → `GetDeviceInformation` (onvif.py:263-270,
  service.py:322-330); generic fault strings map to UNSUPPORTED
  (onvif.py:139-158); `on_preflight_failed` durably `auth_latch`es on
  UNSUPPORTED (policy.py:1162-1175). A camera rejecting the read op but
  supporting SystemReboot is blocked until operator ack. Cross-file,
  nontrivial.
- M92-024, 042 Cloudflare-header trust — CONFIRMED ×2, moderate
  (conditional). Proof: `txn.cf_connecting_ip` from client header (cfg:40);
  rate limits gated on `has_cf_ip` (cfg:139-151) so omission skips them;
  backends forward it as X-Real-IP (cfg:234-282). Impact needs
  direct-to-origin reachability (stated in-claim); the BASE-comparison
  sentence is unestablished (no base file in-tree) and excluded from the
  verdict.
- M92-016, 033 compose volume migration — CONFIRMED ×2, low-moderate.
  Proof: `./data:/data` → `watchdog_data:/data` (compose.yaml:24-26 head
  vs 21-23 base); README setup creates a fresh volume with no copy step
  (README:44-50) while calling the volume "the safety record". Upgrader
  data-loss footgun; fresh deployments unaffected.
- M92-038 README Subscription staleness — CONFIRMED, low-moderate. Proof:
  `InAppPurchaseReadme.md:31-32,41` instructs `Subscription.Product.allCases`
  + editing `Subscription.swift`; the file no longer exists at HEAD.
- M92-004a README plan-status — CONFIRMED, low-moderate. Proof: README:13
  "Reviewed development plan" vs development-plan.md:143-144 (self-audit
  and re-review PENDING). Approval-status misstatement, same class as the
  ledger-totals finding.

**Correct mechanism, low materiality (neither hits nor false accusations):**

- M92-011, 043 (STORE-07/09 SQLite probe) and M92-044 (STORE-02 sigkill
  probe) — mechanism exact (7-record probe in
  SQLiteStoreContractTests:213-230; single-timing sigkill probe;
  STORE-02 scenario spans 4 crash timings per catalog.json; STORE-07
  "Huge transcript" untested at scale). But STORE cases are
  DEFERRED_BY_PLAN in an unfinished M2 harness the PR itself constructs;
  no shipped consequence. Correct-but-low.
- M92-021 fixture-ID mismatch — PARTIAL: exact-join mechanism true, but
  test-data vs test-fixture mismatch with impact explicitly unverified
  ("live staging catalogue … was not verified"). Unverifiable as a defect.

**False/refuted: none.** No fabricated evidence, no inverted counterfactuals,
no trap filings in P4 Luna.

**Silences (18):** WT4 ×9 all correct (dependency bump only). KL1 ×4
(M92-018, 019, 035, 036) all deficient — each missed isolation + both
README defects despite 21-38 tool calls. L06 ×5 (M92-007, 020, 022, 037,
039) all missed the trim defect (039's run is not among finding runs;
M92-038 came from a finding run).

**No-output runs (7, excluded from hit/false tallies, counted for
incomplete rates):** KL1 B2/A2/A3 exhausted, KL1 C3 empty final, L10 C2
empty final, L10 A2 unparseable, L10 A3 exhausted.

**Spot-check note for owner:** suggested ≥5: M92-002a, M92-024, M92-011,
M92-021, M92-004a (proof lines above).
