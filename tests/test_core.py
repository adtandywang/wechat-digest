"""
Tests for WeChat Digest critical paths.

Run with:  python -m pytest tests/ -v
Or:        python tests/test_core.py  (standalone)
"""

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

# Allow running from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fetcher import Article, canonicalize_url, _parse_date, _clean_summary, FetchResult
from report import _safe_json_for_script, _build_stale_accounts


class TestCanonicalizeUrl(unittest.TestCase):
    """URL canonicalization — especially WeChat tracking param stripping."""

    def test_empty_url(self):
        self.assertEqual(canonicalize_url(""), "")
        self.assertEqual(canonicalize_url(None), "")

    def test_strips_fragment(self):
        url = "https://example.com/page#section"
        self.assertEqual(canonicalize_url(url), "https://example.com/page")

    def test_preserves_non_wechat_query(self):
        url = "https://example.com/page?id=123&ref=abc"
        self.assertEqual(canonicalize_url(url), "https://example.com/page?id=123&ref=abc")

    def test_wechat_strips_tracking_params(self):
        """The critical fix: WeChat URLs with chksm, scene, etc. should
        produce the same canonical URL as the clean version."""
        clean = ("https://mp.weixin.qq.com/s?"
                 "__biz=MzI3MDk2NzI2Mg==&mid=123&idx=1&sn=abc")
        dirty = (clean + "&chksm=xyz&scene=21&pass_ticket=secret"
                 "&subscene=132&clicktime=1234&enterid=5678")

        canonical_clean = canonicalize_url(clean)
        canonical_dirty = canonicalize_url(dirty)
        self.assertEqual(canonical_clean, canonical_dirty)

    def test_wechat_keeps_identity_params(self):
        url = ("https://mp.weixin.qq.com/s?"
               "__biz=ABC&mid=100&idx=2&sn=def&chksm=xyz")
        result = canonicalize_url(url)
        self.assertIn("__biz=ABC", result)
        self.assertIn("mid=100", result)
        self.assertIn("idx=2", result)
        self.assertIn("sn=def", result)
        self.assertNotIn("chksm", result)

    def test_wechat_deterministic_ordering(self):
        """Same params in different order should produce identical URL."""
        url1 = "https://mp.weixin.qq.com/s?sn=x&__biz=A&mid=1&idx=1"
        url2 = "https://mp.weixin.qq.com/s?__biz=A&idx=1&mid=1&sn=x"
        self.assertEqual(canonicalize_url(url1), canonicalize_url(url2))

    def test_wechat_short_url_passthrough(self):
        """Short WeChat URLs (mp.weixin.qq.com/s/XXX) should be kept."""
        url = "https://mp.weixin.qq.com/s/bqglHqXG6XMnGVbRMjUsGA"
        result = canonicalize_url(url)
        self.assertEqual(result, url)


class TestArticleDedup(unittest.TestCase):
    """Deduplication via content_hash."""

    def test_same_article_same_hash(self):
        a1 = Article(title="Test", url="https://mp.weixin.qq.com/s/abc",
                     published=datetime.now(timezone.utc),
                     account_name="Acc", account_id="acc-1")
        a2 = Article(title="Test", url="https://mp.weixin.qq.com/s/abc",
                     published=datetime.now(timezone.utc),
                     account_name="Acc", account_id="acc-1")
        self.assertEqual(a1.content_hash, a2.content_hash)

    def test_wechat_tracking_doesnt_change_hash(self):
        """Articles with different tracking params should have same hash."""
        base = "https://mp.weixin.qq.com/s?__biz=A&mid=1&idx=1&sn=x"
        a1 = Article(title="Test", url=base,
                     published=datetime.now(timezone.utc),
                     account_name="Acc", account_id="acc-1")
        a2 = Article(title="Test", url=base + "&chksm=abc&scene=21",
                     published=datetime.now(timezone.utc),
                     account_name="Acc", account_id="acc-1")
        self.assertEqual(a1.content_hash, a2.content_hash)

    def test_different_title_different_hash(self):
        a1 = Article(title="Article A", url="https://example.com/1",
                     published=datetime.now(timezone.utc),
                     account_name="Acc", account_id="acc-1")
        a2 = Article(title="Article B", url="https://example.com/1",
                     published=datetime.now(timezone.utc),
                     account_name="Acc", account_id="acc-1")
        self.assertNotEqual(a1.content_hash, a2.content_hash)


class TestArticleSerialization(unittest.TestCase):
    """Cache round-trip: to_dict → from_dict should be lossless."""

    def test_round_trip(self):
        now = datetime.now(timezone.utc)
        original = Article(
            title="测试文章", url="https://example.com/test",
            published=now, account_name="测试号", account_id="test-1",
            summary="This is a test", author="Author",
            tags=["tag1", "tag2"], priority="high", is_new=True,
        )
        restored = Article.from_dict(original.to_dict())
        self.assertEqual(original.title, restored.title)
        self.assertEqual(original.url, restored.url)
        self.assertEqual(original.content_hash, restored.content_hash)
        self.assertEqual(original.published, restored.published)
        self.assertEqual(original.tags, restored.tags)
        self.assertEqual(original.is_new, restored.is_new)

    def test_from_dict_handles_missing_is_new(self):
        """Caches saved before is_new field existed should not break."""
        data = {
            "title": "Old", "url": "https://x.com/old",
            "published": datetime.now(timezone.utc).isoformat(),
            "account_name": "A", "account_id": "a",
        }
        article = Article.from_dict(data)
        self.assertFalse(article.is_new)


class TestDateParsing(unittest.TestCase):
    """Date parsing edge cases."""

    def test_naive_datetime_becomes_utc(self):
        naive = datetime(2024, 6, 15, 10, 30, 0)
        article = Article(title="T", url="https://x.com/1", published=naive,
                          account_name="A", account_id="a")
        self.assertEqual(article.published.tzinfo, timezone.utc)

    def test_offset_aware_converted_to_utc(self):
        """Beijing time (UTC+8) should be converted, not just relabeled."""
        from datetime import timezone as tz
        beijing = tz(timedelta(hours=8))
        # 2024-06-15 18:00 Beijing = 2024-06-15 10:00 UTC
        beijing_time = datetime(2024, 6, 15, 18, 0, 0, tzinfo=beijing)
        article = Article(title="T", url="https://x.com/1", published=beijing_time,
                          account_name="A", account_id="a")
        self.assertEqual(article.published.hour, 10)
        self.assertEqual(article.published.tzinfo, timezone.utc)

    def test_parse_date_returns_none_for_no_date(self):
        """Entries with no date should return None, not datetime.now()."""
        class FakeEntry:
            pass
        result = _parse_date(FakeEntry())
        self.assertIsNone(result)


class TestMerge(unittest.TestCase):
    """Merge logic: cache + fresh, dedup, new-marking, pruning."""

    def _make_article(self, title, days_ago=0, account_id="a", is_new=False):
        return Article(
            title=title,
            url=f"https://example.com/{title.replace(' ', '-')}",
            published=datetime.now(timezone.utc) - timedelta(days=days_ago),
            account_name="Test", account_id=account_id, is_new=is_new,
        )

    def test_fresh_overrides_cached(self):
        from main import merge_articles
        cached = [self._make_article("Dup", days_ago=1)]
        fresh = [self._make_article("Dup", days_ago=1)]
        previous = {a.content_hash for a in cached}
        merged = merge_articles(cached, fresh, previous, max_age_days=30)
        self.assertEqual(len(merged), 1)

    def test_new_articles_marked(self):
        from main import merge_articles
        cached = [self._make_article("Old", days_ago=5)]
        fresh = [self._make_article("Brand New", days_ago=0)]
        previous = {a.content_hash for a in cached}
        merged = merge_articles(cached, fresh, previous, max_age_days=30)
        new_ones = [a for a in merged if a.is_new]
        self.assertEqual(len(new_ones), 1)
        self.assertEqual(new_ones[0].title, "Brand New")

    def test_old_articles_pruned(self):
        from main import merge_articles
        old = self._make_article("Ancient", days_ago=60)
        recent = self._make_article("Recent", days_ago=1)
        merged = merge_articles([old, recent], [], set(), max_age_days=30)
        titles = [a.title for a in merged]
        self.assertIn("Recent", titles)
        self.assertNotIn("Ancient", titles)

    def test_merge_preserves_both_sources(self):
        from main import merge_articles
        cached = [self._make_article("A", days_ago=10)]
        fresh = [self._make_article("B", days_ago=0)]
        merged = merge_articles(cached, fresh, set(), max_age_days=30)
        titles = {a.title for a in merged}
        self.assertEqual(titles, {"A", "B"})


class TestCleanSummary(unittest.TestCase):

    def test_strips_html(self):
        self.assertEqual(
            _clean_summary("<p>Hello <b>world</b></p>"),
            "Hello world"
        )

    def test_unescapes_entities(self):
        self.assertIn("&", _clean_summary("A &amp; B"))

    def test_truncates(self):
        long = "word " * 100
        result = _clean_summary(long, max_len=50)
        self.assertTrue(len(result) <= 55)  # allow for "..."
        self.assertTrue(result.endswith("..."))


class TestSafeJsonForScript(unittest.TestCase):
    """Ensure embedded JSON can't break out of <script> tags."""

    def test_escapes_script_close(self):
        data = {"text": "Hello </script><img src=x onerror=alert(1)>"}
        result = _safe_json_for_script(data)
        self.assertNotIn("</script>", result)
        self.assertIn("<\\/script>", result)

    def test_escapes_line_separators(self):
        data = {"text": "before\u2028after\u2029end"}
        result = _safe_json_for_script(data)
        self.assertNotIn("\u2028", result)
        self.assertNotIn("\u2029", result)

    def test_valid_json_after_escaping(self):
        data = {"title": "测试</script>", "tags": ["a", "b"]}
        result = _safe_json_for_script(data)
        # The escaped string should be valid JS (browsers handle \/ fine)
        unescaped = result.replace("<\\/", "</")
        parsed = json.loads(unescaped)
        self.assertEqual(parsed["title"], "测试</script>")


class TestHealthTracking(unittest.TestCase):

    def test_stale_accounts_detected(self):
        health = {
            "acc-1": {"consecutive_empty": 5, "last_error": "timeout"},
            "acc-2": {"consecutive_empty": 1, "last_error": ""},
        }
        config = {"accounts": [
            {"id": "acc-1", "name": "Stale Account", "rss_url": "http://x", "priority": "high"},
            {"id": "acc-2", "name": "OK Account", "rss_url": "http://y", "priority": "medium"},
        ]}
        stale = _build_stale_accounts(health, config, threshold=3)
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0]["name"], "Stale Account")
        self.assertEqual(stale[0]["priority"], "high")

    def test_no_rss_url_not_warned(self):
        """Accounts without rss_url should not generate stale warnings."""
        health = {"acc-1": {"consecutive_empty": 10}}
        config = {"accounts": [
            {"id": "acc-1", "name": "No Feed", "rss_url": "", "priority": "high"},
        ]}
        stale = _build_stale_accounts(health, config, threshold=3)
        self.assertEqual(len(stale), 0)

    def test_degraded_feed_detected(self):
        """Feeds with >30% skipped entries should show as degraded."""
        health = {
            "acc-1": {
                "consecutive_empty": 0,
                "status": "degraded",
                "entries_skipped": 4,
                "last_error": "",
            },
        }
        config = {"accounts": [
            {"id": "acc-1", "name": "Degraded Feed", "rss_url": "http://x", "priority": "high"},
        ]}
        problems = _build_stale_accounts(health, config, threshold=3)
        self.assertEqual(len(problems), 1)
        self.assertEqual(problems[0]["warning_type"], "degraded")


if __name__ == "__main__":
    unittest.main()
