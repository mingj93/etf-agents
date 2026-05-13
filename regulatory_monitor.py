#!/usr/bin/env python3
"""Weekly regulatory & microstructure intelligence — theme detection across many sources."""

import os
import re
from datetime import datetime, timedelta, timezone

import anthropic
import feedparser
import requests

# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

REGULATORY_FEEDS = [
    ("SEC Proposed Rules",  "https://www.sec.gov/rss/rules/proposed.xml"),
    ("SEC Final Rules",     "https://www.sec.gov/rss/rules/final.xml"),
    ("SEC Speeches",        "https://www.sec.gov/rss/news/speeches.xml"),
    ("SEC Press Releases",  "https://www.sec.gov/rss/news/press.xml"),
]

INDUSTRY_FEEDS = [
    ("ETF.com",     "https://www.etf.com/rss.xml"),
    ("ETF Trends",  "https://www.etftrends.com/feed/"),
    ("P&I",         "https://www.pionline.com/rss/home"),
    ("Google: ETF regulation",
     "https://news.google.com/rss/search?q=ETF+SEC+regulation+rule+exemption&hl=en-US&gl=US&ceid=US:en"),
    ("Google: ETF market structure",
     "https://news.google.com/rss/search?q=ETF+market+structure+liquidity+creation+redemption+AP&hl=en-US&gl=US&ceid=US:en"),
    ("Google: FINRA ETF",
     "https://news.google.com/rss/search?q=FINRA+regulatory+notice+ETF+broker+dealer&hl=en-US&gl=US&ceid=US:en"),
    ("Google: passive investing policy",
     "https://news.google.com/rss/search?q=passive+investing+index+fund+policy+antitrust&hl=en-US&gl=US&ceid=US:en"),
]

COMMENTARY_FEEDS = [
    # Try Substack feeds directly; feedparser silently returns 0 entries if they 404
    ("Nate Geraci",  "https://nategeraci.substack.com/feed"),
    ("Matt Hougan",  "https://matthougan.substack.com/feed"),
    # Google News relays for people without direct RSS
    ("Google: Eric Balchunas",
     "https://news.google.com/rss/search?q=Eric+Balchunas+ETF&hl=en-US&gl=US&ceid=US:en"),
    ("Google: Dave Nadig",
     "https://news.google.com/rss/search?q=Dave+Nadig+ETF+VettaFi&hl=en-US&gl=US&ceid=US:en"),
    ("Google: Matt Hougan Bitwise",
     "https://news.google.com/rss/search?q=Matt+Hougan+Bitwise+ETF&hl=en-US&gl=US&ceid=US:en"),
    ("Google: Nate Geraci ETF Prime",
     "https://news.google.com/rss/search?q=Nate+Geraci+ETF+Prime&hl=en-US&gl=US&ceid=US:en"),
]

# Matt Levine gets his own feed group so the prompt can treat him separately
LEVINE_FEED_URL = "https://kill-the-newsletter.com/feeds/693pfik7dijxtdctxmp6.xml"
LEVINE_FEEDS = [
    ("Matt Levine / Money Stuff", LEVINE_FEED_URL),
]

ISSUER_FEEDS = [
    ("Google: iShares insights",
     "https://news.google.com/rss/search?q=iShares+BlackRock+ETF+outlook+commentary&hl=en-US&gl=US&ceid=US:en"),
    ("Google: SPDR insights",
     "https://news.google.com/rss/search?q=SPDR+\"State+Street\"+ETF+outlook+commentary&hl=en-US&gl=US&ceid=US:en"),
    ("Google: Invesco ETF",
     "https://news.google.com/rss/search?q=Invesco+ETF+insights+commentary&hl=en-US&gl=US&ceid=US:en"),
    ("Google: WisdomTree research",
     "https://news.google.com/rss/search?q=WisdomTree+ETF+research+commentary&hl=en-US&gl=US&ceid=US:en"),
]

ALL_FEED_GROUPS = {
    "Regulatory": REGULATORY_FEEDS,
    "Industry":   INDUSTRY_FEEDS,
    "Commentary": COMMENTARY_FEEDS,
    "Issuer":     ISSUER_FEEDS,
    "Levine":     LEVINE_FEEDS,
}

# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def strip_html(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"&#?[a-z0-9]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def fetch_all_sources(days=7):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    seen_titles = set()
    articles = []

    for group_name, feeds in ALL_FEED_GROUPS.items():
        for source, url in feeds:
            try:
                feed = feedparser.parse(
                    url,
                    request_headers={"User-Agent": "Mozilla/5.0 (compatible; ETFRegulatoryMonitor/1.0)"},
                )
                count = 0
                for entry in feed.entries:
                    published = None
                    if hasattr(entry, "published_parsed") and entry.published_parsed:
                        published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                    if published and published < cutoff:
                        continue

                    title = strip_html(entry.get("title", "")).strip()
                    if not title or title in seen_titles:
                        continue
                    seen_titles.add(title)

                    summary = strip_html(entry.get("summary", "") or entry.get("description", ""))
                    articles.append({
                        "group":     group_name,
                        "source":    source,
                        "title":     title,
                        "summary":   summary[:400],
                        "link":      entry.get("link", ""),
                        "published": published.strftime("%Y-%m-%d") if published else "recent",
                    })
                    count += 1

                print(f"  {source}: {count} items")

            except Exception as e:
                print(f"  {source}: FAILED — {e}")

    return articles


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def build_digest(articles):
    """Separate regulatory actions from general content for the prompt."""
    regulatory = [a for a in articles if a["group"] == "Regulatory"]
    general    = [a for a in articles if a["group"] != "Regulatory"]
    return regulatory, general


def format_articles(articles):
    return "\n\n".join(
        f"[{a['source']} · {a['published']}] {a['title']}\n{a['summary']}"
        for a in articles
    )


def analyze(regulatory_articles, general_articles, articles):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=3)

    week_ending = datetime.now().strftime("%B %d, %Y")
    reg_text     = format_articles(regulatory_articles) if regulatory_articles else "None this week."
    gen_text     = format_articles(general_articles[:60])
    levine_arts  = [a for a in articles if a["group"] == "Levine"]
    levine_text  = format_articles(levine_arts) if levine_arts else "No issues this week."

    prompt = f"""You are producing a weekly regulatory and microstructure intelligence briefing for a senior ETF industry professional on garden leave. Week ending {week_ending}.

The reader knows the industry deeply. Be specific, name names, cite sources. Skip boilerplate.

You have three sets of content below.

---

SECTION A — CONCRETE REGULATORY ACTIONS (SEC RSS feeds):
{reg_text}

---

SECTION B — INDUSTRY CONTENT (news, commentary, issuer thought leadership):
{gen_text}

---

SECTION C — MATT LEVINE / MONEY STUFF (full newsletter issues):
{levine_text}

---

Produce the briefing in this exact structure using Slack markdown (*bold* not **bold**, no # headers):

*Regulatory Actions This Week*
List only concrete actions: proposed rules, final rules adopted, no-action letters, exemptive orders, enforcement actions. If none, write "None this week." One bullet per action — rule/order name, what it does, why it matters to ETFs. Cite source inline.

---

*Emerging Themes*
Identify distinct themes from Sections A and B. Be adaptive — surface as many themes as genuinely exist, don't pad and don't compress.

For each theme:
*Theme: [one-line title]*
_Why it matters:_ one sentence
_Signals:_ 2–3 bullets with specific data points, quotes, or moves. Name companies, people, products. Cite source in brackets.

---

*Levine This Week* (include this section only if Section C contains material relevant to ETFs, asset management, market structure, or tokenisation — omit the section entirely if it doesn't)
If relevant: 2–4 bullets on the specific passages or arguments from Money Stuff that connect to the themes above or raise independent points worth tracking. Quote him briefly where it adds colour. If his coverage this week is purely about M&A, banking, or other unrelated topics, skip this section without comment.

---

Formatting rules:
- Each theme separated by a blank line
- Mobile-scannable: theme title + first bullet must deliver the point standalone
- Source citations in brackets: [ETF.com], [SEC Final Rules], [Levine/Bloomberg]
- Identify patterns across sources — do not summarise individual articles"""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2500,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


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
        split_at = text.rfind("\n", 0, max_len)
        if split_at == -1:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


def post_to_slack(briefing, article_count, source_count):
    webhook_url = os.environ["REGULATORY_SLACK_WEBHOOK_URL"]
    week = datetime.now().strftime("Week of %B %d, %Y")

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"Regulatory & Microstructure — {week}"},
        },
    ]

    for chunk in chunk_text(briefing):
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": chunk}})

    blocks.append({
        "type": "context",
        "elements": [{
            "type": "mrkdwn",
            "text": f"_{article_count} items across {source_count} sources_",
        }],
    })

    r = requests.post(webhook_url, json={"blocks": blocks}, timeout=10)
    r.raise_for_status()
    print(f"Slack: {r.status_code}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("Fetching sources (last 7 days)...")
    articles = fetch_all_sources(days=7)
    print(f"Total: {len(articles)} unique items")

    if not articles:
        print("Nothing found — exiting without posting.")
        return

    regulatory, general = build_digest(articles)
    print(f"Regulatory actions: {len(regulatory)}, General: {len(general)}")

    print("Analyzing with Claude...")
    briefing = analyze(regulatory, general, articles)
    print(briefing)

    print("Posting to Slack...")
    sources_hit = len({a["source"] for a in articles})
    post_to_slack(briefing, len(articles), sources_hit)
    print("Done.")


if __name__ == "__main__":
    main()
