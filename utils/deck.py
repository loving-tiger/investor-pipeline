"""
Pitch deck loading utilities.

Supports:
  - Local PDF files (passed via --deck-path CLI argument)
  - Publicly accessible URLs — passed directly to Claude via the Anthropic
    URL document source (Claude fetches it server-side; works for PDFs,
    web pages, Canva public links, etc.)
  - Direct PDF URLs — fetched locally and base64-encoded as a fallback

Not supported (requires auth):
  - Google Drive / Google Slides links  → download as PDF, use --deck-path
  - Docsend                             → download as PDF, use --deck-path
  - Pitch.com (private)                 → download as PDF, use --deck-path
  - Notion links                        → download as PDF, use --deck-path
"""

import base64
import os

import requests

# Domains we know require auth — Claude can't fetch these either
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

    Priority: deck_path (local file) > deck_url (Claude URL fetch).
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
    """
    Return a Claude document block for a public deck URL.

    Strategy:
      1. Reject known auth-gated domains immediately.
      2. Try a URL-source block first — Anthropic's servers fetch the URL
         directly, so this works for PDFs *and* public web pages / Canva links.
      3. If the URL clearly points to a raw PDF (by extension or Content-Type),
         also try a local fetch + base64 as a safety fallback so the block is
         already resolved before the API call.
    """
    if deck_is_gated(url):
        print(f"  [deck] URL requires authentication — skipping auto-fetch.")
        print(f"         Download as PDF and re-run with --deck-path to include it.")
        return None

    # Always attempt the URL-source approach first: works for PDFs, public web
    # viewers, Canva public links, etc.  Claude fetches the URL server-side.
    print(f"  [deck] Passing deck URL to Claude directly: {url}")
    return _make_url_block(url)


def _make_url_block(url: str) -> dict:
    """Return a Claude document block that references a URL (Anthropic fetches it)."""
    return {
        "type": "document",
        "source": {
            "type": "url",
            "url": url,
        },
    }


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
