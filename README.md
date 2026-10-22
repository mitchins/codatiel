# Codatiel I2

Codatiel I2 is an experimental, agentic, repository-aware code reviewer.

It investigates pull requests with the OpenAI Agents SDK: a model directs
revision-bound source tools (read, diff, search, definitions, callers)
against pinned base and head revisions, and every cited piece of evidence is
replayed and validated before any finding is reported.

Status: review usefulness is still under qualification. Findings are
advisory only — Codatiel never posts automatic public PR comments, and
nothing here is production-ready. An 80% real-material-useful bar for
public comments is a goal, not a claim.
<!-- trigger CI -->
