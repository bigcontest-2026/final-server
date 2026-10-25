import copy
from collections import Counter
import json
from pathlib import Path
import unittest

from donghaeng_agent.signal_rules import SIGNAL_RULE_VERSION, classify_activity, previous_month


def observation(month="202510", skt=-6, card=-7, robust=False):
    return {
        "record_id": f"behavior:{month}:11680:MAN:20G",
        "key": {"month": month, "region_code": "11680", "sex_code": "MAN", "age_code": "20G"},
        "spatial_unit": "sgg", "comparison_status": "computed",
        "sources": {
            "skt": {"change_percent": skt, "calculation_status": "computed"},
            "card_a": {"change_percent": card, "calculation_status": "computed"},
        },
        "rule": {"candidate": True, "two_month_streak": True},
        "robust": {"candidate": robust},
        "quality": {"status": "hold", "reasons": ["SOURCE_SCOPE_UNVERIFIED"]},
        "operational_alert_allowed": False,
    }


class ActivityClassificationTests(unittest.TestCase):
    def test_inclusive_five_percent_boundary(self):
        result = classify_activity(observation(skt=-5, card=-4.999999))
        self.assertEqual(result["category"], "single_source_decline")
        self.assertIs(result["sources"]["skt"]["threshold_met"], True)
        self.assertIs(result["sources"]["card_a"]["threshold_met"], False)
        self.assertEqual(classify_activity(observation(skt=-5, card=-5))["category"], "joint_decline")

    def test_old_positive_q_and_streak_flags_cannot_trigger(self):
        result = classify_activity(observation(skt=-4.99, card=-0.2, robust=True))
        self.assertEqual(result["category"], "no_threshold_decline")
        self.assertIsNone(result["signals"]["two_month_streak"])

    def test_previous_decline_is_recalculated_at_same_threshold(self):
        current = observation()
        previous = observation("202509", skt=-4, card=-1)
        result = classify_activity(current, previous)
        self.assertEqual(result["category"], "joint_decline")
        self.assertIs(result["signals"]["two_month_streak"], False)
        previous["sources"]["skt"]["change_percent"] = -5
        previous["sources"]["card_a"]["change_percent"] = -5
        self.assertEqual(classify_activity(current, previous)["category"], "priority_review")

    def test_robust_support_requires_two_threshold_declines(self):
        self.assertEqual(classify_activity(observation(robust=True))["category"], "priority_review")
        self.assertEqual(classify_activity(observation(card=2, robust=True))["category"], "single_source_decline")
        self.assertEqual(classify_activity(observation(skt=1, card=2, robust=True))["category"], "no_threshold_decline")
        self.assertEqual(classify_activity(observation(robust=1))["category"], "joint_decline")

    def test_wrong_previous_scope_cannot_supply_continuity_or_evidence(self):
        for field, value in (("month", "202508"), ("region_code", "51110"), ("sex_code", "WMAN"), ("age_code", "30G")):
            with self.subTest(field=field):
                current = observation()
                previous = observation("202509")
                previous["key"][field] = value
                result = classify_activity(current, previous)
                self.assertEqual(result["category"], "joint_decline")
                self.assertIsNone(result["signals"]["two_month_streak"])
                self.assertEqual(result["evidence_ids"], [current["record_id"]])
                self.assertIn("PREVIOUS_OBSERVATION_SCOPE_MISMATCH", result["reason_codes"])

    def test_exact_calendar_previous_month_across_years(self):
        self.assertEqual(previous_month("202601"), "202512")
        self.assertEqual(classify_activity(observation("202601"), observation("202512"))["category"], "priority_review")
        for value in (None, 202510, True, "202513", "202500", "20251", "２０２５１０", "000001"):
            with self.subTest(value=value):
                self.assertIsNone(previous_month(value))

    def test_unknown_other_source_keeps_observed_single_decline(self):
        row = observation(card=None)
        row["comparison_status"] = "unavailable"
        result = classify_activity(row)
        self.assertEqual(result["category"], "single_source_decline")
        self.assertIsNone(result["signals"]["joint_decline"])
        self.assertIsNone(result["sources"]["card_a"]["threshold_met"])
        self.assertIn("OTHER_SOURCE_UNAVAILABLE", result["reason_codes"])

    def test_unknown_cannot_become_no_decline(self):
        result = classify_activity(observation(skt=-1, card=None))
        self.assertEqual(result["category"], "assessment_hold")
        self.assertIs(result["signals"]["joint_decline"], False)
        self.assertEqual(classify_activity(None)["category"], "assessment_hold")

    def test_invalid_numbers_are_unknown_and_json_safe(self):
        for value in (float("nan"), float("inf"), -float("inf"), True, False, "-10", 10 ** 1000):
            with self.subTest(value=repr(value)[:40]):
                result = classify_activity(observation(skt=value, card=1))
                self.assertEqual(result["category"], "assessment_hold")
                self.assertIsNone(result["sources"]["skt"]["change_percent"])
                json.dumps(result, allow_nan=False)

    def test_uncomputed_source_and_explicit_incomparability_are_respected(self):
        row = observation(skt=-20, card=1)
        row["sources"]["skt"]["calculation_status"] = "unavailable"
        self.assertEqual(classify_activity(row)["category"], "assessment_hold")
        row = observation()
        row["comparison_status"] = "not_comparable"
        self.assertEqual(classify_activity(row)["category"], "assessment_hold")

    def test_missing_current_scope_holds_and_dong_is_not_promoted(self):
        for field in ("region_code", "month", "sex_code", "age_code"):
            row = observation()
            del row["key"][field]
            self.assertEqual(classify_activity(row)["category"], "assessment_hold")
        row = observation()
        row["spatial_unit"] = "administrative_dong"
        self.assertEqual(classify_activity(row)["category"], "assessment_hold")

    def test_missing_previous_source_cannot_supply_continuity(self):
        result = classify_activity(observation(), observation("202509", card=None))
        self.assertEqual(result["category"], "joint_decline")
        self.assertIsNone(result["signals"]["two_month_streak"])

    def test_quality_and_provenance_are_preserved_without_mutation(self):
        current = observation(robust=True)
        current["operational_alert_allowed"] = True
        previous = observation("202509")
        before = copy.deepcopy((current, previous))
        result = classify_activity(current, previous)
        self.assertEqual((current, previous), before)
        self.assertEqual(result["quality_status"], "hold")
        self.assertEqual(result["quality_reasons"], ["SOURCE_SCOPE_UNVERIFIED"])
        self.assertIs(result["operational_alert_allowed"], False)
        self.assertEqual(result["rule_version"], SIGNAL_RULE_VERSION)
        self.assertEqual(result["evidence_ids"], [current["record_id"], previous["record_id"]])

    def test_packaged_observations_regression(self):
        bundle = Path(__file__).resolve().parents[2] / "bundles/activity/behavior_evidence.jsonl"
        rows = [json.loads(line) for line in bundle.read_text().splitlines()]
        fields = ("region_code", "month", "sex_code", "age_code")
        index = {tuple(row["key"][field] for field in fields): row for row in rows}
        results = []
        for row in rows:
            key = row["key"]
            previous = index.get((key["region_code"], previous_month(key["month"]), key["sex_code"], key["age_code"]))
            results.append(classify_activity(row, previous))
        self.assertEqual(len(results), 100)
        self.assertEqual(Counter(result["category"] for result in results), {
            "no_threshold_decline": 70, "single_source_decline": 19,
            "joint_decline": 10, "priority_review": 1,
        })
        self.assertTrue(all(result["quality_status"] == "hold" for result in results))
        self.assertTrue(all(result["operational_alert_allowed"] is False for result in results))
        priority = next(result for result in results if result["category"] == "priority_review")
        self.assertEqual(priority["scope"], {"month": "202510", "region_code": "51110", "sex_code": "MAN", "age_code": "20G"})
        self.assertIn("SKT_SEP_OCT_BOUNDARY_VERSION_CHANGED", priority["quality_reasons"])


if __name__ == "__main__":
    unittest.main()
