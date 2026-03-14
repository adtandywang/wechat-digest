# 📰 WeChat Digest (微信公众号摘要)

Automatically aggregate articles from WeChat public accounts into a beautiful, filterable HTML digest. Never miss important content again.

## Features

- **Automated daily fetching** via RSS feeds on GitHub Actions
- **Cumulative 30-day cache** — RSS feeds only keep ~10-20 items, but this tool persists articles across runs so your 30-day view actually has 30 days of content
- **Time-period views** — see what you missed in the past 1 day, 7 days, or 30 days
- **Filtering** — by tag, account, and priority level
- **Priority sorting** — high-priority accounts surface first
- **GitHub Pages** — auto-deployed as a static site you can bookmark
- **Mobile-friendly** — responsive dark-theme UI

## Architecture

```
GitHub Actions (daily cron at 4PM Beijing)
    │
    ├── Load articles_cache.json (from previous runs)
    │
    ├── Fetch fresh articles from RSS feeds
    │
    ├── Merge + deduplicate + prune (>45 days)
    │
    ├── Save updated cache (for tomorrow's run)
    │
    ├── Generate index.html with embedded article data
    │
    └── Deploy to GitHub Pages
```

The **cache layer** is the critical piece. Without it, each daily run would only show whatever the RSS feed currently holds (often just a few days). With it, articles accumulate over time so your 30-day view genuinely spans 30 days.

## Quick Start

### 1. Fork this repo

Click the **Fork** button on GitHub.

### 2. Find RSS feeds for your WeChat accounts

You need an RSS feed URL for each account. Options:

| Service | URL | Cost | Reliability | Notes |
|---|---|---|---|---|
| **Self-hosted RSSHub** | [github.com/DIYgod/RSSHub](https://github.com/DIYgod/RSSHub) | Free (need a VPS) | **Best** | Full control, recommended for production |
| **WeRSS** | [werss.app](https://werss.app) | Paid | **High** | Managed service, most convenient |
| **feeddd.org** | [feeddd.org](https://feeddd.org) | Free | Medium | Community-maintained, may have gaps |
| **Public RSSHub** | [rsshub.app](https://rsshub.app) | Free | Low | Rate-limited, frequently blocked |

**Honest note on reliability:** WeChat is actively hostile to scraping. Free public services like feeddd.org and public RSSHub instances get rate-limited and blocked regularly. If this tool is important to your workflow, **self-host RSSHub on a cheap VPS** or **pay for WeRSS**. Free feeds are fine for testing but will have gaps in production.

### 3. Edit `config.yaml`

```yaml
accounts:
  - name: "机器之心"
    id: "jiqizhixin"
    rss_url: "https://your-rsshub.com/wechat/mp/MzI3..."
    tags:
      - ai
      - tech
    priority: high
```

### 4. Set up the automated feed finder (optional)

```bash
pip install -r requirements.txt
python setup_feeds.py --auto    # extracts biz_id from sample URLs
```

### 5. Enable GitHub Actions

Repo → **Settings** → **Actions** → **General** → Allow all actions.

### 6. Enable GitHub Pages

**Settings** → **Pages** → Source: **Deploy from a branch** → Branch: `gh-pages` / `root`.

Your digest will be at `https://yourusername.github.io/wechat-digest/`.

### 7. (Optional) Run locally

```bash
pip install -r requirements.txt
python main.py
open output/index.html
```

## Configuration Reference

| Field | Description | Default |
|---|---|---|
| `report_periods` | Time periods in days | `[1, 7, 30]` |
| `language` | UI language (`zh` or `en`) | `zh` |
| `max_articles_per_account` | Max articles to fetch per feed | `50` |
| `request.timeout` | HTTP timeout in seconds | `30` |
| `request.retry_count` | Number of retries | `3` |
| `output.directory` | Report output directory | `output` |
| `output.keep_history` | Keep timestamped reports | `true` |
| `output.max_history_days` | Auto-delete reports after N days | `90` |

### Account fields

| Field | Required | Description |
|---|---|---|
| `name` | ✅ | Display name (Chinese OK) |
| `id` | ✅ | Unique English ID (must be unique across all accounts) |
| `rss_url` | ✅ | RSS feed URL |
| `tags` | ❌ | Categories for filtering |
| `priority` | ❌ | `high`, `medium`, or `low` (affects sort order) |

## How the Cache Works

On each run:

1. **Load** `output/articles_cache.json` from the previous run
2. **Fetch** fresh articles from all RSS feeds
3. **Merge** — fresh articles override cached ones on hash collision; articles older than 45 days are pruned; new articles are marked with amber badges
4. **Track health** — per-account fetch success/failure, stale-feed detection, degraded-feed warnings
5. **Save** the merged set + health data back to cache
6. **Generate** HTML from the full merged set

The cache persists via two mechanisms: GitHub Actions cache (fast, 7-day retention) as primary, and GitHub Artifacts (90-day retention) as durable fallback. No automated commits are made to your `main` branch. If both caches miss (e.g., first run), the tool degrades gracefully to showing only what the current RSS fetch provides.

## Troubleshooting

**30-day view only shows recent articles?**
- The cache needs a few daily runs to accumulate. After 30 days of daily runs, it will be fully populated.
- Check GitHub Actions logs for "Restored from Artifact" or "cache restored" messages.

**No articles showing up?**
- Verify your RSS URLs work: `curl -s "YOUR_RSS_URL" | head -50`
- Run `python main.py --dry-run` to see fetch results
- Check GitHub Actions logs for error messages

**Red "stale feed" warnings in the report?**
- The feed URL may be broken or rate-limited. Try opening it in a browser.
- For free services (feeddd.org, public RSSHub), this is common. Consider self-hosting RSSHub.
- For low-frequency accounts, 3 empty daily runs may be a false alarm.

**GitHub Pages not updating?**
- Ensure the `gh-pages` branch exists (created on first successful run)
- Check Settings → Pages is set to deploy from `gh-pages`

## Known Limitations

- **No importance detection** — articles are ranked by account priority and date, not by topic novelty or cross-account coverage. The "new since last run" badges help, but there's no semantic ranking.
- **No "read" tracking** — the tool marks what's new since the *last run*, not since you last *read* the report. If you skip a day, yesterday's articles lose their "new" badge on the next run.
- **Feed reliability** — depends entirely on your RSS source's uptime. Free services (feeddd.org, public RSSHub) are frequently rate-limited. Self-host RSSHub or use WeRSS for production reliability.
- **Stale-feed false alarms** — low-frequency accounts (e.g., posting once a month) will trigger "feed may be broken" after 3 consecutive daily runs with no new articles. This is a known tradeoff; distinguishing "intentionally quiet" from "silently broken" would require expected-frequency metadata.
- **Deduplication is syntactic, not semantic** — same content reposted under a different title or URL won't be caught as a duplicate.

## License

MIT
