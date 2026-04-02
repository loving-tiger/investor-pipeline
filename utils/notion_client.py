"""Notion read/write helpers for the deal flow pipeline."""

import os
import re
from datetime import date
from notion_client import Client

_notion = None

_APPEND_BATCH_SIZE = 100  # Notion API hard limit per children.append call


def _client() -> Client:
    global _notion
    if _notion is None:
        _notion = Client(auth=os.environ["NOTION_TOKEN"])
    return _notion


# ---------------------------------------------------------------------------
# URL / ID resolution
# ---------------------------------------------------------------------------

def resolve_page_id(id_or_url: str) -> str:
    """Accept a bare page ID or a full Notion URL and return a UUID page ID."""
    # Strip query string and fragment
    clean = id_or_url.split("?")[0].split("#")[0].rstrip("/")
    # Last path segment contains the ID as the trailing 32 hex chars
    segment = clean.split("/")[-1]
    match = re.search(r"([0-9a-f]{32})$", segment.replace("-", ""))
    if match:
        raw = match.group(1)
        return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"
    # Already a valid bare ID (with or without dashes)
    return id_or_url


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def get_deal(page_id: str) -> dict:
    """Fetch all deal fields from a Notion page and return a flat dict."""
    page = _client().pages.retrieve(page_id=page_id)
    props = page["properties"]

    def text(name: str, kind: str = "rich_text") -> str:
        try:
            items = props[name][kind]
            return "".join(item["plain_text"] for item in items) if items else ""
        except (KeyError, TypeError):
            return ""

    def select(name: str) -> str:
        try:
            val = props[name]["select"]
            return val["name"] if val else ""
        except (KeyError, TypeError):
            return ""

    def url(name: str) -> str:
        try:
            return props[name]["url"] or ""
        except (KeyError, TypeError):
            return ""

    return {
        "page_id": page_id,
        "company_name":     text("Company Name", "title"),
        "founder_name":     text("Founder Name"),
        "founder_linkedin": url("Founder LinkedIn"),
        "company_website":  url("Company Website"),
        "company_overview": text("Company Overview"),
        "deck_link":        url("Deck Link"),
        "sector":           select("Sector"),
        "stage":            select("Stage"),
        "raise_amount":     text("Raise Amount"),
        "one_liner":        text("One-liner"),
        "status":           select("Status"),
        "investment_memo":  text("Investment Memo"),
    }


# ---------------------------------------------------------------------------
# Write — properties
# ---------------------------------------------------------------------------

def update_status(page_id: str, status: str) -> None:
    """Update the Status select field."""
    _client().pages.update(
        page_id=page_id,
        properties={"Status": {"select": {"name": status}}},
    )


def update_analysis_date(page_id: str) -> None:
    """Set Analysis Date to today (ISO format)."""
    _client().pages.update(
        page_id=page_id,
        properties={"Analysis Date": {"date": {"start": date.today().isoformat()}}},
    )


def write_memo(page_id: str, memo_text: str) -> None:
    """Write investment memo to the property field and replace page body content."""
    _client().pages.update(
        page_id=page_id,
        properties={"Investment Memo": {"rich_text": _chunk(memo_text)}},
    )
    _replace_page_body(page_id, "Investment Memo", memo_text)


def write_email(page_id: str, email_text: str) -> None:
    """Write draft email to the property field and append a section to the page body."""
    _client().pages.update(
        page_id=page_id,
        properties={"Draft Email": {"rich_text": _chunk(email_text)}},
    )
    _append_page_section(page_id, "Draft Email", email_text)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _chunk(text: str, size: int = 2000) -> list:
    """Split text into Notion rich_text objects (2000-char limit each)."""
    chunks = []
    while text:
        chunks.append({"type": "text", "text": {"content": text[:size]}})
        text = text[size:]
    return chunks


def _replace_page_body(page_id: str, heading: str, body: str) -> None:
    """
    Clear all existing blocks on the page then write heading + body paragraphs.

    Called by write_memo so that re-running analyze_deal.py on the same page
    always produces a clean, up-to-date memo rather than duplicating content.
    """
    _clear_page_blocks(page_id)
    _append_page_section(page_id, heading, body)


def _clear_page_blocks(page_id: str) -> None:
    """Delete (archive) all top-level blocks on a page."""
    cursor = None
    while True:
        kwargs = {"block_id": page_id, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        response = _client().blocks.children.list(**kwargs)
        for block in response.get("results", []):
            _client().blocks.delete(block_id=block["id"])
        if not response.get("has_more"):
            break
        cursor = response.get("next_cursor")


def _append_page_section(page_id: str, heading: str, body: str) -> None:
    """
    Append a heading_2 block followed by paragraph blocks to the page body.

    Batches writes in groups of 100 to stay within Notion's API limit.
    """
    blocks: list[dict] = [
        {
            "type": "heading_2",
            "heading_2": {
                "rich_text": [{"type": "text", "text": {"content": heading}}]
            },
        }
    ]

    for para in body.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        for chunk in [para[i: i + 2000] for i in range(0, len(para), 2000)]:
            blocks.append({
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": chunk}}]
                },
            })

    # Write in batches of 100 (Notion API hard limit per call)
    for i in range(0, len(blocks), _APPEND_BATCH_SIZE):
        _client().blocks.children.append(
            block_id=page_id,
            children=blocks[i: i + _APPEND_BATCH_SIZE],
        )
