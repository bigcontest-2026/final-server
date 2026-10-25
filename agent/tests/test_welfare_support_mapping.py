import copy
import hashlib
import json
import unittest

from donghaeng_agent.welfare_support_mapping import (
    PLAN_RULE_VERSION, RULE_DEFINITION, RULE_VERSION,
    category_plan, extract_program_services, review_support_candidates,
)


class WelfareSupportMappingTests(unittest.TestCase):
    def program(self, content="정기적인 안부 확인과 가사 돌봄 서비스를 제공합니다.", **changes):
        return {"version_id": "example", "title": "실제 사업",
                "support_content": content, "source_url": "https://www.bokjiro.go.kr/",
                "source_verified": False, "content_review": "unknown",
                "application_start": None, "application_end": None,
                "age_status": "unknown", "min_age": None, "max_age": None,
                "gender_scope": "unknown", **changes}

    def review(self, program, domains=("O", "U"), **kwargs):
        return review_support_candidates([program], category_plan(domains),
                                         as_of="2026-10-25", **kwargs)[0]

    def test_plan_is_topics_not_score_ranking_and_conditional_needs_are_explicit(self):
        plan = category_plan(["E", "P", "O", "C", "U"])
        self.assertEqual(plan["selected_categories"], ["daily_living", "income_support",
                                                     "isolation_outreach", "social_connection"])
        self.assertEqual(set(plan["category_tiers"].values()), {"explore"})
        self.assertEqual(plan["plan_rule_version"], PLAN_RULE_VERSION)
        self.assertEqual(plan["selection_basis"], "confirmed_needs_then_observation_topics")
        self.assertEqual(plan["within_tier_order"], "category_code_ascending")
        self.assertFalse(plan["within_tier_order_is_priority"])
        self.assertTrue(all(not item["need_confirmed"] for item in plan["categories"]))
        self.assertTrue(all(item["selection_basis"] == "requires_confirmation" for item in plan["pending"]))
        self.assertEqual(set(plan["pending_categories"]),
                         {"employment", "mental_health", "health_care", "housing", "family_support"})
        self.assertFalse(plan["operational_alert_allowed"])
        confirmed = category_plan(["E"], confirmed_needs=["housing"])
        self.assertIn("housing", confirmed["selected_categories"])
        self.assertNotIn("employment", confirmed["selected_categories"])
        with self.assertRaises(ValueError):
            category_plan(["X"])
        with self.assertRaises(ValueError):
            category_plan(["E"], confirmed_needs=["unknown_need"])

    def test_confirmed_need_sorts_before_observation_led_candidates(self):
        plan = category_plan(["E", "P", "C", "U", "O"], confirmed_needs=["housing", "employment"])
        self.assertEqual(plan["selected_categories"], ["employment", "housing", "daily_living",
                                                     "income_support", "isolation_outreach", "social_connection"])
        self.assertEqual([item["tier"] for item in plan["categories"]],
                         ["confirmed_need", "confirmed_need", "explore", "explore", "explore", "explore"])
        self.assertEqual([item["selection_basis"] for item in plan["categories"][:2]],
                         ["confirmed_need", "confirmed_need"])
        self.assertFalse({"housing", "employment"}.intersection(plan["pending_categories"]))

    def test_plan_change_preserves_registered_v3_definition_and_stored_links(self):
        definition = json.dumps(RULE_DEFINITION, ensure_ascii=False, sort_keys=True,
                                separators=(",", ":"), default=str)
        self.assertEqual(RULE_VERSION, "welfare-service-actions-v3")
        self.assertEqual(hashlib.sha256(definition.encode()).hexdigest(),
                         "6f4ff39e95e7dbbff7500ab9e199de448e1240e90eb3d8d3b3fa4c20f109d1d7")
        program = self.program()
        program["support_links"] = extract_program_services(program)
        result = self.review(program)
        self.assertEqual(result["status"], "needs_confirmation")
        self.assertEqual(result["service_links"], program["support_links"])
        self.assertEqual(result["mapping_rule_version"], RULE_VERSION)
        self.assertEqual(result["plan_rule_version"], PLAN_RULE_VERSION)
        for reason in result["recommendation_reasons"]:
            self.assertEqual(reason["tier"], "explore")
            self.assertEqual(reason["selection_basis"], "observation_domain_topic")
            self.assertEqual(reason["service_evidence"]["rule_version"], RULE_VERSION)

    def test_missing_observations_remain_held_without_inventing_a_need(self):
        plan = category_plan([])
        self.assertEqual(plan["status"], "held")
        self.assertEqual(plan["selected_categories"], [])
        self.assertEqual(plan["quality_status"], "hold")
        self.assertFalse(plan["operational_alert_allowed"])
        self.assertEqual(len(plan["pending_categories"]), 5)
        confirmed = category_plan([], confirmed_needs=["housing"])
        self.assertEqual(confirmed["selected_categories"], ["housing"])
        self.assertEqual(confirmed["categories"][0]["matched_domains"], [])
        self.assertEqual(confirmed["category_tiers"], {"housing": "confirmed_need"})
        self.assertEqual(confirmed["quality_status"], "hold")

    def test_age_match_does_not_promote_an_unconfirmed_support_topic(self):
        plan = category_plan(["E"])
        program = self.program("취업 상담을 제공합니다.", age_status="bounded", min_age=19, max_age=39)
        reviewed = review_support_candidates([program], plan, age_range=(20, 29), as_of="2026-10-25")[0]
        self.assertIn("employment", plan["pending_categories"])
        self.assertNotIn("employment", plan["selected_categories"])
        self.assertEqual(reviewed["recommendation_reasons"], [])
        self.assertEqual(reviewed["quality_status"], "hold")
        self.assertFalse(reviewed["operational_alert_allowed"])

    def test_empty_search_and_missing_source_do_not_confirm_support_availability(self):
        plan = category_plan([], confirmed_needs=["housing"])
        self.assertEqual(review_support_candidates([], plan, as_of="2026-10-25"), [])
        reviewed = review_support_candidates([self.program("")], plan, as_of="2026-10-25")[0]
        self.assertEqual(reviewed["status"], "source_hold")
        self.assertIn("선택한 분야의 제공서비스 원문 근거 확인", reviewed["unconfirmed_conditions"])
        self.assertEqual(reviewed["quality_status"], "hold")
        self.assertFalse(reviewed["operational_alert_allowed"])

    def test_recipient_tags_titles_and_target_text_do_not_create_services(self):
        program = self.program("생계급여 수급자에게 가사 돌봄 서비스를 제공합니다.",
                               title="소득 지원", target_text="생계비와 월세를 지원합니다.",
                               source_categories=[{"category_id": "income_support"}])
        categories = {link["category_code"] for link in extract_program_services(program)}
        self.assertEqual(categories, {"daily_living"})
        self.assertEqual(extract_program_services(self.program("", title="월세 지원")), [])

    def test_embedded_target_clause_does_not_create_financial_action(self):
        program = self.program("지원대상: 생계비 지원 대상자\n가사 돌봄 서비스 제공")
        self.assertEqual([link["category_code"] for link in extract_program_services(program)], ["daily_living"])
        program = self.program("생계비 지원사업 참여자에게 안부 확인을 제공합니다.")
        self.assertNotIn("income_support", [link["category_code"] for link in extract_program_services(program)])

    def test_youth_and_staff_funding_are_not_cleaning_or_resident_care(self):
        for content in ["청소년 대상 다양한 학습·상담·진로 교육지원",
                        "청소년의 건강한 성장 지원", "노인맞춤돌봄서비스 종사자 지원",
                        "장애인 활동지원 서비스 제공기관에 운영비 지원",
                        "돌봄 인력 인건비 및 처우개선 지원", "국가사업 추진 지원"]:
            with self.subTest(content=content):
                self.assertNotIn("daily_living", [link["category_code"]
                                 for link in extract_program_services(self.program(content))])
        self.assertIn("daily_living", [link["category_code"] for link in
                      extract_program_services(self.program("방문 청소 서비스를 제공합니다."))])

    def test_visit_services_require_outreach_context(self):
        for content in ["전문가 방문상담 지원(국비 70%, 자부담 30%)",
                        "가정방문 산후조리서비스 제공", "가정방문 건강관리사 파견 비용 지원"]:
            self.assertNotIn("isolation_outreach", [link["category_code"] for link in
                             extract_program_services(self.program(content))])
        for content in ["독거노인 가정방문 상담 지원", "위기가구 발굴 및 서비스 연계", "안부 확인을 실시합니다."]:
            self.assertIn("isolation_outreach", [link["category_code"] for link in
                          extract_program_services(self.program(content))])

    def test_employment_and_social_services_are_not_recipient_descriptions_or_cash_labels(self):
        examples = [("고용보험료의 20~30%, 분기별 지원", "employment"),
                    ("육아휴직 대체인력으로 고용·파견된 근로자에게 지원금 지급", "employment"),
                    ("1인당 연간 20만원의 문화여가비 지원", "social_connection")]
        for content, category in examples:
            self.assertNotIn(category, [link["category_code"] for link in
                             extract_program_services(self.program(content))])
        self.assertIn("employment", [link["category_code"] for link in
                      extract_program_services(self.program("중소기업 매칭 및 일경험(3개월) 지원"))])

    def test_childcare_allowance_and_counseling_referral_remain_actual_support(self):
        self.assertIn("family_support", [link["category_code"] for link in
                      extract_program_services(self.program("월 20만원의 양육수당을 지급합니다."))])
        self.assertIn("mental_health", [link["category_code"] for link in
                      extract_program_services(self.program("서류심사 후 심리상담 기관·단체 의뢰(실비지급)"))])

    def test_leisure_token_is_not_the_end_of_benefit_plus_subject_particle(self):
        for content in ["의료급여가 필요하다고 인정한 자에게 의료급여를 지원",
                        "활동지원 급여가 더 필요한 장애 정도가 심한 장애인에게 시비로 추가급여 지원",
                        "수급권자에게 추가급여가 제공됩니다."]:
            self.assertNotIn("social_connection", [link["category_code"] for link in
                             extract_program_services(self.program(content))])
        for content in ["여가문화복지 프로그램 제공", "문화여가 프로그램을 제공합니다.",
                        "문화·여가 활동 프로그램을 운영합니다."]:
            self.assertIn("social_connection", [link["category_code"] for link in
                          extract_program_services(self.program(content))])

    def test_quotes_offsets_hashes_and_rule_version_are_exact(self):
        content = "  ① 생계비를 매월 지급합니다.\n ② 가사 돌봄 서비스를 제공합니다."
        links = extract_program_services(self.program(content))
        self.assertEqual({item["category_code"] for item in links}, {"income_support", "daily_living"})
        for link in links:
            self.assertEqual(content[link["start"]:link["end"]], link["source_quote"])
            self.assertEqual(link["content_hash"], hashlib.sha256(content.encode()).hexdigest())
            self.assertEqual(link["rule_version"], RULE_VERSION)
            self.assertEqual(link["review_status"], "rule_extracted")

    def test_excluded_and_negative_services_do_not_match(self):
        self.assertEqual(extract_program_services(self.program("생계비를 지원하지 않습니다.")), [])
        result = self.review(self.program("취업 상담을 제공합니다."))
        self.assertEqual(result["status"], "excluded")
        confirmed = category_plan(["E"], confirmed_needs=["employment"])
        result = review_support_candidates([self.program("취업 상담을 제공합니다.")], confirmed,
                                            as_of="2026-10-25")[0]
        self.assertEqual(result["status"], "needs_confirmation")

    def test_unreviewed_official_source_can_be_a_research_candidate(self):
        result = self.review(self.program(), region_context={"region_code": "11680"})
        self.assertEqual(result["status"], "needs_confirmation")
        self.assertFalse(result["source_verified"])
        self.assertEqual(result["content_review"], "unknown")
        self.assertIn("사업 적용 지역 및 거주 요건", result["unconfirmed_conditions"])
        self.assertFalse(result["operational_alert_allowed"])
        self.assertEqual(self.review(self.program(source_url=None))["status"], "source_hold")

    def test_stored_evidence_is_preserved_and_stale_quote_or_hash_is_held(self):
        program = self.program()
        stored = extract_program_services(program)
        stored[0]["evidence_id"] = "support:known"
        stored[0]["review_status"] = "auto_extracted"
        program["support_links"] = stored
        reviewed = self.review(program)
        self.assertEqual(reviewed["service_links"], stored)
        self.assertEqual(reviewed["status"], "needs_confirmation")
        db_names = copy.deepcopy(program)
        for item in db_names["support_links"]:
            item["source_text_sha256"] = item.pop("content_hash")
            item["review_status"] = "reviewed"
        self.assertEqual(self.review(db_names)["status"], "needs_confirmation")
        self.assertFalse(self.review(db_names)["source_verified"])
        changed = {**program, "support_content": program["support_content"] + " 변경"}
        self.assertEqual(self.review(changed)["status"], "source_hold")
        broken = copy.deepcopy(program)
        for item in broken["support_links"]:
            item["source_quote"] = "다른 문장"
        self.assertEqual(self.review(broken)["status"], "source_hold")

    def test_partial_age_is_unknown_and_admin_jurisdiction_never_proves_eligibility(self):
        program = self.program(age_status="bounded", min_age=65, max_age=None,
                               regions=[{"scope_type": "specific", "region_id": "other",
                                         "scope_purpose": "administering_jurisdiction"}])
        self.assertEqual(self.review(program, age_range=(20, 29))["status"], "excluded")
        partial = self.review(program, age_range=(60, float("inf")), region_context={"region_ids": ["11680"]})
        self.assertEqual(partial["status"], "needs_confirmation")
        self.assertIn("연령 구간 일부만 해당", partial["unconfirmed_conditions"])
        self.assertIn("사업 적용 지역 및 거주 요건", partial["unconfirmed_conditions"])

    def test_expired_period_and_explicit_gender_are_excluded(self):
        self.assertEqual(self.review(self.program(application_end="2026-09-30"))["status"], "excluded")
        self.assertEqual(self.review(self.program(gender_scope="female"), sex="MAN")["status"], "excluded")

    def test_reversed_dates_are_unknown_instead_of_a_false_exclusion(self):
        result = self.review(self.program(application_start="2026-12-31", application_end="2026-01-01"))
        self.assertEqual(result["status"], "needs_confirmation")
        self.assertIn("신청 기간 시작·종료일 모순 확인", result["unconfirmed_conditions"])


if __name__ == "__main__":
    unittest.main()
