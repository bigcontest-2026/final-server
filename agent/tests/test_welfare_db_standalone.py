import tempfile
import unittest
from pathlib import Path
from donghaeng_agent.welfare_db import WelfareDB, demo_programs
from donghaeng_agent.welfare_mapping import MappingRule, select_fields, recommend


class DBTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = WelfareDB(Path(self.temp.name) / "demo.sqlite", allow_synthetic=True)
        self.db.create(demo_programs(), corpus_version="test-v1", is_synthetic=True)

    def test_opt_in_and_no_overwrite(self):
        with self.assertRaises(ValueError):
            WelfareDB(self.db.path).search_welfare(welfare_fields=["employment"])
        with self.assertRaises(FileExistsError):
            self.db.create([], corpus_version="other", is_synthetic=True)

    def test_or_dedup_and_coverage(self):
        response = self.db.search_welfare(welfare_fields=["employment", "social_connection"])
        ids = [p["program_id"] for p in response["programs"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(response["coverage"]["unclassified"], 1)
        self.assertEqual(response["coverage"]["total_programs"], 16)
        self.assertEqual(self.db.search_welfare(welfare_fields=[])["programs"], [])

    def test_recommendation_end_to_end(self):
        rule = MappingRule("test", "v1", "draft", ("activity",), ("i5",),
                           ("social_connection",), "test only")
        mapping = select_fields([rule], {"activity": True, "i5": True},
                                 {"activity": ["a"], "i5": ["b"]})
        result = recommend(mapping, self.db.search_welfare, region_codes={"11680"},
                           age_range=(20, 29), support_as_of="2026-10-25")
        status = {p["program_id"]: p["status"] for p in result["programs"]}
        self.assertTrue(result["is_synthetic"])
        self.assertEqual(status["demo-employment-social"], "needs_confirmation")
        self.assertEqual(status["demo-unverified"], "source_hold")
        for suffix in ("wrong-region", "wrong-age", "expired", "content-mismatch"):
            self.assertEqual(status["demo-" + suffix], "excluded")
        self.assertTrue(all(p["is_synthetic"] for p in result["programs"]))


if __name__ == "__main__":
    unittest.main()
