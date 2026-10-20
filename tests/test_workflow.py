"""Cross-command behavior, state freshness, consent and accounting invariants."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import workflow as w


def cart():
    return w.load_json(ROOT / "fixtures/demo-cart.json")


def approved_state():
    state = w.new_state(cart())
    state["preferences"].update(allow_addons=True, approved_optional_ids=["cloth"])
    return state


def patch(state, **values):
    return w.update_state(state, dict(expected_revision=state["revision"], reason="用户提供新资料", **values))


def receipt(state, **values):
    return dict(expected_revision=state["revision"], id="receipt-1", pay_now="200.00", coupon_ids=["beauty"],
                source={"kind": "user", "text": "第一单已买，使用美妆券，实付200元"}, **values)


class WorkflowTests(unittest.TestCase):
    def test_no_addons_by_default_and_identical_choices_merge(self):
        out = w.comparison(w.new_state(cart()))
        self.assertEqual(out["status"], "compared_under_input_rules")
        self.assertEqual(len(out["choices"]), 1)
        self.assertEqual(out["plans"]["cheapest"]["summary"]["total_spend"], "390.00")
        self.assertEqual(out["plans"]["cheapest"]["selected_optional_units"], [])

    def test_explicit_addon_consent_three_options_and_global_savings(self):
        state = approved_state()
        out = w.comparison(state)
        totals = [out["plans"][x]["summary"]["total_spend"] for x in ("original", "cheapest", "fewest_checkouts")]
        self.assertEqual(totals, ["390.00", "345.00", "375.00"])
        self.assertEqual(out["plans"]["fewest_checkouts"]["summary"]["net_savings_vs_original"], "15.00")
        self.assertEqual(out["plans"]["cheapest"]["selected_optional_units"], ["cloth#1"])
        self.assertIn("50g", w.render_comparison(out))

    def test_blanket_permission_does_not_approve_arbitrary_products(self):
        state = w.new_state(cart())
        state["preferences"]["allow_addons"] = True
        self.assertEqual(w.comparison(state)["plans"]["cheapest"]["summary"]["total_spend"], "390.00")
        with self.assertRaises(w.InputError):
            patch(state, preferences={"approved_optional_ids": ["imaginary"]})

    def test_rejecting_addons_restores_original(self):
        state = approved_state()
        state["latest_result"] = w.comparison(state)
        state = patch(state, preferences={"allow_addons": False})
        self.assertIsNone(state["latest_result"])
        self.assertEqual(w.comparison(state)["plans"]["cheapest"]["summary"]["total_spend"], "390.00")

    def test_price_quantity_selection_and_coupon_changes_affect_calculation(self):
        state = approved_state()
        first = w.comparison(state)["plans"]["cheapest"]["summary"]["total_spend"]
        state = patch(state, items=[{"id": "lamp", "price": "199.00"}])
        changed = w.comparison(state)["plans"]["cheapest"]["summary"]["total_spend"]
        self.assertNotEqual(first, changed)
        state = patch(state, items=[{"id": "cream", "quantity": 2}])
        self.assertNotEqual(changed, w.comparison(state)["plans"]["cheapest"]["summary"]["total_spend"])
        state = patch(state, promotions=[{"id": "beauty", "total_uses": 0}])
        self.assertNotIn("beauty", w.comparison(state)["plans"]["cheapest"]["coupon_usage"])
        state = patch(state, remove_items=["cleanser"])
        self.assertNotIn("cleanser", [x["id"] for x in state["cart"]["items"]])
        self.assertEqual(state["revision"], 5)

    def test_revision_guard_and_original_state_remains_untouched(self):
        state = approved_state()
        before = deepcopy(state)
        with self.assertRaises(w.InputError):
            w.update_state(state, {"expected_revision": 99, "reason": "过期资料"})
        with self.assertRaises(w.InputError):
            patch(state, items=[{"id": "lamp", "price": "1.001"}])
        self.assertEqual(state, before)

    def test_history_keeps_old_input_and_result_but_current_is_invalidated(self):
        state = approved_state()
        state["latest_result"] = w.comparison(state)
        state = patch(state, items=[{"id": "lamp", "price": "199.00"}])
        self.assertIsNone(w.current_result(state))
        self.assertEqual(state["history"][0]["result"]["plans"]["cheapest"]["summary"]["total_spend"], "345.00")
        self.assertEqual(next(x for x in state["history"][0]["cart"]["items"] if x["id"] == "lamp")["price"], "190.00")

    def test_manual_edit_fingerprint_hides_stale_result(self):
        state = approved_state()
        state["latest_result"] = w.comparison(state)
        state["cart"]["items"][0]["price"] = "999.00"
        self.assertIsNone(w.current_result(state))
        self.assertIsNone(w.view_state(state)["latest_result"])
        self.assertNotIn("345.00", w.render_state(state))

    def test_missing_shipping_blocks_instead_of_assuming_free(self):
        data = cart(); data["shipping"] = []
        out = w.comparison(w.new_state(data))
        self.assertEqual(out["status"], "needs_information")
        self.assertEqual(out["plans"], {})
        self.assertEqual(sum(q["id"].startswith("shipping:") for q in out["questions"]), 2)

    def test_draft_missing_price_preserves_other_products_and_resumes(self):
        data = cart(); data["items"][0]["price"] = None
        state = w.new_state(data)
        self.assertEqual(w.comparison(state)["status"], "needs_information")
        state = patch(state, items=[{"id": "cream", "price": "180.00"}])
        self.assertEqual(len(state["cart"]["items"]), 5)
        self.assertEqual(w.comparison(state)["status"], "compared_under_input_rules")

    def test_incomplete_coupon_is_never_silently_dropped(self):
        data = cart()
        del data["promotions"][0]["platform"]
        out = w.comparison(w.new_state(data))
        self.assertEqual(out["status"], "needs_information")
        self.assertIn("promotion:shop-A:platform", [q["id"] for q in out["questions"]])

    def test_scope_typo_is_invalid_rather_than_silent_zero_discount(self):
        data = cart(); data["promotions"][0]["shops"] = ["typo"]
        with self.assertRaises(w.InputError):
            w.comparison(w.new_state(data))

    def test_optional_incomplete_product_does_not_block_unapproved_cart(self):
        data = cart(); data["items"][3]["price"] = None
        self.assertEqual(w.comparison(w.new_state(data))["status"], "compared_under_input_rules")
        state = w.new_state(data)
        state["preferences"].update(allow_addons=True, approved_optional_ids=["cloth"])
        self.assertEqual(w.comparison(state)["status"], "needs_information")

    def test_over_limit_is_retained_and_recoverable(self):
        data = cart(); data["items"][0]["quantity"] = 13
        state = w.new_state(data)
        self.assertEqual(w.comparison(state)["status"], "needs_information")
        self.assertEqual(state["cart"]["items"][0]["quantity"], 13)
        state = patch(state, items=[{"id": "cream", "quantity": 1}])
        self.assertEqual(w.comparison(state)["status"], "compared_under_input_rules")

    def test_question_resolution_preserves_cart(self):
        state = patch(approved_state(), questions=[{"id": "stacking", "text": "两种券能叠加吗？", "blocking": True}])
        self.assertEqual(w.comparison(state)["status"], "needs_information")
        state = patch(state, resolve_questions=["stacking"])
        self.assertEqual(w.comparison(state)["status"], "compared_under_input_rules")
        self.assertEqual(len(state["cart"]["items"]), 5)

    def test_missing_sources_are_visible(self):
        data = cart(); del data["items"][0]["source"]
        out = w.comparison(w.new_state(data))
        self.assertEqual(out["status"], "compared_under_input_rules")
        self.assertIn("source:item:cream", [q["id"] for q in out["questions"]])

    def test_distinct_checkout_groups_still_require_separate_checkouts(self):
        data = {"version": 1, "items": [{"id": "a", "platform": "taobao", "shop": "s", "price": "100", "checkout_group": "now"},
                                             {"id": "b", "platform": "tmall", "shop": "s", "price": "100", "checkout_group": "later"}],
                "promotions": [], "shipping": [{"platform": "taobao", "shop": "s", "fee": "0", "basis": "price"}]}
        out = w.comparison(w.new_state(data))
        self.assertEqual(out["minimum_checkouts"], 2)
        self.assertEqual(out["plans"]["fewest_checkouts"]["summary"]["checkouts"], 2)

    def test_fewest_can_cost_more_than_global_original_and_signed_cents_are_correct(self):
        data = {"version": 1, "items": [{"id": "a", "platform": "taobao", "shop": "s", "price": "10", "quantity": 2}],
                "promotions": [{"id": "auto", "platform": "taobao", "mode": "automatic", "threshold": "10", "discount": "1.25", "basis": "price"}],
                "shipping": [{"platform": "taobao", "shop": "s", "fee": "0", "basis": "price"}]}
        out = w.comparison(w.new_state(data))
        self.assertEqual(out["plans"]["original"]["summary"]["total_spend"], "17.50")
        self.assertEqual(out["plans"]["fewest_checkouts"]["summary"]["net_savings_vs_original"], "-1.25")

    def test_presale_total_paid_and_due_are_separate(self):
        state = w.new_state(w.load_json(ROOT / "fixtures/taobao-presale.json"))
        summary = w.comparison(state)["plans"]["cheapest"]["summary"]
        self.assertEqual((summary["total_spend"], summary["deposit_paid"], summary["pay_now"]), ("720.00", "100.00", "620.00"))

    def test_purchase_debits_coupons_and_replans_remaining_goods(self):
        state = approved_state(); state["latest_result"] = w.comparison(state)
        state = w.record_purchase(state, "cheapest", 1, receipt(state))
        self.assertEqual({x["id"] for x in state["cart"]["items"]}, {"lamp", "cloth", "box"})
        self.assertIsNone(state["latest_result"])
        out = w.comparison(state)
        self.assertEqual(out["plans"]["cheapest"]["summary"]["total_spend"], "145.00")
        self.assertEqual(out["plans"]["original"]["summary"]["total_spend"], "190.00")
        self.assertEqual(out["scope"], "remaining_cart")
        self.assertEqual(out["plans"]["cheapest"]["summary"]["task_total_spend"], "345.00")
        self.assertIn("连同已购", w.render_comparison(out))
        self.assertEqual(state["purchases"][0]["actual_total_spend"], "200.00")
        state["latest_result"] = out
        final_receipt = {"expected_revision": 2, "id": "receipt-2", "pay_now": "145.00", "coupon_ids": ["shop-B"], "source": "用户确认第二单已买"}
        state = w.record_purchase(state, "cheapest", 1, final_receipt)
        self.assertEqual(w.comparison(state)["status"], "complete")

    def test_shared_coupon_is_consumed_globally_after_partial_purchase(self):
        data = {"version": 1, "items": [{"id": "a", "platform": "taobao", "shop": "s", "price": "100", "quantity": 2}],
                "promotions": [{"id": "c", "platform": "taobao", "mode": "selectable", "threshold": "100", "discount": "20", "basis": "price", "total_uses": 2}],
                "shipping": [{"platform": "taobao", "shop": "s", "fee": "0", "basis": "price"}]}
        state = w.new_state(data); state["latest_result"] = w.comparison(state)
        state = w.record_purchase(state, "cheapest", 1, {"expected_revision": 1, "id": "p1", "pay_now": "80", "coupon_ids": ["c"], "source": "用户已买"})
        self.assertEqual(state["cart"]["items"][0]["quantity"], 1)
        self.assertEqual(state["cart"]["promotions"][0]["total_uses"], 1)
        self.assertEqual(w.comparison(state)["plans"]["cheapest"]["summary"]["pay_now"], "80.00")

    def test_consumed_coupon_balance_survives_rule_cleanup_and_reintroduction(self):
        state = approved_state(); state["latest_result"] = w.comparison(state)
        state = w.record_purchase(state, "cheapest", 1, receipt(state))
        self.assertEqual(state["coupon_balances"]["beauty"], 0)
        rule = next(x for x in cart()["promotions"] if x["id"] == "beauty")
        rule.pop("total_uses")
        state = patch(state, items=[{"id": "new-beauty", "name": "新商品", "platform": "taobao", "shop": "A", "category": "beauty", "price": "300"}],
                      promotions=[rule], shipping=[{"platform": "taobao", "shop": "A", "fee": "0", "basis": "price"}])
        self.assertEqual(next(x for x in state["cart"]["promotions"] if x["id"] == "beauty")["total_uses"], 0)
        self.assertNotIn("beauty", w.comparison(state)["plans"]["cheapest"]["coupon_usage"])

    def test_purchase_difference_stops_remaining_planning_until_reconciled(self):
        state = approved_state(); state["latest_result"] = w.comparison(state)
        proof = receipt(state); proof["pay_now"] = "206.00"
        state = w.record_purchase(state, "cheapest", 1, proof)
        self.assertEqual(w.comparison(state)["status"], "needs_information")
        self.assertEqual(state["purchases"][0]["actual_total_spend"], "206.00")
        state = patch(state, resolve_questions=[state["questions"][0]["id"]])
        self.assertEqual(w.comparison(state)["status"], "compared_under_input_rules")

    def test_purchase_requires_current_plan_actual_coupons_and_idempotency(self):
        state = approved_state()
        with self.assertRaises(w.InputError):
            w.record_purchase(state, "cheapest", 1, receipt(state))
        state["latest_result"] = w.comparison(state)
        proof = receipt(state); proof["coupon_ids"] = []
        with self.assertRaises(w.InputError):
            w.record_purchase(state, "cheapest", 1, proof)
        purchased = w.record_purchase(state, "cheapest", 1, receipt(state))
        proof = receipt(purchased)
        with self.assertRaises(w.InputError):
            w.record_purchase(purchased, "cheapest", 1, proof)

    def test_search_limit_does_not_return_partial_plans(self):
        with self.assertRaises(w.SearchLimit):
            w.comparison(approved_state(), state_limit=1)

    def test_atomic_roundtrip_and_busy_task_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            state = approved_state()
            with w.task_lock(directory):
                w.save_state(directory, state)
                with self.assertRaises(w.InputError):
                    with w.task_lock(directory):
                        pass
            self.assertEqual(w.read_state(directory), state)
            self.assertFalse((Path(directory) / ".shopping.lock").exists())


class CLITests(unittest.TestCase):
    def call(self, *args, cwd=None):
        return subprocess.run([sys.executable, str(ROOT / "scripts/shopping.py"), *map(str, args)],
                              cwd=cwd, capture_output=True, text=True)

    def test_full_task_lifecycle_from_another_working_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            task = Path(directory) / "task"
            self.assertEqual(self.call("new", task, "--input", ROOT / "fixtures/demo-cart.json", cwd=directory).returncode, 0)
            out = self.call("compare", task, cwd=directory)
            self.assertEqual(json.loads(out.stdout)["plans"]["cheapest"]["summary"]["total_spend"], "390.00")
            update = Path(directory) / "patch.json"
            update.write_text(json.dumps({"expected_revision": 1, "reason": "用户同意清洁布加购", "preferences": {"allow_addons": True, "approved_optional_ids": ["cloth"]}}))
            self.assertEqual(self.call("update", task, "--patch", update).returncode, 0)
            out = self.call("compare", task)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(json.loads(out.stdout)["plans"]["cheapest"]["summary"]["total_spend"], "345.00")
            saved = w.load_json(task / "session.json")
            proof = Path(directory) / "receipt.json"; proof.write_text(json.dumps(receipt(saved)))
            self.assertEqual(self.call("purchase", task, "--plan", "cheapest", "--order", 1, "--receipt", proof).returncode, 0)
            out = self.call("compare", task)
            self.assertEqual(json.loads(out.stdout)["plans"]["cheapest"]["summary"]["total_spend"], "145.00")

    def test_failed_compute_replaces_previous_result_with_no_totals(self):
        with tempfile.TemporaryDirectory() as directory:
            self.call("new", directory, "--input", ROOT / "fixtures/demo-cart.json")
            self.call("compare", directory)
            out = self.call("compare", directory, "--time-limit", "0.000000000001")
            self.assertEqual(out.returncode, 3, out.stderr)
            self.assertEqual(json.loads(out.stdout)["plans"], {})
            shown = self.call("show", directory)
            self.assertEqual(json.loads(shown.stdout)["latest_result"]["status"], "search_limit")

    def test_incomplete_and_invalid_json_have_structured_exit_codes(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self.call("new", directory).returncode, 0)
            out = self.call("compare", directory)
            self.assertEqual(out.returncode, 4)
            self.assertEqual(json.loads(out.stdout)["status"], "needs_information")
            path = Path(directory) / "bad.json"; path.write_text('{"version":1,"version":1}')
            out = self.call("solve", path)
            self.assertEqual(out.returncode, 2)
            self.assertEqual(json.loads(out.stderr)["status"], "invalid_input")
            self.assertEqual(out.stdout, "")

    def test_malformed_cart_shape_returns_json_error_without_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad-cart.json"
            for data in ([], {"version": 1, "items": [], "promotions": [{}]}):
                path.write_text(json.dumps(data))
                out = self.call("solve", path)
                self.assertEqual(out.returncode, 2)
                self.assertEqual(json.loads(out.stderr)["status"], "invalid_input")
                self.assertNotIn("Traceback", out.stderr)

    def test_copy_is_self_contained_and_demo_creates_no_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            relocated = Path(directory) / "skill"
            shutil.copytree(ROOT / "scripts", relocated / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copytree(ROOT / "fixtures", relocated / "fixtures")
            out = subprocess.run([sys.executable, str(relocated / "scripts/shopping.py"), "demo", "--format", "json"], cwd=directory, capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(json.loads(out.stdout)["plans"]["cheapest"]["summary"]["total_spend"], "345.00")
            self.assertFalse((relocated / "tasks").exists())


if __name__ == "__main__":
    unittest.main()
