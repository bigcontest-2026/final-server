# Final integration review — report flow

Review verdict: **Changes requested** (two P2 findings in the reviewed snapshot).

Reviewed the design, implementation plan, Task 1–3 reports, `/private/tmp/hermes-report-flow-kftxc4s_/all-review.diff`, and owning backend/store/controller/regeneration sources. Application code was not modified. This review does not duplicate the separate visual/UI review.

## Findings

### P2 — Shared-family report listings cannot open reports created from the other analysis

Location: `service/shared/hermi-response-workspace.js:211–212` (called by the saved-report button at line 420).

The new backend returns the whole family's completed reports, while preserving each report's actual `analysis_report_id` and frozen snapshot. `readReport()` still calls `validateReport()`, which requires the report's analysis ID to equal the controller's requested ID. Create a report from the observation, investigate it, then open the derived analysis's response workspace: the completed report is listed but clicking it throws “같은 현황 분석에 연결된 보고서인지 확인하지 못했습니다.” The reverse direction and sibling analyses have the same problem. This directly defeats the specification's requirement to access completed reports from either entry point.

Separate strict generation validation from read validation. When reading, verify the requested report is in the server-returned family list (including legacy reports without a common-workspace field), validate its original analysis identity against that list, and retain its own frozen analysis snapshot. Do not merely relax all report checks or rewrite the snapshot to the current analysis.

Evidence: executed the real controller in Node VM with a synthetic `analysis_child` workspace containing one `analysis_root` report. Output: `shared report listed: 1 read error: 같은 현황 분석에 연결된 보고서인지 확인하지 못했습니다.` No network or database was used.

### P2 — Conflict recovery hides authored fields before deleting the only local copy

Location: `service/shared/hermi-response-workspace.js:377–378`, with restore rejection at line 95 and removal at lines 84–86.

When another tab advances the server version, the local draft is correctly marked as conflicting and cannot be restored automatically. The only offered preservation path tells the user to copy the preview and continue with the current saved version. That preview includes only title, target, content and feedback; it omits rationale, checks, confirmation records and evidence, support needs, program reference, kind and report selection. Choosing “현재 저장본으로 계속” deletes the draft storage entry. A user who wrote eligibility evidence or execution checks cannot recover those changes through the interface before that deletion.

Expose the complete authored draft in the conflict preview or a complete downloadable export, and make the discard consequence clear. Keep the stale-version save guard. Add coverage using at least rationale/checks and nested policy-review/support-need evidence so the recovery path cannot silently drop these fields again.

Evidence: called the actual `recoveryHTML()` with unique rationale/checks/review/evidence strings and a conflicting draft. All four strings were absent from the returned HTML. The controller's only restoration path rejects `draft.conflict`; the continue path removes the complete storage key.

## Other reviewed boundaries

- The family migration uses a write transaction and membership receipts, copies revisions, keeps original report rows, and advances a merged workspace beyond populated legacy versions. No further actionable regression found in that path.
- Completed reports retain the actual requested analysis snapshot; their fingerprint includes the common workspace ID.
- Comparison selection resolves stored reports on the server, checks scope/provenance/rules and observations, freezes comparison snapshots, and separates completed/in-progress research by a comparison-specific key. No further actionable backend regression found.
- Legacy draft guards recheck selected case and scope after the fresh-case fetch. The regeneration patch is integrated into the existing import path; sources and tests were inspected without regenerating application files during this review.

## Validation limits

Only two small synthetic Node probes were executed to substantiate the findings. Previously passing backend/agent suites were not rerun. Frontend suite/build and real browser verification remain with the parent task. No external model, external database, actual runtime DB, commit, push or deployment was used. Line references describe the snapshot inspected before parent fixes.

## Scoped re-review of final fix wave — 2026-10-26

**Verdict: all four known issues ADDRESSED. No new actionable regression found in the fix-only diff.** This supersedes the initial changes-requested verdict for the findings above.

Reviewed `2026-10-25-final-fix-report.md`, `/private/tmp/hermes-report-flow-kftxc4s_/final-fix.diff`, the current controller and recovery renderer, the legacy HTML lifecycle, the regeneration patch, and the added regression coverage.

| Known issue | Verdict | Review evidence |
| --- | --- | --- |
| Family report history blocked by strict current analysis ID | ADDRESSED | Read validation uses the server-returned report list as membership evidence and checks exact report ID, original analysis ID, historical root and optional frozen snapshot ID. Generation retains strict current-analysis validation. Legacy rows without a root and historical roots remain readable without rewriting snapshots. |
| Incomplete conflicting-draft preview | ADDRESSED | Human-readable preview covers all authored fields, nested review/support-need evidence and references. A complete JSON copy is supplied in a readonly textarea, with explicit deletion wording. Conflict restore remains blocked. |
| Clean revert retains obsolete intermediate draft | ADDRESSED | Persistence now removes the browser entry when the edit snapshot equals the saved state, and resets storage status. The new regression checks edit/revert, absent recovery after reload and inactive clean-state exit protection. |
| Legacy refresh/menu return loses current input | ADDRESSED | Current editor content is captured before DOM replacement and before navigation, and persisted on live draft input. Regeneration applies the lifecycle hooks to both unpatched imports and already-patched HTML. Regression fixtures remove the old DOM before checking restored content. |

Independently executed one small, in-memory Node probe against the final controller/renderer: listed family and missing-root legacy reports opened with their original snapshots; unlisted and wrong-parent reports were rejected; recovery JSON round-tripped the complete synthetic draft. A `</textarea><script>…` value stayed escaped and did not produce an executable script tag. The export is plain escaped textarea text and contains no evaluation path.

The reported **393/393 tests** and successful build were reviewed as fix-agent evidence, not independently rerun. No full suite, application source edit, external service, model, actual runtime DB, commit or deployment was performed in this scoped re-review. Real browser layout/keyboard validation remains the parent task's responsibility.
