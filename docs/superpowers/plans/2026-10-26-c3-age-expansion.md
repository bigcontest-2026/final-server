# C3 Adult Age Expansion Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Support C3 references for 20s, 30s, 40s, 50s and ages 60+ in the current server.

**Architecture:** Port the C3 1.1.0 age aggregation from submission while retaining the hermes-complete model structure. Build expanded history from raw Shinhan data into a separate verified bundle and preserve the existing V3 observations.

**Tech Stack:** Python, pandas/numpy/scikit-learn for offline calculation, jsonschema, FastAPI, pytest.

**Spec:** `docs/superpowers/specs/2026-10-26-c3-age-expansion-design.md`

## Global Constraints

- 20세 이상 5개 연령대 × 남녀. `60+` maps to `60GU`.
- Keep V3, I5, 5% classification and hold/operational boundaries unchanged.
- No fabricated production data. No replacement of the active bundle before full validation.
- Keep changes to the current final-server repository; reference repositories are read-only.

## Review Focus

- Original 60/70/80/90 duplicate detection must precede age aggregation.
- Missing, duplicate or cross-age/month/sex/region references must fail validation.
- Baseline/evaluation periods must remain distinct; August/September remain unlinked.
- C3 regeneration may change auxiliary IF flags, but original baseline/count/amount and decline decisions must match.
- A missing raw file or failed build must leave the active bundle untouched.

### Task 1: Age mapping and C3 calculation source

**Files:** `model/C3_Shinhan_baseline/`, `model/final_model/src/c3_contract.py`, `model/final_model/src/pipeline.py`, `model/final_model/schemas/historical.schema.json`, `model/final_model/tests/test_c3_expansion.py`.

**Interfaces:** `age_code(age)` normalizes C3 ages; `expected_link_keys(config, regions)` derives evaluation keys. Existing `historical_key` retains its interface.

- [x] Write tests for five age mappings, schema acceptance and daily 60+ sum with original duplicate rejection.
- [x] Run focused tests and observe failures against the old implementation.
- [x] Port C3 source/config/calendar and narrowly update pipeline/schema.
- [x] Run focused tests. Expected: all new cases pass.

### Task 2: Safe regeneration and server integration

**Files:** `model/final_model/src/c3_expansion.py`, `scripts/expand_c3.py`, `service/backend/tests/test_c3_age_api.py`, `docs/C3_AGE_EXPANSION.md`.

**Interfaces:** Regeneration consumes a verified old bundle and real Shinhan ZIP, produces an independent completed bundle. Linkage consumes Task 1 age/key mappings.

- [x] Write failing tests for full age linkage, old baseline preservation, missing/duplicate keys, count mismatch and failed-build isolation.
- [x] Implement regeneration with saved provenance and manifest hashes.
- [x] Verify expanded-age HTTP responses using test fixtures and run required backend CI plus local smoke tests.
- [x] If real input arrives, regenerate, validate 60 links and publish; otherwise record the missing-data boundary explicitly.
- [x] Request an independent review and resolve the substantive findings. Commit/push delivery is recorded in Git and the PR.

## Progress

- Baseline: backend CI 846 passed, 29 allowed skips, 79 subtests passed.
- Reference audit: hermes-complete and current final-server C3 source are identical; only submission has the expansion code. Raw ZIP and expanded bundle not found locally; user has been asked for a path.
- C3 tests: 27 passed, including raw-input end-to-end regeneration, 60 evaluation links, 120 monthly count reconciliations and successful old 20/30 baseline preservation. Test data remains temporary.
- Independent review: fixed reference-month reconciliation and stale validation/artifact copying; added successful prior-history preservation and all reference dimension rejection coverage.
- Publication remains pending a real raw ZIP or verified expanded bundle. The active activity bundle has not been modified.

- Workspace verification (including concurrent I5 edits): C3 27 passed; model 68 passed / 26 optional skips; backend-agent CI 863 passed / 29 allowed skips / 79 subtests passed; local smoke passed.
- Committed C3 snapshot verification: model 57 passed / 26 optional skips; backend-agent 854 passed / 29 allowed skips / 79 subtests passed; required-suite and skip-policy validation passed; local smoke passed.
