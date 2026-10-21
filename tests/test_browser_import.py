"""Browser snapshot validation, conversion and CLI import behavior."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import browser_import as bi
import workflow as w


def snapshot():
    return w.load_json(ROOT / "fixtures/browser-cart-snapshot.json")


def imported():
    state, report = bi.import_into_state(None, snapshot())
    return state, report


class BrowserImportTests(unittest.TestCase):
    def test_snapshot_creates_task_with_browser_sources_and_calculates(self):
        state, report = imported()
        self.assertEqual(report["status"], "imported")
        self.assertEqual(len(state["cart"]["items"]), 2)
        self.assertEqual(len(state["cart"]["promotions"]), 1)
        self.assertEqual(len(state["cart"]["shipping"]), 2)
        self.assertEqual(state["data_as_of"], "2026-10-21T18:00:00+08:00")
        lamp = next(x for x in state["cart"]["items"] if x["id"] == "lamp")
        self.assertEqual(lamp["source"]["kind"], "browser")
        self.assertEqual(lamp["source"]["page"], "cart")
        self.assertEqual(lamp["price_kind"], "before_order_promotions")
        out = w.comparison(state)
        self.assertEqual(out["status"], "compared_under_input_rules")
        self.assertEqual(out["plans"]["cheapest"]["summary"]["total_spend"], "354.00")

    def test_complete_promotion_hint_is_not_reported_as_unaccounted(self):
        state, report = imported()
        self.assertNotIn("browser:promotion-hints",
                         [q["id"] for q in state["questions"]])
        self.assertEqual(report["blocking_questions"], 0)

    def test_existing_task_merges_and_invalidates_old_result(self):
        state, _ = imported()
        state["latest_result"] = w.comparison(state)
        changed = snapshot()
        changed["captured_at"] = "2026-10-21T19:00:00+08:00"
        changed["cart"]["items"][1]["price"] = "189.00"
        state, report = bi.import_into_state(state, changed)
        self.assertEqual(state["revision"], 2)
        self.assertEqual(next(x for x in state["cart"]["items"] if x["id"] == "lamp")["price"], "189.00")
        self.assertIsNone(state["latest_result"])
        self.assertEqual(state["history"][-1]["operation"], "update")
        self.assertEqual(report["captured_at"], "2026-10-21T19:00:00+08:00")

    def test_coupon_after_price_is_blocked_not_double_discounted(self):
        data = snapshot()
        cream = data["cart"]["items"][0]
        cream["price"] = "123.00"
        cream["price_text"] = "预估到手价 ¥123.00"
        cream["price_label"] = "预估到手价"
        cream.pop("price_kind", None)
        state, _ = bi.import_into_state(None, data)
        self.assertEqual(next(x for x in state["cart"]["items"] if x["id"] == "cream")["price_kind"],
                         "after_coupons")
        out = w.comparison(state)
        self.assertEqual(out["status"], "needs_information")
        self.assertIn("item:cream:price_kind", [q["id"] for q in out["questions"]])

    def test_unlabelled_cart_price_is_unknown_and_blocks(self):
        data = snapshot()
        cream = data["cart"]["items"][0]
        cream.pop("price_kind", None)
        cream.pop("price_label", None)
        cream["price_text"] = "¥180.00"
        cream["raw_text"] = "面霜 50g ¥180.00 数量1"
        cream["source"]["text"] = cream["raw_text"]
        state, report = bi.import_into_state(None, data)
        self.assertEqual(next(x for x in state["cart"]["items"] if x["id"] == "cream")["price_kind"],
                         "unknown")
        self.assertTrue(any(q["id"] == "item:cream:price_kind" for q in state["questions"]))
        self.assertEqual(w.comparison(state)["status"], "needs_information")

    def test_cart_display_price_is_an_explicit_assumption(self):
        data = snapshot()
        cream = data["cart"]["items"][0]
        cream.pop("price_kind", None)
        cream["price_label"] = "购物车展示价"
        state, report = bi.import_into_state(None, data)
        self.assertIn("面霜", report["assumed_price_kind"])
        self.assertTrue(any("结算预览需复核" in note for note in state["cart"]["assumptions"]))

    def test_incomplete_promotion_is_preserved_as_question_not_guessed(self):
        data = snapshot()
        data["cart"]["promotions"].append({
            "id": "cross",
            "name": "跨店满300减40",
            "platform": "taobao",
            "raw_text": "跨店满300减40，具体叠加方式见活动说明",
        })
        state, report = bi.import_into_state(None, data)
        self.assertNotIn("cross", [x["id"] for x in state["cart"]["promotions"]])
        self.assertIn("browser:promotion:cross:unparsed",
                      [q["id"] for q in state["questions"]])
        self.assertGreaterEqual(report["blocking_questions"], 1)
        self.assertEqual(w.comparison(state)["status"], "needs_information")

    def test_unmatched_promotion_scope_is_not_silently_applied(self):
        data = snapshot()
        data["cart"]["promotions"].append({
            "id": "other",
            "name": "别的商品券",
            "platform": "taobao",
            "mode": "selectable",
            "item_ids": ["not-in-cart"],
            "threshold": "100",
            "discount": "10",
            "basis": "price",
        })
        state, report = bi.import_into_state(None, data)
        self.assertNotIn("other", [x["id"] for x in state["cart"]["promotions"]])
        self.assertIn("browser:promotion:other:scope", [q["id"] for q in state["questions"]])

    def test_quantity_over_limit_is_preserved_not_silently_reduced(self):
        data = snapshot()
        data["cart"]["items"][0]["quantity"] = 15
        state, report = bi.import_into_state(None, data)
        cream = next(x for x in state["cart"]["items"] if x["id"] == "cream")
        self.assertEqual(cream["quantity"], 15)
        self.assertTrue(any("超过12件" in warning for warning in report["warnings"]))
        self.assertEqual(w.comparison(state)["status"], "needs_information")

    def test_missing_field_questions_are_not_duplicated(self):
        data = snapshot()
        data["cart"]["items"][0].pop("shop")
        data["cart"]["shipping"] = [x for x in data["cart"]["shipping"] if x["shop"] != "A"]
        state, report = bi.import_into_state(None, data)
        out = w.comparison(state)
        ids = [q["id"] for q in out["questions"]]
        self.assertEqual(ids.count("item:cream:shop"), 1)
        self.assertEqual([q["id"] for q in report["questions"]].count("item:cream:shop"), 1)

    def test_presale_flag_creates_a_blocking_question(self):
        data = snapshot()
        data["cart"]["items"][1]["presale"] = True
        state, _ = bi.import_into_state(None, data)
        self.assertIn("item:lamp:presale", [q["id"] for q in state["questions"]])
        self.assertEqual(w.comparison(state)["status"], "needs_information")

    def test_personal_data_is_rejected_or_redacted(self):
        data = snapshot()
        data["cart"]["items"][0]["address"] = "上海市某路1号"
        with self.assertRaises(bi.InputError):
            bi.import_into_state(None, data)

        data = snapshot()
        data["cart"]["items"][0]["raw_text"] = "面霜 活动价 ¥180 电话13800138000 地址：上海市某路1号"
        data["cart"]["items"][0]["source"]["cookie"] = "secret-cookie"
        data["source"]["url"] = "https://cart.taobao.com/cart.htm?token=secret-token&id=1"
        state, _ = bi.import_into_state(None, data)
        serialised = json.dumps(state, ensure_ascii=False)
        self.assertNotIn("13800138000", serialised)
        self.assertNotIn("上海市某路1号", serialised)
        self.assertNotIn("secret-cookie", serialised)
        self.assertNotIn("secret-token", serialised)
        self.assertIn("[手机号已隐藏]", serialised)
        self.assertIn("[地址已隐藏]", serialised)

    def test_invalid_quantity_is_kept_as_one_with_warning(self):
        data = snapshot()
        data["cart"]["items"][0]["quantity"] = "很多"
        state, report = bi.import_into_state(None, data)
        cream = next(x for x in state["cart"]["items"] if x["id"] == "cream")
        self.assertEqual(cream["quantity"], 1)
        self.assertTrue(any("数量无法确认" in warning for warning in report["warnings"]))

    def test_missing_cart_and_unknown_fields_are_rejected(self):
        data = snapshot(); data.pop("cart")
        with self.assertRaises(bi.InputError):
            bi.import_into_state(None, data)
        data = snapshot(); data["cart"]["items"][0]["unexpected"] = 1
        with self.assertRaises(bi.InputError):
            bi.import_into_state(None, data)

    def test_cart_import_cli_creates_task_and_compares(self):
        with tempfile.TemporaryDirectory() as directory:
            task = Path(directory) / "task"
            made = subprocess.run(
                [sys.executable, str(ROOT / "scripts/shopping.py"), "import-cart", str(task),
                 "--snapshot", str(ROOT / "fixtures/browser-cart-snapshot.json"),
                 "--format", "text"],
                capture_output=True, text=True)
            self.assertEqual(made.returncode, 0, made.stderr)
            self.assertIn("浏览器购物车已导入", made.stdout)
            out = subprocess.run(
                [sys.executable, str(ROOT / "scripts/shopping.py"), "compare", str(task)],
                capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(json.loads(out.stdout)["plans"]["cheapest"]["summary"]["total_spend"], "354.00")

    def test_invalid_snapshot_does_not_create_session(self):
        with tempfile.TemporaryDirectory() as directory:
            task = Path(directory) / "task"
            bad = Path(directory) / "bad.json"
            bad.write_text('{"schema_version":1,"cart":{"items":[{"address":"x"}]}}')
            out = subprocess.run(
                [sys.executable, str(ROOT / "scripts/shopping.py"), "import-cart", str(task),
                 "--snapshot", str(bad)],
                capture_output=True, text=True)
            self.assertEqual(out.returncode, 2)
            self.assertFalse((task / "session.json").exists())


if __name__ == "__main__":
    unittest.main()
