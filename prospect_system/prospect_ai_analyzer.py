from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from openai import OpenAI

from job_bot.openai_compat import create_chat_completion_with_fallback

from dashboard_state import ProspectAnalysisState, ScrapedPage
from errors import ProspectAIError
from prospect_card import ProspectCard
from prospect_config import ProspectSettings
from prospect_scraper import extract_contact_email


def _safe_json_loads(text: str) -> dict[str, Any]:
    cleaned = str(text or "").strip()
    if not cleaned:
        return {}
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                return {}
    return {}


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _remove_dash_punctuation(value: str) -> str:
    text = str(value or "")
    text = re.sub(r"(?m)^\s*[-\u2010-\u2015\u2212]\s+", "", text)
    text = re.sub(r"\s+[-\u2010-\u2015\u2212]\s+", ", ", text)
    text = re.sub(r"[\u2010-\u2015\u2212]", ", ", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\s+,", ",", text)
    return text.strip()


def _format_email_text(value: Any) -> str:
    return _remove_dash_punctuation(_clean_text(value))


def _format_email_body(value: Any) -> str:
    body = str(value or "").strip()
    if not body:
        return ""
    body = _remove_dash_punctuation(body)
    body = body.replace("\\n", "\n").replace("\r\n", "\n").replace("\r", "\n")
    body = re.sub(r"<br\s*/?>", "\n", body, flags=re.IGNORECASE)
    body = re.sub(r"</p>\s*<p[^>]*>", "\n\n", body, flags=re.IGNORECASE)
    body = re.sub(r"</?p[^>]*>", "", body, flags=re.IGNORECASE)
    if "\n" not in body:
        sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", body) if part.strip()]
        if len(sentences) >= 3:
            paragraphs: list[str] = []
            first = sentences[0]
            if re.match(r"^(hi|hello|dear)\b", first, flags=re.IGNORECASE):
                paragraphs.append(first)
                sentences = sentences[1:]
            while sentences:
                paragraphs.append(" ".join(sentences[:2]))
                sentences = sentences[2:]
            body = "\n\n".join(paragraphs)
    lines = [" ".join(line.split()).strip() for line in body.split("\n")]
    normalized = "\n".join(lines)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return _remove_dash_punctuation(normalized)


def _host(url: str) -> str:
    return urlsplit(str(url or "")).netloc.lower().strip(".")


def _same_site(url_a: str, url_b: str) -> bool:
    host_a = _host(url_a)
    host_b = _host(url_b)
    if not host_a or not host_b:
        return False
    return host_a == host_b or host_a.endswith("." + host_b) or host_b.endswith("." + host_a)


@dataclass(slots=True)
class InformationDecision:
    enough_information: bool
    selected_extra_urls: list[str] = field(default_factory=list)
    reason: str = ""
    missing_information: list[str] = field(default_factory=list)
    confidence: float = 0.0


class ProspectAIAnalyzer:
    def __init__(self, settings: ProspectSettings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger
        self.client = OpenAI(api_key=settings.openai_api_key) if settings.openai_api_key else None

    def assess_information_need(
        self,
        state: ProspectAnalysisState,
        *,
        website: str,
        candidate_links: list[dict[str, Any]],
    ) -> InformationDecision:
        if self.client is None or not self.settings.ai_model:
            return self._fallback_information_decision(state, website=website, candidate_links=candidate_links)
        pages = self._pages_payload(state, website=website, text_limit=3500)
        remaining_page_budget = max(0, self.settings.max_pages_to_scrape - len(self._pages_for_site(state, website)))
        visible_candidate_links = candidate_links[:60]
        payload = {
            "website": website,
            "target_criteria": state.target_criteria,
            "outreach_goal": state.outreach_goal,
            "scraped_pages": pages,
            "candidate_internal_links": visible_candidate_links,
            "max_pages_to_scrape": self.settings.max_pages_to_scrape,
            "scraped_page_count": len(pages),
            "remaining_page_budget": remaining_page_budget,
            "already_scraped_urls": [page.final_url for page in state.scraped_pages if _same_site(page.final_url, website)],
            "return_format": {
                "enough_information": "bool",
                "selected_extra_urls": ["exact_absolute_url_from_candidate_internal_links"],
                "reason": "short_string",
                "missing_information": ["string"],
                "confidence": "float_0_to_1",
            },
        }
        system_prompt = (
            "You are a strict, evidence-based AI prospect discovery analyst deciding whether the currently scraped website "
            "evidence is sufficient to evaluate prospect fit. Your task is to review scraped_pages, target_criteria, "
            "outreach_goal, candidate_internal_links, already_scraped_urls, and the scraping limits. Decide whether the "
            "current evidence is enough to judge the organization's fit, and choose the highest-value remaining internal "
            "URLs that should be scraped next. Return JSON only. Do not include markdown, comments, explanations outside "
            "JSON, or extra text. Output must be valid JSON parseable by Python json.loads(), with no trailing commas and "
            "no null values. Use only the keys listed in return_format: enough_information, selected_extra_urls, reason, "
            "missing_information, and confidence. "
            "Core rules: use only the scraped website text and candidate_internal_links provided in the input. Do not use "
            "outside knowledge, assumptions, web browsing, or invented URLs. Do not approve or reject the prospect; your "
            "role is only to decide evidence sufficiency and next pages to scrape. Be conservative: vague, generic, thin, "
            "blocked, duplicated, navigation-heavy, or unclear content is usually not enough for a confident fit decision. "
            "Never return URLs that are not exact URL strings from candidate_internal_links. Never rewrite, normalize, "
            "shorten, expand, or invent URLs. Do not select duplicate URLs, URLs in already_scraped_urls, anchor variants, "
            "tracking URLs when a cleaner candidate exists, login/account/cart/checkout/search/tag/category/archive pages, "
            "privacy/terms/cookie/accessibility pages, social media pages, media files, images, videos, or unrelated pages "
            "unless a candidate URL clearly contains essential organization information. "
            "Decision criteria: set enough_information to true only when scraped_pages provide reliable evidence for most "
            "of these: what the organization does; who it serves or sells to; its products, services, programs, or "
            "activities; its mission, audience, beneficiaries, industry, or market; clear relevance or non-relevance to "
            "target_criteria; a plausible outreach angle aligned with outreach_goal; and any obvious contact, team, or "
            "organizational context if available. Set enough_information to false when the core activity, target audience, "
            "services, programs, mission, criteria match, or outreach angle would require guessing, or when important "
            "context is likely available on unvisited internal pages. "
            "URL selection priorities, in order: About/Mission/Who We Are/Company overview; Services/Solutions/Products/"
            "Programs/What We Do; Industries/Customers/Beneficiaries/Use Cases/Case Studies; Team/Leadership/Staff/Board; "
            "Contact/Locations/Get in Touch; Partners/Community/Fundraising/Donate/Support/Impact; FAQ/Resources/Pricing/"
            "Membership/Admissions/Eligibility; News/Blog/Events/Reports only when likely to explain current activity or "
            "audience. If remaining_page_budget is 0, selected_extra_urls must be an empty array. If remaining_page_budget "
            "is 1, select only the single best remaining URL. Otherwise select a small ordered list, most valuable first, "
            "and no longer than remaining_page_budget. If no candidate is clearly useful, return an empty array. "
            "Field requirements: enough_information must be a JSON boolean, not a string. selected_extra_urls must be an "
            "array of exact URL strings copied only from candidate_internal_links. reason must briefly explain both the "
            "evidence sufficiency decision and why selected URLs were chosen or not chosen. missing_information must list "
            "the most important missing facts or evidence gaps; use an empty array if none. confidence must be a number "
            "from 0.0 to 1.0 indicating confidence in this sufficiency decision, not the prospect fit score."
        )
        try:
            data = self._chat_json(system_prompt, payload, stage="information_need")
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[prospect-ai] information decision failed: %s", str(exc))
            return self._fallback_information_decision(state, website=website, candidate_links=candidate_links)
        allowed_urls = {
            str(item.get("url") or "").strip()
            for item in visible_candidate_links
            if str(item.get("url") or "").strip()
        }
        return InformationDecision(
            enough_information=self._as_bool(data.get("enough_information")),
            selected_extra_urls=[
                url
                for item in data.get("selected_extra_urls", [])
                if (url := str(item).strip()) and url in allowed_urls
            ][:remaining_page_budget],
            reason=_clean_text(data.get("reason")),
            missing_information=[
                _clean_text(item)
                for item in data.get("missing_information", [])
                if _clean_text(item)
            ],
            confidence=self._as_float(data.get("confidence")),
        )

    def create_prospect_card(
        self,
        state: ProspectAnalysisState,
        *,
        website: str,
        reanalysis_instruction: str = "",
    ) -> ProspectCard:
        pages = self._pages_for_site(state, website)
        if self.client is None or not self.settings.ai_model:
            return self._fallback_card(state, website=website, reason="AI credentials are missing.")
        payload = {
            "website": website,
            "target_criteria": state.target_criteria,
            "outreach_goal": state.outreach_goal,
            "reanalysis_instruction": reanalysis_instruction,
            "scraped_pages": self._pages_payload(state, website=website, text_limit=7000),
            "return_format": {
                "company": "string",
                "website": "absolute_url",
                "category": "string",
                "country_region": "string_or_unknown",
                "target_audience": "string_or_unknown",
                "what_they_do": "string",
                "services_products": "string",
                "fit_score": "integer_0_to_100",
                "reason": "string",
                "why_relevant": "string",
                "pain_point": "string_or_unknown",
                "suggested_offer": "string_or_unknown",
                "recommended_contact_type": "string_or_unknown",
                "confidence": "High|Medium|Low plus short reason",
                "signals_of_fit": ["string"],
                "missing_information": ["string"],
                "possible_collaboration_angle": "string",
                "risk_uncertainty_flags": ["string"],
                "recommended_action": "string",
                "outreach_angle": "string",
                "contact_email": "email_or_empty",
                "contact_url": "absolute_url_or_empty",
                "next_step": "string",
                "ai_summary": "short_summary",
                "ai_reasoning_summary": "short_reasoning_summary",
            },
        }
        system_prompt = (
            "You are a strict, evidence-based AI prospect analyst creating CRM-ready prospect cards for human review. "
            "Your task is to evaluate the scraped website content against the provided target_criteria and outreach_goal, "
            "then produce one complete prospect card. Return JSON only. Do not include markdown, comments, explanations "
            "outside JSON, or extra text. The output must be valid JSON parseable by Python json.loads(). Do not include "
            "trailing commas or null values. Use strings, integers, and arrays exactly as requested by return_format. "
            "Use only the scraped website evidence provided in scraped_pages. Do not use outside knowledge, assumptions, "
            "inferred facts, or web browsing. Do not fabricate company details, contact details, emails, names, services, "
            "locations, partnerships, funding, intent, pain points, buying readiness, or approvals. Prefer 'Unknown' or "
            "an empty string over guessing. If information is missing, unclear, contradictory, weakly supported, blocked, "
            "generic, or mostly unreadable, state that clearly in missing_information and risk_uncertainty_flags, lower "
            "fit_score, and explain why. Do not mark a prospect as approved; final approval must remain a human decision. "
            "Be conservative with scoring: high scores require multiple direct pieces of scraped evidence, not vague keyword "
            "matches alone. Every major conclusion must be supported by scraped website evidence. "
            "Evaluation method: compare the organization's apparent business, audience, services, mission, industry, and "
            "needs against target_criteria; compare the likely outreach angle against outreach_goal; identify whether the "
            "organization has a supported pain point, relevant need, and plausible reason to engage; separate confirmed "
            "evidence from uncertainty. "
            "Fit score guidance: 90-100 means very strong fit with multiple direct evidence points, clear pain point, and "
            "strong outreach relevance. 75-89 means strong fit with good evidence but some missing or less specific details. "
            "55-74 means medium fit with partial match or plausible relevance but incomplete important evidence. 35-54 means "
            "weak fit with limited overlap or weak evidence. 0-34 means poor fit or not enough reliable evidence to justify "
            "outreach. The application derives fit_status from fit_score, so do not rely on a separate fit_status field. "
            "Confidence guidance: use 'High - ...' when evidence is clear, specific, and consistent; 'Medium - ...' when "
            "evidence supports the conclusion but has gaps or ambiguity; 'Low - ...' when content is thin, generic, unclear, "
            "blocked, or only indirectly relevant. "
            "Field requirements: company must be the organization name only if clearly found, otherwise 'Unknown'. website "
            "must be the analyzed website URL. category, country_region, target_audience, what_they_do, and services_products "
            "must be based only on evidence. reason explains fit or non-fit. why_relevant explains the strongest relevance "
            "to target_criteria. pain_point must be evidence-supported or 'Unknown'. suggested_offer and outreach_angle must "
            "align with outreach_goal and evidence without inventing needs. recommended_contact_type should be a role type, "
            "not a specific person unless clearly shown. contact_email and contact_url must only contain values directly "
            "found in scraped evidence, otherwise use an empty string. signals_of_fit should list concise evidence-backed "
            "signals. missing_information should list important absent facts that would improve the decision. "
            "risk_uncertainty_flags should list specific uncertainty reasons. recommended_action and next_step should be "
            "human-review actions such as review, gather more information, approve for CRM consideration, or reject; they "
            "must not claim final approval. ai_summary should summarize the organization and fit in 1-2 sentences. "
            "ai_reasoning_summary should summarize the evidence-based reasoning in 2-4 concise sentences."
        )
        try:
            data = self._chat_json(system_prompt, payload, stage="prospect_card")
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[prospect-ai] prospect card failed: %s", str(exc))
            return self._fallback_card(state, website=website, reason=str(exc))

        try:
            score = int(float(data.get("fit_score", 0)))
        except (TypeError, ValueError):
            score = 0
        score = max(0, min(score, 100))
        fit_status = self.settings.fit_status_for_score(score)
        if not data.get("contact_email"):
            data["contact_email"] = extract_contact_email(
                "\n".join(page.text for page in pages),
                "\n".join(page.metadata.get("html_excerpt", "") for page in pages),
            )
        return ProspectCard.from_ai_json(
            data,
            website=website,
            fit_status=fit_status,
            pages_scraped=[page.final_url for page in pages],
            session_id=state.session_id,
        )

    def generate_outreach_draft(
        self,
        state: ProspectAnalysisState,
        *,
        card: ProspectCard,
        regenerate_instruction: str = "",
    ) -> dict[str, str]:
        if self.client is None or not self.settings.ai_model:
            return self._fallback_outreach_draft(card, state)
        payload = {
            "target_criteria": state.target_criteria,
            "outreach_goal": state.outreach_goal,
            "prospect_card": card.to_dict(),
            "regenerate_instruction": regenerate_instruction,
            "scraped_page_context": self._pages_payload(state, website=card.website, text_limit=3500),
            "return_format": {
                "subject": "string",
                "body": "string",
                "cta": "string",
            },
        }
        system_prompt = (
            "You are a strict, evidence-based outreach copywriter and prospect analyst creating a warm, professional, "
            "human-sounding email draft for CRM review. Your task is to write one natural outreach email based only on "
            "the provided prospect_card, scraped_page_context, target_criteria, outreach_goal, fit_score, confidence, "
            "and optional regenerate_instruction. Return JSON only. Do not include markdown, comments, explanations "
            "outside JSON, or extra text. The output must be valid JSON parseable by Python json.loads(). Do not include "
            "trailing commas or null values. Use only the keys in return_format: subject, body, and cta. "
            "Core evidence rules: use only information provided in scraped_page_context, prospect_card, target_criteria, "
            "outreach_goal, and regenerate_instruction. Do not use outside knowledge, assumptions, web browsing, inferred "
            "company facts, or invented context. Do not fabricate names, roles, contact details, partnerships, funding, "
            "company size, locations, intent, urgency, pain points, buying readiness, prior contact, referrals, meetings, "
            "or approval status. If a detail is not clearly supported, do not mention it. If the evidence is weak, generic, "
            "blocked, contradictory, or incomplete, make the email more cautious and exploratory. Do not claim the recipient "
            "has a confirmed problem unless the evidence directly supports it. One specific, evidence-backed observation is "
            "better than several weak assumptions. The email must be suitable for human review before sending. "
            "Fit-score logic: for fit_score 90-100, write a confident, specific email with a clear value connection and "
            "direct but low-pressure CTA. For 75-89, write a confident but not exaggerated email using one strong "
            "evidence-backed reason for relevance. For 55-74, write an exploratory email and frame the offer as potentially "
            "relevant. For 35-54, write a cautious, low-pressure email focused on learning whether there is a fit. For "
            "0-34, do not write a persuasive sales email; keep it conservative and review-oriented, or politely indicate "
            "that outreach may not be recommended inside the body. "
            "Confidence logic: if confidence is High, the email may be specific and direct. If confidence is Medium, use "
            "softer wording such as 'may be relevant', 'could be useful', or 'wanted to ask'. If confidence is Low, avoid "
            "strong claims and keep the CTA focused on confirming relevance. "
            "Human-sounding style requirements: write like a real person sending a thoughtful note after reviewing the "
            "organization's website. Be friendly, professional, restrained, and specific. Keep the body ideally 80-150 "
            "words. Use short paragraphs, natural transitions, and simple language. Make the email unlikely to be perceived "
            "as AI-written by avoiding formulaic structure, generic compliments, inflated praise, sales hype, excessive "
            "adjectives, and overly polished marketing language. Do not use phrases such as 'I hope this email finds you "
            "well', 'I was impressed by', 'game-changing', 'revolutionize', 'unlock', 'seamless', 'cutting-edge', 'leverage', "
            "'synergy', 'tailored solutions', 'at your earliest convenience', or 'I wanted to reach out because'. Do not "
            "mention AI, scraped data, fit score, CRM, prospect card, target criteria, or internal analysis. Do not use fake "
            "familiarity, false urgency, manipulative language, unsupported numbers, testimonials, case studies, or promised "
            "outcomes. Do not ask multiple questions in the CTA. "
            "Punctuation and formatting requirements: do not use dash punctuation, em dashes, en dashes, hyphen bullets, "
            "or hyphenated compounds when a simple alternative exists. Format body as a real email with paragraph breaks "
            "using newline characters. Do not return the body as one long line. Do not use bullet lists. "
            "Email construction: subject should be short, natural, and not clickbait; avoid title case unless it reads "
            "naturally. Opening should mention one specific evidence-backed observation about the organization. Relevance "
            "bridge should connect outreach_goal to the organization's apparent work, audience, services, or mission without "
            "overclaiming. Offer should state the suggested value in plain language. CTA should be a simple, low-friction, "
            "specific question such as asking whether it makes sense to share a short idea, send more details, or connect "
            "with the right person. Include a signature only if sender information is provided; never invent sender name, "
            "company, phone number, or title. "
            "Personalization rules: mention the company or organization name only if clearly provided. Mention a service, "
            "program, audience, mission, or activity only if clearly supported by evidence. Mention a pain point only if "
            "supported; otherwise frame the offer around possible relevance rather than a confirmed problem. If "
            "recommended_contact_type is available, write the email so it is appropriate for that role type, but do not "
            "address a specific person unless the name is provided. Follow regenerate_instruction only when it does not "
            "conflict with evidence rules, JSON requirements, or professional tone. "
            "Field requirements: subject is a short natural email subject. body is the complete email body, ready for human "
            "review, with paragraph breaks. cta is the one core call to action from the email. If evidence is insufficient, "
            "prioritize accuracy over persuasion and make the uncertainty clear in natural language."
        )
        try:
            data = self._chat_json(system_prompt, payload, stage="outreach_draft")
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[prospect-ai] outreach draft failed: %s", str(exc))
            return self._fallback_outreach_draft(card, state)
        fallback_subject = _format_email_text(f"Potential collaboration with {card.company}")
        return {
            "subject": _format_email_text(data.get("subject")) or fallback_subject,
            "body": _format_email_body(data.get("body")),
            "cta": _format_email_text(data.get("cta")) or "Would you be open to a quick reply if this is relevant?",
        }

    def _chat_json(self, system_prompt: str, payload: dict[str, Any], *, stage: str) -> dict[str, Any]:
        if self.client is None:
            raise ProspectAIError("OPENAI_API_KEY is missing.")
        response = create_chat_completion_with_fallback(
            self.client.chat.completions.create,
            model=self.settings.ai_model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            logger=self.logger,
            log_label="prospect-ai",
            timeout_seconds=self.settings.openai_request_timeout_seconds,
            retry_budget=self.settings.ai_retry_budget,
            backoff_base_seconds=self.settings.ai_backoff_base_seconds,
        )
        content = response.choices[0].message.content or "{}"
        data = _safe_json_loads(content)
        if not data:
            raise ProspectAIError(f"AI returned invalid JSON for {stage}.")
        return data

    @staticmethod
    def _as_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            return value.strip().lower() in {"true", "yes", "1", "enough"}
        return False

    @staticmethod
    def _as_float(value: Any) -> float:
        try:
            return max(0.0, min(float(value), 1.0))
        except (TypeError, ValueError):
            return 0.0

    def _pages_for_site(self, state: ProspectAnalysisState, website: str) -> list[ScrapedPage]:
        pages = [page for page in state.scraped_pages if _same_site(page.final_url or page.url, website)]
        return pages or list(state.scraped_pages)

    def _pages_payload(self, state: ProspectAnalysisState, *, website: str, text_limit: int) -> list[dict[str, Any]]:
        payload: list[dict[str, Any]] = []
        for page in self._pages_for_site(state, website):
            payload.append(
                {
                    "url": page.final_url or page.url,
                    "title": page.title,
                    "text_excerpt": page.text_excerpt(text_limit),
                    "metadata": page.metadata,
                    "links": page.links[:30],
                    "status": page.status,
                    "strategy": page.strategy,
                }
            )
        return payload

    def _fallback_information_decision(
        self,
        state: ProspectAnalysisState,
        *,
        website: str,
        candidate_links: list[dict[str, Any]],
    ) -> InformationDecision:
        pages = self._pages_for_site(state, website)
        text_length = sum(len(page.text or "") for page in pages)
        enough = text_length >= 1600 or len(pages) >= self.settings.max_pages_to_scrape
        selected = [
            str(item.get("url") or "").strip()
            for item in sorted(candidate_links, key=lambda value: float(value.get("structural_score", 0)), reverse=True)
            if str(item.get("url") or "").strip()
        ][: max(0, self.settings.max_pages_to_scrape - len(pages))]
        return InformationDecision(
            enough_information=enough,
            selected_extra_urls=selected,
            reason="Local fallback used because AI analysis was unavailable.",
            missing_information=["AI information sufficiency check did not run."],
            confidence=0.25,
        )

    def _fallback_card(self, state: ProspectAnalysisState, *, website: str, reason: str) -> ProspectCard:
        pages = self._pages_for_site(state, website)
        combined_text = "\n".join(page.text for page in pages)
        first_page = pages[0] if pages else None
        title = first_page.title if first_page else ""
        company = self._company_from_title_or_url(title, website)
        contact_email = extract_contact_email(combined_text)
        contact_url = self._fallback_contact_url(pages)
        criteria_terms = {
            token
            for token in re.findall(r"[A-Za-z][A-Za-z0-9-]{3,}", state.target_criteria.lower())
            if len(token) > 3
        }
        matched_terms = [token for token in criteria_terms if token in combined_text.lower()]
        score = int(min(65, 25 + (len(matched_terms) * 8) + min(len(combined_text) // 700, 20)))
        fit_status = self.settings.fit_status_for_score(score)
        return ProspectCard(
            company=company,
            website=website,
            category="Unknown",
            fit_score=score,
            fit_status=fit_status,
            reason=f"AI card generation was unavailable: {reason}",
            pain_point="Unknown",
            suggested_offer=state.outreach_goal or "Unknown",
            recommended_contact_type="Human review",
            confidence="Low - local fallback",
            next_step="Review manually before saving or sending outreach.",
            contact_email=contact_email,
            contact_url=contact_url,
            ai_summary=(combined_text[:280] + "...") if len(combined_text) > 280 else combined_text,
            why_relevant="Local fallback found limited evidence; AI review is recommended.",
            ai_reasoning_summary="Generated without AI due to missing or failed AI analysis.",
            missing_information=["AI analysis did not complete.", "Manual validation is required."],
            risk_uncertainty_flags=["Low-confidence fallback card."],
            pages_scraped=[page.final_url for page in pages],
            source_session_id=state.session_id,
        )

    @staticmethod
    def _company_from_title_or_url(title: str, url: str) -> str:
        cleaned_title = _clean_text(title)
        if cleaned_title:
            return re.split(r"\s[-|:]\s", cleaned_title)[0][:80].strip() or cleaned_title[:80]
        host = _host(url).removeprefix("www.")
        return host or "Unknown"

    @staticmethod
    def _fallback_contact_url(pages: list[ScrapedPage]) -> str:
        for page in pages:
            for link in page.links:
                url = str(link.get("url") or "")
                text = str(link.get("anchor_text") or "").lower()
                if "linkedin.com" in url.lower() or "contact" in text or "contact" in url.lower():
                    return url
        return pages[0].final_url if pages else ""

    @staticmethod
    def _fallback_outreach_draft(card: ProspectCard, state: ProspectAnalysisState) -> dict[str, str]:
        subject = f"Potential collaboration with {card.company}"
        body = (
            f"Hi {card.company} team,\n\n"
            f"I reviewed {card.website} and noticed a possible fit around {card.outreach_angle or state.outreach_goal}.\n\n"
            f"If this is relevant, I would be happy to share a short idea tailored to your current priorities.\n\n"
            "Best,\n"
        )
        return {
            "subject": subject,
            "body": _format_email_body(body),
            "cta": "Would a brief reply be the best next step?",
        }
