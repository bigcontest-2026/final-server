# Final report-flow review fixes

Implemented on 2026-10-26 in the existing dirty working tree. This wave addresses the four unique P2 findings from `2026-10-25-final-integration-review.md` and `2026-10-25-ui-review.md`. No backend sources, actual runtime DB, external services/models, commits, resets, pushes or deployment were used. Existing changes were preserved. Public controller interfaces are unchanged.

## Fixes

1. **Shared-family saved-report reads.** Generated reports retain strict requested-analysis validation, plus canonical root validation when supplied. Reading history instead requires membership in the server-returned workspace report list, exact requested report ID, and matching original analysis ID and optional historical root between list and detail. A provided frozen analysis snapshot must identify that original analysis. The original report and frozen snapshot remain untouched. Both missing-root legacy rows and pre-migration reports whose stored historical root differs from the current canonical root remain readable only when the trusted family listing includes them.
2. **Complete conflict recovery.** Recovery now displays type, selection, title, target, rationale, content, checks, feedback, source reference, all policy-review records and support-need records with status, text and evidence. It also provides the complete draft JSON in a labeled, readonly, full-width textarea for selection/copy. Values are escaped. Conflicting recovery opens the content preview by default. Both explanatory text and the continue button state that continuing deletes the browser draft. The stale-version restore rejection remains. UI tests verify the primary action stays disabled until recovery is resolved and a previously selected welfare program is retained through restore or discard.
3. **Edit/revert temporary-draft cleanup.** When current plans return to the saved server snapshot, persistence removes the obsolete intermediate browser draft and returns the local status to ready. Discard also resets the local status after successful removal. A reload no longer offers the canceled intermediate edit, and clean-state exit protection remains inactive.
4. **Legacy editor lifecycle.** The adapter captures the active editor before `renderPage()` tears down the DOM and before its navigation wrapper invokes the original navigation. Live draft inputs also persist immediately, enabling browser reload recovery without requiring a scope change. The regeneration patch applies these lifecycle updates both to newly imported HTML and to HTML already containing the scope guard; repeat patching remains idempotent.

## Regression evidence

The tests were added and executed before each corresponding implementation:

- Workspace read wave: five cross-entry family/legacy reads failed under the original strict read check; additional unlisted same-parent and generated wrong-root tests also failed. After separation and membership validation, all workspace tests pass. Negative coverage includes unlisted reports, swapped report IDs, wrong original parent, mismatched historical root, wrong frozen snapshot parent, and wrong generated parent/root/scope/versions/workspace.
- Recovery wave: full authored-content/export coverage, edit → changed value → original value cleanup, and nested escaping failed against the previous implementation; all pass after the fix.
- Legacy lifecycle wave: same-scope refresh, leave/return navigation, and immediate-input reload recovery each failed for both shipped and regenerated HTML (six failures); all pass after the lifecycle patch. The refresh fixture explicitly removes editor inputs during DOM teardown so preservation after removal cannot accidentally satisfy the test.
- Additional existing-behavior coverage confirms all 24 migrated plans can still be edited and saved, while adding a new plan remains limited. Four UI tests cover blocked primary actions and pending welfare-program preservation during recovery.

The new coverage adds 25 tests over the previous 368-test suite.

## Verification

- `npm --prefix service/frontend test`: **393/393 tests, 21/21 files passed**.
- `npm --prefix service/frontend run build`: **passed** (`tsc -b` and Vite production build).
- `git diff --check`: **passed**.
- `.venv/bin/python service/frontend/scripts/hermes_analysis_reports.py service/shared/hermes-complete.html`: reapplied after the last JS edit; public asset hashes synchronized.
- Direct repeat-patch comparison: **idempotent**, with owning report/workspace/overview/desktop script hashes verified against file contents.

The implementation regressions execute real controllers, HTML adapters, regeneration scripts and renderer functions with synthetic in-memory responses and storage. No browser visual or keyboard verification was performed by this fix agent; parent task owns that verification. Backend suites were not rerun in this frontend-only wave, as instructed.

## Files changed in this wave

- `service/shared/hermi-response-workspace.js`
- `service/shared/hermes-complete.html`
- `service/frontend/scripts/hermes_draft_scope.py`
- `service/frontend/tests/responseWorkspace.test.ts`
- `service/frontend/tests/responseRecovery.test.ts`
- `service/frontend/tests/responseRecoveryUI.test.ts` (new)
- `service/frontend/tests/reportScope.test.ts`
- This report.

`hermes_analysis_reports.py` was run for regeneration; its source did not need changes. The optional comparison-prompt grammatical change was left out of this fix wave.

The fix-only review diff is `/private/tmp/hermes-report-flow-kftxc4s_/final-fix.diff`, compared with the supplied `pre-fix` snapshot. It includes the new UI regression file in full.
