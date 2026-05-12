#!/usr/bin/env python3
import os
import re
import feedparser
import anthropic
import requests
from datetime import datetime, timedelta, timezone

FEEDS = [
    # Specialist ETF sources (reliable)
    ("ETF Trends",           "https://www.etftrends.com/feed/"),
    ("ETF.com",              "https://www.etf.com/rss.xml"),
    # Google News relays — surfaces headlines from WSJ, Bloomberg, FT, Reuters etc. without IP blocks
    ("Google News: ETF",     "https://news.google.com/rss/search?q=ETF+exchange+traded+fund&hl=en-US&gl=US&ceid=US:en"),
    ("Google News: ETF flows","https://news.google.com/rss/search?q=ETF+flows+fund+flows&hl=en-US&gl=US&ceid=US:en"),
    ("Google News: ETF launch","https://news.google.com/rss/search?q=ETF+launch+new+fund+SEC&hl=en-US&gl=US&ceid=US:en"),
    ("Google News: BlackRock ETF","https://news.google.com/rss/search?q=BlackRock+iShares+Vanguard+ETF&hl=en-US&gl=US&ceid=US:en"),
    ("Google News: ETF regulation","https://news.google.com/rss/search?q=ETF+regulation+SEC+passive+investing&hl=en-US&gl=US&ceid=US:en"),
    # Direct feeds that tend to work from cloud IPs
    ("Morningstar",          "https://www.morningstar.com/feeds/article.rss"),
    ("Pensions & Investments","https://www.pionline.com/rss/home"),
    ("CNBC Finance",         "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
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


def fetch_articles(hours=24):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    articles = []

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

                # Skip only if we have a date AND it's clearly too old
                if published and published < cutoff:
                    continue

                title = strip_html(entry.get("title", ""))
                summary = strip_html(entry.get("summary", "") or entry.get("description", ""))
                link = entry.get("link", "")

                if is_etf_relevant(f"{title} {summary}"):
                    matched += 1
                    articles.append({
                        "source": source,
                        "title": title,
                        "summary": summary[:600],
                        "link": link,
                        "published": published.strftime("%Y-%m-%d %H:%M UTC") if published else "Unknown",
                    })

            print(f"  {source}: {entry_count} entries, {matched} relevant")

        except Exception as e:
            print(f"  {source}: FAILED — {e}")

    return articles


def generate_brief(articles):
    if not articles:
        return "No ETF-relevant articles found in the last 24 hours."

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    articles_text = "\n\n".join(
        f"SOURCE: {a['source']}\nTITLE: {a['title']}\nSUMMARY: {a['summary']}\nLINK: {a['link']}"
        for a in articles
    )

    today = datetime.now().strftime("%A, %B %d, %Y")

    prompt = f"""You are writing a morning news brief for someone on garden leave from a senior role at an ETF issuer. They stay informed on the industry but are not currently working. Today is {today}.

Here are ETF-relevant articles from the last 24 hours:

{articles_text}

Write a concise morning brief with these three sections:

**Top Stories** — 3 to 5 bullets covering the most important ETF news. Lead with what happened, not just that something happened.

**Why It Matters** — 2 to 3 sentences on the broader significance. Connect dots across stories if relevant.

**Worth Watching** — 1 to 3 bullets on developing situations or early signals worth tracking over the coming days.

Style: tight, direct, industry-fluent. Assume deep familiarity with ETF mechanics, issuer dynamics, and regulatory landscape. Skip obvious context. Include article links where relevant."""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1200,
        messages=[{"role": "user", "content": prompt}],
    )

    return response.content[0].text


def post_to_slack(brief, article_count, source_count):
    webhook_url = os.environ["SLACK_WEBHOOK_URL"]
    date_str = datetime.now().strftime("%A, %B %d")

    payload = {
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"ETF Morning Brief — {date_str}"},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": brief},
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"_{article_count} relevant articles from {source_count} sources_",
                    }
                ],
            },
        ]
    }

    response = requests.post(webhook_url, json=payload, timeout=10)
    response.raise_for_status()
    print(f"Slack: {response.status_code}")


def main():
    print("Fetching articles...")
    articles = fetch_articles(hours=24)
    print(f"Found {len(articles)} relevant articles")

    print("Generating brief...")
    brief = generate_brief(articles)
    print(brief)

    print("Posting to Slack...")
    sources_hit = len({a["source"] for a in articles})
    post_to_slack(brief, len(articles), sources_hit)
    print("Done.")


if __name__ == "__main__":
    main()
