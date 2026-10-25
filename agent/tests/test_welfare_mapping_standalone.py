import unittest
from donghaeng_agent.welfare_mapping import MappingRule, select_fields, recommend


class WelfareMappingTests(unittest.TestCase):
    def setUp(self):
        self.rule = MappingRule("test-rule", "test-v1", "draft", ("joint",),
                                ("i5_context",), ("social_connection",), "synthetic example")
        self.mapping = select_fields([self.rule], {"joint": True, "i5_context": True},
                                     {"joint": ["ev-activity"], "i5_context": ["ev-i5"]})
        self.program = {"program_id": "test", "program_version_id": "v1", "title": "synthetic",
                        "program_tags": ["employment", "social_connection"], "source_verified": True,
                        "content_matches": {"social_connection": {"status": "match", "reason": "group activity",
                                                                    "evidence_ids": ["official-text"]}},
                        "region_codes": ["11680"], "age_range": [19, 39],
                        "start_date": "2026-01-01", "end_date": "2026-12-31",
                        "application_status": "open", "personal_conditions": ["unemployed"]}

    def run_program(self, program):
        return recommend(self.mapping, lambda **kw: {"status": "ok", "programs": [program],
                         "coverage": {"unclassified": 2}, "corpus_version": "test"},
                         region_codes={"11680"}, age_range=(20, 29), support_as_of="2026-10-25")["programs"][0]

    def test_unknown_i5_never_triggers(self):
        result = select_fields([self.rule], {"joint": True}, {"joint": ["ev"]})
        self.assertEqual(result["welfare_fields"], [])
        self.assertEqual(len(result["pending_rules"]), 1)

    def test_draft_only_in_research(self):
        self.assertEqual(select_fields([self.rule], {}, {}, research_mode=False)["pending_rules"], [])
        self.assertEqual(self.mapping["matched_rules"][0]["status"], "draft")

    def test_personal_unknown_is_not_rejection(self):
        result = self.run_program(self.program)
        self.assertEqual(result["status"], "needs_confirmation")
        self.assertIn("unemployed", result["unconfirmed_conditions"])
        self.assertEqual(len(result["recommendation_reasons"]), 1)

    def test_expired_or_wrong_region_excluded(self):
        for patch in ({"end_date": "2026-09-01"}, {"region_codes": ["51110"]}, {"age_range": [40, 59]}):
            with self.subTest(patch=patch):
                self.assertEqual(self.run_program({**self.program, **patch})["status"], "excluded")

    def test_tag_match_without_content_evidence_is_held(self):
        self.assertEqual(self.run_program({**self.program, "content_matches": {}})["status"], "source_hold")

    def test_partial_age_overlap_unknown(self):
        result = self.run_program({**self.program, "age_range": [25, 39]})
        self.assertIn("age", result["unconfirmed_conditions"])

    def test_empty_mapping_does_not_query_all(self):
        def forbidden(**kwargs):
            self.fail("must not query with no tags")
        self.assertEqual(recommend({"welfare_fields": []}, forbidden)["status"], "mapping_held")

    def test_search_failure_distinct_from_empty(self):
        self.assertEqual(recommend(self.mapping, lambda **kw: {"status": "failed"})["status"], "search_failed")


if __name__ == "__main__":
    unittest.main()
