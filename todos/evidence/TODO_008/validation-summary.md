# TODO_008 — local software validation

Date: 2026-11-11 (Europe/Moscow).
BASE: `30477bb3976216e1be612bd9832dc34c8257d6c6` on `dev`.
Source checkpoint: [source-checkpoint.json](source-checkpoint.json).
Source patch: [source.patch](source.patch), SHA256 `0280fabb0e13b76696a0745289cf53b5ba36cdbd44f9c4cfdc8d8ce19cff811f`.
The manifest covers gdi/, tests/, scripts/, .github/, test.sh and pyproject.toml.
All listed bytes still match after the full run. Documentation was reviewed separately.

## Executed checks

- Initial targeted subset: 31 tests, one failure in CLI error-kind propagation.
  Preserved evidence: [targeted-tests.txt](targeted-tests.txt).
- Corrected targeted subset: 32 PASS, 13.789 seconds.
  Command: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_snapshot_builder tests.test_agent -v`.
  Evidence: [targeted-tests-2.txt](targeted-tests-2.txt).
- Common full local discovery: `GDI_TEST_DOCKER=catthehacker/ubuntu:act-latest bash test.sh`.
  476 tests in 461.639 seconds: 473 PASS, 3 explicitly opt-in service SKIP, no failures.
  Evidence: [full-tests.txt](full-tests.txt).
- Docker workflow/crash-cleanup acceptance ran. The three ServiceV2CliTests require
  an explicitly configured acceptance user service and were not enabled; they are
  not a gate for this offline agent snapshot builder change.
- The runtime regression builds gdi.pyz in an isolated committed source fixture,
  removes those sources, and exercises successful snapshot-build plus a typed
  failure for an unknown adapter. The user's deployed runtime was not replaced.
- Full + incremental Git bundle restoration, incomplete/changed connector shapes,
  duplicate IDs/names, download metadata mismatch, no-clobber creation and input
  preservation are covered by shared discovery tests.
- `git diff --check` and local documentation link resolution: PASS.

## Environment and boundaries

Python: `3.10.12 (main, Jun 22 2026, 18:55:27) [GCC 11.4.0]` (`/home/dream/.platformio/penv/bin/python`).
Platform: `Linux-6.8.0-138-generic-x86_64-with-glibc2.35`.
Docker image: `catthehacker/ubuntu:act-latest`,
`sha256:c58e2b364da03b0c804c7d660f2ecbedf2f221a382b9baa0b344b0144780ff43`.

Read-only connector shape probes used list_folder and raw fetch; sanitized fixtures
preserve the observed field names, with invented IDs/URLs and generated Git data.
Real Drive clone/publication and the user's worker service were not used as fixtures.
The adapter supports bounded non-paginated list_folder responses only; reaching the
limit or encountering unknown completeness semantics fails closed. It does not
independently prove freshness or completeness on Drive.

The operator postponed release until the other agent finishes. No commit, GitHub
push, GDI push, runtime deploy, remote migration or worker restart was performed.
The local installed CLI is editable and sees this checkout's code changes.

## Version-only follow-up: 0.5.0

Operator requested 0.5.0 after the full run. Source/package declarations and current
documentation were updated. The actual installed editable CLI returns `gdi 0.5.0`.
Only gdi/__init__.py and pyproject.toml differ from the tested source manifest.
[Version patch](version-0.5.0.patch), SHA256 `2959ed5b0882ed96154186511de427bf5cddc7ea9ae0f99c712491a51811f20e`,
applies after the recorded source patch. [Checks](version-0.5.0-check.json): PASS.
The full suite was not repeated for this declaration-only change. No release was published.
