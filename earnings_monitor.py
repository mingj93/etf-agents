#!/usr/bin/env python3
"""Quarterly earnings monitor — checks SEC EDGAR for new 10-Q filings from ETF issuers."""

import json
import os
import re
import time
from pathlib import Path

import anthropic
import requests

COMPANIES = {
    "BLK":  "BlackRock",
    "STT":  "State Street",
    "IVZ":  "Invesco",
    "BEN":  "Franklin Templeton",
    "WETF": "WisdomTree",
    "AMG":  "Affiliated Managers Group",
    "SEIC": "SEI Investments",
    "APAM": "Artisan Partners",
    "CNS":  "Cohen & Steers",
    "VRTS": "Virtus Investment Partners",
}

STATE_FILE = Path("processed_filings.json")
SEC_HEADERS = {"User-Agent": "ETFEarningsMonitor mingj93@gmail.com"}


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def load_cik_map():
    r = requests.get(
        "https://www.sec.gov/files/company_tickers.json",
        headers=SEC_HEADERS, timeout=15,
    )
    r.raise_for_status()
    return {
        entry["ticker"].upper(): str(entry["cik_str"]).zfill(10)
        for entry in r.json().values()
    }


def get_latest_10q(cik):
    r = requests.get(
        f"https://data.sec.gov/submissions/CIK{cik}.json",
        headers=SEC_HEADERS, timeout=15,
    )
    r.raise_for_status()
    filings = r.json()["filings"]["recent"]

    for i, form in enumerate(filings["form"]):
        if form == "10-Q":
            return {
                "accession":   filings["accessionNumber"][i],
                "date":        filings["filingDate"][i],
                "period":      filings["reportDate"][i],
                "primary_doc": filings["primaryDocument"][i],
            }
    return None


def fetch_filing_text(cik, accession, primary_doc):
    cik_int = int(cik)
    accession_clean = accession.replace("-", "")
    url = (
        f"https://www.sec.gov/Archives/edgar/data/"
        f"{cik_int}/{accession_clean}/{primary_doc}"
    )
    r = requests.get(url, headers=SEC_HEADERS, timeout=45)
    r.raise_for_status()

    text = re.sub(r"<[^>]+>", " ", r.text)
    text = re.sub(r"&#?[a-z0-9]+;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    # Focus on MD&A — where AUM, flows, and product commentary live
    for marker in ["management's discussion and analysis", "item 2."]:
        idx = text.lower().find(marker)
        if idx > 1000:
            return text[idx: idx + 40000]

    # Fallback: skip cover pages, take 40k chars
    start = len(text) // 10
    return text[start: start + 40000]


def analyze_filing(company_name, ticker, text, period):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=3)

    prompt = f"""You are analyzing a 10-Q SEC filing from {company_name} ({ticker}) for the period ending {period}.

The reader is a senior ETF industry professional — they know the industry deeply. Be specific and quantitative. Skip boilerplate.

Extract what's available under these headings (omit any section with no relevant data):

*ETF AUM & Flows* — specific AUM figures, net flows, organic growth rate, quarter-over-quarter and year-over-year comparisons.

*Fee Compression* — average fee rates, revenue yield trends, basis point changes, pricing pressure commentary.

*Product Launches & Pipeline* — new ETFs launched, filed with SEC, or mentioned as planned; any closures or mergers.

*Competitive & Regulatory Remarks* — anything said about the competitive landscape, passive vs active, regulatory changes, market structure.

*Surprises or Flags* — anything unusual, a miss vs expectations, or an early signal worth tracking.

Rules:
- Use Slack markdown (*bold*, not **bold**)
- First line must be the single most important takeaway — lead with it, no preamble
- 2–4 tight bullets per section
- Include specific numbers wherever available

Filing text:
{text}"""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


def chunk_text(text, max_len=2900):
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, max_len)
        if split_at == -1:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


def post_to_slack(company_name, ticker, period, analysis, filing_date, cik, accession):
    webhook_url = os.environ["EARNINGS_SLACK_WEBHOOK_URL"]

    cik_int = int(cik)
    accession_clean = accession.replace("-", "")
    filing_url = (
        f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_clean}/"
    )

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{company_name} ({ticker}) — {period} 10-Q"},
        },
        {
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": f"Filed {filing_date} · <{filing_url}|View on SEC EDGAR>",
            }],
        },
    ]

    for chunk in chunk_text(analysis):
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": chunk}})

    r = requests.post(webhook_url, json={"blocks": blocks}, timeout=10)
    r.raise_for_status()
    print(f"  Slack: {r.status_code}")


def main():
    state = load_state()

    print("Loading SEC ticker map...")
    cik_map = load_cik_map()

    for ticker, company_name in COMPANIES.items():
        print(f"\n{company_name} ({ticker})")

        cik = cik_map.get(ticker)
        if not cik:
            print(f"  CIK not found, skipping")
            continue

        try:
            filing = get_latest_10q(cik)
        except Exception as e:
            print(f"  EDGAR error: {e}")
            continue

        if not filing:
            print(f"  No 10-Q found")
            continue

        accession = filing["accession"]
        if state.get(ticker) == accession:
            print(f"  Already processed ({filing['date']})")
            continue

        print(f"  New filing: {accession} (filed {filing['date']}, period {filing['period']})")

        try:
            text = fetch_filing_text(cik, accession, filing["primary_doc"])
            print(f"  Fetched {len(text):,} chars")

            analysis = analyze_filing(company_name, ticker, text, filing["period"])
            post_to_slack(
                company_name, ticker, filing["period"],
                analysis, filing["date"], cik, accession,
            )

            state[ticker] = accession
            time.sleep(2)

        except Exception as e:
            print(f"  Error: {e}")
            continue

    save_state(state)
    print("\nDone.")


if __name__ == "__main__":
    main()
