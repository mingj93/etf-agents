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
MAX_FILINGS_PER_RUN = 75

# Each config: what to search and which file_type marks the main prospectus document.
# EFTS returns document-level hits; file_type tells us if it's the main doc or an exhibit.
SEARCH_CONFIGS = [
    {
        "forms":           "N-1A",
        "query":           '"exchange-traded fund" OR "exchange traded fund" OR "ETF"',
        "main_file_types": {"N-1A"},
    },
    {
        "forms":           "485APOS",
        "query":           '"exchange-traded fund" OR "exchange traded fund" OR "ETF"',
        "main_file_types": {"485APOS"},
    },
    {
        "forms":           "485BPOS",
        "query":           '"exchange-traded fund" OR "exchange traded fund"',
        "main_file_types": {"485BPOS"},
    },
    {
        # Grantor trusts and commodity/crypto ETFs register via S-1, not N-1A
        "forms":           "S-1,S-1/A",
        "query":           '"authorized participant" OR "creation basket" OR "exchange-traded"',
        "main_file_types": {"S-1", "S-1/A"},
    },
]

NON_ETF_SIGNALS = [
    "variable series", "variable insurance", "tax-free", "money market",
    "municipal", "muni bond", "separate account", "fixed income series",
]


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state():
    default = {"processed_accessions": [], "weekly_extractions": []}
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else default


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# EDGAR search — document-level hits, paginated
# ---------------------------------------------------------------------------

def search_efts(forms, query, start_dt, end_dt, from_offset=0):
    try:
        r = requests.get(
            "https://efts.sec.gov/LATEST/search-index",
            params={
                "q":         query,
                "forms":     forms,
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
        print(f"  EFTS error: {e}")
        return [], 0


def get_all_hits(forms, query, start_dt, end_dt):
    all_hits, offset = [], 0
    while True:
        hits, total = search_efts(forms, query, start_dt, end_dt, offset)
        all_hits.extend(hits)
        if not hits or len(all_hits) >= total or len(all_hits) >= MAX_FILINGS_PER_RUN * 3:
            break
        offset += 10
        time.sleep(0.3)
    return all_hits


# ---------------------------------------------------------------------------
# Document URL — parsed directly from EFTS _id (no index file needed)
# ---------------------------------------------------------------------------

def doc_url_from_hit(hit):
    """
    EFTS _id format: "{accession}:{document_filename}"
    The filer CIK is always the accession number prefix.
    """
    raw_id = hit.get("_id", "")
    if ":" not in raw_id:
        return None, None
    accession, doc_name = raw_id.split(":", 1)
    filer_cik = accession.split("-")[0].lstrip("0")
    nodashes = accession.replace("-", "")
    url = f"https://www.sec.gov/Archives/edgar/data/{filer_cik}/{nodashes}/{doc_name}"
    return accession, url


# ---------------------------------------------------------------------------
# Document fetch and ETF relevance check
# ---------------------------------------------------------------------------

ETF_SIGNALS = [
    "exchange-traded fund", "exchange traded fund", " etf ", "etf share",
    "etf trust", "etfs", "authorized participant", "creation basket",
]


def fetch_key_sections(doc_url):
    try:
        r = requests.get(doc_url, headers=SEC_HEADERS, timeout=15)
        if r.status_code != 200:
            print(f"    HTTP {r.status_code}")
            return None

        text = re.sub(r"<[^>]+>", " ", r.text)
        text = re.sub(r"&#?[a-z0-9]+;", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        lower = text.lower()

        if not any(s in lower for s in ETF_SIGNALS):
            print(f"    No ETF signals in document")
            return None

        for marker in ["principal investment strategies", "investment objective",
                       "fees and expenses", "fund summary", "use of proceeds"]:
            idx = lower.find(marker)
            if idx > 500:
                return text[max(0, idx - 300): idx + 12000]

        return text[1000:13000]

    except Exception as e:
        print(f"    Fetch error: {e}")
        return None


# ---------------------------------------------------------------------------
# Claude: per-filing extraction (~$0.003 each)
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

distinctive_features options: options-income, defined-outcome/buffer, single-stock,
ETF-share-class-of-mutual-fund, tokenized, 24h-trading, novel-custody,
crypto/digital-asset, leveraged, inverse, active-nontransparent, multi-share-class.

launch_type: new_fund for N-1A or S-1 initial registrations and 485APOS adding a new series;
amendment_substantive for fee cuts, strategy shifts, new share class;
amendment_routine for board/officer/disclosure-only changes (set surface=false for these);
conversion for mutual-fund-to-ETF conversions.

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

Write the digest in Slack markdown (*bold* not **bold**, no # headers). Include only sections that have content:

*New Launches*
Each genuinely new ETF: fund name, sponsor, one-clause strategy, expense ratio, distinctive features.

*New Issuers Entering the Market*
First-time ETF filers — who they are, what they're launching, why it matters.

*Structural Innovations*
Novel mechanics or product types. Group similar ones.

*Thematic Clusters*
Multiple sponsors filing similar strategies — surface the pattern, name all of them.

*Notable Fee Moves*
Sub-10bps launches or fee cuts on amendments.

*Mutual Fund Conversions*
Funds converting to ETF structure.

Lead each section with the most notable item. Name funds and sponsors specifically."""

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
    force_digest  = os.environ.get("FORCE_DIGEST",  "false").lower() == "true"
    reset_state   = os.environ.get("RESET_STATE",   "false").lower() == "true"
    is_monday     = datetime.now().weekday() == 0

    if reset_state:
        print("RESET_STATE=true — clearing state.")
        processed, weekly = set(), []
    else:
        processed = set(state.get("processed_accessions", []))
        weekly    = state.get("weekly_extractions", [])

    end_dt   = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=lookback_days)
    new_count = 0

    print(f"Range: {start_dt.date()} → {end_dt.date()} | lookback={lookback_days}d | known_processed={len(processed)}")

    for cfg in SEARCH_CONFIGS:
        if new_count >= MAX_FILINGS_PER_RUN:
            print("Per-run cap reached.")
            break

        forms           = cfg["forms"]
        query           = cfg["query"]
        main_file_types = cfg["main_file_types"]

        print(f"\n{forms}...")
        hits = get_all_hits(forms, query, start_dt, end_dt)
        print(f"  {len(hits)} document hits")

        seen_accessions = set()
        for hit in hits:
            if new_count >= MAX_FILINGS_PER_RUN:
                break

            src       = hit.get("_source", {})
            file_type = src.get("file_type", "")

            # Skip exhibits — only process the main prospectus document
            if file_type not in main_file_types:
                continue

            accession, doc_url = doc_url_from_hit(hit)
            if not accession or not doc_url:
                continue
            if accession in processed or accession in seen_accessions:
                continue
            seen_accessions.add(accession)

            names  = src.get("display_names", [])
            entity = names[0] if isinstance(names, list) and names else "Unknown"
            filed  = (src.get("file_date") or "")[:10]

            # Skip obvious non-ETF filers
            if any(s in entity.lower() for s in NON_ETF_SIGNALS):
                print(f"  SKIP non-ETF: {entity}")
                processed.add(accession)
                continue

            print(f"  {entity} | {accession}")
            print(f"    {doc_url}")

            text = fetch_key_sections(doc_url)
            if not text:
                processed.add(accession)
                continue
            print(f"    Got {len(text):,} chars")

            extracted = extract_filing(entity, forms.split(",")[0], filed, text)
            if not extracted:
                processed.add(accession)
                continue

            lt   = extracted.get("launch_type", "?")
            fn   = extracted.get("fund_name") or "?"
            er   = extracted.get("expense_ratio", "?")
            surf = extracted.get("surface", False)
            print(f"    → {lt}: {fn} | {er} | surface={surf}")

            weekly.append({
                "accession":   accession,
                "entity_name": entity,
                "form_type":   forms.split(",")[0],
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

    state["processed_accessions"] = list(processed)[-1000:]
    state["weekly_extractions"]   = weekly
    save_state(state)
    print("Done.")


if __name__ == "__main__":
    main()
