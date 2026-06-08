from __future__ import annotations

_EMPTY_MARKERS = {"", "unknown", "not specified", "n/a", "none"}
_PLACEHOLDER_TITLE_MARKERS = {
    "job",
    "vacancy",
    "position",
    "opening",
    "project",
    "brief",
    "contract",
    "role",
}
_NEGATIVE_NOTE_MARKERS = {
    "list page",
    "search page",
    "multi-job",
    "multi-opportunity",
    "feed",
    "not a single job",
    "not a single opportunity",
    "blocked",
    "cloudflare",
    "captcha",
    "cookie policy",
    "listing page",
}


def display_value(value: str, *, fallback: str = "Unknown") -> str:
    text = str(value or "").strip()
    if text.lower() in _EMPTY_MARKERS:
        return fallback
    return text or fallback


def stabilize_job_confidence(
    confidence: float,
    *,
    is_job_post: bool,
    title: str,
    description: str,
    location: str,
    salary: str,
    company: str,
    posted_at_utc: str,
    notes: str,
) -> float:
    normalized = max(0.0, min(float(confidence or 0.0), 1.0))
    if not is_job_post or normalized <= 0.0:
        return 0.0

    evidence_score = _evidence_score(
        title=title,
        description=description,
        location=location,
        salary=salary,
        company=company,
        posted_at_utc=posted_at_utc,
        notes=notes,
    )
    calibrated = normalized * 0.82 + evidence_score * 0.18
    if evidence_score < 0.3:
        calibrated = min(calibrated, 0.72)
    if _has_negative_notes(notes):
        calibrated = min(calibrated, max(normalized * 0.7, 0.2))
    return round(max(0.0, min(calibrated, 1.0)), 3)


def _evidence_score(
    *,
    title: str,
    description: str,
    location: str,
    salary: str,
    company: str,
    posted_at_utc: str,
    notes: str,
) -> float:
    score = 0.0
    cleaned_title = display_value(title)
    cleaned_description = display_value(description)
    cleaned_location = display_value(location)
    cleaned_payment = display_value(salary, fallback="")
    cleaned_counterparty = display_value(company, fallback="")
    lowered_title = cleaned_title.lower()
    description_word_count = len(str(description or "").split())
    notes_word_count = len(str(notes or "").split())

    if cleaned_title != "Unknown" and lowered_title not in _PLACEHOLDER_TITLE_MARKERS:
        score += 0.26
    elif cleaned_title != "Unknown":
        score += 0.12

    if cleaned_description != "Unknown":
        if description_word_count >= 80:
            score += 0.24
        elif description_word_count >= 30:
            score += 0.19
        elif description_word_count >= 12:
            score += 0.12
        else:
            score += 0.06

    if cleaned_location != "Unknown":
        score += 0.11
    if cleaned_counterparty:
        score += 0.06
    if cleaned_payment:
        score += 0.08
    if str(posted_at_utc or "").strip():
        score += 0.08
    if notes_word_count >= 6 and not _has_negative_notes(notes):
        score += 0.04

    if cleaned_title == "Unknown":
        score -= 0.1
    if cleaned_description == "Unknown":
        score -= 0.12
    if _has_negative_notes(notes):
        score -= 0.18
    return max(0.0, min(score, 1.0))


def _has_negative_notes(notes: str) -> bool:
    lowered = str(notes or "").strip().lower()
    return any(marker in lowered for marker in _NEGATIVE_NOTE_MARKERS)
