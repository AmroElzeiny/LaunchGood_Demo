from __future__ import annotations

import logging
import unittest

from job_bot.filter_ai import FilterAI


class FilterAITests(unittest.IsolatedAsyncioTestCase):
    def test_split_location_preferences_supports_multiple_locations(self) -> None:
        values = FilterAI.split_location_preferences("Mumbai, India; Thane\nPune | Navi Mumbai & Remote Global")
        self.assertEqual(values, ["Mumbai, India", "Thane", "Pune", "Navi Mumbai", "Remote Global"])

    async def test_expand_keywords_adds_remote_canonical_term(self) -> None:
        ai = FilterAI(openai_api_key="", openai_model="gpt-4o-mini", logger=logging.getLogger("test"))
        expanded = await ai.expand_keywords(["wfh", "python"])
        self.assertIn("remote", expanded)
        self.assertIn("python", expanded)

    async def test_interpret_keywords_returns_structured_fallback_output(self) -> None:
        ai = FilterAI(openai_api_key="", openai_model="gpt-4o-mini", logger=logging.getLogger("test"))
        interpretation = await ai.interpret_keywords(["UX/UI", "wfh"])
        self.assertEqual(interpretation.input_keywords, ["ux/ui", "wfh"])
        self.assertIn("ux/ui", interpretation.expanded_keywords)
        self.assertIn("remote", interpretation.expanded_keywords)
        self.assertEqual(len(interpretation.per_keyword), 2)

    async def test_confirm_post_matches_filters_falls_back_to_prechecks_when_ai_unavailable(self) -> None:
        ai = FilterAI(openai_api_key="", openai_model="gpt-4o-mini", logger=logging.getLogger("test"))
        match, reason = await ai.confirm_post_matches_filters(
            post_x={"url": "https://example.com/jobs/1", "title": "Backend Engineer"},
            user_filters={"location_preference": "Cairo, Egypt"},
            precheck_results={
                "location": {"matched": False, "reason": "Post location mismatch."},
                "salary": {"matched": True, "reason": "Salary in range."},
            },
        )
        self.assertFalse(match)
        self.assertIn("location", reason.lower())

    async def test_confirm_post_matches_filters_passes_when_all_prechecks_pass(self) -> None:
        ai = FilterAI(openai_api_key="", openai_model="gpt-4o-mini", logger=logging.getLogger("test"))
        match, reason = await ai.confirm_post_matches_filters(
            post_x={"url": "https://example.com/jobs/2", "title": "Python Developer"},
            user_filters={"keywords": ["python"]},
            precheck_results={"keywords": {"matched": True, "reason": "Keyword match found."}},
        )
        self.assertTrue(match)
        self.assertIn("passed", reason.lower())

    async def test_confirm_post_matches_filters_ai_prompt_includes_remote_within_country_rule(self) -> None:
        ai = FilterAI(openai_api_key="", openai_model="gpt-4o-mini", logger=logging.getLogger("test"))
        ai.client = object()
        captured: dict[str, object] = {}

        def fake_chat_json(system_prompt: str, payload: dict[str, object]) -> dict[str, object]:
            captured["system_prompt"] = system_prompt
            captured["payload"] = payload
            return {"match": True, "reason": "remote-country match"}

        ai._chat_json = fake_chat_json  # type: ignore[method-assign]
        match, reason = await ai.confirm_post_matches_filters(
            post_x={
                "url": "https://example.com/jobs/3",
                "title": "Product Designer",
                "location": "Remote",
                "location_context": "Remote role open only to candidates in Egypt.",
            },
            user_filters={
                "role_title": "Product Designer",
                "location_preference": "Remote within Egypt",
                "remote_country": "Egypt",
                "location_rule": "remote_within_country",
                "requires_remote": True,
            },
            precheck_results={"location": {"matched": True, "reason": "Remote Egypt match."}},
        )

        self.assertTrue(match)
        self.assertEqual(reason, "remote-country match")
        payload = captured["payload"]
        assert isinstance(payload, dict)
        self.assertEqual(payload["user_filters"]["location_rule"], "remote_within_country")
        self.assertEqual(payload["user_filters"]["remote_country"], "Egypt")
        self.assertTrue(payload["user_filters"]["requires_remote"])
        task = str(payload["task"])
        self.assertIn("remote_within_country", task)
        self.assertIn("remote posts", task)
        self.assertIn("remote_country", task)

    async def test_confirm_post_matches_filters_ai_prompt_supports_any_of_multiple_specific_locations(self) -> None:
        ai = FilterAI(openai_api_key="", openai_model="gpt-4o-mini", logger=logging.getLogger("test"))
        ai.client = object()
        captured: dict[str, object] = {}

        def fake_chat_json(system_prompt: str, payload: dict[str, object]) -> dict[str, object]:
            captured["system_prompt"] = system_prompt
            captured["payload"] = payload
            return {"match": True, "reason": "matched one location"}

        ai._chat_json = fake_chat_json  # type: ignore[method-assign]
        match, reason = await ai.confirm_post_matches_filters(
            post_x={
                "url": "https://example.com/jobs/4",
                "title": "Backend Engineer",
                "location": "Pune, India",
            },
            user_filters={
                "location_preference": "Mumbai; Pune",
                "location_preferences": ["Mumbai", "Pune"],
                "location_rule": "specific_location",
            },
            precheck_results={"location": {"matched": True, "reason": "Matched one location from the list."}},
        )

        self.assertTrue(match)
        self.assertEqual(reason, "matched one location")
        payload = captured["payload"]
        assert isinstance(payload, dict)
        self.assertEqual(payload["user_filters"]["location_preferences"], ["Mumbai", "Pune"])
        task = str(payload["task"])
        self.assertIn("location_preferences", task)
        self.assertIn("any one of those locations", task)

    async def test_confirm_post_matches_filters_ai_prompt_supports_any_of_mixed_location_filters(self) -> None:
        ai = FilterAI(openai_api_key="", openai_model="gpt-4o-mini", logger=logging.getLogger("test"))
        ai.client = object()
        captured: dict[str, object] = {}

        def fake_chat_json(system_prompt: str, payload: dict[str, object]) -> dict[str, object]:
            captured["system_prompt"] = system_prompt
            captured["payload"] = payload
            return {"match": True, "reason": "matched mixed location filters"}

        ai._chat_json = fake_chat_json  # type: ignore[method-assign]
        match, reason = await ai.confirm_post_matches_filters(
            post_x={
                "url": "https://example.com/jobs/4b",
                "title": "Product Designer",
                "location": "Austin, USA",
                "location_context": "Austin, USA On-site role in Austin, USA.",
            },
            user_filters={
                "location_preference": "Remote Global; On-site/hybrid within USA",
                "location_filters": [
                    {"label": "Remote Global", "rule": "remote_global_only"},
                    {"label": "On-site/hybrid within USA", "rule": "onsite_hybrid_within_country", "country": "USA"},
                ],
                "location_rule": "any_of_location_filters",
            },
            precheck_results={"location": {"matched": True, "reason": "Matched one saved filter."}},
        )

        self.assertTrue(match)
        self.assertEqual(reason, "matched mixed location filters")
        payload = captured["payload"]
        assert isinstance(payload, dict)
        self.assertEqual(payload["user_filters"]["location_rule"], "any_of_location_filters")
        self.assertEqual(len(payload["user_filters"]["location_filters"]), 2)
        task = str(payload["task"])
        self.assertIn("location_filters", task)
        self.assertIn("OR list of saved location filters", task)

    async def test_confirm_post_matches_filters_ai_prompt_includes_remote_global_rule(self) -> None:
        ai = FilterAI(openai_api_key="", openai_model="gpt-4o-mini", logger=logging.getLogger("test"))
        ai.client = object()
        captured: dict[str, object] = {}

        def fake_chat_json(system_prompt: str, payload: dict[str, object]) -> dict[str, object]:
            captured["system_prompt"] = system_prompt
            captured["payload"] = payload
            return {"match": True, "reason": "remote global match"}

        ai._chat_json = fake_chat_json  # type: ignore[method-assign]
        match, reason = await ai.confirm_post_matches_filters(
            post_x={
                "url": "https://example.com/jobs/5",
                "title": "Product Designer",
                "location": "Remote",
                "location_context": "Remote from anywhere, worldwide team.",
            },
            user_filters={
                "role_title": "Product Designer",
                "location_preference": "Remote Global",
                "location_rule": "remote_global_only",
                "requires_remote": True,
                "requires_global_remote": True,
            },
            precheck_results={"location": {"matched": True, "reason": "Global remote match."}},
        )

        self.assertTrue(match)
        self.assertEqual(reason, "remote global match")
        payload = captured["payload"]
        assert isinstance(payload, dict)
        self.assertEqual(payload["user_filters"]["location_rule"], "remote_global_only")
        self.assertTrue(payload["user_filters"]["requires_global_remote"])
        task = str(payload["task"])
        self.assertIn("remote_global_only", task)
        self.assertIn("globally remote", task)

    async def test_confirm_post_matches_filters_ai_prompt_includes_onsite_hybrid_country_rule(self) -> None:
        ai = FilterAI(openai_api_key="", openai_model="gpt-4o-mini", logger=logging.getLogger("test"))
        ai.client = object()
        captured: dict[str, object] = {}

        def fake_chat_json(system_prompt: str, payload: dict[str, object]) -> dict[str, object]:
            captured["system_prompt"] = system_prompt
            captured["payload"] = payload
            return {"match": True, "reason": "onsite country match"}

        ai._chat_json = fake_chat_json  # type: ignore[method-assign]
        match, reason = await ai.confirm_post_matches_filters(
            post_x={
                "url": "https://example.com/jobs/7",
                "title": "Product Designer",
                "location": "Cairo, Egypt",
                "location_context": "On-site role in Cairo, Egypt.",
            },
            user_filters={
                "role_title": "Product Designer",
                "location_preference": "On-site/hybrid within Egypt",
                "onsite_hybrid_country": "Egypt",
                "location_rule": "onsite_hybrid_within_country",
                "requires_remote": False,
                "requires_onsite_or_hybrid": True,
            },
            precheck_results={"location": {"matched": True, "reason": "On-site/hybrid-within-country alert matched jobs in Egypt."}},
        )

        self.assertTrue(match)
        self.assertEqual(reason, "onsite country match")
        payload = captured["payload"]
        assert isinstance(payload, dict)
        self.assertEqual(payload["user_filters"]["location_rule"], "onsite_hybrid_within_country")
        self.assertEqual(payload["user_filters"]["onsite_hybrid_country"], "Egypt")
        self.assertFalse(payload["user_filters"]["requires_remote"])
        self.assertTrue(payload["user_filters"]["requires_onsite_or_hybrid"])
        task = str(payload["task"])
        self.assertIn("onsite_hybrid_within_country", task)
        self.assertIn("reject fully remote posts", task)

    async def test_assess_role_alignment_fallback_rejects_adjacent_keyword_overlap_role(self) -> None:
        ai = FilterAI(openai_api_key="", openai_model="gpt-4o-mini", logger=logging.getLogger("test"))
        match, reason = await ai.assess_role_alignment(
            requested_role="UX/UI Designer",
            post_title="Frontend Engineer",
            post_description="Build React UI components and collaborate with product designers.",
            post_location="Remote",
        )

        self.assertFalse(match)
        self.assertIn("primary role", reason.lower())

    async def test_assess_role_alignment_fallback_accepts_broad_ux_ui_mixed_design_build_role(self) -> None:
        ai = FilterAI(openai_api_key="", openai_model="gpt-4o-mini", logger=logging.getLogger("test"))
        match, reason = await ai.assess_role_alignment(
            requested_role="UX/UI Designer",
            post_title="Frontend Engineer",
            post_description=(
                "Own the design system, build wireframes and prototypes in Figma, map user flows, "
                "and implement polished product experiences."
            ),
            post_location="Remote",
        )

        self.assertTrue(match)
        self.assertIn("design", reason.lower())

    async def test_check_remote_global_match_fallback_rejects_country_limited_remote(self) -> None:
        ai = FilterAI(openai_api_key="", openai_model="gpt-4o-mini", logger=logging.getLogger("test"))
        match, reason = await ai.check_remote_global_match(
            post_title="Product Designer",
            post_description="Remote role open only to candidates based in Egypt.",
            post_location="Remote - Egypt",
        )

        self.assertFalse(match)
        self.assertIn("country", reason.lower())

    async def test_check_remote_global_match_fallback_rejects_country_limited_remote_in_russian(self) -> None:
        ai = FilterAI(openai_api_key="", openai_model="gpt-4o-mini", logger=logging.getLogger("test"))
        match, reason = await ai.check_remote_global_match(
            post_title="Продуктовый дизайнер",
            post_description="Удаленная работа только для кандидатов из России и по московскому времени.",
            post_location="Удаленно, Россия",
        )

        self.assertFalse(match)
        self.assertIn("country", reason.lower())

    async def test_assess_work_arrangement_fallback_detects_true_global_remote(self) -> None:
        ai = FilterAI(openai_api_key="", openai_model="gpt-4o-mini", logger=logging.getLogger("test"))
        assessment = await ai.assess_work_arrangement(
            post_title="Product Designer",
            post_description="Work from anywhere in the world on our globally remote design team.",
            post_location="Remote",
        )

        self.assertTrue(assessment.is_remote)
        self.assertEqual(assessment.remote_scope, "global")
        self.assertFalse(assessment.has_scope_restriction)

    async def test_check_remote_country_eligibility_fallback_rejects_global_remote(self) -> None:
        ai = FilterAI(openai_api_key="", openai_model="gpt-4o-mini", logger=logging.getLogger("test"))
        match, reason = await ai.check_remote_country_eligibility(
            target_country="Egypt",
            post_title="Product Designer",
            post_description="Work from anywhere in the world on our globally remote design team.",
            post_location="Remote",
        )

        self.assertFalse(match)
        self.assertIn("does not count", reason.lower())

    async def test_semantic_match_any_uses_lexical_path_without_embeddings(self) -> None:
        ai = FilterAI(openai_api_key="", openai_model="gpt-4o-mini", logger=logging.getLogger("test"))
        match_yes, _ = await ai.semantic_match_any(
            ["python developer"],
            "urgent role for python developer remote",
            threshold=0.58,
            label="keyword",
        )
        self.assertTrue(match_yes)

        match_no, reason = await ai.semantic_match_any(
            ["data scientist"],
            "urgent role for python developer remote",
            threshold=0.58,
            label="keyword",
        )
        self.assertFalse(match_no)
        self.assertIn("embeddings unavailable", reason.lower())

    async def test_semantic_match_any_rejects_frontend_role_for_ux_ui_topic(self) -> None:
        ai = FilterAI(openai_api_key="", openai_model="gpt-4o-mini", logger=logging.getLogger("test"))
        match, reason = await ai.semantic_match_any(
            ["UX/UI Designer"],
            "Front-End Developer building React applications and UI components.",
            threshold=0.56,
            label="role",
        )
        self.assertFalse(match)
        self.assertIn("not ux/ui", reason.lower())

    async def test_semantic_match_any_accepts_mixed_design_build_role_for_ux_ui_topic(self) -> None:
        ai = FilterAI(openai_api_key="", openai_model="gpt-4o-mini", logger=logging.getLogger("test"))
        match, reason = await ai.semantic_match_any(
            ["UX/UI Designer"],
            (
                "Frontend Engineer owning wireframes, prototypes, user flows, and Figma design system work "
                "while implementing the final interface."
            ),
            threshold=0.56,
            label="role",
        )
        self.assertTrue(match)
        self.assertIn("design", reason.lower())

    async def test_semantic_match_any_accepts_product_designer_for_ux_ui_topic(self) -> None:
        ai = FilterAI(openai_api_key="", openai_model="gpt-4o-mini", logger=logging.getLogger("test"))
        match, reason = await ai.semantic_match_any(
            ["UX/UI Designer"],
            "Senior Product Designer creating Figma prototypes, user flows, and usability improvements.",
            threshold=0.56,
            label="role",
        )
        self.assertTrue(match)
        self.assertIn("ux/ui", reason.lower())

    async def test_semantic_match_any_accepts_russian_product_designer_for_ux_ui_topic(self) -> None:
        ai = FilterAI(openai_api_key="", openai_model="gpt-4o-mini", logger=logging.getLogger("test"))
        match, reason = await ai.semantic_match_any(
            ["UX/UI Designer"],
            "Продуктовый дизайнер создает прототипы, пользовательские сценарии и улучшает юзабилити в Figma.",
            threshold=0.56,
            label="role",
        )
        self.assertTrue(match)
        self.assertIn("ux/ui", reason.lower())

    async def test_expand_keywords_adds_russian_remote_variations(self) -> None:
        ai = FilterAI(openai_api_key="", openai_model="gpt-4o-mini", logger=logging.getLogger("test"))
        expanded = await ai.expand_keywords(["удаленно", "figma"])
        self.assertIn("remote", expanded)
        self.assertIn("удаленно", expanded)

    async def test_semantic_match_any_accepts_inflected_russian_keyword_forms(self) -> None:
        ai = FilterAI(openai_api_key="", openai_model="gpt-4o-mini", logger=logging.getLogger("test"))
        match, reason = await ai.semantic_match_any(
            ["прототип"],
            "Нужны интерактивные прототипы и быстрые вайрфреймы в Figma.",
            threshold=0.56,
            label="keyword",
        )

        self.assertTrue(match)
        self.assertIn("match", reason.lower())


    def test_fallback_remote_global_match_handles_real_russian_text(self) -> None:
        matched, reason = FilterAI._fallback_remote_global_match(
            "",
            (
                "\u0423\u0434\u0430\u043b\u0435\u043d\u043d\u0430\u044f \u0440\u0430\u0431\u043e\u0442\u0430 "
                "\u0442\u043e\u043b\u044c\u043a\u043e \u0434\u043b\u044f "
                "\u043a\u0430\u043d\u0434\u0438\u0434\u0430\u0442\u043e\u0432 \u0438\u0437 "
                "\u0420\u043e\u0441\u0441\u0438\u0438"
            ),
            "",
        )

        self.assertFalse(matched)
        self.assertIn("country", reason.lower())


if __name__ == "__main__":
    unittest.main()
