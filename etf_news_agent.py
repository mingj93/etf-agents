#!/usr/bin/env python3
import json
import os
import re
import feedparser
import anthropic
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_FILE = Path("daily_brief_seen.json")

FEEDS = [
    # Specialist ETF sources
    ("ETF Trends",            "https://www.etftrends.com/feed/"),
    ("ETF.com",               "https://www.etf.com/rss.xml"),
    ("RIABiz",                "https://riabiz.com/feed/"),
    ("Citywire USA",          "https://citywire.com/usa/rss"),
    ("ThinkAdvisor",          "https://www.thinkadvisor.com/feed/"),
    ("Investment News",       "https://www.investmentnews.com/rss/home"),
    # Google News relays — Bloomberg, Reuters, FT, WSJ etc. without IP blocks
    ("Google News: ETF",      "https://news.google.com/rss/search?q=ETF+exchange+traded+fund&hl=en-US&gl=US&ceid=US:en"),
    ("Google News: ETF flows", "https://news.google.com/rss/search?q=ETF+flows+fund+flows&hl=en-US&gl=US&ceid=US:en"),
    ("Google News: ETF launch","https://news.google.com/rss/search?q=ETF+launch+new+fund+SEC+filing&hl=en-US&gl=US&ceid=US:en"),
    ("Google News: Bloomberg ETF","https://news.google.com/rss/search?q=Bloomberg+ETF+iShares+Vanguard&hl=en-US&gl=US&ceid=US:en"),
    ("Google News: Reuters ETF","https://news.google.com/rss/search?q=Reuters+ETF+fund+flows+asset+management&hl=en-US&gl=US&ceid=US:en"),
    ("Google News: BlackRock ETF","https://news.google.com/rss/search?q=BlackRock+iShares+Vanguard+ETF&hl=en-US&gl=US&ceid=US:en"),
    ("Google News: ETF regulation","https://news.google.com/rss/search?q=ETF+regulation+SEC+passive+investing&hl=en-US&gl=US&ceid=US:en"),
    ("Google News: RIA ETF",  "https://news.google.com/rss/search?q=RIA+ETF+wealth+management+advisor+model+portfolio&hl=en-US&gl=US&ceid=US:en"),
    ("Google News: Morningstar ETF","https://news.google.com/rss/search?q=Morningstar+ETF+fund+rating&hl=en-US&gl=US&ceid=US:en"),
    # Direct feeds
    ("Morningstar",           "https://www.morningstar.com/feeds/article.rss"),
    ("Pensions & Investments", "https://www.pionline.com/rss/home"),
    ("CNBC Finance",          "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
]

ETF_KEYWORDS = [
    "etf", "exchange-traded fund", "exchange traded fund",
    "fund flow", "etf flow", "passive fund", "index fund",
    "blackrock", "vanguard", "state street", "fidelity", "invesco",
    "franklin templeton", "ishares", "spdr", "wisdomtree", "dimensional",
    "expense ratio", "fund launch", "new fund", "sec filing",
    "1940 act", "40 act", "creation unit", "redemption basket",
    "nav discount", "nav premium", "authorized participant",
    "etf regulation", "etf issuer", "fund industry", "passive investing",
    "assets under management", "aum", "net flows", "fund flows",
]


def is_etf_relevant(text):
    lower = text.lower()
    return any(kw in lower for kw in ETF_KEYWORDS)


def strip_html(text):
    return re.sub(r"<[^>]+>", "", text or "")


def load_seen_urls():
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return set()


def save_seen_urls(urls):
    # Keep only the most recent 500 URLs to prevent unbounded growth
    STATE_FILE.write_text(json.dumps(list(urls)[-500:]))


def fetch_articles(hours=24):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    seen_urls = load_seen_urls()
    articles = []
    seen_links = set()

    for source, url in FEEDS:
        try:
            feed = feedparser.parse(url, request_headers={
                "User-Agent": "Mozilla/5.0 (compatible; ETFNewsAgent/1.0)"
            })
            entry_count = len(feed.entries)
            matched = 0

            for entry in feed.entries:
                published = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)

                # Skip if we have a date and it's too old; accept undated articles (URL dedup handles repeats)
                if published and published < cutoff:
                    continue

                link = entry.get("link", "")

                # Skip articles seen in yesterday's brief
                if link in seen_urls or link in seen_links:
                    continue
                seen_links.add(link)

                title = strip_html(entry.get("title", ""))
                summary = strip_html(entry.get("summary", "") or entry.get("description", ""))

                if is_etf_relevant(f"{title} {summary}"):
                    matched += 1
                    articles.append({
                        "source": source,
                        "title": title,
                        "summary": summary[:600],
                        "link": link,
                        "published": published.strftime("%Y-%m-%d %H:%M UTC"),
                    })

            print(f"  {source}: {entry_count} entries, {matched} new & relevant")

        except Exception as e:
            print(f"  {source}: FAILED — {e}")

    return articles, seen_links


def generate_brief(articles):
    if not articles:
        return "No ETF-relevant articles found in the last 24 hours."

    # Cap at 25 to keep the prompt size manageable
    articles = articles[:25]

    client = anthropic.Anthropic(
        api_key=os.environ["ANTHROPIC_API_KEY"],
        max_retries=3,
    )

    articles_text = "\n\n".join(
        f"SOURCE: {a['source']}\nTITLE: {a['title']}\nSUMMARY: {a['summary'][:300]}\nLINK: {a['link']}"
        for a in articles
    )

    today = datetime.now().strftime("%A, %B %d, %Y")
    print(f"Sending {len(articles)} articles to Claude (~{len(articles_text)} chars)")

    prompt = f"""You are writing a morning news brief for someone on garden leave from a senior role at an ETF issuer. They stay informed on the industry but are not currently working. Today is {today}.

Here are ETF-relevant articles from the last 24 hours:

{articles_text}

Write a morning brief in exactly this structure, using Slack markdown (*bold* not **bold**, no # headers, no --- dividers):

*TL;DR*
1. One sentence summary of story 1
2. One sentence summary of story 2
3. One sentence summary of story 3
4. One sentence summary of story 4
5. One sentence summary of story 5
6. One sentence summary of story 6
7. One sentence summary of story 7
8. One sentence summary of story 8
9. One sentence summary of story 9
10. One sentence summary of story 10

*Top Stories*
For each of the 10 stories, one bullet in this format:
• *Headline* — 2-3 sentences on what happened and why it matters. Link formatted as <url|Source> (Slack format — never paste raw URLs).

*Why It Matters*
2-3 sentences connecting the dots across today's stories.

*Worth Watching*
2-3 bullets on situations to track over coming days.

Style: tight, direct, industry-fluent. Assume deep ETF knowledge — skip basics. Use Slack link syntax <url|Source Name> for every link so URLs stay clean."""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )

    return response.content[0].text


def chunk_text(text, max_len=2900):
    """Split text into chunks that fit within Slack's block size limit."""
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


def post_to_slack(brief, article_count, source_count):
    webhook_url = os.environ["SLACK_WEBHOOK_URL"]
    date_str = datetime.now().strftime("%A, %B %d")

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"ETF Morning Brief — {date_str}"},
        }
    ]

    for chunk in chunk_text(brief):
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": chunk},
        })

    blocks.append({
        "type": "context",
        "elements": [
            {
                "type": "mrkdwn",
                "text": f"_{article_count} relevant articles from {source_count} sources_",
            }
        ],
    })

    response = requests.post(webhook_url, json={"blocks": blocks}, timeout=10)
    response.raise_for_status()
    print(f"Slack: {response.status_code}")


def main():
    print("Fetching articles...")
    articles, all_seen_links = fetch_articles(hours=24)
    print(f"Found {len(articles)} new relevant articles")

    print("Generating brief...")
    brief = generate_brief(articles)
    print(brief)

    print("Posting to Slack...")
    sources_hit = len({a["source"] for a in articles})
    post_to_slack(brief, len(articles), sources_hit)

    # Save all links seen today so tomorrow's run skips them
    save_seen_urls(all_seen_links)
    print(f"Saved {len(all_seen_links)} URLs to seen state.")
    print("Done.")


if __name__ == "__main__":
    main()
