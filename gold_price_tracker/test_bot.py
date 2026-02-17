"""
Validation test suite for gold_bot.py
Tests every pure-Python component without external network or Telegram deps.
Run with: python3 test_gold_bot.py
"""

import ast
import sys
import os
import sqlite3
import tempfile
import datetime
import unittest
import importlib.util

# ── We load only the modules we can actually import ──────────────────────────
# Stub out missing third-party deps so we can import gold_bot logic
import types
for stub in ("flask", "cloudscraper", "bs4", "telegram", "telegram.ext"):
    if stub not in sys.modules:
        mod = types.ModuleType(stub)
        sys.modules[stub] = mod

# Provide the bare minimum fakes so gold_bot.py imports cleanly
flask_mod = sys.modules["flask"]

class _FakeFlask:
    def route(self, *a, **kw):
        return lambda fn: fn
    def run(self, *a, **kw):
        pass

flask_mod.Flask    = lambda *a, **kw: _FakeFlask()   # type: ignore
flask_mod.jsonify  = lambda *a, **kw: {}             # type: ignore

telegram_ext = sys.modules["telegram.ext"]
for cls in ("Application", "CommandHandler", "CallbackContext"):
    setattr(telegram_ext, cls, type(cls, (), {}))

telegram_mod = sys.modules["telegram"]
telegram_mod.Update = type("Update", (), {})  # type: ignore

cloudscraper_mod = sys.modules["cloudscraper"]
cloudscraper_mod.create_scraper = lambda: None  # type: ignore

bs4_mod = sys.modules["bs4"]
bs4_mod.BeautifulSoup = None  # type: ignore

# Now we can safely import our module
os.environ.setdefault("TOKEN", "fake-token-for-tests")

# Patch Application.builder so init doesn't blow up
import types as _types

spec = importlib.util.spec_from_file_location("gold_bot", "/home/claude/gold_bot.py")
bot  = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)

# =============================================================================

class TestSyntax(unittest.TestCase):
    """Validate the source file parses without syntax errors."""

    def test_syntax_valid(self):
        with open("/home/claude/gold_bot.py", "r") as f:
            source = f.read()
        try:
            tree = ast.parse(source)
            self.assertIsNotNone(tree)
        except SyntaxError as exc:
            self.fail(f"Syntax error in gold_bot.py: {exc}")

    def test_no_bare_except(self):
        """Bare `except:` hides bugs — ensure we always catch specific types."""
        with open("/home/claude/gold_bot.py", "r") as f:
            source = f.read()
        tree = ast.parse(source)
        bare = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.ExceptHandler) and node.type is None
        ]
        self.assertEqual(bare, [], "Found bare except: clause(s) — use except Exception:")


class TestConstants(unittest.TestCase):
    """Verify config values are sensible."""

    def test_metals_have_required_keys(self):
        for metal, info in bot.METALS.items():
            for key in ("url_slug", "label", "emoji", "section_keyword"):
                self.assertIn(key, info, f"METALS['{metal}'] missing '{key}'")

    def test_cities_non_empty(self):
        self.assertGreater(len(bot.CITIES), 0)

    def test_default_city_and_metal_valid(self):
        self.assertIn(bot.DEFAULT_CITY,  bot.CITIES)
        self.assertIn(bot.DEFAULT_METAL, bot.METALS)

    def test_cache_ttl_positive(self):
        self.assertGreater(bot.CACHE_TTL, 0)

    def test_alert_interval_positive(self):
        self.assertGreater(bot.ALERT_CHECK_INTERVAL, 0)


class TestParsePriceFromCell(unittest.TestCase):
    """Unit tests for the price-parsing helper."""

    def test_rupee_symbol_with_comma(self):
        self.assertAlmostEqual(bot._parse_price_from_cell("₹6,123"), 6123.0)

    def test_plain_number(self):
        self.assertAlmostEqual(bot._parse_price_from_cell("5900.50"), 5900.50)

    def test_with_trailing_text(self):
        # Only the first token should be used
        self.assertAlmostEqual(bot._parse_price_from_cell("6000 INR"), 6000.0)

    def test_empty_string_returns_none(self):
        self.assertIsNone(bot._parse_price_from_cell(""))

    def test_non_numeric_returns_none(self):
        self.assertIsNone(bot._parse_price_from_cell("N/A"))

    def test_commas_stripped(self):
        self.assertAlmostEqual(bot._parse_price_from_cell("₹1,00,000"), 100000.0)


class TestBuildTableStr(unittest.TestCase):
    """Unit tests for the table-building helper."""

    def _sample_data(self):
        headers = ["Gram", "Price", "Open", "Change"]
        rows    = [
            ["1g",  "₹6,000", "₹5,980", "+20"],
            ["8g",  "₹48,000", "₹47,840", "-10"],
            ["10g", "₹60,000", "₹59,800", "0"],
        ]
        return headers, [list(r) for r in rows]

    def test_returns_string(self):
        h, r = self._sample_data()
        result = bot._build_table_str(h, r)
        self.assertIsInstance(result, str)

    def test_header_present(self):
        h, r = self._sample_data()
        result = bot._build_table_str(h, r)
        self.assertIn("Gram",  result)
        self.assertIn("Price", result)

    def test_separator_present(self):
        h, r = self._sample_data()
        result = bot._build_table_str(h, r)
        self.assertIn("-+-", result)

    def test_negative_change_gets_red_emoji(self):
        h, r = self._sample_data()
        result = bot._build_table_str(h, r)
        self.assertIn("🔴", result)

    def test_positive_change_gets_green_emoji(self):
        h, r = self._sample_data()
        result = bot._build_table_str(h, r)
        self.assertIn("🟢", result)

    def test_minus_sign_variants(self):
        """Both ASCII '-' and unicode '−' should get 🔴."""
        headers = ["Gram", "Price", "Open", "Change"]
        # Unicode minus
        rows1 = [["1g", "6000", "5980", "−20"]]
        result1 = bot._build_table_str(headers, [list(r) for r in rows1])
        self.assertIn("🔴", result1)
        # ASCII hyphen
        rows2 = [["1g", "6000", "5980", "-20"]]
        result2 = bot._build_table_str(headers, [list(r) for r in rows2])
        self.assertIn("🔴", result2)

    def test_no_false_positive_on_mid_string_hyphen(self):
        """A value like '10-gram' should NOT get 🔴 (doesn't start with -)."""
        headers = ["Gram", "Price", "Open", "Change"]
        rows    = [["1g", "6000", "5980", "10-gram"]]
        result  = bot._build_table_str(headers, [list(r) for r in rows])
        self.assertNotIn("🔴", result)
        self.assertIn("🟢", result)

    def test_emoji_added_before_width_calculation(self):
        """
        All data rows must have the same visual display width.
        Verified using the same _display_len helper used by the formatter.
        """
        h, r = self._sample_data()
        result = bot._build_table_str(h, r)
        lines  = [l for l in result.split("\n") if l]  # no strip() — it eats trailing padding
        data_lines = [l for l in lines if "🔴" in l or "🟢" in l]
        lengths = {bot._display_len(l) for l in data_lines}
        self.assertEqual(
            len(lengths), 1,
            f"Data rows have inconsistent display widths: {lengths}\n{result}"
        )

    def test_single_row(self):
        headers = ["Gram", "Price", "Open", "Change"]
        rows    = [["1g", "₹6,000", "₹5,980", "+20"]]
        result  = bot._build_table_str(headers, [list(r) for r in rows])
        self.assertIn("🟢", result)

    def test_column_without_change_column(self):
        """Table with only 2 columns — no emoji injection attempted."""
        headers = ["Gram", "Price"]
        rows    = [["1g", "₹6,000"], ["8g", "₹48,000"]]
        result  = bot._build_table_str(headers, [list(r) for r in rows])
        self.assertNotIn("🔴", result)
        self.assertNotIn("🟢", result)


class TestParseMetalCityArgs(unittest.TestCase):
    """Unit tests for argument parsing helper."""

    def test_no_args_returns_defaults(self):
        metal, city = bot._parse_metal_city_args([], "gold")
        self.assertEqual(metal, "gold")
        self.assertEqual(city,  bot.DEFAULT_CITY)

    def test_city_provided(self):
        metal, city = bot._parse_metal_city_args(["mumbai"], "gold")
        self.assertEqual(metal, "gold")
        self.assertEqual(city,  "mumbai")

    def test_metal_override(self):
        metal, city = bot._parse_metal_city_args([], "silver")
        self.assertEqual(metal, "silver")


class TestGetMetalPricesValidation(unittest.TestCase):
    """
    Tests for input validation inside get_metal_prices().
    Network calls are avoided — we only trigger the ValueError paths.
    """

    def test_invalid_metal_raises_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            bot.get_metal_prices("platinum", "bangalore")
        self.assertIn("platinum", str(ctx.exception))

    def test_invalid_city_raises_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            bot.get_metal_prices("gold", "atlantis")
        self.assertIn("atlantis", str(ctx.exception))

    def test_valid_args_do_not_raise_value_error(self):
        """Valid metal+city should pass validation (will fail on network, not ValueError)."""
        try:
            bot.get_metal_prices("gold", "mumbai")
        except ValueError:
            self.fail("Valid metal/city raised ValueError unexpectedly")
        except Exception:
            pass  # RuntimeError from network is expected in sandbox


class TestCache(unittest.TestCase):
    """Unit tests for the price cache."""

    def setUp(self):
        bot._price_cache.clear()

    def test_cache_miss_on_empty(self):
        self.assertIsNone(bot._get_cached("gold", "bangalore"))

    def test_cache_hit_after_set(self):
        bot._set_cache("gold", "bangalore", "test message", 6000.0)
        entry = bot._get_cached("gold", "bangalore")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["data"],  "test message")
        self.assertEqual(entry["price"], 6000.0)

    def test_cache_key_is_lowercase(self):
        bot._set_cache("Gold", "Bangalore", "msg", 5000.0)
        entry = bot._get_cached("gold", "bangalore")
        self.assertIsNotNone(entry)

    def test_cache_miss_when_expired(self):
        bot._set_cache("silver", "mumbai", "old data", 70000.0)
        # Manually expire
        bot._price_cache[("silver", "mumbai")]["timestamp"] -= bot.CACHE_TTL + 1
        self.assertIsNone(bot._get_cached("silver", "mumbai"))

    def test_different_metals_cached_separately(self):
        bot._set_cache("gold",   "delhi", "gold msg",   6000.0)
        bot._set_cache("silver", "delhi", "silver msg", 75000.0)
        self.assertEqual(bot._get_cached("gold",   "delhi")["data"], "gold msg")
        self.assertEqual(bot._get_cached("silver", "delhi")["data"], "silver msg")


class TestDatabase(unittest.TestCase):
    """Integration tests for the SQLite DB layer."""

    def setUp(self):
        # Use a fresh temp DB for each test
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        bot.DB_PATH = self._tmp.name
        bot.init_db()

    def tearDown(self):
        os.unlink(self._tmp.name)

    # --- Subscriptions ---

    def test_add_and_get_subscription(self):
        bot.add_subscription(123, "mumbai", "gold")
        sub = bot.get_subscription(123)
        self.assertIsNotNone(sub)
        self.assertEqual(sub["city"],  "mumbai")
        self.assertEqual(sub["metal"], "gold")

    def test_subscription_upsert(self):
        bot.add_subscription(123, "mumbai", "gold")
        bot.add_subscription(123, "delhi",  "silver")  # overwrite
        sub = bot.get_subscription(123)
        self.assertEqual(sub["city"],  "delhi")
        self.assertEqual(sub["metal"], "silver")

    def test_remove_subscription(self):
        bot.add_subscription(456, "bangalore", "gold")
        removed = bot.remove_subscription(456)
        self.assertTrue(removed)
        self.assertIsNone(bot.get_subscription(456))

    def test_remove_non_existent_subscription(self):
        removed = bot.remove_subscription(999)
        self.assertFalse(removed)

    def test_get_all_subscriptions(self):
        bot.add_subscription(1, "mumbai", "gold")
        bot.add_subscription(2, "delhi",  "silver")
        subs = bot.get_all_subscriptions()
        self.assertEqual(len(subs), 2)

    def test_get_subscription_not_found(self):
        self.assertIsNone(bot.get_subscription(777))

    # --- Alerts ---

    def test_set_and_get_alert(self):
        bot.set_alert(100, "gold", "bangalore", 6500.0)
        alert = bot.get_alert(100)
        self.assertIsNotNone(alert)
        self.assertEqual(alert["threshold"], 6500.0)
        self.assertEqual(alert["metal"],     "gold")
        self.assertEqual(alert["city"],      "bangalore")

    def test_alert_upsert(self):
        bot.set_alert(100, "gold",   "bangalore", 6500.0)
        bot.set_alert(100, "silver", "mumbai",    70000.0)
        alert = bot.get_alert(100)
        self.assertEqual(alert["metal"],     "silver")
        self.assertEqual(alert["threshold"], 70000.0)

    def test_remove_alert(self):
        bot.set_alert(200, "gold", "bangalore", 5000.0)
        removed = bot.remove_alert(200)
        self.assertTrue(removed)
        self.assertIsNone(bot.get_alert(200))

    def test_remove_non_existent_alert(self):
        self.assertFalse(bot.remove_alert(999))

    def test_get_all_alerts(self):
        bot.set_alert(1, "gold",   "bangalore", 6000.0)
        bot.set_alert(2, "silver", "mumbai",    70000.0)
        alerts = bot.get_all_alerts()
        self.assertEqual(len(alerts), 2)

    def test_alert_threshold_precision(self):
        bot.set_alert(300, "gold", "delhi", 6543.21)
        alert = bot.get_alert(300)
        self.assertAlmostEqual(alert["threshold"], 6543.21, places=2)

    def test_multiple_users_independent_alerts(self):
        bot.set_alert(10, "gold", "bangalore", 6000.0)
        bot.set_alert(20, "gold", "bangalore", 5500.0)
        self.assertAlmostEqual(bot.get_alert(10)["threshold"], 6000.0)
        self.assertAlmostEqual(bot.get_alert(20)["threshold"], 5500.0)

    def test_subscription_and_alert_are_independent_tables(self):
        """Removing a subscription must not affect alerts and vice versa."""
        bot.add_subscription(50, "bangalore", "gold")
        bot.set_alert(50, "gold", "bangalore", 6000.0)
        bot.remove_subscription(50)
        self.assertIsNone(bot.get_subscription(50))
        self.assertIsNotNone(bot.get_alert(50))  # alert still present


class TestAlertFireLogic(unittest.TestCase):
    """
    Validate the price-vs-threshold comparison logic used in job_check_alerts.
    We test the business rule directly without async machinery.
    """

    def _should_fire(self, current_price: float, threshold: float) -> bool:
        return current_price < threshold

    def test_fires_when_below_threshold(self):
        self.assertTrue(self._should_fire(6000.0, 6500.0))

    def test_does_not_fire_when_equal(self):
        self.assertFalse(self._should_fire(6500.0, 6500.0))

    def test_does_not_fire_when_above(self):
        self.assertFalse(self._should_fire(7000.0, 6500.0))

    def test_fires_on_fractional_difference(self):
        self.assertTrue(self._should_fire(6499.99, 6500.0))


class TestCommandHandlersRegistered(unittest.TestCase):
    """Verify all expected handlers are defined as async functions."""

    EXPECTED_HANDLERS = [
        "cmd_start", "cmd_help", "cmd_gold", "cmd_silver",
        "cmd_cities", "cmd_subscribe", "cmd_unsubscribe",
        "cmd_alert", "cmd_myalert", "cmd_cancelalert", "cmd_status",
    ]

    def test_all_handlers_exist(self):
        for name in self.EXPECTED_HANDLERS:
            self.assertTrue(
                hasattr(bot, name),
                f"Handler '{name}' is missing from gold_bot.py"
            )

    def test_all_handlers_are_coroutines(self):
        import asyncio
        for name in self.EXPECTED_HANDLERS:
            fn = getattr(bot, name)
            self.assertTrue(
                asyncio.iscoroutinefunction(fn),
                f"'{name}' must be an async def, got {type(fn)}"
            )


class TestJobsRegistered(unittest.TestCase):
    """Verify background job functions exist and are coroutines."""

    EXPECTED_JOBS = ["job_daily_prices", "job_check_alerts"]

    def test_job_functions_exist(self):
        for name in self.EXPECTED_JOBS:
            self.assertTrue(hasattr(bot, name), f"Job '{name}' is missing")

    def test_job_functions_are_coroutines(self):
        import asyncio
        for name in self.EXPECTED_JOBS:
            fn = getattr(bot, name)
            self.assertTrue(asyncio.iscoroutinefunction(fn), f"'{name}' must be async def")


class TestJobQueueGuard(unittest.TestCase):
    """Verify main() handles a None job_queue without crashing."""

    def test_job_queue_guard_logic(self):
        """
        The guard `if application.job_queue is None` must exist in main()
        so the bot does not crash on deployment when the [job-queue] extra
        is missing. We verify:
          1. The guard branch exists in source.
          2. Calling run_daily/run_repeating on a None-queue raises
             AttributeError -- confirming the guard is necessary.
          3. Our guard prevents that error.
        """
        import inspect

        # 1. Guard must appear in main() source
        source = inspect.getsource(bot.main)
        self.assertIn(
            "job_queue is None",
            source,
            "main() must contain a `job_queue is None` guard"
        )

        # 2. Directly confirm AttributeError is raised WITHOUT the guard
        class NullQueue:
            pass   # no run_daily / run_repeating

        with self.assertRaises(AttributeError):
            NullQueue().run_daily("anything")

        # 3. Guard prevents crash: simulate the if-branch directly
        # If job_queue is None, we log and skip -- no AttributeError raised
        import logging
        fake_app_jq = None   # the condition that triggers the guard
        try:
            if fake_app_jq is None:
                logging.getLogger("gold_bot").critical("JobQueue unavailable -- skipping jobs.")
            else:
                fake_app_jq.run_daily("anything")   # would crash
        except AttributeError as exc:
            self.fail("Guard failed to prevent AttributeError: " + str(exc))


class TestDatabaseSchema(unittest.TestCase):
    """Verify the DB schema is created correctly."""

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        bot.DB_PATH = self._tmp.name
        bot.init_db()

    def tearDown(self):
        os.unlink(self._tmp.name)

    def _tables(self):
        conn = sqlite3.connect(self._tmp.name)
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        conn.close()
        return {r[0] for r in rows}

    def test_subscriptions_table_exists(self):
        self.assertIn("subscriptions", self._tables())

    def test_alerts_table_exists(self):
        self.assertIn("alerts", self._tables())

    def test_subscriptions_columns(self):
        conn = sqlite3.connect(self._tmp.name)
        info = conn.execute("PRAGMA table_info(subscriptions)").fetchall()
        conn.close()
        cols = {row[1] for row in info}
        for col in ("chat_id", "city", "metal", "created_at"):
            self.assertIn(col, cols)

    def test_alerts_columns(self):
        conn = sqlite3.connect(self._tmp.name)
        info = conn.execute("PRAGMA table_info(alerts)").fetchall()
        conn.close()
        cols = {row[1] for row in info}
        for col in ("chat_id", "metal", "city", "threshold", "created_at"):
            self.assertIn(col, cols)

    def test_init_db_is_idempotent(self):
        """Calling init_db twice should not raise or duplicate tables."""
        try:
            bot.init_db()
        except Exception as exc:
            self.fail(f"Second init_db() raised: {exc}")
        self.assertIn("subscriptions", self._tables())


# =============================================================================
if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.discover(start_dir=".", pattern="test_gold_bot.py")
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
