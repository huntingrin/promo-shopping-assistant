"""Pure helper tests for the optional browser adapter, plus an optional mock page."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "browser_cart", ROOT / "optional/browser/browser_cart.py")
adapter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(adapter)


class BrowserAdapterHelperTests(unittest.TestCase):
    def test_price_kind_mapping(self):
        self.assertEqual(adapter.infer_price_kind("活动价"), ("before_order_promotions", False))
        self.assertEqual(adapter.infer_price_kind("购物车展示价"), ("before_order_promotions", True))
        self.assertEqual(adapter.infer_price_kind("预估到手价"), ("after_coupons", False))
        self.assertEqual(adapter.infer_price_kind("尾款"), ("tail_payment", False))
        self.assertEqual(adapter.infer_price_kind(""), ("unknown", False))
        self.assertEqual(adapter.infer_price_kind("", explicit="unknown"), ("unknown", False))

    def test_price_parsing_and_candidates(self):
        self.assertEqual(adapter.parse_price("活动价 ¥1,299.00"), "1299.00")
        self.assertEqual(adapter.parse_price("券后价 88.5"), "88.5")
        self.assertIsNone(adapter.parse_price("没有金额"))
        self.assertEqual(adapter.parse_amounts("原价 ¥199 活动价 ¥180"), ["199", "180"])

    def test_redaction_removes_personal_data(self):
        text = "电话13800138000，邮箱a@example.com，地址：上海市某路1号，链接?token=secret"
        redacted = adapter.redact_text(text)
        self.assertNotIn("13800138000", redacted)
        self.assertNotIn("a@example.com", redacted)
        self.assertNotIn("上海市某路1号", redacted)
        self.assertNotIn("secret", redacted)
        self.assertIn("[手机号已隐藏]", redacted)
        self.assertIn("[地址已隐藏]", redacted)

    def test_relevant_lines_and_deduplication(self):
        text = "活动价 ¥199\n无关内容\n活动价 ¥199\n运费 ¥0\n"
        lines = adapter.relevant_lines(text, ("活动价", "运费"))
        self.assertEqual(lines, ["活动价 ¥199", "运费 ¥0"])
        self.assertEqual(adapter.merge_texts(["a", "a"], ["b"]), ["a", "b"])

    def test_stable_ids_are_deterministic_and_safe(self):
        first = adapter.safe_ident("https://item.taobao.com/item.htm?id=10002")
        second = adapter.safe_ident("https://item.taobao.com/item.htm?id=10002")
        self.assertEqual(first, second)
        self.assertNotIn("#", first)

    def test_multiple_unlabelled_prices_are_not_guessed(self):
        warnings, questions = [], []
        item = adapter.make_item({
            "id": "1",
            "title": "台灯",
            "url": "https://item.taobao.com/item.htm?id=1",
            "shop": "B店",
            "price": None,
            "price_text": "¥199.00 / ¥299.00",
            "price_candidates": ["199.00", "299.00"],
            "price_label": "购物车展示价",
        }, warnings, questions)
        self.assertIsNone(item["price"])
        self.assertIn("item:1:price", [q["id"] for q in questions])

    def test_same_item_url_with_different_skus_gets_different_ids(self):
        base = {"id": "1", "title": "台灯", "shop": "B店", "price": "199.00",
                "price_label": "活动价", "url": "https://item.taobao.com/item.htm?id=1"}
        first = adapter.make_item({**base, "spec": "暖白光"}, [], [])
        second = adapter.make_item({**base, "spec": "冷白光"}, [], [])
        self.assertNotEqual(first["id"], second["id"])


class BrowserAdapterMockPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import playwright.sync_api  # noqa: F401
        except Exception as exc:
            raise unittest.SkipTest(f"Playwright 未安装：{exc}")

    def test_same_item_with_different_skus_is_not_deduplicated(self):
        from playwright.sync_api import sync_playwright
        html = """
        <html><body>
          <div class="cart-item">
            <input type="checkbox" checked>
            <a href="https://shop123.taobao.com">B店</a>
            <a href="https://item.taobao.com/item.htm?id=10002">台灯</a>
            <span>规格：暖白光</span>
            <span>活动价 ¥199.00</span>
            <input type="number" value="1">
          </div>
          <div class="cart-item">
            <input type="checkbox" checked>
            <a href="https://shop123.taobao.com">B店</a>
            <a href="https://item.taobao.com/item.htm?id=10002">台灯</a>
            <span>规格：冷白光</span>
            <span>活动价 ¥209.00</span>
            <input type="number" value="1">
          </div>
        </body></html>
        """
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(headless=True)
            except Exception:
                try:
                    browser = pw.chromium.launch(headless=True, channel="chrome")
                except Exception as exc:
                    self.skipTest(f"浏览器不可用：{exc}")
            page = browser.new_page()
            page.set_content(html)
            data = page.evaluate(adapter.EXTRACT_CART_JS)
            self.assertEqual(len(data["items"]), 2)
            self.assertEqual({item["spec"] for item in data["items"]}, {"暖白光", "冷白光"})
            page.close()
            browser.close()

    def test_checkout_preview_is_read_only_and_restores_selection(self):
        from types import SimpleNamespace
        from playwright.sync_api import sync_playwright
        cart_html = """
        <html><body>
          <input type="checkbox" checked id="item">
          <a href="https://item.taobao.com/item.htm?id=1">台灯 暖白光</a>
          <span>活动价 ¥199.00</span>
          <a href="checkout.html">结算</a>
        </body></html>
        """
        checkout_html = """
        <html><body>
          <div>商品合计 ¥199.00</div>
          <div>优惠 -¥25.00</div>
          <div>运费 ¥0.00</div>
          <div>应付 ¥174.00</div>
          <div>提交订单</div>
        </body></html>
        """
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            cart = directory / "cart.html"
            checkout = directory / "checkout.html"
            cart.write_text(cart_html, encoding="utf-8")
            checkout.write_text(checkout_html, encoding="utf-8")
            args = SimpleNamespace(checkout=True, page_timeout=10, cart_url=cart.as_uri())
            with sync_playwright() as pw:
                try:
                    browser = pw.chromium.launch(headless=True)
                except Exception:
                    try:
                        browser = pw.chromium.launch(headless=True, channel="chrome")
                    except Exception as exc:
                        self.skipTest(f"浏览器不可用：{exc}")
                context = browser.new_context()
                page = context.new_page()
                page.goto(cart.as_uri())
                result, warnings = adapter.capture_checkout(context, page, args, 1)
                self.assertTrue(result["captured"], warnings)
                self.assertTrue(any("应付" in line for line in result["summary_lines"]))
                self.assertEqual(page.url, cart.as_uri())
                self.assertTrue(page.evaluate("document.querySelector('#item').checked"))
                context.close()
                browser.close()

    def test_extract_cart_from_mock_page_and_login_markers(self):
        from playwright.sync_api import sync_playwright
        html = """
        <html><body>
          <div class="cart-item">
            <input type="checkbox" checked>
            <a href="https://shop123.taobao.com">B店</a>
            <a href="https://item.taobao.com/item.htm?id=10002">台灯 暖白光</a>
            <span>规格：暖白光</span>
            <span>活动价 ¥199.00</span>
            <input type="number" value="1">
            <span>满180减25</span>
          </div>
        </body></html>
        """
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(headless=True)
            except Exception:
                try:
                    browser = pw.chromium.launch(headless=True, channel="chrome")
                except Exception as exc:
                    self.skipTest(f"浏览器不可用：{exc}")
            page = browser.new_page()
            page.set_content(html)
            data = page.evaluate(adapter.EXTRACT_CART_JS)
            self.assertEqual(len(data["items"]), 1)
            item = data["items"][0]
            self.assertEqual(item["title"], "台灯 暖白光")
            self.assertEqual(item["price"], "199.00")
            self.assertEqual(item["shop"], "B店")
            self.assertEqual(item["marketplace"], "taobao")
            self.assertEqual(item["quantity"], 1)
            self.assertTrue(item["selected"])
            self.assertTrue(any("满180减25" in line for line in data["promotion_lines"]))

            login = browser.new_page()
            login.set_content("<html><body>扫码登录 密码登录</body></html>")
            self.assertTrue(adapter.login_required(login))
            self.assertFalse(adapter.risk_control(login))
            login.set_content("<html><body>安全验证 请完成验证</body></html>")
            self.assertTrue(adapter.risk_control(login))
            login.close()
            page.close()
            browser.close()

    def test_adapter_cli_captures_local_mock_cart(self):
        html = """
        <html><body>
          <div class="cart-item">
            <input type="checkbox" checked>
            <a href="https://shop123.taobao.com">B店</a>
            <a href="https://item.taobao.com/item.htm?id=10002">台灯 暖白光</a>
            <span>规格：暖白光</span>
            <span>活动价 ¥199.00</span>
            <input type="number" value="1">
          </div>
        </body></html>
        """
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            page_path = directory / "cart.html"
            page_path.write_text(html, encoding="utf-8")
            output = directory / "browser-cart.json"
            result = subprocess.run(
                [sys.executable, str(ROOT / "optional/browser/browser_cart.py"), "capture",
                 "--cart-url", page_path.as_uri(), "--output", str(output),
                 "--headless", "--browser", "auto",
                 "--profile", str(directory / "profile"),
                 "--no-details", "--no-dialogs", "--no-checkout"],
                capture_output=True, text=True)
            if result.returncode == 3:
                self.skipTest(result.stderr.strip() or "浏览器不可用")
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(data["schema_version"], 1)
            self.assertEqual(len(data["cart"]["items"]), 1)
            self.assertEqual(data["cart"]["items"][0]["price"], "199.00")
            self.assertFalse(data["checkout"]["captured"])
            imported = subprocess.run(
                [sys.executable, str(ROOT / "scripts/shopping.py"), "import-cart",
                 str(directory / "task"), "--snapshot", str(output)],
                capture_output=True, text=True)
            self.assertEqual(imported.returncode, 0, imported.stderr)
            self.assertTrue((directory / "task" / "session.json").exists())


if __name__ == "__main__":
    unittest.main()
