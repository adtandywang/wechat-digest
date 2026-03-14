#!/usr/bin/env python3
"""
WeChat Digest - Main Entry Point

Key design decisions:
  - Loads previous article cache so the 30-day view survives across runs
  - Marks articles as "new" if they weren't in the previous cache
  - Tracks per-account health: warns when a feed goes silent
  - Writes cache atomically to prevent corruption
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from fetcher import fetch_all, Article, FetchSummary
from report import generate_html, save_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)
_VALID_PRIORITIES = {"high", "medium", "low"}


# ── Config ───────────────────────────────────────────────────────

def load_config(path: str = "config.yaml") -> dict:
    config_path = Path(path)
    if not config_path.exists():
        logger.error(f"Config file not found: {config_path}")
        sys.exit(1)

    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    validate_config(config)
    return config


def validate_config(config: dict):
    """Fail fast on malformed configuration."""
    accounts = config.get("accounts")
    if not accounts:
        logger.error("No accounts defined in config. Add accounts to config.yaml")
        sys.exit(1)

    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    for i, acc in enumerate(accounts):
        name = (acc.get("name") or "").strip()
        account_id = (acc.get("id") or "").strip()
        if not name or not account_id:
            logger.error(f"Account {i} missing 'name' or 'id' field")
            sys.exit(1)
        if account_id in seen_ids:
            logger.error(f"Duplicate account id: {account_id}")
            sys.exit(1)
        if name in seen_names:
            logger.error(f"Duplicate account name: {name}")
            sys.exit(1)
        seen_ids.add(account_id)
        seen_names.add(name)

        priority = (acc.get("priority") or "medium").lower()
        if priority not in _VALID_PRIORITIES:
            logger.error(f"Invalid priority for account '{name}': {priority}")
            sys.exit(1)

    periods = config.get("report_periods", [1, 7, 30])
    if not isinstance(periods, list) or not periods:
        logger.error("report_periods must be a non-empty list of integers")
        sys.exit(1)
    if any((not isinstance(p, int) or p <= 0) for p in periods):
        logger.error("report_periods must only contain positive integers")
        sys.exit(1)


# ── Cache Layer ──────────────────────────────────────────────────

def load_cache(output_dir: str = "output") -> dict:
    """
    Load the full cache file, which contains:
      - articles: list of article dicts
      - health: per-account health tracking
      - fetched_at: timestamp of last run
    """
    cache_path = Path(output_dir) / "articles_cache.json"
    if not cache_path.exists():
        logger.info("No existing cache found (first run?)")
        return {"articles": [], "health": {}, "fetched_at": None}

    try:
        raw = cache_path.read_text(encoding="utf-8")
        data = json.loads(raw)
        logger.info(
            f"Loaded cache: {data.get('count', '?')} articles "
            f"(from {data.get('fetched_at', 'unknown')})"
        )
        return data
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        logger.warning(f"Failed to load cache, starting fresh: {e}")
        return {"articles": [], "health": {}, "fetched_at": None}


def merge_articles(
    cached: list[Article],
    fresh: list[Article],
    previous_hashes: set[str],
    max_age_days: int = 45,
) -> list[Article]:
    """
    Merge cached and fresh articles.
    - Fresh wins on hash collision
    - Marks articles as is_new if not in previous_hashes
    - Prunes articles older than max_age_days
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)

    by_hash: dict[str, Article] = {}
    for article in cached:
        if article.published >= cutoff:
            by_hash[article.content_hash] = article
    for article in fresh:
        if article.published >= cutoff:
            article.is_new = article.content_hash not in previous_hashes
            by_hash[article.content_hash] = article

    merged = sorted(by_hash.values(), key=lambda a: a.published, reverse=True)
    new_count = sum(1 for a in merged if a.is_new)
    logger.info(
        f"Merged: {len(cached)} cached + {len(fresh)} fresh "
        f"→ {len(merged)} unique ({new_count} new since last run)"
    )
    return merged


def update_health(
    previous_health: dict,
    fetch_summary: FetchSummary,
    config: dict,
) -> dict:
    """
    Track per-account feed health across runs.

    Stores for each account_id:
      - last_success: ISO timestamp of last successful fetch with >0 articles
      - last_attempt: ISO timestamp of last fetch attempt
      - consecutive_empty: consecutive runs with 0 fresh articles
      - last_error: most recent error message (if any)
      - status: "ok" | "empty" | "degraded" | "failed" | "no_rss"
      - entries_skipped: entries skipped in last run (no date, no URL, etc.)

    Logs warnings for stale and degraded feeds.
    """
    now = datetime.now(timezone.utc).isoformat()
    health = dict(previous_health)

    for result in fetch_summary.results:
        aid = result.account_id
        entry = health.get(aid, {
            "last_success": None,
            "last_attempt": None,
            "consecutive_empty": 0,
            "last_error": "",
            "status": "ok",
            "entries_skipped": 0,
        })

        entry["last_attempt"] = now
        entry["entries_skipped"] = result.entries_skipped

        if result.error == "no_rss_url":
            entry["status"] = "no_rss"
        elif not result.success:
            entry["last_error"] = result.error
            entry["consecutive_empty"] = entry.get("consecutive_empty", 0) + 1
            entry["status"] = "failed"
        elif len(result.articles) > 0:
            entry["last_success"] = now
            entry["consecutive_empty"] = 0
            entry["last_error"] = ""
            # Degraded if we lost a significant fraction of entries
            if (result.entries_skipped > 0
                    and result.entries_seen > 0
                    and result.entries_skipped / result.entries_seen > 0.3):
                entry["status"] = "degraded"
            else:
                entry["status"] = "ok"
        else:
            # Successful fetch but zero articles
            entry["consecutive_empty"] = entry.get("consecutive_empty", 0) + 1
            entry["status"] = "empty"

        health[aid] = entry

    # Warn about stale feeds
    stale_threshold = 3
    accounts_by_id = {a["id"]: a for a in config.get("accounts", [])}
    stale_warnings = []
    degraded_warnings = []

    for aid, entry in health.items():
        acc = accounts_by_id.get(aid, {})
        if not acc.get("rss_url"):
            continue

        name = acc.get("name", aid)
        priority = acc.get("priority", "medium")

        if entry.get("consecutive_empty", 0) >= stale_threshold:
            runs = entry["consecutive_empty"]
            stale_warnings.append((name, priority, runs, entry.get("last_error", "")))

        if entry.get("status") == "degraded":
            skipped = entry.get("entries_skipped", 0)
            degraded_warnings.append((name, priority, skipped))

    if stale_warnings:
        logger.warning("=" * 50)
        logger.warning("STALE FEED WARNINGS:")
        for name, priority, runs, err in sorted(
            stale_warnings, key=lambda x: x[1] != "high"
        ):
            marker = " ⚠️ HIGH PRIORITY" if priority == "high" else ""
            logger.warning(
                f"  [{name}] {runs} consecutive empty runs{marker}"
                + (f" (last error: {err})" if err else "")
            )
        logger.warning("=" * 50)

    if degraded_warnings:
        logger.warning("DEGRADED FEED WARNINGS (>30% entries skipped):")
        for name, priority, skipped in degraded_warnings:
            logger.warning(f"  [{name}] {skipped} entries skipped (bad date/URL)")

    return health


def save_cache(
    articles: list[Article],
    health: dict,
    output_dir: str = "output",
):
    """Save articles + health data atomically."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    cache_path = out / "articles_cache.json"
    tmp_path = cache_path.with_suffix(".json.tmp")

    data = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "count": len(articles),
        "articles": [a.to_dict() for a in articles],
        "health": health,
    }

    tmp_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp_path.replace(cache_path)
    logger.info(f"Cache saved: {len(articles)} articles + health data")


def cleanup_old_reports(config: dict):
    output_cfg = config.get("output", {})
    if not output_cfg.get("keep_history", False):
        return

    max_days = output_cfg.get("max_history_days", 90)
    output_dir = Path(output_cfg.get("directory", "output"))
    if not output_dir.exists():
        return

    cutoff = datetime.now(timezone.utc).timestamp() - (max_days * 86400)
    for f in output_dir.glob("report_*.html"):
        if f.stat().st_mtime < cutoff:
            f.unlink()
            logger.info(f"Removed old report: {f.name}")


# ── Main ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="WeChat Digest - Article Aggregator")
    parser.add_argument("--config", "-c", default="config.yaml")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output", "-o", default=None)
    args = parser.parse_args()

    logger.info("=" * 50)
    logger.info("WeChat Digest - Starting")
    logger.info("=" * 50)

    config = load_config(args.config)
    accounts = config.get("accounts", [])
    output_dir = config.get("output", {}).get("directory", "output")
    logger.info(f"Loaded {len(accounts)} accounts from config")

    # Step 1: Load previous cache
    cache_data = load_cache(output_dir)
    cached_articles = []
    for a in cache_data.get("articles", []):
        try:
            cached_articles.append(Article.from_dict(a))
        except Exception as e:
            logger.warning(f"Skipping corrupt cached article: {e}")
    previous_hashes = {a.content_hash for a in cached_articles}
    previous_health = cache_data.get("health", {})

    # Step 2: Fetch fresh
    fetch_summary = fetch_all(config)
    fresh_articles = fetch_summary.articles

    if not fresh_articles and not cached_articles:
        logger.warning("No articles fetched and no cache. Check RSS URLs.")
        if not args.dry_run:
            html_content = generate_html([], config, health={})
            save_report(html_content, config, args.output or "index.html")
        return

    if not fresh_articles:
        logger.warning("No fresh articles fetched. Using cached articles only.")

    # Step 3: Merge + mark new
    max_period = max(config.get("report_periods", [30]))
    all_articles = merge_articles(
        cached_articles, fresh_articles, previous_hashes,
        max_age_days=max_period + 15,
    )

    # Step 4: Update health tracking
    health = update_health(previous_health, fetch_summary, config)

    # Step 5: Save cache (critical for next run)
    save_cache(all_articles, health, output_dir)

    if args.dry_run:
        logger.info("[DRY RUN] Skipping report generation")
        new_articles = [a for a in all_articles if a.is_new]
        logger.info(f"  {len(new_articles)} new articles since last run:")
        for a in new_articles[:15]:
            logger.info(f"    {a.published.strftime('%m-%d')} [{a.account_name}] {a.title}")
        if len(new_articles) > 15:
            logger.info(f"    ... and {len(new_articles) - 15} more")
        return

    # Step 6: Generate report
    html_content = generate_html(all_articles, config, health=health)
    save_report(html_content, config, args.output or "index.html")
    cleanup_old_reports(config)
    logger.info("Done!")


if __name__ == "__main__":
    main()
