from __future__ import annotations

import json
import unittest

from job_bot.extractors import extract_domain_opportunity_card, local_fallback_card, page_to_text


class _FakeCssResult:
    def __init__(self, values: list[str]) -> None:
        self._values = list(values)

    def getall(self) -> list[str]:
        return list(self._values)

    def get(self) -> str | None:
        return self._values[0] if self._values else None


class _FakePage:
    def __init__(self, *, scripts: list[str], body_text: list[str], selector_map: dict[str, list[str]] | None = None) -> None:
        self._scripts = list(scripts)
        self._body_text = list(body_text)
        self._selector_map = {key: list(values) for key, values in (selector_map or {}).items()}

    def css(self, selector: str) -> _FakeCssResult:
        if selector == 'script[type="application/ld+json"]::text':
            return _FakeCssResult(self._scripts)
        if selector == "body ::text":
            return _FakeCssResult(self._body_text)
        if selector in self._selector_map:
            return _FakeCssResult(self._selector_map[selector])
        return _FakeCssResult([])


class ExtractorFallbackTests(unittest.TestCase):
    def test_local_fallback_card_extracts_project_brief_from_generic_jsonld(self) -> None:
        payload = {
            "@type": "CreativeWork",
            "name": "Landing Page Redesign Project",
            "description": (
                "Client needs a freelance UX/UI designer for a SaaS landing page redesign. "
                "Fixed price budget available and proposals are requested this week."
            ),
            "provider": {"name": "Acme SaaS"},
            "budget": {"price": "2500", "priceCurrency": "USD"},
            "location": "Remote",
            "serviceType": "project brief",
            "skills": ["Figma", "Landing pages"],
            "url": "https://example.com/projects/landing-page-redesign",
            "datePublished": "2026-04-05",
            "deadline": "2026-04-10",
        }
        page = _FakePage(
            scripts=[json.dumps(payload)],
            body_text=["Landing page redesign", "Freelance UX/UI designer", "Fixed price budget"],
        )

        card = local_fallback_card("https://example.com", "https://example.com/projects/landing-page-redesign", page)

        self.assertIsNotNone(card)
        assert card is not None
        self.assertTrue(card.is_relevant_opportunity)
        self.assertFalse(card.is_job_post)
        self.assertEqual(card.opportunity_kind, "project_post")
        self.assertEqual(card.counterparty, "Acme SaaS")
        self.assertIn("2500", card.payment_terms)
        self.assertEqual(card.opportunity_location, "Remote")
        self.assertIn("Figma", card.skills_required)
        self.assertEqual(card.proposal_or_contact_url, "https://example.com/projects/landing-page-redesign")

    def test_domain_template_extracts_project_detail_before_ai_is_needed(self) -> None:
        page = _FakePage(
            scripts=[],
            body_text=[
                "Project brief",
                "Deliverables: dashboard redesign and design system cleanup",
                "Budget: USD 4500 fixed price",
                "Proposal deadline: 2026-04-12",
            ],
            selector_map={
                "h1": ["Dashboard Redesign for B2B SaaS"],
                ".client-name": ["Northwind Labs"],
                ".project-description": ["Dashboard redesign, audit, wireframes, and Figma cleanup for a SaaS client."],
                ".budget": ["USD 4500 fixed price"],
                ".engagement-type": ["Project"],
                ".duration": ["2-3 weeks"],
                ".skills li": ["Figma", "Dashboards", "Design systems"],
                ".proposal-deadline": ["2026-04-12"],
                ".location": ["Remote"],
            },
        )
        html = """
        <html>
          <body>
            <a href="/submit-proposal">Submit proposal</a>
          </body>
        </html>
        """

        card = extract_domain_opportunity_card(
            "https://example.com",
            "https://example.com/projects/dashboard-redesign",
            page,
            page_kind="project_post",
            page_text=page_to_text(page),
            html=html,
        )

        self.assertIsNotNone(card)
        assert card is not None
        self.assertTrue(card.extraction_method.startswith("local-dom-template-fallback:"))
        self.assertEqual(card.counterparty, "Northwind Labs")
        self.assertEqual(card.engagement_type, "Project")
        self.assertIn("4500", card.payment_terms)
        self.assertIn("Figma", card.skills_required)
        self.assertEqual(card.proposal_or_contact_url, "https://example.com/submit-proposal")

    def test_local_fallback_uses_heuristic_dom_for_brief_style_pages(self) -> None:
        page = _FakePage(
            scripts=[],
            body_text=[
                "Landing Page Optimization Brief",
                "Client: Founder-led SaaS",
                "Deliverables: landing page redesign, copy refresh, and Figma cleanup",
                "Budget: USD 2500 fixed price",
                "Engagement Type: One-off project",
                "Duration: 1-2 weeks",
                "Proposal deadline: 2026-04-15",
                "Remote: Global",
                "Skills: Figma, landing pages, SaaS",
            ],
        )
        html = """
        <html>
          <head>
            <meta name="description" content="Client needs a freelance UX/UI designer for a landing page redesign brief." />
          </head>
          <body>
            <a href="https://briefs.example.com/send-proposal">Send proposal</a>
          </body>
        </html>
        """

        card = local_fallback_card(
            "https://example.com",
            "https://example.com/briefs/landing-page-optimization",
            page,
            page_kind="project_post",
            page_text=page_to_text(page),
            html=html,
        )

        self.assertIsNotNone(card)
        assert card is not None
        self.assertEqual(card.extraction_method, "local-heuristic-dom-fallback")
        self.assertEqual(card.counterparty, "Founder-led SaaS")
        self.assertIn("2500", card.payment_terms)
        self.assertIn("One-off project", card.engagement_type)
        self.assertIn("landing page redesign", card.scope_summary.lower())
        self.assertEqual(card.proposal_or_contact_url, "https://briefs.example.com/send-proposal")

    def test_domain_template_parses_russian_deadline_without_ai(self) -> None:
        page = _FakePage(
            scripts=[],
            body_text=[
                "Тендер на UX аудит",
                "Срок подачи: 12 апреля 2026",
                "Бюджет: 120000 RUB",
            ],
            selector_map={
                "h1": ["UX аудит продукта"],
                ".project-description": ["Нужен UX/UI дизайнер для аудита и прототипов."],
                ".budget": ["120000 RUB"],
                ".proposal-deadline": ["12 апреля 2026"],
                ".location": ["Удаленно"],
            },
        )
        html = """
        <html>
          <body>
            <a href="/respond">Откликнуться</a>
          </body>
        </html>
        """

        card = extract_domain_opportunity_card(
            "https://workspace.ru",
            "https://workspace.ru/tenders/ux-audit-18451",
            page,
            page_kind="request_for_proposal",
            page_text=page_to_text(page),
            html=html,
        )

        self.assertIsNotNone(card)
        assert card is not None
        self.assertEqual(card.proposal_deadline, "2026-04-12T00:00:00Z")
        self.assertEqual(card.proposal_or_contact_url, "https://workspace.ru/respond")


if __name__ == "__main__":
    unittest.main()
