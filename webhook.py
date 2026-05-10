#!/usr/bin/env python3
"""
webhook.py — FastAPI webhook receiver for Typeform + Tally founder submissions.

Receives form_response events from Typeform and Tally, validates HMAC-SHA256
signatures, and creates a new row in the Notion Deal Flow database with
Status set to "Pending Review".

Run locally:
    uvicorn webhook:app --reload --port 8000

Deploy to Render:
    render.yaml in the project root handles all configuration.
    Set NOTION_TOKEN, NOTION_DB_ID, TYPEFORM_WEBHOOK_SECRET,
    and TALLY_WEBHOOK_SECRET in the Render dashboard environment variables.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import threading
import time
from typing import Any

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from notion_client import Client
from pydantic import BaseModel

import utils.tally_parser as tally_parser
import utils.typeform_parser as typeform_parser

app = FastAPI(title="Deal Flow Webhook", version="1.0.0")

# ---------------------------------------------------------------------------
# In-memory job state (single-process, WEB_CONCURRENCY=1)
# ---------------------------------------------------------------------------

_job_states: dict[str, dict[str, Any]] = {}


def _set_job_state(page_id: str, **kwargs: Any) -> None:
    if page_id not in _job_states:
        _job_states[page_id] = {"status": "idle", "step": "", "search_count": 0, "started_at": time.time()}
    _job_states[page_id].update(kwargs)


_notion: Client | None = None


def _notion_client() -> Client:
    global _notion
    if _notion is None:
        _notion = Client(auth=os.environ["NOTION_TOKEN"])
    return _notion


# ---------------------------------------------------------------------------
# Signature validation
# ---------------------------------------------------------------------------

def _verify_signature(body: bytes, signature_header: str, secret_env_var: str) -> bool:
    """
    Validate an HMAC-SHA256 webhook signature (used by both Typeform and Tally).

    Both platforms compute base64(HMAC-SHA256(secret, raw_body)) and send
    the result as: sha256=<base64_digest>
    """
    secret = os.environ.get(secret_env_var, "")
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

    if not _verify_signature(body, sig, "TYPEFORM_WEBHOOK_SECRET"):
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")

    payload = await request.json()
    event_type = payload.get("event_type")

    if event_type != "form_response":
        return JSONResponse({"status": "ignored", "event_type": event_type})

    fields = typeform_parser.parse_submission(payload)
    return await _create_notion_page(fields, source="typeform")


@app.post("/webhook/tally")
async def tally_webhook(request: Request):
    """
    Handle an inbound Tally form_response event.

    On success: creates a Notion page and returns {"status": "created", ...}
    On bad signature: 401
    On Notion error: 500
    """
    body = await request.body()
    sig = request.headers.get("Tally-Signature", "")

    if not _verify_signature(body, sig, "TALLY_WEBHOOK_SECRET"):
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")

    payload = await request.json()
    event_type = payload.get("eventType", "")

    if event_type != "FORM_RESPONSE":
        return JSONResponse({"status": "ignored", "event_type": event_type})

    fields = tally_parser.parse_submission(payload)
    return await _create_notion_page(fields, source="tally")


# ---------------------------------------------------------------------------
# Research memo generation (async, admin-triggered)
# ---------------------------------------------------------------------------

class ResearchMemoRequest(BaseModel):
    notion_page_id: str


def _log(msg: str) -> None:
    print(msg, flush=True)


def _run_research_memo_background(page_id: str) -> None:
    """Run the full research memo pipeline in a background thread."""
    import sys
    sys.path.insert(0, os.path.dirname(__file__))

    try:
        from utils.notion_client import get_deal, update_status, write_research_memo
        from utils.research_agent import run_research_agent
        from utils.prompts import RESEARCH_SYSTEM_PROMPT, RESEARCH_PROMPT
        import anthropic

        _set_job_state(page_id, status="running", step="Fetching deal from Notion…", search_count=0, started_at=time.time())
        _log(f"[research-memo] Starting pipeline for page {page_id}")
        deal = get_deal(page_id)
        company = deal.get("company_name", page_id)
        _log(f"[research-memo] Company: {company}")

        _set_job_state(page_id, step="Running research…", search_count=0)
        update_status(page_id, "In Analysis")

        def on_search(count: int, tool_name: str, query: str) -> None:
            _set_job_state(page_id, step=f"Researching — search {count} of ~20…", search_count=count)

        research = run_research_agent(deal, on_search=on_search)
        _log(f"[research-memo] Research complete — {len(research):,} chars")

        _set_job_state(page_id, step="Generating memo with Claude…")
        _log("[research-memo] Calling Claude...")
        prompt_text = RESEARCH_PROMPT.format(
            company_name=deal["company_name"],
            founder_name=deal.get("founder_name", ""),
            founder_linkedin=deal.get("founder_linkedin", ""),
            company_website=deal.get("company_website", ""),
            stage=deal.get("stage", ""),
            raise_amount=deal.get("raise_amount", ""),
            sector=deal.get("sector", ""),
            one_liner=deal.get("one_liner", ""),
            company_overview=(deal.get("company_overview") or "").strip() or "(not provided)",
            research=research,
        )
        _log(f"[research-memo] Prompt ready — {len(prompt_text):,} chars")

        model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        response = client.messages.create(
            model=model,
            max_tokens=8192,
            system=RESEARCH_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt_text}],
        )
        memo = response.content[0].text
        _log(f"[research-memo] Memo generated — {len(memo):,} chars")

        _set_job_state(page_id, step="Writing to Notion…")
        _log("[research-memo] Writing to Notion...")
        write_research_memo(page_id, memo)
        update_status(page_id, "Pending Review")
        _set_job_state(page_id, status="done", step="Done!")
        _log(f"[research-memo] Done — {company}")

    except Exception as exc:
        import traceback
        _set_job_state(page_id, status="error", step=f"Error: {exc}")
        _log(f"[research-memo] ERROR for page {page_id}: {exc}")
        _log(traceback.format_exc())


@app.post("/generate/research-memo")
async def generate_research_memo(payload: ResearchMemoRequest, request: Request):
    """
    Admin-triggered endpoint: kick off the research memo pipeline in the background.

    Protected by PIPELINE_SECRET header. Returns immediately; pipeline runs async.
    """
    secret = os.environ.get("PIPELINE_SECRET", "")
    provided = request.headers.get("X-Pipeline-Secret", "")
    if secret and not hmac.compare_digest(secret, provided):
        raise HTTPException(status_code=401, detail="Invalid pipeline secret.")

    page_id = payload.notion_page_id
    if not page_id:
        raise HTTPException(status_code=400, detail="notion_page_id is required.")

    thread = threading.Thread(
        target=_run_research_memo_background,
        args=(page_id,),
        daemon=False,
    )
    thread.start()

    return JSONResponse({"status": "started", "notion_page_id": page_id})


@app.get("/generate/research-memo/status/{page_id}")
async def research_memo_status(page_id: str, request: Request):
    """Return the current job state for a given Notion page ID."""
    secret = os.environ.get("PIPELINE_SECRET", "")
    provided = request.headers.get("X-Pipeline-Secret", "")
    if secret and not hmac.compare_digest(secret, provided):
        raise HTTPException(status_code=401, detail="Invalid pipeline secret.")

    state = _job_states.get(page_id, {"status": "idle", "step": "", "search_count": 0})
    return JSONResponse(state)


# ---------------------------------------------------------------------------
# Shared page creation
# ---------------------------------------------------------------------------

async def _create_notion_page(fields: dict, source: str) -> JSONResponse:
    """Create a Notion page from a parsed fields dict."""
    company = fields.get("company_name") or "Untitled Submission"

    try:
        page = _notion_client().pages.create(
            parent={"database_id": os.environ["NOTION_DB_ID"]},
            properties=_build_notion_properties(fields),
        )
    except Exception as exc:
        print(f"ERROR creating Notion page for '{company}' (source={source}): {exc}")
        raise HTTPException(status_code=500, detail=f"Notion error: {exc}")

    page_id = page["id"]
    print(f"[webhook:{source}] Created Notion page {page_id} — {company}")

    return JSONResponse({
        "status": "created",
        "page_id": page_id,
        "company": company,
        "source": source,
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
