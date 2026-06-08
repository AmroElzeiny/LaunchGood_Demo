from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from job_bot.human_review_queue import job_card_from_review_item
from job_bot.models import JobCard
from job_bot.storage import StateStore


class HumanReviewQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._temp_dir.name) / "job_bot_state.db"
        self.store = StateStore(self.db_path)

    def tearDown(self) -> None:
        self.store.close()
        self._temp_dir.cleanup()

    @staticmethod
    def _sample_card() -> JobCard:
        return JobCard(
            website="https://example.com",
            url="https://example.com/jobs/backend",
            title="Backend Engineer",
            description="Build APIs",
            salary="Unknown",
            location="Remote",
            is_job_post=True,
            confidence=0.0,
            extraction_method="ai-pass-3",
            company="Acme",
        )

    def test_queue_and_decide_human_review_item(self) -> None:
        item_id = self.store.queue_human_review_item(
            self._sample_card(),
            reason="low_confidence_extraction:0.440<0.620",
            source="ai-pass-3",
        )
        self.assertGreater(item_id, 0)

        pending = self.store.list_human_review_items(status="pending", limit=10)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].id, item_id)
        self.assertEqual(pending[0].status, "pending")
        self.assertEqual(pending[0].confidence, 0.0)
        self.assertEqual(self.store.list_human_review_items(status="pending", only_undispatched=True, limit=10)[0].id, item_id)

        self.store.mark_human_review_dispatched(item_id, admin_chat_id=100000001, admin_message_id=77)
        dispatched = self.store.get_human_review_item(item_id)
        self.assertIsNotNone(dispatched)
        assert dispatched is not None
        self.assertEqual(dispatched.admin_chat_id, 100000001)
        self.assertEqual(dispatched.admin_message_id, 77)
        self.assertEqual(self.store.list_human_review_items(status="pending", only_undispatched=True, limit=10), [])

        self.store.set_human_review_decision(
            item_id,
            decision="neglected",
            reviewer="qa",
            notes="Looks like a non-job detail page.",
        )
        pending_after = self.store.list_human_review_items(status="pending", limit=10)
        self.assertEqual(pending_after, [])

        guidance = self.store.build_review_guidance(limit=10)
        self.assertIn("NEGLECTED", guidance)
        self.assertIn("Backend Engineer", guidance)

    def test_queue_human_review_item_preserves_project_fields_in_card_json(self) -> None:
        item_id = self.store.queue_human_review_item(
            JobCard(
                website="https://example.com",
                url="https://example.com/projects/landing-page-redesign",
                title="Landing Page Redesign",
                description="Redesign a SaaS landing page and tidy the design system.",
                salary="USD 2400 fixed",
                location="Remote",
                is_job_post=False,
                is_relevant_opportunity=True,
                confidence=0.62,
                extraction_method="local-jsonld-fallback",
                company="Acme",
                client="Acme",
                budget="USD 2400 fixed",
                duration="2 weeks",
                engagement_type="project",
                skills_required="Figma, design systems",
                contact_url="https://example.com/projects/landing-page-redesign",
            ),
            reason="project_review",
            source="local-jsonld-fallback",
        )

        item = self.store.get_human_review_item(item_id)
        self.assertIsNotNone(item)
        assert item is not None
        reconstructed = job_card_from_review_item(item)

        self.assertEqual(reconstructed.title, "Landing Page Redesign")
        self.assertEqual(reconstructed.budget, "USD 2400 fixed")
        self.assertEqual(reconstructed.duration, "2 weeks")
        self.assertEqual(reconstructed.engagement_type, "project")
        self.assertEqual(reconstructed.skills_required, "Figma, design systems")
        self.assertEqual(reconstructed.contact_url, "https://example.com/projects/landing-page-redesign")


if __name__ == "__main__":
    unittest.main()
