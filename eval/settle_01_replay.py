"""SETTLE-01 settling replay (no inference, read-only).

Replays recorded watch histories under pending-settle policies to measure
dispatches avoided, added latency and maximum-deferral fires.

Inputs are SANITISED measurements, not private sources: opaque head
labels, dispatch times as offsets in seconds from each PR's first
dispatch, and input-token counts from the runs' own metrics.json
(spend.usage). No repository names, file contents, diffs or credentials.
The private head-SHA mapping lives in the run archive, not here.

Method and its limits:
- SQLite pair records carry NO first-observation column (upsert_pair
  refreshes updated_ts on every discovery poll; verified in code). The
  scheduler sees heads at poll granularity (300s).
- For each head, first observation o(H) is bracketed: later than the
  previous dispatch (else it would have dispatched sooner under
  same-cycle dispatch with a free cap) and no later than its own
  dispatch poll. Right edge o=d(H); left edge o=d(prev)+eps
  (o_1 = d_1 - 300). Both edges are replayed; the range is reported.
- Historical arrival uncertainty is +/-5 minutes (one poll interval).
  Counts are robust for quiet periods >= 300s; latencies carry that
  jitter and are labelled approximate (trailing ~).
- Going forward, pending_settle.first_seen records true first
  observations, so future replays of settled operation are exact.

Usage: python3 eval/settle_01_replay.py
"""

POLL = 300
# Grid phases preserved from the original absolute-epoch replay so results
# are bit-for-bit reproducible: poll boundaries fall at PHASE (mod POLL)
# in offset space. A phase is a mod-300 residue and reveals nothing about
# absolute time. (Absolute dispatch epochs stay in the run archive.)
PHASE_PR15 = 51
PHASE_PR16 = 230
PHASE_WALL5 = 20

# (head label, dispatch offset seconds from first dispatch, input tokens)
PR15 = [
    ("H1", 0, 953245),
    ("H2", 618, 550371),
    ("H3", 1200, 926579),
    ("H4", 1822, 1311263),
    ("H5", 2391, 1038005),
]
PR16 = [("H1", 0, 1401796)]  # settle dispatch; a conditional follow-up
# after an incomplete is orthogonal and preserved as-is.
WALLTIDBITS5 = [("H1", 0, 2759)]


def arrivals(heads, edge):
    ds = [d for _, d, _ in heads]
    if edge == "right":
        return [d for _, d, _ in heads]
    out = [ds[0] - POLL]
    for prev in ds[:-1]:
        out.append(prev + 1)
    return out


def simulate(heads, arrivals_list, quiet, maxdef, phase):
    toks = {h: t for h, _, t in heads}
    first_seen_at = {h: o for o, h in zip(arrivals_list, [h for h, _, _ in heads])}
    order = sorted(zip(arrivals_list, [h for h, _, _ in heads]))
    start = min(arrivals_list) - quiet - POLL
    end = max(arrivals_list) + maxdef + quiet + POLL
    grid0 = ((start - phase) // POLL) * POLL + phase
    polls = list(range(grid0, end + POLL, POLL))
    pending = None  # [head, first_seen, last_change]
    reviewed = set()
    dispatches = []  # (head, dispatch_time, latency, by)
    idx = 0
    for p in polls:
        while idx < len(order) and order[idx][0] <= p:
            _, h = order[idx]
            idx += 1
            if h in reviewed:
                continue
            if pending is None:
                pending = [h, order[idx - 1][0], order[idx - 1][0]]
            else:
                pending[0] = h
                pending[2] = order[idx - 1][0]
        if pending is not None:
            h, fs, lc = pending
            by = None
            if p - lc >= quiet:
                by = "quiet"
            elif p - fs >= maxdef:
                by = "deferral"
            if by is not None:
                dispatches.append((h, p, p - first_seen_at[h], by))
                reviewed.add(h)
                pending = None
    return dispatches, toks


def report(name, heads, quiet, maxdef, phase):
    lines = []
    for edge in ("right", "left"):
        disp, toks = simulate(heads, arrivals(heads, edge), quiet, maxdef,
                              phase)
        actual = len(heads)
        avoided = [h for h, _, _ in heads if h not in {d[0] for d in disp}]
        lat = sorted(d[2] for d in disp)
        med = lat[len(lat) // 2] if lat else 0
        lines.append(
            "%s edge=%s Q=%d M=%d: dispatches=%d/%d actual, avoided=%d "
            "tokens_avoided=%d latency_med~%ds latency_max~%ds "
            "deferral_fired=%s heads=%s" % (
                name, edge, quiet, maxdef, len(disp), actual, len(avoided),
                sum(toks[h] for h in avoided), med, max(lat) if lat else 0,
                any(d[3] == "deferral" for d in disp),
                ",".join(d[0] for d in disp)))
    return lines


if __name__ == "__main__":
    print("PR15 actual: 5 dispatches, 4779463 input tokens")
    for q, m in [(300, 1200), (300, 1800), (300, 3600),
                 (600, 1200), (600, 1800), (600, 3600),
                 (900, 1200), (900, 1800), (900, 3600)]:
        for line in report("PR15", PR15, q, m, PHASE_PR15):
            print(line)
    for line in report("PR15-default", PR15, 900, 3600, PHASE_PR15):
        print(line)
    for line in report("PR16", PR16, 900, 3600, PHASE_PR16):
        print(line)
    for line in report("WALL5", WALLTIDBITS5, 900, 3600, PHASE_WALL5):
        print(line)
