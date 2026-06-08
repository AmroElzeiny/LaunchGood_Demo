from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from job_bot.telegram_subscription_store import SubscriptionStore, format_utc_iso, normalize_website, parse_utc_iso, utc_now_dt


class SubscriptionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._temp_dir.name) / "telegram_subscriptions.db"
        self.store = SubscriptionStore(self.db_path)

    def tearDown(self) -> None:
        self.store.close()
        self._temp_dir.cleanup()

    def test_upsert_trial_subscription_and_get_active(self) -> None:
        created = self.store.upsert_trial_subscription(
            user_id=1001,
            username="alice",
            started_at_utc="2026-03-07T10:00:00Z",
            ends_at_utc="2099-03-09T10:00:00Z",
        )
        self.assertTrue(created)

        active = self.store.get_active_subscription(1001)
        self.assertIsNotNone(active)
        assert active is not None
        self.assertEqual(active.user_id, 1001)
        self.assertEqual(active.username, "alice")
        self.assertEqual(active.plan, "trial_7d")
        self.assertTrue(active.is_active)

    def test_get_active_subscription_returns_none_for_expired_trial(self) -> None:
        created = self.store.upsert_trial_subscription(
            user_id=1002,
            username="expired-user",
            started_at_utc="2020-01-01T00:00:00Z",
            ends_at_utc="2020-01-03T00:00:00Z",
        )
        self.assertTrue(created)
        self.assertIsNone(self.store.get_active_subscription(1002))

    def test_trial_subscription_can_be_claimed_only_once(self) -> None:
        first = self.store.upsert_trial_subscription(
            user_id=1003,
            username="trial-user",
            started_at_utc="2026-03-01T00:00:00Z",
            ends_at_utc="2026-03-03T00:00:00Z",
        )
        second = self.store.upsert_trial_subscription(
            user_id=1003,
            username="trial-user",
            started_at_utc="2026-03-10T00:00:00Z",
            ends_at_utc="2026-03-12T00:00:00Z",
        )
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertTrue(self.store.has_claimed_trial(1003))
        self.store.close()
        self.store = SubscriptionStore(self.db_path)
        self.assertTrue(self.store.has_claimed_trial(1003))

    def test_clear_all_trial_claims_allows_reclaim_after_restart(self) -> None:
        created = self.store.upsert_trial_subscription(
            user_id=1004,
            username="trial-reset-user",
            started_at_utc="2026-03-01T00:00:00Z",
            ends_at_utc="2026-03-03T00:00:00Z",
        )
        self.assertTrue(created)
        self.assertTrue(self.store.has_claimed_trial(1004))
        self.assertGreaterEqual(self.store.clear_all_trial_claims(), 1)
        self.assertFalse(self.store.has_claimed_trial(1004))

        self.store.close()
        self.store = SubscriptionStore(self.db_path)
        self.assertFalse(self.store.has_claimed_trial(1004))

    def test_notification_preference_defaults_to_true_and_persists(self) -> None:
        self.assertTrue(self.store.get_notification_preference(2001))
        self.store.set_notification_preference(2001, enabled=False)
        self.assertFalse(self.store.get_notification_preference(2001))
        self.store.set_notification_preference(2001, enabled=True)
        self.assertTrue(self.store.get_notification_preference(2001))

    def test_add_user_website_is_unique_per_user(self) -> None:
        added_first = self.store.add_user_website(3001, "https://example.com/jobs")
        added_second = self.store.add_user_website(3001, "https://example.com/jobs")
        added_other_user = self.store.add_user_website(3002, "https://example.com/jobs")

        self.assertTrue(added_first)
        self.assertFalse(added_second)
        self.assertTrue(added_other_user)

    def test_normalize_website_strips_tracking_and_sticky_job_query_params(self) -> None:
        self.assertEqual(
            normalize_website(
                "https://www.indeed.com/q-ux-designer-jobs.html?utm_source=chatgpt.com&sort=date&vjk=abc123"
            ),
            "https://www.indeed.com/q-ux-designer-jobs.html?sort=date",
        )
        self.assertEqual(
            normalize_website(
                "https://www.linkedin.com/jobs/ux-designer-jobs-worldwide/?currentJobId=4382976707"
            ),
            "https://www.linkedin.com/jobs/ux-designer-jobs-worldwide",
        )

    def test_disabled_websites_are_rejected_and_purged(self) -> None:
        self.assertFalse(
            self.store.add_user_website(
                3005,
                "https://www.rabota.ru/vacancy/%D0%B4%D0%B8%D0%B7%D0%B0%D0%B9%D0%BD%D0%B5%D1%80%20ux",
            )
        )
        self.assertEqual(self.store.get_user_websites(3005), [])
        self.assertFalse(
            self.store.add_user_website(
                3005,
                "https://www.upwork.com/freelance-jobs/uiux/",
            )
        )
        self.assertEqual(self.store.get_user_websites(3005), [])

    def test_removed_exact_website_url_is_rejected(self) -> None:
        self.assertFalse(
            self.store.add_user_website(
                3007,
                "https://www.fl.ru/projects/category/dizajn/web-dizajner-verstalschik-dizajn/",
            )
        )
        self.assertEqual(self.store.get_user_websites(3007), [])

    def test_disabled_website_queues_notice_before_purge(self) -> None:
        self.store.upsert_trial_subscription(
            user_id=3006,
            username="rima",
            started_at_utc="2026-03-01T00:00:00Z",
            ends_at_utc="2099-03-01T00:00:00Z",
        )
        created_at = format_utc_iso(utc_now_dt())
        self.store._conn.execute(
            """
            INSERT INTO user_websites (user_id, url, currency, created_at_utc)
            VALUES (?, ?, ?, ?)
            """,
            (
                3006,
                "https://www.rabota.ru/vacancy/%D0%B4%D0%B8%D0%B7%D0%B0%D0%B9%D0%BD%D0%B5%D1%80%20ux",
                "RUB",
                created_at,
            ),
        )
        self.store._conn.commit()

        self.store.close()
        self.store = SubscriptionStore(self.db_path)

        self.assertEqual(self.store.get_user_websites(3006), [])
        notices = self.store.list_due_system_notices(limit=10)
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0].user_id, 3006)
        self.assertEqual(notices[0].username, "rima")
        self.assertEqual(notices[0].notice_key, "website_removed:rabota.ru")

    def test_removed_exact_website_url_queues_notice_before_purge(self) -> None:
        self.store.upsert_trial_subscription(
            user_id=3008,
            username="lena",
            started_at_utc="2026-03-01T00:00:00Z",
            ends_at_utc="2099-03-01T00:00:00Z",
        )
        created_at = format_utc_iso(utc_now_dt())
        self.store._conn.execute(
            """
            INSERT INTO user_websites (user_id, url, currency, created_at_utc)
            VALUES (?, ?, ?, ?)
            """,
            (
                3008,
                "https://www.fl.ru/projects/category/dizajn/web-dizajner-verstalschik-dizajn/",
                "RUB",
                created_at,
            ),
        )
        self.store._conn.commit()

        self.store.close()
        self.store = SubscriptionStore(self.db_path)

        self.assertEqual(self.store.get_user_websites(3008), [])
        notices = self.store.list_due_system_notices(limit=10)
        self.assertTrue(any(notice.user_id == 3008 for notice in notices))
        exact_notice = next(notice for notice in notices if notice.user_id == 3008)
        self.assertEqual(
            exact_notice.notice_key,
            "website_removed:https://www.fl.ru/projects/category/dizajn/web-dizajner-verstalschik-dizajn",
        )

    def test_filter_selected_websites_returns_only_urls_chosen_by_users(self) -> None:
        self.store.upsert_trial_subscription(
            user_id=3001,
            username="alice",
            started_at_utc="2026-03-01T00:00:00Z",
            ends_at_utc="2099-03-01T00:00:00Z",
        )
        self.store.upsert_trial_subscription(
            user_id=3002,
            username="bob",
            started_at_utc="2026-03-01T00:00:00Z",
            ends_at_utc="2099-03-01T00:00:00Z",
        )
        self.store.add_user_website(3001, "https://example.com/jobs")
        self.store.add_user_website(3002, "https://another.example/jobs/remote")

        filtered = self.store.filter_selected_websites(
            [
                "https://example.com/jobs/",
                "https://ignored.example/jobs",
                "https://another.example/jobs/remote",
                "https://example.com/jobs",
            ]
        )

        self.assertEqual(
            filtered,
            [
                "https://example.com/jobs/",
                "https://another.example/jobs/remote",
            ],
        )

    def test_filter_selected_websites_ignores_inactive_users(self) -> None:
        self.store.upsert_trial_subscription(
            user_id=3003,
            username="active-user",
            started_at_utc="2026-03-01T00:00:00Z",
            ends_at_utc="2099-03-01T00:00:00Z",
        )
        self.store.upsert_trial_subscription(
            user_id=3004,
            username="inactive-user",
            started_at_utc="2020-01-01T00:00:00Z",
            ends_at_utc="2020-01-03T00:00:00Z",
        )
        self.store.add_user_website(3003, "https://active.example/jobs")
        self.store.add_user_website(3004, "https://inactive.example/jobs")

        filtered = self.store.filter_selected_websites(
            [
                "https://active.example/jobs",
                "https://inactive.example/jobs",
            ]
        )

        self.assertEqual(filtered, ["https://active.example/jobs"])
        self.assertEqual(
            self.store.get_all_user_websites(active_only=True),
            ["https://active.example/jobs"],
        )

    def test_filter_selected_websites_returns_empty_when_no_user_chose_any(self) -> None:
        filtered = self.store.filter_selected_websites(
            [
                "https://example.com/jobs",
                "https://another.example/jobs/remote",
            ]
        )

        self.assertEqual(filtered, [])

    def test_get_active_website_selection_counts_orders_by_active_users(self) -> None:
        self.store.upsert_trial_subscription(
            user_id=3010,
            username="alice",
            started_at_utc="2026-03-01T00:00:00Z",
            ends_at_utc="2099-03-01T00:00:00Z",
        )
        self.store.upsert_trial_subscription(
            user_id=3011,
            username="bob",
            started_at_utc="2026-03-01T00:00:00Z",
            ends_at_utc="2099-03-01T00:00:00Z",
        )
        self.store.add_user_website(3010, "https://popular.example/jobs")
        self.store.add_user_website(3011, "https://popular.example/jobs")
        self.store.add_user_website(3010, "https://custom.example/jobs")

        counts = self.store.get_active_website_selection_counts()

        self.assertEqual(counts["https://popular.example/jobs"], 2)
        self.assertEqual(counts["https://custom.example/jobs"], 1)

    def test_mute_windows_replace_and_delivery_check(self) -> None:
        self.store.replace_mute_windows(4001, {1, 13, 22})
        self.assertEqual(self.store.get_mute_windows(4001), {1, 13, 22})
        self.store.set_notification_preference(4001, enabled=False)
        self.assertFalse(self.store.should_deliver_now(4001))
        self.store.set_notification_preference(4001, enabled=True)
        next_time = self.store.next_allowed_delivery_time(4001)
        self.assertTrue(next_time.endswith("Z"))

    def test_quiet_hours_use_local_timezone_offset(self) -> None:
        self.store.set_timezone_offset_minutes(4002, 120)
        self.store.replace_quiet_hours_range(4002, 22, 8)

        blocked_utc = datetime(2026, 3, 14, 20, 15, tzinfo=timezone.utc)
        allowed_utc = datetime(2026, 3, 15, 7, 15, tzinfo=timezone.utc)

        self.assertFalse(self.store.should_deliver_now(4002, at_utc=blocked_utc))
        self.assertTrue(self.store.should_deliver_now(4002, at_utc=allowed_utc))
        self.assertEqual(
            self.store.next_allowed_delivery_time(4002, from_utc=blocked_utc),
            "2026-03-15T06:00:00Z",
        )

    def test_next_digest_delivery_time_uses_local_timezone(self) -> None:
        self.store.set_timezone_offset_minutes(4003, 120)

        from_utc = datetime(2026, 3, 14, 3, 30, tzinfo=timezone.utc)

        self.assertEqual(
            self.store.next_digest_delivery_time(4003, from_utc=from_utc),
            "2026-03-14T04:00:00Z",
        )

    def test_notification_queue_due_and_sent(self) -> None:
        self.store.queue_notification(
            user_id=5001,
            username="queue-user",
            card_url="https://example.com/post-1",
            message_text="message one",
            available_after_utc="2000-01-01T00:00:00Z",
        )
        due = self.store.get_due_notifications(limit=10)
        self.assertEqual(len(due), 1)
        self.store.mark_notification_sent(due[0].queue_id)
        self.assertEqual(self.store.get_due_notifications(limit=10), [])

    def test_notification_queue_preserves_delivery_event_id(self) -> None:
        self.store.queue_notification(
            user_id=5003,
            username="queue-user",
            card_url="https://example.com/post-3",
            message_text="message three",
            available_after_utc="2000-01-01T00:00:00Z",
            delivery_event_id=77,
        )

        due = self.store.get_due_notifications(limit=10)
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0].delivery_event_id, 77)

    def test_delivery_job_metrics_track_backlog_delay_and_match_time(self) -> None:
        queue_id = self.store.queue_delivery_job(
            website="https://example.com/jobs",
            card_url="https://example.com/jobs/1",
            card_json='{"url":"https://example.com/jobs/1"}',
            available_after_utc="2000-01-01T00:00:00Z",
        )
        self.assertEqual(self.store.get_job_delivery_queue_backlog(), 1)

        claimed = self.store.claim_due_delivery_jobs(limit=1)
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0].queue_id, queue_id)

        with self.store._lock:
            self.store._conn.execute(
                """
                UPDATE delivery_job_queue
                SET created_at_utc = ?,
                    started_processing_at_utc = ?,
                    completed_at_utc = ?,
                    status = 'completed'
                WHERE id = ?
                """,
                (
                    "2026-04-05T10:00:00Z",
                    "2026-04-05T10:01:00Z",
                    "2026-04-05T10:03:30Z",
                    queue_id,
                ),
            )
            self.store._conn.commit()

        self.assertEqual(self.store.get_job_delivery_queue_backlog(), 0)
        self.assertEqual(self.store.get_average_delivery_delay_seconds(), 210.0)
        self.assertEqual(self.store.get_average_match_time_seconds(), 150.0)

    def test_filter_mismatch_events_are_recorded_and_grouped(self) -> None:
        self.store.record_filter_mismatch_event(
            user_id=5100,
            username="alice",
            card_url="https://example.com/jobs/1",
            card_website="https://example.com/jobs",
            mismatch_stage="location",
            reason="location mismatch",
        )
        self.store.record_filter_mismatch_event(
            user_id=5100,
            username="alice",
            card_url="https://example.com/jobs/2",
            card_website="https://example.com/jobs",
            mismatch_stage="location",
            reason="location mismatch",
        )
        self.store.record_filter_mismatch_event(
            user_id=5100,
            username="alice",
            card_url="https://example.com/jobs/3",
            card_website="https://example.com/jobs",
            mismatch_stage="role",
            reason="role mismatch",
        )

        self.assertEqual(self.store.count_filter_mismatch_events(5100), 3)
        self.assertEqual(
            self.store.get_filter_mismatch_stage_counts(5100),
            [("location", 2), ("role", 1)],
        )

    def test_manual_review_cases_round_trip_and_decision(self) -> None:
        review_id = self.store.queue_manual_review_case(
            user_id=5150,
            username="alice",
            card_url="https://example.com/jobs/product-designer-frontend",
            card_website="https://example.com/jobs",
            card_title="Product Designer / Frontend",
            card_company="Acme",
            card_location="Remote",
            card_salary="USD 4000 per month",
            card_language="en",
            card_payload={
                "website": "https://example.com/jobs",
                "url": "https://example.com/jobs/product-designer-frontend",
                "title": "Product Designer / Frontend",
                "description": "Own design and React work.",
                "salary": "USD 4000 per month",
                "location": "Remote",
                "is_job_post": True,
                "confidence": 0.71,
                "extraction_method": "test",
                "company": "Acme",
                "language": "en",
            },
            review_kind="role_ambiguity",
            ambiguity_summary="The role mixes design and engineering signals.",
            match_reason="Likely relevant, but the role mixes product design and frontend ownership.",
            role_title="UX/UI Designer",
            location_preference="Remote Global",
            salary_preference="",
            include_no_salary=True,
            keywords=["figma", "saas"],
            context_payload={"ambiguity_reasons": ["The role mixes design and engineering signals."]},
        )

        pending = self.store.list_manual_review_cases(status="pending", only_undispatched=True, limit=10)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].review_id, review_id)
        self.assertEqual(pending[0].card_company, "Acme")

        self.store.mark_manual_review_dispatched(review_id, admin_chat_id=100000001, admin_message_id=777)
        reviewed = self.store.get_manual_review_case(review_id)
        self.assertIsNotNone(reviewed)
        assert reviewed is not None
        self.assertEqual(reviewed.admin_chat_id, 100000001)
        self.assertEqual(reviewed.admin_message_id, 777)

        self.store.set_manual_review_decision(
            review_id,
            decision="approved_sent",
            reviewer="admin",
            notes="Approved after review.",
            sent_count=1,
        )
        decided = self.store.get_manual_review_case(review_id)
        self.assertIsNotNone(decided)
        assert decided is not None
        self.assertEqual(decided.status, "approved_sent")
        self.assertEqual(decided.sent_count, 1)
        self.assertEqual(self.store.list_manual_review_cases(status="pending", limit=10), [])

    def test_inactivity_report_anchor_and_delivery_counts_round_trip(self) -> None:
        first_id = self.store.record_delivery_event(
            user_id=5200,
            username="alice",
            card_url="https://example.com/jobs/1",
            card_title="UX Designer",
            card_company="Acme",
            card_location="Remote",
            card_website="https://example.com/jobs",
            match_reason="Matched your saved alert settings.",
        )
        second_id = self.store.record_delivery_event(
            user_id=5200,
            username="alice",
            card_url="https://example.com/jobs/2",
            card_title="Product Designer",
            card_company="Acme",
            card_location="Remote",
            card_website="https://example.com/jobs",
            match_reason="Matched your saved alert settings.",
        )

        self.assertGreater(second_id, first_id)
        self.assertEqual(self.store.count_delivery_events(5200), 2)
        self.assertTrue(self.store.get_latest_delivery_event_at_utc(5200).endswith("Z"))
        self.store.mark_delivery_event_sent(first_id, telegram_chat_id=5200, telegram_message_id=77)
        first_event = self.store.get_delivery_event(first_id)
        self.assertIsNotNone(first_event)
        assert first_event is not None
        self.assertEqual(first_event.telegram_chat_id, 5200)
        self.assertEqual(first_event.telegram_message_id, 77)
        self.assertTrue(first_event.sent_at_utc.endswith("Z"))

        self.store.mark_inactivity_report_sent(5200, anchor_utc="2026-04-04T10:00:00Z")
        self.assertEqual(self.store.get_last_inactivity_report_anchor_utc(5200), "2026-04-04T10:00:00Z")
        self.assertTrue(self.store.get_last_inactivity_report_sent_at_utc(5200).endswith("Z"))

    def test_cancel_queued_notifications_for_user_only_cancels_active_rows(self) -> None:
        self.store.queue_notification(
            user_id=5004,
            username="queue-user-a",
            card_url="https://example.com/post-4",
            message_text="message four",
            available_after_utc="2000-01-01T00:00:00Z",
        )
        self.store.queue_notification(
            user_id=5004,
            username="queue-user-a",
            card_url="https://example.com/post-5",
            message_text="message five",
            available_after_utc="2000-01-01T00:00:00Z",
        )
        self.store.queue_notification(
            user_id=5005,
            username="queue-user-b",
            card_url="https://example.com/post-6",
            message_text="message six",
            available_after_utc="2000-01-01T00:00:00Z",
        )

        due = self.store.get_due_notifications(limit=10)
        sent_queue_id = next(item.queue_id for item in due if item.user_id == 5004)
        self.store.mark_notification_sent(sent_queue_id)

        cancelled_count = self.store.cancel_queued_notifications_for_user(
            5004,
            reason="filter_changed:websites",
        )

        self.assertEqual(cancelled_count, 1)
        rows = self.store._conn.execute(
            """
            SELECT user_id, status, last_error
            FROM notification_queue
            ORDER BY id ASC
            """
        ).fetchall()
        self.assertEqual(
            [(int(row["user_id"]), str(row["status"]), row["last_error"]) for row in rows],
            [
                (5004, "sent", None),
                (5004, "cancelled", "filter_changed:websites"),
                (5005, "queued", None),
            ],
        )

    def test_expired_subscription_cancels_queued_notifications(self) -> None:
        self.store.upsert_trial_subscription(
            user_id=5002,
            username="expired-queue-user",
            started_at_utc="2020-01-01T00:00:00Z",
            ends_at_utc="2020-01-02T00:00:00Z",
        )
        self.store.queue_notification(
            user_id=5002,
            username="expired-queue-user",
            card_url="https://example.com/post-2",
            message_text="message two",
            available_after_utc="2000-01-01T00:00:00Z",
        )

        self.assertIsNone(self.store.get_active_subscription(5002))
        self.assertEqual(self.store.get_due_notifications(limit=10), [])

        row = self.store._conn.execute(
            "SELECT status, last_error FROM notification_queue WHERE user_id = ? LIMIT 1",
            (5002,),
        ).fetchone()
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(str(row["status"]), "cancelled")
        self.assertEqual(str(row["last_error"]), "subscription_inactive")

    def test_reset_dashboard_stats_clears_history_but_keeps_live_subscribers(self) -> None:
        self.store.upsert_trial_subscription(
            user_id=5901,
            username="active-user",
            started_at_utc="2026-03-01T00:00:00Z",
            ends_at_utc="2099-03-01T00:00:00Z",
        )
        self.store.add_user_website(5901, "https://example.com/jobs")
        self.store.log_filter_change(user_id=5901, filter_name="websites", filter_value="https://example.com/jobs")
        self.store.queue_notification(
            user_id=5901,
            username="active-user",
            card_url="https://example.com/post-7",
            message_text="message seven",
            available_after_utc="2000-01-01T00:00:00Z",
        )
        event_id = self.store.record_delivery_event(
            user_id=5901,
            username="active-user",
            card_url="https://example.com/post-7",
            card_title="Backend Engineer",
            card_company="Example",
            card_location="Remote",
            card_website="https://example.com/jobs",
            match_reason="role match",
        )
        self.store.save_match_feedback(event_id, 5901, "up")
        self.store.create_payment_session(
            user_id=5901,
            username="active-user",
            plan="monthly",
            duration_days=30,
            amount_usd=1.0,
            provider_payment_id="mock-5901",
            payment_url="https://nowpayments.io/payment/?iid=mock-5901",
            status="waiting",
        )

        self.store.reset_dashboard_stats()

        self.assertIsNotNone(self.store.get_active_subscription(5901))
        self.assertEqual(self.store.get_user_websites(5901), ["https://example.com/jobs"])
        self.assertEqual(self.store._conn.execute("SELECT COUNT(*) FROM notification_queue").fetchone()[0], 0)
        self.assertEqual(self.store._conn.execute("SELECT COUNT(*) FROM delivery_events").fetchone()[0], 0)
        self.assertEqual(self.store._conn.execute("SELECT COUNT(*) FROM match_feedback").fetchone()[0], 0)
        self.assertEqual(self.store._conn.execute("SELECT COUNT(*) FROM filter_audit_logs").fetchone()[0], 0)
        self.assertEqual(self.store._conn.execute("SELECT COUNT(*) FROM payment_sessions").fetchone()[0], 0)

    def test_payment_session_lifecycle_and_paid_activation(self) -> None:
        local_payment_id = self.store.create_payment_session(
            user_id=6001,
            username="payer",
            plan="monthly",
            duration_days=30,
            amount_usd=1.0,
            provider_payment_id="mock-123",
            payment_url="https://nowpayments.io/payment/?iid=mock-123",
            status="waiting",
        )
        session = self.store.get_payment_session(local_payment_id)
        self.assertIsNotNone(session)
        assert session is not None
        self.assertEqual(session.status, "waiting")

        self.store.update_payment_status(local_payment_id, "finished")
        session_after_update = self.store.get_payment_session(local_payment_id)
        assert session_after_update is not None
        self.assertEqual(session_after_update.status, "finished")

        active_sub = self.store.activate_paid_subscription(
            user_id=6001,
            username="payer",
            plan="monthly",
            duration_days=30,
        )
        self.assertTrue(active_sub.is_active)
        self.assertEqual(active_sub.plan, "monthly")

    def test_paid_activation_extends_existing_active_time(self) -> None:
        started = utc_now_dt() - timedelta(days=1)
        existing_end = utc_now_dt() + timedelta(days=10)
        self.store.upsert_trial_subscription(
            user_id=6002,
            username="renew-user",
            started_at_utc=format_utc_iso(started),
            ends_at_utc=format_utc_iso(existing_end),
        )

        active_sub = self.store.activate_paid_subscription(
            user_id=6002,
            username="renew-user",
            plan="monthly",
            duration_days=30,
        )

        self.assertGreaterEqual(
            parse_utc_iso(active_sub.ends_at_utc),
            existing_end + timedelta(days=30) - timedelta(seconds=1),
        )

    def test_structured_location_salary_and_filter_audit(self) -> None:
        self.store.set_role_preference(7001, "UX/UI Designer")
        self.assertEqual(self.store.get_role_preference(7001), "UX/UI Designer")

        self.store.set_location_preference(
            user_id=7001,
            location="Cairo, Egypt",
            country="Egypt",
            state="Cairo Governorate",
            city="Cairo",
            confidence=0.92,
        )
        location = self.store.get_location_structured(7001)
        self.assertEqual(location["country"], "Egypt")
        self.assertEqual(location["state"], "Cairo Governorate")
        self.assertEqual(location["city"], "Cairo")

        self.store.set_salary_range_preference(
            user_id=7001,
            salary_range_usd="USD 1000-3000 per month",
            min_usd=1000,
            max_usd=3000,
            currency="USD",
            confidence=0.88,
        )
        salary = self.store.get_salary_range_structured(7001)
        self.assertEqual(salary["salary_range_usd"], "USD 1000-3000 per month")
        self.assertEqual(salary["min_usd"], 1000.0)
        self.assertEqual(salary["max_usd"], 3000.0)
        self.assertEqual(salary["currency"], "USD")

        self.store.log_filter_change(7001, "location", "Cairo, Egypt")
        self.store.log_filter_change(7001, "salary_range_usd", "USD 1000-3000 per month")
        changes = self.store.list_filter_changes(7001, limit=10)
        self.assertGreaterEqual(len(changes), 2)
        self.assertEqual(changes[0][0], "salary_range_usd")
        self.assertEqual(changes[1][0], "location")

    def test_project_preferences_round_trip_and_clear(self) -> None:
        self.store.set_project_preferences(
            7002,
            {
                "deliverables": ["landing page redesign", "dashboard redesign"],
                "minimum_payment_usd": 1200,
                "maximum_payment_usd": 5000,
                "minimum_payment_rub": 120000,
                "maximum_payment_rub": 350000,
            },
        )

        loaded = self.store.get_project_preferences(7002)

        self.assertEqual(loaded["deliverables"], ["landing page redesign", "dashboard redesign"])
        self.assertEqual(loaded["minimum_payment_usd"], 1200.0)
        self.assertEqual(loaded["maximum_payment_usd"], 5000.0)
        self.assertEqual(loaded["minimum_payment_rub"], 120000.0)
        self.assertEqual(loaded["maximum_payment_rub"], 350000.0)
        self.assertTrue(self.store.get_include_no_salary(7002))

        self.store.clear_user_alert(7002)

        cleared = self.store.get_project_preferences(7002)
        self.assertEqual(cleared["deliverables"], [])
        self.assertIsNone(cleared["minimum_payment_usd"])
        self.assertIsNone(cleared["maximum_payment_usd"])
        self.assertIsNone(cleared["minimum_payment_rub"])
        self.assertIsNone(cleared["maximum_payment_rub"])

    def test_clear_user_alert_resets_preferences_and_pauses_alert(self) -> None:
        self.store.set_role_preference(7050, "Product Designer")
        self.store.set_location_preference(7050, "Remote Global (Limited Chances)")
        self.store.replace_keywords(7050, ["figma", "saas"])
        self.store.add_user_website(7050, "https://example.com/jobs")
        self.store.set_notification_preference(7050, enabled=True)
        self.store.queue_notification(
            user_id=7050,
            username="designer",
            card_url="https://example.com/jobs/1",
            message_text="queued job",
            available_after_utc="2000-01-01T00:00:00Z",
        )

        self.store.clear_user_alert(7050)

        self.assertEqual(self.store.get_role_preference(7050), "")
        self.assertEqual(self.store.get_location_preference(7050), "")
        self.assertEqual(self.store.get_user_keywords(7050), [])
        self.assertEqual(self.store.get_user_websites(7050), [])
        self.assertFalse(self.store.get_notification_preference(7050))
        row = self.store._conn.execute(
            "SELECT status, last_error FROM notification_queue WHERE user_id = ? LIMIT 1",
            (7050,),
        ).fetchone()
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(str(row["status"]), "cancelled")
        self.assertEqual(str(row["last_error"]), "alert_deleted")

    def test_has_alert_configuration_requires_role(self) -> None:
        self.store.add_user_website(7051, "https://example.com/jobs")
        self.store.replace_keywords(7051, ["figma"])
        self.store.set_location_preference(7051, "Remote Global (Limited Chances)")

        self.assertFalse(self.store.has_alert_configuration(7051))

        self.store.set_role_preference(7051, "UX/UI Designer")
        self.assertTrue(self.store.has_alert_configuration(7051))

    def test_match_feedback_guidance_uses_delivery_events(self) -> None:
        event_up = self.store.record_delivery_event(
            user_id=7052,
            username="feedback-user",
            card_url="https://example.com/jobs/backend-1",
            card_title="Backend Engineer",
            card_company="Acme",
            card_location="Remote",
            card_website="https://example.com",
            match_reason="Role match",
        )
        event_down = self.store.record_delivery_event(
            user_id=7052,
            username="feedback-user",
            card_url="https://example.com/jobs/frontend-1",
            card_title="Frontend Engineer",
            card_company="Beta",
            card_location="Cairo, Egypt",
            card_website="https://example.com",
            match_reason="Keyword match",
        )

        self.store.save_match_feedback(event_up, 7052, "up")
        self.store.save_match_feedback(event_down, 7052, "down")
        guidance = self.store.build_match_feedback_guidance(7052, limit=8)

        self.assertIn("LIKED | title=Backend Engineer", guidance)
        self.assertIn("DISLIKED | title=Frontend Engineer", guidance)

    def test_match_feedback_guidance_includes_saved_feedback_details(self) -> None:
        event_down = self.store.record_delivery_event(
            user_id=7053,
            username="feedback-user",
            card_url="https://example.com/jobs/frontend-2",
            card_title="Frontend Engineer",
            card_company="Gamma",
            card_location="Remote - Egypt",
            card_website="https://example.com",
            match_reason="Keyword match",
        )

        self.store.save_match_feedback(event_down, 7053, "down")
        self.store.save_match_feedback_details(
            event_down,
            7053,
            raw_feedback="role=frontend, location=Egypt only, not UX/UI",
            feedback_summary="Avoid frontend engineering roles restricted to Egypt for this user.",
            job_title_issue="Frontend Engineer instead of UX/UI Designer.",
            location_issue="Remote within Egypt.",
        )
        guidance = self.store.build_match_feedback_guidance(7053, limit=8)

        self.assertIn("feedback_summary=Avoid frontend engineering roles restricted to Egypt for this user.", guidance)
        self.assertIn("title_issue=Frontend Engineer instead of UX/UI Designer.", guidance)
        self.assertIn("location_issue=Remote within Egypt.", guidance)

    def test_expiry_reminders_follow_trial_and_paid_windows(self) -> None:
        now_dt = utc_now_dt()
        self.store.upsert_trial_subscription(
            user_id=7060,
            username="trial-reminder",
            started_at_utc=format_utc_iso(now_dt - timedelta(hours=36)),
            ends_at_utc=format_utc_iso(now_dt + timedelta(hours=6)),
        )
        self.store.activate_paid_subscription(
            user_id=7061,
            username="paid-reminder",
            plan="monthly",
            duration_days=2,
        )
        reminders = self.store.list_subscriptions_for_expiry_reminder()
        reminder_user_ids = {sub.user_id for sub in reminders}
        self.assertIn(7060, reminder_user_ids)
        self.assertIn(7061, reminder_user_ids)

    def test_clear_location_filter_changes_removes_location_history_only(self) -> None:
        self.store.log_filter_change(7101, "location", "Cairo, Egypt")
        self.store.log_filter_change(7101, "keywords", "python")
        self.store.log_filter_change(7101, "location", "Alexandria, Egypt")

        self.store.clear_filter_changes(7101, "location")
        changes = self.store.list_filter_changes(7101, limit=10)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0][0], "keywords")

    def test_busy_timeout_is_configured(self) -> None:
        row = self.store._conn.execute("PRAGMA busy_timeout").fetchone()
        self.assertIsNotNone(row)
        assert row is not None
        self.assertGreaterEqual(int(row[0]), 30000)

    def test_search_method_defaults_to_opportunities(self) -> None:
        self.store.set_search_method(7201, "projects")
        self.assertEqual(self.store.get_search_method(7201), "opportunities")
        self.store.set_search_method(7201, "both")
        self.assertEqual(self.store.get_search_method(7201), "opportunities")

    def test_keywords_are_capped_to_fifty(self) -> None:
        self.store.replace_keywords(7301, [f"kw{i}" for i in range(100)])
        saved = self.store.get_user_keywords(7301)
        self.assertEqual(len(saved), 50)

    def test_landing_seen_flag(self) -> None:
        self.assertFalse(self.store.has_seen_landing(7401))
        self.store.mark_seen_landing(7401)
        self.assertTrue(self.store.has_seen_landing(7401))
        self.store.close()
        self.store = SubscriptionStore(self.db_path)
        self.assertTrue(self.store.has_seen_landing(7401))

    def test_first_start_admin_notified_flag(self) -> None:
        self.assertFalse(self.store.has_first_start_admin_notified(7402))
        self.store.mark_first_start_admin_notified(7402)
        self.assertTrue(self.store.has_first_start_admin_notified(7402))
        self.store.close()
        self.store = SubscriptionStore(self.db_path)
        self.assertTrue(self.store.has_first_start_admin_notified(7402))

    def test_read_paths_do_not_leave_open_write_transaction(self) -> None:
        self.store.get_active_subscribers()
        self.assertFalse(self.store._conn.in_transaction)

        self.store.get_active_subscription(999999)
        self.assertFalse(self.store._conn.in_transaction)

        self.store.list_subscriptions_for_24h_reminder()
        self.assertFalse(self.store._conn.in_transaction)


if __name__ == "__main__":
    unittest.main()
