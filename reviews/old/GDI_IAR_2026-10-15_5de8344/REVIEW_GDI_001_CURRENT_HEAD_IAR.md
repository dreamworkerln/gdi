# REVIEW_GDI_001 — Implementation Engineering Audit of GDI current HEAD

- **Status:** COMPLETE
- **Verdict:** YELLOW / CONDITIONAL
- **Review type:** ИАР (implementation engineering audit; software correctness/regression/implementation quality, not a security audit)
- **Reviewed repository:** GDI
- **Reviewed ref:** `refs/heads/dev`
- **Reviewed HEAD:** `5de8344df7337f4b0f22d3cfa0d937b270d806e7`
- **Reviewed publication:** `f6686fd21332159303d7a0526149972f1bd20338833a6f393b454fdcb6245c59`
- **Repository ID:** `4b3d23458a15492aabc47f6ffd024f99`
- **Reviewer attribution baseline:** `dbdea2c4d8aee9e4b19a773fb4f9b49dda504f05`
- **Baseline note:** this baseline was chosen by the reviewer to attribute recent regressions; it is **not** represented as an operator-approved/accepted base.
- **Source changes by reviewer:** none.

## Executive summary

The current GDI implementation is substantially stronger than a test-green-only assessment would suggest. Publication-chain validation, exact-byte snapshot/clone behavior, CI request/result identity fencing, immutable log-chunk verification, cancellation/late-result handling, GC quiescence/race checks, and current pull auto-merge rollback behavior all received direct semantic/test review. Exact-SHA CI was ordered through GDI itself and was independently verified by `gdi agent ci result` as `verified:true / PASS`.

The audit nevertheless confirms one **Medium runtime regression** in the live network-route contract: once a long-running worker has selected a proxy route, stale route state can override newly reread proxy/NO_PROXY settings as long as the old route continues to work. That contradicts the documented promise that proxy settings are reread during operation and that worker configuration need not change when the host network changes. A second **Low** finding is contradictory architecture documentation for persistent CI follow cursors.

No data-integrity, publication-chain, clone/pull, terminal-CI verification, cancellation-fencing, GC-safety, or current auto-merge correctness defect of Medium-or-higher severity was confirmed beyond F001. Because F001 violates an explicitly claimed live-operational contract but does not undermine core repository/publication integrity, the scoped verdict is **YELLOW / CONDITIONAL**, not GREEN.

## Findings

### F001 — MEDIUM — selected network route can mask live host proxy/NO_PROXY changes

**Affected code / contract**

- `gdi/network.py:97` explicitly states settings are read afresh so a running worker can follow host changes.
- `gdi/network.py:165-177` puts `self.selected` before fresh inherited/discovered routes. `value = {**direct, **value}` lets stale selected `no_proxy` overwrite current host bypass state.
- `gdi/network.py:186-192` again lets route data override freshly read `no_proxy`.
- `gdi/network.py:194-216` retains any successful old route as `self.selected`. If it keeps working, the fresh route need never become active.
- `gdi/ci_proxy.py:194-205` builds a new CI environment from `self.network.selected`, so a later CI job can inherit stale bypass policy.
- `gdi/worker.py:579-590` obtains network environment for new jobs.
- `docs/worker.md:219-248` promises runtime reread of proxy settings, no required worker-config change when the network changes, and preservation of NO_PROXY/no_proxy exclusions.

**Reachable trigger**

1. A long-running worker/GDI process successfully selects route A with proxy/NO_PROXY A.
2. Host proxy settings or NO_PROXY change to B.
3. Route A remains reachable, so calls through A still succeed.
4. A later network operation or CI job runs.

**Observed behavior**

The deterministic route repro retains the old route after environment mutation:

`first_route= {'no_proxy': 'old.internal', 'http_proxy': 'http://old-proxy:8080', 'https_proxy': 'http://old-proxy:8080'}`

`successful_route= {'no_proxy': 'old.internal', 'http_proxy': 'http://old-proxy:8080', 'https_proxy': 'http://old-proxy:8080'}`

The CI repro likewise emits:

`ci_env_NO_PROXY= old.internal,localhost,127.0.0.1,::1`

`contains_new= False`

`contains_old= True`

**Consequence**

A live proxy endpoint replacement or NO_PROXY addition/removal can be ignored by new operations/jobs until the old route fails or the worker restarts. This can produce operationally incorrect routing and inconsistent CI connectivity despite the documented hot-reload contract.

**Regression attribution**

`git blame` attributes the relevant route-selection/environment logic to `ed1b5131` (`feat: automatically switch proxy routes for GDI and CI`), inside the reviewer attribution range.

**Why existing tests/CI did not catch it**

The current proxy suite covers failover, remembered routes, auth non-retry, parsing, bypass preservation and CI proxy behavior, but does not cover **mutating host settings after a route is selected while that old route remains successful**. The exact-SHA full CI therefore passes despite this semantic gap.

**Confidence:** HIGH.

**Acceptance criterion**

The next network operation/new CI job after a host proxy or NO_PROXY change must reflect current host settings even if the previously selected route is still reachable. Add regressions for selected-route + proxy endpoint mutation and selected-route + NO_PROXY add/remove without forcing the old route to fail.

### F002 — LOW — ARCHITECTURE.md documents obsolete CI follow cursor semantics

`ARCHITECTURE.md:184-185` says each new follow invocation starts from the beginning and that the resume cursor is not persisted between CLI invocations. Current behavior is the opposite:

- `gdi/ci.py:505-553` persists and validates a per-job cursor under `.gdi/ci/follow/`;
- `docs/ci.md:109-113` correctly documents cross-invocation resume and `--restart` replay;
- focused follow-cursor tests: 6/6 PASS.

The stale architecture lines predate the reviewer attribution range, so this is classified as **pre-existing documentation drift**, not a regression introduced by current HEAD.

**Consequence:** contradictory operator/developer expectations during CI log recovery/replay.

**Confidence:** HIGH.

**Acceptance criterion:** update `ARCHITECTURE.md` to match the implemented persisted cursor and `--restart` semantics.

## Exact-SHA CI ordered through GDI

CI was requested and delivered through the current GDI protocol rather than treated as an external unverified status signal.

| Field | Value |
| --- | --- |
| Job | `03797b57ea864954a2fec0e8125ee0d8` |
| Worker | `user-dream` |
| Profile | `full` |
| Profile revision | `0009727f305bd84ee3ea973addf04df2a1fdf0418d1198528d65b5f0ffbf77ab` |
| Target | `5de8344df7337f4b0f22d3cfa0d937b270d806e7` |
| Publication | `f6686fd21332159303d7a0526149972f1bd20338833a6f393b454fdcb6245c59` |
| Result | **PASS** |
| GDI verification | **verified:true** |
| Exit code | 0 |
| Warnings | none |
| Duration | 622.079 s |
| Unit/integration suite | 366 tests, OK, 5 conditional skips |
| Docker acceptance | 2 tests, PASS |
| Immutable log chunks | 33 |
| Log bytes | 211075 |
| Concatenated chunks / build.log SHA256 | `9cc1175e99b0f78ad969a6bfaae2f3839bc306b01e2f54ff1b5b9c190317f578` |

The final PASS is accepted here only because `gdi agent ci result` verified exact request/result identities, artifacts and the complete immutable log chunk chain and returned `verified:true`.

## Reviewer validation

| Check | Result |
| --- | --- |
| `git diff --check` on reviewer attribution range | PASS |
| Python `compileall` | PASS |
| Auto-merge/rollback/concurrency/CLI focused suite | 15 PASS |
| Agent publication/snapshot/clone + agent-CI integrity suite | 25 PASS |
| GC quiescence/race/dependency suite | 21 PASS, 1 prerequisite skip |
| Existing network/proxy focused suite | 24 PASS |
| Follow-cursor focused suite | 6 PASS |
| F001 direct route-state repro | REPRODUCED |
| F001 CI NO_PROXY repro | REPRODUCED |
| Full exact-SHA remote GDI CI | PASS, `verified:true` |
| Local monolithic `./test.sh` | INCOMPLETE: exceeded 300 s tool-call limit; not counted as PASS |

The incomplete local monolithic invocation showed no code failure in the captured portion, but it is deliberately not promoted to PASS. The complete suite result comes from the exact-SHA remote CI above.

## Areas inspected

The audit included documented contracts and implementation paths for repository publication/snapshot/clone/pull, exact-byte and identity fencing, current auto-merge and rollback behavior, CI request/ready/inbox/result protocol, immutable log publication and verification, cancellation/late-result handling, worker recovery/ledger behavior, scheduler/worker operational paths, network/proxy route selection and CI proxy inheritance, GC quiescence/dependency/race handling, persisted CI follow cursors, error handling, partial failures and current diagnostics.

No source fix, commit or GDI publication was made as part of this review.

## Implementation quality and acknowledged debt

The audit did not relabel already acknowledged TODO items as new defects. Existing recorded debt includes local spool/cache/log retention and quotas, Drive CI artifact/log cleanup, unfinished-job constraints around bundle GC, latest-PASS convenience, stronger Docker cleanup isolation, replacement of patched `act`, and future Drive Changes API/PubSub work.

A lightweight AST branch-node/span heuristic also shows several large control-flow surfaces (not a cyclomatic-complexity measurement), notably `gdi/cli.py:execute` and configuration/workflow/executor paths. This is nonblocking technical debt because it raises regression radius and review cost, but no additional correctness finding is asserted from size alone.

## Scoped verdict

**YELLOW / CONDITIONAL.** Core repository/publication integrity and exact CI verification are supported by strong implementation and evidence, and the current exact target passes its complete verified CI. However, F001 is a confirmed runtime regression against an explicit live-network-change contract. A clean GREEN for the claimed operational behavior requires F001 to be fixed and covered by the acceptance tests above. F002 should be corrected to remove contradictory architecture documentation but is not independently release-blocking.

## Evidence

See the accompanying `evidence/` directory. `evidence-index.txt` maps each file to its purpose; `ci-result-verified.json` is the authoritative GDI CI verification record; the two standalone finding records contain trigger/consequence/confidence/acceptance details.
