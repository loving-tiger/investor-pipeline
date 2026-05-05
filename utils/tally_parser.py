"""
Parse a Tally webhook payload into a flat dict of deal fields.

Tally identifies fields by auto-generated keys and human-readable labels.
We match on lowercased label substrings since Tally doesn't support custom
refs on the free plan.
"""

from __future__ import annotations

# Maps lowercased label substrings → internal field names.
# Order matters: more specific matches first.
_LABEL_MAP: list[tuple[str, str]] = [
    ("company name",        "company_name"),
    ("name of your company","company_name"),
    ("one-liner",           "one_liner"),
    ("one liner",           "one_liner"),
    ("what does your company do", "one_liner"),
    ("your name",           "founder_name"),
    ("founder name",        "founder_name"),
    ("your full name",      "founder_name"),
    ("stage",               "stage"),
    ("sector",              "sector"),
    ("how much are you raising", "raise_amount"),
    ("raise amount",        "raise_amount"),
    ("raising",             "raise_amount"),
    ("company website",     "company_website"),
    ("website",             "company_website"),
    ("linkedin",            "founder_linkedin"),
    ("email address",       "founder_email"),
    ("your email",          "founder_email"),
    ("pitch deck",          "deck_link"),
    ("deck link",           "deck_link"),
    ("deck",                "deck_link"),
    ("tell us more",        "company_overview"),
    ("company overview",    "company_overview"),
    ("traction",            "company_overview"),
]

# All internal field names we want to return
_ALL_FIELDS = [
    "company_name",
    "one_liner",
    "founder_name",
    "stage",
    "sector",
    "raise_amount",
    "company_website",
    "founder_linkedin",
    "founder_email",
    "deck_link",
    "company_overview",
]


def parse_submission(payload: dict) -> dict:
    """
    Extract a flat field dict from a Tally form_response payload.

    Returns all deal fields keyed by internal name.
    Missing or unanswered fields default to empty string.
    """
    fields = payload.get("data", {}).get("fields", [])

    by_internal: dict[str, str] = {}
    for field in fields:
        label = field.get("label", "").lower()
        value = _extract_value(field)
        if not value:
            continue
        internal = _match_label(label)
        if internal and internal not in by_internal:
            by_internal[internal] = value

    return {field: by_internal.get(field, "") for field in _ALL_FIELDS}


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

def _match_label(label: str) -> str | None:
    """Return the internal field name for a Tally field label, or None."""
    for substring, internal in _LABEL_MAP:
        if substring in label:
            return internal
    return None


def _extract_value(field: dict) -> str:
    """Pull a plain string out of any Tally field object."""
    value = field.get("value")
    if value is None:
        return ""

    field_type = field.get("type", "")

    # Multiple choice / dropdown — value is a list of option dicts or strings
    if isinstance(value, list):
        labels = []
        for item in value:
            if isinstance(item, dict):
                labels.append(item.get("text") or item.get("label") or "")
            elif isinstance(item, str):
                labels.append(item)
        return ", ".join(filter(None, labels))

    # Checkbox / boolean
    if isinstance(value, bool):
        return str(value)

    return str(value).strip()
