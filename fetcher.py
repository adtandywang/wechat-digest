"""
WeChat Digest - RSS Feed Fetcher
Fetches and parses articles from RSS feeds for WeChat public accounts.
"""

import html
import time
import logging
import hashlib
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit, urlunsplit, parse_qs, urlencode

import feedparser
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

# Query parameters that uniquely identify a WeChat article.
# Everything else (chksm, scene, pass_ticket, subscene, etc.)
# is volatile tracking noise that changes between sessions.
_WECHAT_IDENTITY_PARAMS = frozenset({"__biz", "mid", "idx", "sn"})


@dataclass
class Article:
    """Represents a single WeChat article."""
    title: str
    url: str
    published: datetime
    account_name: str
    account_id: str
    summary: str = ""
    author: str = ""
    tags: list[str] = field(default_factory=list)
    priority: str = "medium"
    content_hash: str = ""
    is_new: bool = False  # Set by merge logic: True if not in previous cache

    def __post_init__(self):
        if self.published.tzinfo is None:
            self.published = self.published.replace(tzinfo=timezone.utc)
        else:
            self.published = self.published.astimezone(timezone.utc)

        self.title = (self.title or "Untitled").strip()
        self.url = canonicalize_url(self.url)
        self.summary = (self.summary or "").strip()
        self.author = (self.author or "").strip()
        self.priority = (self.priority or "medium").lower()

        if not self.content_hash:
            raw = f"{self.title}|{self.url}|{self.account_id}"
            self.content_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "published": self.published.isoformat(),
            "account_name": self.account_name,
            "account_id": self.account_id,
            "summary": self.summary,
            "author": self.author,
            "tags": self.tags,
            "priority": self.priority,
            "content_hash": self.content_hash,
            "is_new": self.is_new,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Article":
        data = dict(data)
        if isinstance(data.get("published"), str):
            data["published"] = datetime.fromisoformat(data["published"])
        # Handle caches saved before is_new existed
        data.setdefault("is_new", False)
        return cls(**data)


def canonicalize_url(url: str) -> str:
    """
    Normalize a URL for stable deduplication.

    For WeChat URLs specifically, strips volatile tracking parameters
    (chksm, scene, pass_ticket, etc.) and keeps only the identity
    parameters (__biz, mid, idx, sn). This prevents the same article
    from generating different hashes when tracking tokens rotate.
    """
    if not url:
        return ""
    parts = urlsplit(url.strip())

    if "mp.weixin.qq.com" in (parts.netloc or ""):
        qs = parse_qs(parts.query, keep_blank_values=False)
        filtered = {k: v for k, v in qs.items() if k in _WECHAT_IDENTITY_PARAMS}
        # Sort keys for deterministic output
        new_query = urlencode(sorted(filtered.items()), doseq=True)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, ""))

    # Non-WeChat URLs: strip fragment only
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def _create_session(config: dict) -> requests.Session:
    """Create a requests session with retry logic."""
    req_config = config.get("request", {})
    session = requests.Session()

    retry_strategy = Retry(
        total=req_config.get("retry_count", 3),
        backoff_factor=max(req_config.get("retry_delay", 1), 0),
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "HEAD"]),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    session.headers.update({
        "User-Agent": req_config.get(
            "user_agent",
            "Mozilla/5.0 (compatible; WeChatDigest/1.0)"
        )
    })
    return session


def _parse_date(entry) -> datetime | None:
    """
    Extract and parse publish date from a feed entry.
    Uses astimezone() so offset-aware timestamps are converted correctly.

    Returns None if no date can be parsed — caller decides whether to
    skip the entry or use a fallback. Returning datetime.now() here
    would silently place undated old articles into the 1-day window.
    """
    for attr in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, attr, None)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue

    for attr in ("published", "updated"):
        raw = getattr(entry, attr, None)
        if raw:
            try:
                from dateutil.parser import parse as dateutil_parse
                parsed_dt = dateutil_parse(raw)
                if parsed_dt.tzinfo is None:
                    return parsed_dt.replace(tzinfo=timezone.utc)
                return parsed_dt.astimezone(timezone.utc)
            except (ImportError, ValueError, TypeError, OverflowError):
                continue

    return None


def _clean_summary(raw: str, max_len: int = 300) -> str:
    """Strip HTML tags, unescape entities, and truncate."""
    import re
    text = html.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        text = text[:max_len].rsplit(" ", 1)[0] + "..."
    return text


@dataclass
class FetchResult:
    """Result of fetching one account, including health metadata."""
    account_id: str
    account_name: str
    articles: list[Article]
    success: bool
    error: str = ""
    entries_seen: int = 0       # Total entries in feed
    entries_parsed: int = 0     # Successfully parsed into Articles
    entries_skipped: int = 0    # Skipped (no URL, no date, parse error)


def fetch_account(
    account: dict,
    config: dict,
    session: Optional[requests.Session] = None,
) -> FetchResult:
    """
    Fetch articles from a single account's RSS feed.
    Returns a FetchResult with articles and health metadata.
    """
    name = account["name"]
    account_id = account["id"]
    rss_url = account.get("rss_url", "")

    if not rss_url:
        return FetchResult(
            account_id=account_id, account_name=name,
            articles=[], success=True, error="no_rss_url",
        )

    if session is None:
        session = _create_session(config)

    timeout = config.get("request", {}).get("timeout", 30)
    max_articles = config.get("max_articles_per_account", 50)

    logger.info(f"[{name}] Fetching from {rss_url}")

    try:
        resp = session.get(rss_url, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"[{name}] Failed to fetch RSS: {e}")
        return FetchResult(
            account_id=account_id, account_name=name,
            articles=[], success=False, error=str(e),
        )

    feed = feedparser.parse(resp.content)

    if feed.bozo and not feed.entries:
        err = str(feed.bozo_exception)
        logger.warning(f"[{name}] Feed parse error: {err}")
        return FetchResult(
            account_id=account_id, account_name=name,
            articles=[], success=False, error=f"parse_error: {err}",
        )

    articles: list[Article] = []
    entries_seen = len(feed.entries[:max_articles])
    entries_skipped = 0
    for entry in feed.entries[:max_articles]:
        try:
            title = getattr(entry, "title", None) or "Untitled"
            link = canonicalize_url(getattr(entry, "link", "") or "")
            if not link:
                logger.warning(f"[{name}] Skipping entry without URL: {title!r}")
                entries_skipped += 1
                continue

            published = _parse_date(entry)
            if published is None:
                logger.warning(f"[{name}] Skipping entry with unparseable date: {title!r}")
                entries_skipped += 1
                continue

            summary_raw = getattr(entry, "summary", "") or ""
            author = getattr(entry, "author", "") or ""

            article = Article(
                title=title,
                url=link,
                published=published,
                account_name=name,
                account_id=account_id,
                summary=_clean_summary(summary_raw),
                author=author,
                tags=list(account.get("tags", [])),
                priority=account.get("priority", "medium"),
            )
            articles.append(article)
        except Exception as e:
            logger.warning(f"[{name}] Failed to parse entry: {e}")
            entries_skipped += 1
            continue

    articles.sort(key=lambda a: a.published, reverse=True)
    entries_parsed = len(articles)
    if entries_skipped > 0:
        logger.info(f"[{name}] Fetched {entries_parsed} articles ({entries_skipped} entries skipped)")
    else:
        logger.info(f"[{name}] Fetched {entries_parsed} articles")
    return FetchResult(
        account_id=account_id, account_name=name,
        articles=articles, success=True,
        entries_seen=entries_seen, entries_parsed=entries_parsed,
        entries_skipped=entries_skipped,
    )


@dataclass
class FetchSummary:
    """Summary of a full fetch run, including per-account health."""
    articles: list[Article]
    results: list[FetchResult]

    @property
    def failed_accounts(self) -> list[FetchResult]:
        return [r for r in self.results if not r.success]

    @property
    def empty_accounts(self) -> list[FetchResult]:
        return [r for r in self.results
                if r.success and not r.error and len(r.articles) == 0]


def fetch_all(config: dict) -> FetchSummary:
    """
    Fetch articles from all configured accounts.
    Returns FetchSummary with articles and per-account health data.
    """
    accounts = config.get("accounts", [])
    if not accounts:
        logger.warning("No accounts configured in config.yaml")
        return FetchSummary(articles=[], results=[])

    session = _create_session(config)
    all_articles: list[Article] = []
    all_results: list[FetchResult] = []

    for i, account in enumerate(accounts):
        result = fetch_account(account, config, session)
        all_results.append(result)
        all_articles.extend(result.articles)

        if i < len(accounts) - 1:
            time.sleep(1)

    # Deduplicate by content_hash
    seen: set[str] = set()
    unique: list[Article] = []
    for article in all_articles:
        if article.content_hash not in seen:
            seen.add(article.content_hash)
            unique.append(article)

    unique.sort(key=lambda a: a.published, reverse=True)

    failed = [r for r in all_results if not r.success]
    if failed:
        logger.warning(
            f"Failed to fetch {len(failed)} account(s): "
            + ", ".join(r.account_name for r in failed)
        )

    logger.info(f"Total: {len(unique)} unique articles from {len(accounts)} accounts")
    return FetchSummary(articles=unique, results=all_results)
