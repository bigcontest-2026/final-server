import tempfile
import unittest
from pathlib import Path
from donghaeng_agent.welfare_erd import ERDWelfareDB, create_demo, review_candidates


class ERDTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'erd.sqlite'
        create_demo(self.path)
        self.db=ERDWelfareDB(self.path,'erd-demo-v2',allow_synthetic=True)

    def test_category_order_and_dedup(self):
        data=self.db.search(category_ids=['demo:life'],i5_domains=['P','U'])
        self.assertEqual(data['coverage'], {'total':5,'matched':4,'unclassified':1})
        self.assertEqual(data['programs'][-1]['program_id'],'work')
        self.assertEqual(len({p['program_id'] for p in data['programs']}),4)

    def test_no_query_no_all_records(self):
        self.assertEqual(self.db.search()['programs'],[])

    def test_unknown_domains_and_categories_rejected(self):
        with self.assertRaises(ValueError): self.db.search(i5_domains=['Z'])
        with self.assertRaises(ValueError): self.db.search(category_ids=['social_connection'])

    def test_synthetic_optin_and_preservation(self):
        with self.assertRaises(ValueError): ERDWelfareDB(self.path,'erd-demo-v2').search(i5_domains=['P'])
        with self.assertRaises(FileExistsError): create_demo(self.path)

    def test_unpublished_not_searchable(self):
        with self.assertRaises(ValueError): ERDWelfareDB(self.path,'missing',allow_synthetic=True).search()

    def test_review_excludes_expired_preserves_unknown(self):
        result=review_candidates(self.db.search(i5_domains=['P','U']),
            region_ids={'demo-gangnam'},age_range=(20,29),as_of='2026-10-25')
        states={p['program_id']:p['status'] for p in result['programs']}
        self.assertEqual(states,dict(expired='excluded',social='needs_confirmation',
                                     unknown='source_hold',work='needs_confirmation'))
        self.assertFalse(result['operational_alert_allowed'])

    def test_explicit_region_and_age_mismatch(self):
        result=review_candidates(self.db.search(i5_domains=['P']),
            region_ids={'other'},age_range=(60,69),as_of='2026-10-25')
        self.assertTrue(all(p['status']=='excluded' for p in result['programs']))
