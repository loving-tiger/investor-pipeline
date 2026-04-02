"""
Pitch deck loading utilities.

Supports:
  - Local PDF files (passed via --deck-path CLI argument)
  - Publicly accessible PDF URLs (fetched automatically from the Deck Link field)

Not supported (requires auth):
  - Google Drive / Google Slides links
  - Docsend
  - Pitch.com
  - Notion links
  → Download the PDF manually and pass via --deck-path instead.
"""

import base64
import os

import requests

# Domains we know require auth — don't bother trying to fetch
_GATED_DOMAINS = (
    "docs.google.com",
    "drive.google.com",
    "docsend.com",
    "pitch.com",
    "notion.so",
    "notion.site",
)

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; deal-flow-pipeline/1.0)"}


def load_deck(deck_path: str | None = None, deck_url: str | None = None) -> dict | None:
    """
    Return a Claude `document` content block for the pitch deck, or None if
    the deck can't be loaded.

    Priority: deck_path (local file) > deck_url (remote fetch).
    """
    if deck_path:
        return _load_local(deck_path)
    if deck_url:
        return _load_url(deck_url)
    return None


def deck_is_gated(url: str) -> bool:
    """Return True if the URL is known to require authentication."""
    return any(domain in url for domain in _GATED_DOMAINS)


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

def _load_local(path: str) -> dict | None:
    """Read a local PDF and return a Claude document block."""
    # Expand ~ so --deck-path ~/Downloads/deck.pdf works correctly
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        print(f"  [deck] File not found: {path}")
        return None
    ext = os.path.splitext(path)[1].lower()
    if ext != ".pdf":
        print(f"  [deck] Only PDF files are supported for direct analysis (got {ext}).")
        print("         Convert the deck to PDF first, then re-run with --deck-path.")
        return None
    with open(path, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode("utf-8")
    print(f"  [deck] Loaded local PDF: {os.path.basename(path)}")
    return _make_block(data)


def _load_url(url: str) -> dict | None:
    """Fetch a PDF from a public URL and return a Claude document block."""
    if deck_is_gated(url):
        print(f"  [deck] URL appears to require authentication ({url}).")
        print("         Download the deck as PDF and pass it via --deck-path.")
        return None
    try:
        print("  [deck] Fetching deck from URL...")
        resp = requests.get(url, headers=_HEADERS, timeout=20, allow_redirects=True)
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "")
        if "pdf" not in content_type.lower() and not url.lower().endswith(".pdf"):
            print(f"  [deck] URL did not return a PDF (Content-Type: {content_type}).")
            print("         Download the deck as PDF and pass it via --deck-path.")
            return None
        data = base64.standard_b64encode(resp.content).decode("utf-8")
        size_kb = len(resp.content) // 1024
        print(f"  [deck] Fetched PDF from URL ({size_kb} KB)")
        return _make_block(data)
    except requests.RequestException as exc:
        print(f"  [deck] Could not fetch deck: {exc}")
        print("         Download the deck as PDF and pass it via --deck-path.")
        return None


def _make_block(b64_data: str) -> dict:
    """Wrap base64 PDF data in a Claude document content block."""
    return {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": b64_data,
        },
    }
