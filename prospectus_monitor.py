#!/usr/bin/env python3
"""ETF Prospectus Monitor — daily EDGAR check, weekly Slack digest."""

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic
import requests

STATE_FILE = Path("prospectus_state.json")
SEC_HEADERS = {"User-Agent": "ETFProspectusMonitor mingj93@gmail.com"}
FORM_TYPES = ["N-1A", "485APOS", "485BPOS"]
MAX_FILINGS_PER_RUN = 75  # cap to avoid GitHub Actions timeouts on large lookbacks


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state():
    default = {"processed_accessions": [], "weekly_extractions": []}
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else default


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# EDGAR search
# ---------------------------------------------------------------------------

def search_efts(form_type, start_dt, end_dt, from_offset=0):
    """Query EDGAR full-text search API for ETF-related filings."""
    # Use a tighter query for high-volume form types to avoid EFTS 500 errors
    if form_type == "485BPOS":
        query = '"exchange-traded fund" OR "exchange traded fund"'
    else:
        query = '"exchange-traded fund" OR "exchange traded fund" OR "ETF"'
    try:
        r = requests.get(
            "https://efts.sec.gov/LATEST/search-index",
            params={
                "q":         query,
                "forms":     form_type,
                "dateRange": "custom",
                "startdt":   start_dt.strftime("%Y-%m-%d"),
                "enddt":     end_dt.strftime("%Y-%m-%d"),
                "from":      from_offset,
            },
            headers=SEC_HEADERS,
            timeout=20,
        )
        r.raise_for_status()
        data = r.json().get("hits", {})
        return data.get("hits", []), data.get("total", {}).get("value", 0)
    except Exception as e:
        print(f"  EFTS search error: {e}")
        return [], 0


def get_all_hits(form_type, start_dt, end_dt):
    all_hits, offset = [], 0
    while True:
        hits, total = search_efts(form_type, start_dt, end_dt, offset)
        all_hits.extend(hits)
        if not hits or len(all_hits) >= total or len(all_hits) >= MAX_FILINGS_PER_RUN:
            break
        offset += 10
        time.sleep(0.3)
    return all_hits


# ---------------------------------------------------------------------------
# Document fetching
# ---------------------------------------------------------------------------

def get_main_doc_url(accession_no, cik=None):
    """Derive filing index URL from accession number and find the main HTM document."""
    if not cik:
        cik = accession_no.split("-")[0].lstrip("0")
    nodashes = accession_no.replace("-", "")
    index_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{nodashes}/{accession_no}-index.json"
    try:
        r = requests.get(index_url, headers=SEC_HEADERS, timeout=10)
        if r.status_code != 200:
            return None
        docs = r.json().get("documents", [])
        # EDGAR index JSON uses "document" field (not "filename")
        def docname(d):
            return d.get("document") or d.get("filename") or ""
        # Prefer document whose type matches the form type
        for doc in docs:
            name = docname(doc)
            if doc.get("type", "") in ("N-1A", "485APOS", "485BPOS", "PROSPECTUS") \
                    and name.lower().endswith((".htm", ".html")):
                return f"https://www.sec.gov/Archives/edgar/data/{cik}/{nodashes}/{name}"
        # Fallback: first HTM document
        for doc in docs:
            name = docname(doc)
            if name.lower().endswith((".htm", ".html")):
                return f"https://www.sec.gov/Archives/edgar/data/{cik}/{nodashes}/{name}"
    except Exception:
        pass
    return None


def fetch_key_sections(doc_url):
    """Fetch the prospectus document and extract summary / principal strategies sections."""
    try:
        r = requests.get(doc_url, headers=SEC_HEADERS, timeout=15)
        if r.status_code != 200:
            return None

        text = re.sub(r"<[^>]+>", " ", r.text)
        text = re.sub(r"&#?[a-z0-9]+;", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        lower = text.lower()

        # Quick ETF relevance check — any of these phrases confirms it's an ETF filing
        etf_signals = ["exchange-traded fund", "exchange traded fund", " etf ", "etf share", "etf trust", "etfs"]
        if not any(s in lower for s in etf_signals):
            return None

        # Find the most informative starting point
        for marker in [
            "principal investment strategies",
            "investment objective",
            "fees and expenses",
            "fund summary",
        ]:
            idx = lower.find(marker)
            if idx > 500:
                return text[max(0, idx - 300): idx + 12000]

        return text[1000:13000]  # fallback

    except Exception:
        return None


# ---------------------------------------------------------------------------
# Claude: per-filing extraction (cheap — ~$0.003 each)
# ---------------------------------------------------------------------------

def extract_filing(entity_name, form_type, filed_date, text):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=2)

    prompt = f"""SEC {form_type} filing by {entity_name}, filed {filed_date}.

Return ONLY a JSON object — no markdown, no explanation:
{{
  "fund_name": "full ETF name, or null",
  "sponsor": "investment adviser / sponsor",
  "strategy": "one sentence",
  "expense_ratio": "e.g. 0.15%, or not stated",
  "distinctive_features": [],
  "launch_type": "new_fund | amendment_substantive | amendment_routine | conversion | new_share_class",
  "is_new_issuer": false,
  "surface": true,
  "notes": "anything notable for an ETF professional, or empty string"
}}

distinctive_features — include any that apply:
options-income, defined-outcome/buffer, single-stock, ETF-share-class-of-mutual-fund,
tokenized, 24h-trading, novel-custody, crypto/digital-asset, leveraged, inverse,
active-nontransparent, multi-share-class, interval-fund

launch_type rules:
- new_fund: N-1A initial registration, or 485APOS adding a genuinely new series
- amendment_substantive: meaningful change — new strategy, fee cut, new share class
- amendment_routine: board/officer changes, generic disclosure edits, no strategic change
- conversion: mutual fund converting to ETF structure
- new_share_class: adding a share class to an existing ETF

Set surface=false for amendment_routine. Set surface=true for everything else.

Text:
{text[:7000]}"""

    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        raw = re.sub(r"^```json?\n?", "", raw).rstrip("```").strip()
        return json.loads(raw)
    except Exception as e:
        print(f"    Extraction error: {e}")
        return None


# ---------------------------------------------------------------------------
# Claude: weekly synthesis
# ---------------------------------------------------------------------------

def synthesize(extractions):
    notable = [
        e for e in extractions
        if e.get("extracted", {}).get("surface")
        and e.get("extracted", {}).get("launch_type") != "amendment_routine"
    ]
    if not notable:
        return "No notable ETF filings this week — only routine amendments."

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=3)
    week = datetime.now().strftime("Week of %B %d, %Y")

    prompt = f"""Weekly ETF prospectus digest for a senior ETF industry professional on garden leave. {week}.

Filings (JSON):
{json.dumps(notable, indent=2)}

Write the digest in Slack markdown (*bold* not **bold**, no # headers). Include only sections that have content — omit empty ones entirely:

*New Launches*
Each genuinely new ETF: one bullet — fund name, sponsor, strategy in one clause, expense ratio, any distinctive features.

*New Issuers Entering the Market*
First-time ETF filers. Who are they, what are they launching, why does it matter?

*Structural Innovations*
Novel mechanics, structures, or product types. Group similar ones together.

*Thematic Clusters*
Multiple sponsors filing similar strategies this week — surface the pattern, name all of them.

*Notable Fee Moves*
Sub-10bps launches or amendment fee cuts. Flag anything competitively significant.

*Mutual Fund Conversions*
Any fund converting to ETF structure.

Lead each section with the most notable item. Be specific — name funds and sponsors. One bullet per fund unless clustering."""

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------

def chunk_text(text, max_len=2900):
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        i = text.rfind("\n", 0, max_len)
        if i == -1:
            i = max_len
        chunks.append(text[:i])
        text = text[i:].lstrip("\n")
    return chunks


def post_to_slack(digest, n_filings):
    week = datetime.now().strftime("Week of %B %d, %Y")
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"ETF Launches & Filings — {week}"}},
        *[{"type": "section", "text": {"type": "mrkdwn", "text": c}} for c in chunk_text(digest)],
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"_{n_filings} filings processed_"}]},
    ]
    r = requests.post(os.environ["PROSPECTUS_SLACK_WEBHOOK_URL"], json={"blocks": blocks}, timeout=10)
    r.raise_for_status()
    print(f"Slack: {r.status_code}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    state = load_state()
    lookback_days = int(os.environ.get("LOOKBACK_DAYS", "7"))
    force_digest = os.environ.get("FORCE_DIGEST", "false").lower() == "true"
    is_monday = datetime.now().weekday() == 0

    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=lookback_days)

    reset_state = os.environ.get("RESET_STATE", "false").lower() == "true"
    if reset_state:
        print("RESET_STATE=true — clearing processed accessions and weekly extractions.")
        processed, weekly = set(), []
    else:
        processed = set(state.get("processed_accessions", []))
        weekly = state.get("weekly_extractions", [])
    new_count = 0

    print(f"Range: {start_dt.date()} → {end_dt.date()} | lookback={lookback_days}d | force_digest={force_digest} | known_processed={len(processed)}")

    for form_type in FORM_TYPES:
        if new_count >= MAX_FILINGS_PER_RUN:
            print("Per-run cap reached.")
            break

        print(f"\n{form_type}...")
        hits = get_all_hits(form_type, start_dt, end_dt)
        print(f"  {len(hits)} hits")
        if hits:
            print(f"  _source keys: {list(hits[0].get('_source', {}).keys())}")

        seen_accessions = set()
        for hit in hits:
            if new_count >= MAX_FILINGS_PER_RUN:
                break

            src = hit.get("_source", {})
            accession = src.get("adsh") or hit.get("_id", "").split(":")[0]
            if not accession or accession in processed or accession in seen_accessions:
                continue
            seen_accessions.add(accession)

            names = src.get("display_names", [])
            entity = names[0] if isinstance(names, list) and names else "Unknown"
            filed = (src.get("file_date") or "")[:10]
            ciks = src.get("ciks", [])

            # Pre-filter: skip obvious non-ETF entities (variable annuity/insurance funds,
            # municipal bond funds, money market funds — these match "ETF" in boilerplate text)
            entity_lower = entity.lower()
            non_etf_signals = ["variable series", "variable insurance", "tax-free", "money market",
                               "municipal", "muni bond", "separate account", "fixed income series"]
            if any(s in entity_lower for s in non_etf_signals):
                print(f"    SKIP: non-ETF entity name ({entity})")
                processed.add(accession)
                continue

            print(f"  {entity} / {accession}")

            # Filing path always uses the filer's CIK = accession prefix (not the registrant CIK)
            filer_cik = accession.split("-")[0].lstrip("0")
            print(f"    filer_cik={filer_cik} registrant_ciks={ciks} accession={accession}")
            doc_url = get_main_doc_url(accession, filer_cik)
            if not doc_url:
                print(f"    SKIP: no document URL found in filing index")
                processed.add(accession)
                continue

            text = fetch_key_sections(doc_url)
            if not text:
                print(f"    SKIP: fetch failed or ETF signals not found in document")
                processed.add(accession)
                continue
            print(f"    Got {len(text):,} chars")

            extracted = extract_filing(entity, form_type, filed, text)
            if not extracted:
                processed.add(accession)
                continue

            lt = extracted.get("launch_type", "?")
            fn = extracted.get("fund_name") or "?"
            er = extracted.get("expense_ratio", "?")
            surf = extracted.get("surface", False)
            print(f"    → {lt}: {fn} | {er} | surface={surf}")

            weekly.append({
                "accession":   accession,
                "entity_name": entity,
                "form_type":   form_type,
                "filed_date":  filed,
                "extracted":   extracted,
            })
            processed.add(accession)
            new_count += 1
            time.sleep(0.5)

    print(f"\nNew this run: {new_count} | Weekly total: {len(weekly)}")

    if (is_monday or force_digest) and weekly:
        print("Synthesizing...")
        digest = synthesize(weekly)
        print(digest)
        post_to_slack(digest, len(weekly))
        weekly = []
        print("Accumulator reset.")
    elif is_monday or force_digest:
        print("Nothing to synthesize.")

    # Keep last 1000 accessions to prevent unbounded growth
    state["processed_accessions"] = list(processed)[-1000:]
    state["weekly_extractions"] = weekly
    save_state(state)
    print("Done.")


if __name__ == "__main__":
    main()
