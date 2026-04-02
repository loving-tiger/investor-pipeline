#!/usr/bin/env python3
"""
webhook.py — FastAPI webhook receiver for Typeform founder submissions.

Receives form_response events from Typeform, validates the HMAC-SHA256
signature, and creates a new row in the Notion Deal Flow database with
Status set to "Pending Review".

Run locally:
    uvicorn webhook:app --reload --port 8000

Deploy to Render:
    render.yaml in the project root handles all configuration.
    Set NOTION_TOKEN, NOTION_DB_ID, TYPEFORM_WEBHOOK_SECRET in the
    Render dashboard environment variables.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from notion_client import Client

from utils.typeform_parser import parse_submission

app = FastAPI(title="Deal Flow Webhook", version="1.0.0")

_notion: Client | None = None


def _notion_client() -> Client:
    global _notion
    if _notion is None:
        _notion = Client(auth=os.environ["NOTION_TOKEN"])
    return _notion


# ---------------------------------------------------------------------------
# Signature validation
# ---------------------------------------------------------------------------

def _verify_signature(body: bytes, signature_header: str) -> bool:
    """
    Validate the Typeform webhook signature.

    Typeform computes HMAC-SHA256(secret, raw_body) and sends the result
    in the Typeform-Signature header as: sha256=<hex_digest>
    """
    secret = os.environ.get("TYPEFORM_WEBHOOK_SECRET", "")
    if not secret:
        # No secret configured — skip validation (local dev only)
        return True
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256)
    expected = "sha256=" + base64.b64encode(mac.digest()).decode("utf-8")
    return hmac.compare_digest(expected, signature_header)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    """Keep-alive / health check endpoint."""
    return {"status": "ok"}


@app.post("/webhook/typeform")
async def typeform_webhook(request: Request):
    """
    Handle an inbound Typeform form_response event.

    On success: creates a Notion page and returns {"status": "created", ...}
    On ignored event type: returns {"status": "ignored"}
    On bad signature: 401
    On Notion error: 500
    """
    body = await request.body()
    sig = request.headers.get("Typeform-Signature", "")

    if not _verify_signature(body, sig):
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")

    payload = await request.json()
    event_type = payload.get("event_type")

    if event_type != "form_response":
        return JSONResponse({"status": "ignored", "event_type": event_type})

    fields = parse_submission(payload)
    company = fields.get("company_name") or "Untitled Submission"

    try:
        page = _notion_client().pages.create(
            parent={"database_id": os.environ["NOTION_DB_ID"]},
            properties=_build_notion_properties(fields),
        )
    except Exception as exc:
        print(f"ERROR creating Notion page for '{company}': {exc}")
        raise HTTPException(status_code=500, detail=f"Notion error: {exc}")

    page_id = page["id"]
    print(f"[webhook] Created Notion page {page_id} — {company}")

    return JSONResponse({
        "status": "created",
        "page_id": page_id,
        "company": company,
    })


# ---------------------------------------------------------------------------
# Notion property builders
# ---------------------------------------------------------------------------

def _build_notion_properties(fields: dict) -> dict:
    """Convert a flat fields dict into a Notion properties payload."""
    props: dict = {
        "Company Name": {
            "title": [{"text": {"content": fields.get("company_name") or "Untitled"}}]
        },
        "Status": {"select": {"name": "Pending Review"}},
    }

    _add_text(props,  "Founder Name",     fields.get("founder_name"))
    _add_text(props,  "Raise Amount",     fields.get("raise_amount"))
    _add_text(props,  "One-liner",        fields.get("one_liner"))
    _add_text(props,  "Company Overview", fields.get("company_overview"))
    _add_email(props, "Founder Email",    fields.get("founder_email"))
    _add_url(props,   "Founder LinkedIn", fields.get("founder_linkedin"))
    _add_url(props,   "Company Website",  fields.get("company_website"))
    _add_url(props,   "Deck Link",        fields.get("deck_link"))
    _add_select(props, "Sector",          fields.get("sector"))
    _add_select(props, "Stage",           fields.get("stage"))

    return props


def _add_text(props: dict, key: str, value: str | None) -> None:
    if value:
        props[key] = {"rich_text": [{"text": {"content": value}}]}


def _add_email(props: dict, key: str, value: str | None) -> None:
    if value:
        props[key] = {"email": value}


def _add_url(props: dict, key: str, value: str | None) -> None:
    if value:
        props[key] = {"url": value}


def _add_select(props: dict, key: str, value: str | None) -> None:
    if value:
        props[key] = {"select": {"name": value}}
