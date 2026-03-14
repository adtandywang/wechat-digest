#!/usr/bin/env python3
"""
WeChat Digest - Feed Setup Helper
==================================
This script helps you find RSS feed URLs for your WeChat accounts.

It attempts multiple strategies:
  1. Extract __biz parameter from sample article pages → build RSSHub URL
  2. Search feeddd.org for matching feeds
  3. Output a report of what was found and what needs manual setup

Usage:
    python setup_feeds.py                    # Interactive mode
    python setup_feeds.py --auto             # Auto-fill config.yaml
    python setup_feeds.py --dry-run          # Show what would be found
    python setup_feeds.py --rsshub-base URL  # Use custom RSSHub instance
"""

import argparse
import re
import sys
import time
import logging
from pathlib import Path
from urllib.parse import unquote

import requests
import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Default RSSHub instances (try in order) ──────────────────────
RSSHUB_INSTANCES = [
    "https://rsshub.app",
    "https://rsshub.rssforever.com",
    "https://rsshub.moeyy.cn",
]


def extract_biz_from_url(url: str, session: requests.Session) -> str | None:
    """
    Fetch a WeChat article page and extract the __biz parameter.
    The __biz identifies the public account uniquely.
    """
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }
        resp = session.get(url, headers=headers, timeout=15, allow_redirects=True)
        resp.raise_for_status()
        text = resp.text

        # Method 1: JavaScript variable
        patterns = [
            r'var\s+biz\s*=\s*["\']([^"\']+)["\']',
            r'__biz\s*=\s*([A-Za-z0-9=+/]+)',
            r'__biz=([A-Za-z0-9%=+/]+)',
            r'"biz"\s*:\s*"([^"]+)"',
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                biz = unquote(match.group(1)).strip()
                if len(biz) > 5:  # sanity check
                    return biz

        return None

    except Exception as e:
        logger.warning(f"  Failed to fetch {url}: {e}")
        return None


def verify_rsshub_feed(biz: str, rsshub_base: str, session: requests.Session) -> bool:
    """Check if an RSSHub feed URL returns valid content."""
    feed_url = f"{rsshub_base}/wechat/mp/{biz}"
    try:
        resp = session.get(feed_url, timeout=10)
        # RSSHub returns XML for valid feeds
        if resp.status_code == 200 and ("<rss" in resp.text or "<feed" in resp.text):
            return True
    except Exception:
        pass
    return False


def find_rsshub_url(biz: str, session: requests.Session, custom_base: str = None) -> str | None:
    """Try multiple RSSHub instances to find a working feed."""
    instances = [custom_base] if custom_base else RSSHUB_INSTANCES

    for base in instances:
        if verify_rsshub_feed(biz, base, session):
            return f"{base}/wechat/mp/{biz}"
    return None


def search_feeddd(account_name: str, session: requests.Session) -> str | None:
    """
    Search feeddd.org for a matching feed.
    Note: feeddd.org's API/structure may change; this is best-effort.
    """
    try:
        # feeddd.org provides a feed list page
        resp = session.get(
            "https://feeddd.org/feeds",
            params={"q": account_name},
            timeout=10,
        )
        if resp.status_code == 200:
            # Look for feed URLs in the response
            matches = re.findall(
                r'(https://feeddd\.org/feeds/[a-zA-Z0-9_-]+)',
                resp.text,
            )
            if matches:
                return matches[0]
    except Exception:
        pass
    return None


def process_accounts(config: dict, args) -> dict:
    """
    Process all accounts and try to find RSS feed URLs.

    Returns a summary dict with results.
    """
    session = requests.Session()
    accounts = config.get("accounts", [])
    rsshub_base = args.rsshub_base if hasattr(args, "rsshub_base") else None

    results = {
        "found_biz": [],
        "found_feed": [],
        "not_found": [],
        "skipped": [],
    }

    total = len(accounts)
    for i, account in enumerate(accounts):
        name = account["name"]
        sample_url = account.get("sample_url", "")
        existing_rss = account.get("rss_url", "")

        logger.info(f"[{i+1}/{total}] {name}")

        # Skip if already has RSS URL
        if existing_rss:
            logger.info(f"  Already has RSS URL, skipping")
            results["skipped"].append(name)
            continue

        if not sample_url:
            logger.warning(f"  No sample URL, skipping")
            results["not_found"].append({"name": name, "reason": "no sample_url"})
            continue

        # Step 1: Extract biz_id from sample article
        biz = extract_biz_from_url(sample_url, session)
        if biz:
            logger.info(f"  Found biz_id: {biz}")
            results["found_biz"].append({"name": name, "biz": biz})

            # Step 2: Try to find a working RSSHub feed
            feed_url = find_rsshub_url(biz, session, rsshub_base)
            if feed_url:
                logger.info(f"  Found working feed: {feed_url}")
                account["rss_url"] = feed_url
                results["found_feed"].append({"name": name, "url": feed_url})
            else:
                # Store the RSSHub URL anyway (user may self-host)
                default_url = f"{rsshub_base or RSSHUB_INSTANCES[0]}/wechat/mp/{biz}"
                account["rss_url"] = default_url
                logger.info(f"  No verified feed, using default: {default_url}")
                results["found_feed"].append({
                    "name": name, "url": default_url, "unverified": True,
                })
        else:
            logger.warning(f"  Could not extract biz_id")

            # Step 3: Try feeddd.org search
            feeddd_url = search_feeddd(name, session)
            if feeddd_url:
                logger.info(f"  Found on feeddd.org: {feeddd_url}")
                account["rss_url"] = feeddd_url
                results["found_feed"].append({"name": name, "url": feeddd_url})
            else:
                results["not_found"].append({"name": name, "reason": "no biz_id found"})

        # Rate limiting
        time.sleep(2)

    return results


def print_report(results: dict):
    """Print a summary of the setup process."""
    print("\n" + "=" * 60)
    print("  SETUP REPORT")
    print("=" * 60)

    found = results["found_feed"]
    not_found = results["not_found"]
    skipped = results["skipped"]

    print(f"\n  Found feeds:   {len(found)}")
    print(f"  Not found:     {len(not_found)}")
    print(f"  Skipped:       {len(skipped)}")

    if found:
        print("\n  ✅ FEEDS FOUND:")
        for item in found:
            verified = "" if not item.get("unverified") else " (unverified)"
            print(f"     {item['name']}: {item['url']}{verified}")

    if not_found:
        print("\n  ❌ NEEDS MANUAL SETUP:")
        for item in not_found:
            print(f"     {item['name']} — {item['reason']}")
            print(f"       → Try searching on https://feeddd.org/feed/list")
            print(f"       → Or use WeRSS (https://werss.app)")

    if skipped:
        print("\n  ⏭  SKIPPED (already configured):")
        for name in skipped:
            print(f"     {name}")

    print("\n" + "=" * 60)


def main():
    parser = argparse.ArgumentParser(description="WeChat Digest - Feed Setup Helper")
    parser.add_argument(
        "--config", "-c",
        default="config.yaml",
        help="Path to config file",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Auto-update config.yaml with found feeds",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show results without modifying config",
    )
    parser.add_argument(
        "--rsshub-base",
        default=None,
        help="Custom RSSHub instance URL (e.g., https://your-rsshub.com)",
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Error: Config file not found: {config_path}")
        sys.exit(1)

    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    print(f"Processing {len(config.get('accounts', []))} accounts...")
    print(f"This will take a few minutes (rate-limited to be polite).\n")

    results = process_accounts(config, args)
    print_report(results)

    # Save updated config
    if args.auto and not args.dry_run:
        print("\n⚠️  WARNING: --auto uses PyYAML's yaml.dump which DESTROYS")
        print("   all comments, formatting, and section headers in config.yaml.")
        print("   A backup will be saved, but you'll lose your organized layout.")
        confirm = input("   Continue? [y/N] ").strip().lower()
        if confirm != "y":
            print("   Aborted. Copy the URLs from above into config.yaml manually.")
            return

        backup_path = config_path.with_suffix(".yaml.bak")
        backup_path.write_text(config_path.read_text(encoding="utf-8"), encoding="utf-8")
        logger.info(f"Backup saved to {backup_path}")

        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        print(f"\n✅ Config updated: {config_path}")
        print(f"   Backup: {backup_path}")
    elif not args.dry_run:
        print(f"\n📋 RECOMMENDED: Copy the URLs above into config.yaml by hand.")
        print(f"   (This preserves your comments and formatting.)")
        print(f"   Or run with --auto to overwrite config.yaml (will lose comments).")


if __name__ == "__main__":
    main()
