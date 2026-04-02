"""
Parse a Typeform webhook payload into a flat dict of deal fields.

Field refs defined here must match the refs set when creating the form
via setup_typeform.py. The ref is the stable identifier for each field —
it doesn't change if the question text is edited.
"""

from __future__ import annotations

# Canonical field refs — must match setup_typeform.py
FIELD_REFS = [
    "company_name",
    "one_liner",
    "founder_name",
    "stage",
    "sector",
    "raise_amount",
    "company_website",
    "founder_linkedin",
    "deck_link",
    "company_overview",
]


def parse_submission(payload: dict) -> dict:
    """
    Extract a flat field dict from a Typeform form_response payload.

    Returns all 10 deal fields keyed by their internal names.
    Missing or unanswered fields default to empty string.
    """
    answers = payload.get("form_response", {}).get("answers", [])

    by_ref: dict[str, str] = {}
    for answer in answers:
        ref = answer.get("field", {}).get("ref", "")
        value = _extract_value(answer)
        if ref and value:
            by_ref[ref] = value

    return {field: by_ref.get(field, "") for field in FIELD_REFS}


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

def _extract_value(answer: dict) -> str:
    """Pull a plain string out of any Typeform answer object."""
    answer_type = answer.get("type", "")

    if answer_type in ("text",):
        return answer.get("text", "")
    if answer_type == "choice":
        return answer.get("choice", {}).get("label", "")
    if answer_type == "url":
        return answer.get("url", "")
    if answer_type == "email":
        return answer.get("email", "")
    if answer_type == "number":
        val = answer.get("number")
        return str(val) if val is not None else ""
    if answer_type == "boolean":
        return str(answer.get("boolean", ""))

    # Fallback: try common value keys
    for key in ("text", "url", "email", "number"):
        if key in answer:
            return str(answer[key])
    return ""
