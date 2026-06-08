from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from job_bot.models import JobCard
from job_bot.storage import StateStore


class StateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._temp_dir.name) / "job_bot_state.db"
        self.store = StateStore(self.db_path)

    def tearDown(self) -> None:
        self.store.close()
        self._temp_dir.cleanup()

    def test_busy_timeout_is_configured(self) -> None:
        row = self.store._conn.execute("PRAGMA busy_timeout").fetchone()
        self.assertIsNotNone(row)
        assert row is not None
        self.assertGreaterEqual(int(row[0]), 30000)

    def test_read_path_does_not_leave_transaction_open(self) -> None:
        self.store.get_link_state("https://example.com", "https://example.com/jobs/1")
        self.assertFalse(self.store._conn.in_transaction)

    def test_register_job_cluster_marks_duplicate_and_prefers_company_site(self) -> None:
        aggregator_card = JobCard(
            website="https://www.linkedin.com",
            url="https://www.linkedin.com/jobs/view/123",
            title="Backend Engineer",
            description="Build APIs",
            salary="USD 3000-5000 per month",
            location="Remote",
            is_job_post=True,
            confidence=0.9,
            extraction_method="test",
            company="Acme",
        )
        company_card = JobCard(
            website="https://jobs.acme.com",
            url="https://jobs.acme.com/backend-engineer",
            title="Backend Engineer",
            description="Build APIs",
            salary="USD 3000-5000 per month",
            location="Remote",
            is_job_post=True,
            confidence=0.9,
            extraction_method="test",
            company="Acme",
        )

        first = self.store.register_job_cluster(aggregator_card)
        second = self.store.register_job_cluster(company_card)

        self.assertFalse(first.is_duplicate)
        self.assertTrue(second.is_duplicate)
        self.assertTrue(second.canonical_changed)
        self.assertEqual(second.canonical_url, "https://jobs.acme.com/backend-engineer")

    def test_register_job_cluster_returns_same_cluster_key_for_cross_site_duplicate(self) -> None:
        linkedin_card = JobCard(
            website="https://www.linkedin.com",
            url="https://www.linkedin.com/jobs/view/456",
            title="Product Designer",
            description="Design product experiences",
            salary="USD 4000 per month",
            location="Cairo, Egypt",
            is_job_post=True,
            confidence=0.92,
            extraction_method="test",
            company="Nova",
        )
        wellfound_card = JobCard(
            website="https://wellfound.com",
            url="https://wellfound.com/jobs/789",
            title="Product Designer",
            description="Design product experiences",
            salary="USD 4000 per month",
            location="Cairo, Egypt",
            is_job_post=True,
            confidence=0.91,
            extraction_method="test",
            company="Nova",
        )

        first = self.store.register_job_cluster(linkedin_card)
        second = self.store.register_job_cluster(wellfound_card)

        self.assertTrue(second.is_duplicate)
        self.assertEqual(first.cluster_key, second.cluster_key)

    def test_register_job_cluster_deduplicates_russian_cards_across_sites(self) -> None:
        workspace_card = JobCard(
            website="https://workspace.ru",
            url="https://workspace.ru/tenders/redizayn-sayta-123",
            title="Продуктовый дизайнер",
            description="Проектировать интерфейсы и пользовательские сценарии",
            salary="200000 RUB",
            location="Москва, Россия",
            is_job_post=True,
            confidence=0.94,
            extraction_method="test",
            company="Яндекс",
            language="ru",
        )
        fl_card = JobCard(
            website="https://www.fl.ru",
            url="https://www.fl.ru/projects/456/ux-redesign.html",
            title="Продуктовый дизайнер",
            description="Проектировать интерфейсы и пользовательские сценарии",
            salary="200000 RUB",
            location="Москва, Россия",
            is_job_post=True,
            confidence=0.93,
            extraction_method="test",
            company="Яндекс",
            language="ru",
        )

        first = self.store.register_job_cluster(workspace_card)
        second = self.store.register_job_cluster(fl_card)

        self.assertFalse(first.is_duplicate)
        self.assertTrue(second.is_duplicate)
        self.assertEqual(first.cluster_key, second.cluster_key)

    def test_reset_dashboard_stats_clears_dashboard_tables(self) -> None:
        self.store.mark_status("https://example.com", "https://example.com/jobs/1", status="queued_human_review")
        self.store.save_cycle_stats(
            cycle_utc="2026-03-26T00:00:00Z",
            website="https://example.com",
            fetched_pages=1,
            discovered_links=2,
            checked_links=1,
            seen_links_in_row=0,
            new_cards=1,
            errors=0,
            last_error=None,
        )
        item_id = self.store.queue_human_review_item(
            JobCard(
                website="https://example.com",
                url="https://example.com/jobs/2",
                title="Backend Engineer",
                description="Build APIs",
                salary="Unknown",
                location="Remote",
                is_job_post=True,
                confidence=0.4,
                extraction_method="test",
            ),
            reason="low_confidence_extraction:0.400<0.620",
        )
        self.store.set_human_review_decision(item_id, decision="neglected")
        self.store.register_job_cluster(
            JobCard(
                website="https://example.com",
                url="https://example.com/jobs/3",
                title="Platform Engineer",
                description="Operate systems",
                salary="Unknown",
                location="Remote",
                is_job_post=True,
                confidence=0.9,
                extraction_method="test",
                company="Acme",
            )
        )

        self.store.reset_dashboard_stats()

        self.assertEqual(self.store._conn.execute("SELECT COUNT(*) FROM seen_links").fetchone()[0], 0)
        self.assertEqual(self.store._conn.execute("SELECT COUNT(*) FROM cycle_stats").fetchone()[0], 0)
        self.assertEqual(self.store._conn.execute("SELECT COUNT(*) FROM human_review_queue").fetchone()[0], 0)
        self.assertEqual(self.store._conn.execute("SELECT COUNT(*) FROM human_review_feedback").fetchone()[0], 0)
        self.assertEqual(self.store._conn.execute("SELECT COUNT(*) FROM job_clusters").fetchone()[0], 0)
        self.assertEqual(self.store._conn.execute("SELECT COUNT(*) FROM job_cluster_sources").fetchone()[0], 0)

    def test_summarize_alert_health_aggregates_recent_scans_and_rejections(self) -> None:
        healthy_site = "https://example.com/jobs"
        blocked_site = "https://blocked.example/jobs"
        self.store.save_cycle_stats(
            cycle_utc="2026-04-04T10:00:00Z",
            website=healthy_site,
            fetched_pages=1,
            discovered_links=5,
            checked_links=2,
            seen_links_in_row=0,
            new_cards=1,
            errors=0,
            last_error=None,
        )
        self.store.save_cycle_stats(
            cycle_utc="2026-04-04T10:05:00Z",
            website=blocked_site,
            fetched_pages=0,
            discovered_links=2,
            checked_links=0,
            seen_links_in_row=0,
            new_cards=0,
            errors=1,
            last_error="blocked by source",
        )
        self.store.mark_status(
            healthy_site,
            "https://example.com/jobs/old-post",
            status="neglected_old_post",
            neglected=True,
        )
        self.store.mark_status(
            healthy_site,
            "https://example.com/jobs/filter-miss",
            status="neglected_filter_mismatch",
            neglected=True,
        )

        summary = self.store.summarize_alert_health(
            [healthy_site, blocked_site],
            since_utc="2000-01-01T00:00:00Z",
        )

        self.assertEqual(summary.last_scan_utc, "2026-04-04T10:05:00Z")
        self.assertEqual(summary.discovered_posts, 7)
        self.assertEqual(summary.blocked_sources, 1)
        self.assertEqual(summary.blocked_source_urls, (blocked_site,))
        self.assertEqual(summary.freshness_rejects, 1)
        self.assertEqual(summary.rejected_posts, 2)

    def test_get_discovery_feedback_marks_challenge_blocked_sources(self) -> None:
        website = "https://blocked.example/jobs"
        self.store.record_discovery_page_outcome(website, website, "challenge_blocked")
        self.store.record_discovery_page_outcome(website, website, "challenge_blocked")

        feedback = self.store.get_discovery_feedback(website)

        self.assertEqual(feedback.blocked_source_urls, (website,))

    def test_list_temporarily_blocked_sites_returns_recent_challenge_blocks(self) -> None:
        website = "https://blocked.example/jobs"
        self.store.record_discovery_page_outcome(website, website, "challenge_blocked")
        self.store.record_discovery_page_outcome(website, website, "challenge_blocked")

        blocked = self.store.list_temporarily_blocked_sites([website], cooldown_seconds=1800, challenge_threshold=2)

        self.assertIn(website, blocked)
        self.assertEqual(blocked[website].reason_code, "challenge_blocked")
        self.assertTrue(blocked[website].blocked_until_utc.endswith("Z"))

    def test_summarize_alert_health_counts_challenge_blocked_sources(self) -> None:
        blocked_site = "https://blocked.example/jobs"
        self.store.record_discovery_page_outcome(blocked_site, blocked_site, "challenge_blocked")
        self.store.record_discovery_page_outcome(blocked_site, blocked_site, "challenge_blocked")

        summary = self.store.summarize_alert_health(
            [blocked_site],
            since_utc="2000-01-01T00:00:00Z",
        )

        self.assertEqual(summary.blocked_sources, 1)
        self.assertEqual(summary.blocked_source_urls, (blocked_site,))

    def test_register_job_cluster_deduplicates_rewritten_cross_site_reposts(self) -> None:
        aggregator_card = JobCard(
            website="https://www.linkedin.com",
            url="https://www.linkedin.com/jobs/view/991",
            title="Senior Product Designer",
            description="Own UX flows, prototypes, Figma design systems, and product research for checkout experiences.",
            salary="$120000-$150000",
            location="Remote, Germany",
            is_job_post=True,
            confidence=0.93,
            extraction_method="test",
            company="Acme",
        )
        rewritten_card = JobCard(
            website="https://jobs.acme.com",
            url="https://jobs.acme.com/openings/product-design-checkout",
            title="Product Design Lead",
            description="Lead checkout UX, run user research, evolve the Figma system, and prototype core purchase journeys.",
            salary="$120000-$150000",
            location="Germany (Remote)",
            is_job_post=True,
            confidence=0.95,
            extraction_method="test",
            company="Acme",
        )

        first = self.store.register_job_cluster(aggregator_card)
        second = self.store.register_job_cluster(rewritten_card)

        self.assertFalse(first.is_duplicate)
        self.assertTrue(second.is_duplicate)
        self.assertEqual(second.canonical_url, "https://jobs.acme.com/openings/product-design-checkout")


if __name__ == "__main__":
    unittest.main()
