"""Discount arithmetic, shared ledgers, order dependence and independent oracle."""
from copy import deepcopy
from decimal import Decimal
import itertools
import json
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import discounts as d
import workflow as w


def item(ident="a", price="100", **extra):
    return dict(id=ident, platform="taobao", marketplace="tmall", shop="A", price=price, **extra)


def percent(ident="official", **extra):
    result = dict(id=ident, platform="taobao", mode="automatic", kind="percentage", threshold="0", basis="price",
                  rate="0.15", discount_basis="price", rounding="half_up", rounding_scope="item")
    result.update(extra)
    return result


def fixed(ident="coupon", **extra):
    result = dict(id=ident, platform="taobao", mode="selectable", threshold="100", basis="remaining", discount="20")
    result.update(extra)
    return result


def balance(ident="red", resource_id="red", **extra):
    result = dict(id=ident, platform="taobao", mode="selectable", kind="balance", stage="payment",
                  threshold="0", basis="payable", resource_id=resource_id, allocation="maximum")
    result.update(extra)
    return result


def cart(items=None, promotions=None, resources=None, shipping=None, **extra):
    return dict(version=1, items=items or [item()], promotions=promotions or [],
                resources=resources or [], shipping=shipping or [{"platform": "taobao", "shop": "A", "fee": "0", "basis": "price"}], **extra)


def money_resource(ident="red", amount="10"):
    return dict(id=ident, kind="money", balance=amount, source="假设权益余额，仅用于程序验证")


def solve(data, plan="cheapest"):
    return w.comparison(w.new_state(data))["plans"][plan]


class DiscountTests(unittest.TestCase):
    def test_official_then_vip_sequential_or_parallel_bases(self):
        data = cart(promotions=[percent(), percent("vip", rate="0.05", discount_basis="remaining")])
        self.assertEqual(solve(data)["summary"]["total_spend"], "80.75")
        data["promotions"][1]["discount_basis"] = "price"
        self.assertEqual(solve(data)["summary"]["total_spend"], "80.00")

    def test_item_and_checkout_rounding_are_distinct_and_integer_exact(self):
        data = cart([item(price="0.05", quantity=2)], [percent(rate="0.10")], max_orders=1)
        self.assertEqual(solve(data)["summary"]["total_spend"], "0.08")
        data["promotions"][0]["rounding_scope"] = "checkout"
        self.assertEqual(solve(data)["summary"]["total_spend"], "0.09")
        data["promotions"][0].update(rounding="floor", rounding_scope="item")
        self.assertEqual(solve(data)["summary"]["total_spend"], "0.10")

    def test_rounding_ties_and_ceil(self):
        for mode, expected in (("floor", "0.01"), ("half_up", "0.00"), ("ceil", "0.00")):
            self.assertEqual(solve(cart([item(price="0.01")], [percent(rate="0.5", rounding=mode)]))["summary"]["total_spend"], expected)

    def test_per_checkout_and_per_item_caps(self):
        data = cart([item(quantity=2)], [percent(rate="0.20", cap="15")], max_orders=1)
        self.assertEqual(solve(data)["summary"]["total_spend"], "185.00")
        data["promotions"][0].pop("cap")
        data["promotions"][0]["per_item_cap"] = "10"
        self.assertEqual(solve(data)["summary"]["total_spend"], "180.00")

    def test_quantity_tiers_pick_highest_qualifying_tier(self):
        rule = percent(tiers=[{"min_quantity": 2, "rate": "0.10"}, {"min_quantity": 3, "rate": "0.20"}])
        rule.pop("rate")
        for quantity, expected in ((1, "100.00"), (2, "180.00"), (3, "240.00")):
            self.assertEqual(solve(cart([item(quantity=quantity)], [rule]))["summary"]["total_spend"], expected)

    def test_every_two_items_fixed_reduction(self):
        rule = fixed(mode="automatic", threshold="0", discount="10", quantity_step=2)
        self.assertEqual(solve(cart([item(price="20", quantity=5)], [rule]))["summary"]["total_spend"], "80.00")

    def test_min_quantity_counts_eligible_items_only(self):
        rule = percent(min_quantity=2, item_ids=["a"])
        self.assertEqual(solve(cart([item("a"), item("b")], [rule]))["summary"]["total_spend"], "200.00")

    def test_shared_vip_budget_and_coupon_counts_do_not_repeat(self):
        rows = [item("a", "200", checkout_group="a"), item("b", "200", checkout_group="b")]
        rule = percent("vip", rate="0.05", resource_id="vip-budget")
        plan = solve(cart(rows, [rule], [money_resource("vip-budget", "10")]))
        self.assertEqual(plan["summary"]["total_spend"], "390.00")
        self.assertEqual(plan["resource_usage"], {"vip-budget": "10.00"})
        rule.pop("resource_id"); rule["total_uses"] = 1
        plan = solve(cart(rows, [rule]))
        self.assertEqual(plan["summary"]["total_spend"], "390.00")
        self.assertEqual(plan["coupon_usage"], {"vip": 1})

    def test_red_packet_is_a_shared_balance_across_split_checkouts(self):
        rows = [item("a", checkout_group="a"), item("b", checkout_group="b")]
        plan = solve(cart(rows, [balance()], [money_resource()]))
        self.assertEqual(plan["summary"]["total_spend"], "190.00")
        self.assertEqual(plan["resource_usage"], {"red": "10.00"})

    def test_points_use_explicit_conversion_steps_and_cannot_overdraw(self):
        resource = {"id": "coins", "kind": "points", "balance": 250}
        rule = balance("coins", "coins", units_per_step=100, step_value="1.00")
        plan = solve(cart(promotions=[rule], resources=[resource]))
        self.assertEqual(plan["summary"]["total_spend"], "98.00")
        self.assertEqual(plan["resource_usage"], {"coins": 200})
        rule["cap"] = "1.50"
        self.assertEqual(solve(cart(promotions=[rule], resources=[resource]))["resource_usage"], {"coins": 100})

    def test_points_percentage_limit_and_red_packet_stack(self):
        rule = balance("coins", "coins", rate="0.05", discount_basis="price", rounding="floor", rounding_scope="checkout", units_per_step=1, step_value="0.01")
        data = cart(promotions=[balance(), rule], resources=[money_resource(), {"id": "coins", "kind": "points", "balance": 1000}])
        plan = solve(data)
        self.assertEqual(plan["summary"]["total_spend"], "85.00")
        self.assertEqual(plan["resource_usage"], {"red": "10.00", "coins": 500})

    def test_payment_credit_does_not_retrigger_free_shipping(self):
        shipping = [{"platform": "taobao", "shop": "A", "fee": "10", "basis": "remaining", "free_threshold": "100"}]
        plan = solve(cart(promotions=[balance()], resources=[money_resource()], shipping=shipping))
        self.assertEqual(plan["summary"]["total_spend"], "90.00")
        self.assertEqual(plan["summary"]["shipping"], "0.00")

    def test_shipping_credits_preserve_original_fee_and_cash_conservation(self):
        shipping = [{"platform": "taobao", "shop": "A", "fee": "10", "basis": "remaining", "free_threshold": "100"}]
        plan = solve(cart(promotions=[percent(rate="0.10"), balance(include_shipping=True)], resources=[money_resource(amount="30")], shipping=shipping))
        self.assertEqual(plan["summary"]["total_spend"], "70.00")
        self.assertEqual(plan["summary"]["shipping"], "10.00")
        self.assertEqual(plan["summary"]["shipping_discount"], "3.00")
        self.assertEqual(plan["orders"][0]["shipping"][0]["pay_now"], "7.00")

    def test_payment_threshold_includes_shipping_only_when_explicit(self):
        shipping = [{"platform": "taobao", "shop": "A", "fee": "10", "basis": "price"}]
        rule = fixed(stage="payment", basis="payable", discount="5", include_shipping=True)
        data = cart([item(price="90")], [rule], shipping=shipping)
        self.assertEqual(solve(data)["summary"]["total_spend"], "95.00")
        rule["include_shipping"] = False
        self.assertEqual(solve(data)["summary"]["total_spend"], "100.00")

    def test_whole_checkout_condition_is_not_an_item_filter(self):
        data = cart([item("a"), item("b")], [fixed(item_ids=["a"], whole_checkout=True)])
        out = w.comparison(w.new_state(data))
        self.assertEqual(out["plans"]["cheapest"]["summary"]["total_spend"], "180.00")
        self.assertEqual(out["plans"]["fewest_checkouts"]["summary"]["total_spend"], "200.00")

    def test_automatic_budget_can_make_checkout_order_matter(self):
        rows = [item("a", checkout_group="a"), item("b", "80", checkout_group="b")]
        rules = [percent("vip", rate="0.10", resource_id="vip-budget"), fixed(item_ids=["a"], threshold="95", discount="50")]
        plan = solve(cart(rows, rules, [money_resource("vip-budget", "10")]))
        self.assertEqual(plan["summary"]["total_spend"], "120.00")
        self.assertEqual(plan["orders"][0]["items"][0]["id"], "b")

    def test_order_dependent_purchase_checks_starting_automatic_balances(self):
        rows = [item("a", checkout_group="a"), item("b", "80", checkout_group="b")]
        rules = [percent("vip", rate="0.10", resource_id="vip-budget"), fixed(item_ids=["a"], threshold="95", discount="50")]
        state = w.new_state(cart(rows, rules, [money_resource("vip-budget", "10")]))
        state["latest_result"] = w.comparison(state)
        orders = state["latest_result"]["plans"]["cheapest"]["orders"]
        proof = {"expected_revision": 1, "id": "later", "pay_now": orders[1]["pay_now"], "coupon_ids": ["coupon"],
                 "resource_usage": orders[1]["resource_usage"], "source": "实际已买"}
        with self.assertRaises(w.InputError):
            w.record_purchase(state, "cheapest", 2, proof)
        first = {"expected_revision": 1, "id": "first", "pay_now": orders[0]["pay_now"], "coupon_ids": [],
                 "resource_usage": orders[0]["resource_usage"], "source": "第一步已买"}
        state = w.record_purchase(state, "cheapest", 1, first)
        self.assertEqual(state["cart"]["resources"][0]["balance"], "2.00")
        self.assertEqual(w.comparison(state)["plans"]["cheapest"]["summary"]["total_spend"], "48.00")

    def test_optional_only_first_checkout_can_be_required_for_true_optimum(self):
        data = cart([item("a"), item("b", "10", required=False)],
                    [percent("vip", rate="1", resource_id="vip-budget"), fixed(item_ids=["a"], discount="80")],
                    [money_resource("vip-budget", "10")])
        state = w.new_state(data)
        state["preferences"].update(allow_addons=True, approved_optional_ids=["b"])
        out = w.comparison(state)
        self.assertEqual(out["plans"]["cheapest"]["summary"]["total_spend"], "20.00")
        self.assertEqual(out["plans"]["cheapest"]["orders"][0]["items"][0]["id"], "b")
        self.assertEqual(out["plans"]["fewest_checkouts"]["summary"]["total_spend"], "90.00")

    def test_flexible_balance_enumerates_partial_use_without_assuming_it(self):
        rule = balance(stage="goods", allocation="flexible")
        data = cart(promotions=[rule, fixed(threshold="95")], resources=[money_resource()])
        plan = solve(data)
        self.assertEqual(plan["summary"]["total_spend"], "75.00")
        self.assertEqual(plan["resource_usage"], {"red": "5.00"})
        rule["allocation"] = "maximum"
        self.assertEqual(solve(data)["summary"]["total_spend"], "80.00")
        rule["mode"] = "automatic"
        self.assertEqual(solve(data)["summary"]["total_spend"], "90.00")

    def test_minimum_payable_floors_do_not_double_count(self):
        plan = solve(cart(promotions=[percent(rate="1", min_item_payable="1", min_payable="5")]))
        self.assertEqual(plan["summary"]["total_spend"], "5.00")

    def test_deposit_is_not_discounted_or_refunded_by_percentage_or_credit(self):
        data = cart([item(deposit_paid="90")], [percent(rate="0.50"), balance(include_shipping=True)], [money_resource(amount="100")],
                    shipping=[{"platform": "taobao", "shop": "A", "fee": "6", "basis": "price"}])
        s = solve(data)["summary"]
        self.assertEqual((s["total_spend"], s["deposit_paid"], s["pay_now"]), ("90.00", "90.00", "0.00"))

    def test_missing_rounding_balance_or_point_conversion_blocks(self):
        for field in ("rounding", "rounding_scope", "discount_basis", "rate"):
            rule = percent(); del rule[field]
            out = w.comparison(w.new_state(cart(promotions=[rule])))
            self.assertEqual(out["status"], "needs_information")
            self.assertEqual(out["plans"], {})
        data = cart(promotions=[balance("coins", "coins")], resources=[{"id": "coins", "kind": "points", "balance": 100}])
        self.assertEqual(w.comparison(w.new_state(data))["status"], "needs_information")
        data["resources"] = []
        self.assertEqual(w.comparison(w.new_state(data))["status"], "needs_information")

    def test_explicitly_unknown_payable_floor_is_preserved_for_clarification(self):
        state = w.new_state(cart(promotions=[percent(min_item_payable=None)]))
        out = w.comparison(state)
        self.assertEqual(out["status"], "needs_information")
        self.assertIsNone(state["cart"]["promotions"][0]["min_item_payable"])
        state = w.update_state(state, {"expected_revision": 1, "reason": "用户核实至少付1分钱", "promotions": [{"id": "official", "min_item_payable": "0.01"}]})
        self.assertEqual(w.comparison(state)["status"], "compared_under_input_rules")

    def test_invalid_rules_and_unsupported_asset_models_are_rejected(self):
        cases = [percent(rate="1.2"), percent(rate="0"), percent(rate=0.15), percent(rounding="bankers"),
                 percent(tiers=[]), balance(mode="automatic", allocation="flexible"),
                 fixed(quantity_step=2, repeat=True), percent(stage="goods", include_shipping=True)]
        for rule in cases:
            with self.subTest(rule=rule), self.assertRaises(w.InputError):
                w.new_state(cart(promotions=[rule]))
        with self.assertRaises(w.InputError):
            solve(cart(promotions=[balance(), percent()], resources=[money_resource()]))

    def test_purchase_requires_actual_resource_use_and_debits_money_and_points(self):
        rules = [balance(item_ids=["a"]), balance("coins", "coins", units_per_step=1, step_value="0.01", item_ids=["a"])]
        state = w.new_state(cart([item("a", checkout_group="a"), item("b", checkout_group="b")], rules,
                                 [money_resource(amount="10"), {"id": "coins", "kind": "points", "balance": 500}]))
        state["latest_result"] = w.comparison(state)
        orders = state["latest_result"]["plans"]["cheapest"]["orders"]
        index = next(i for i, o in enumerate(orders) if o.get("resource_usage"))
        order = orders[index]
        proof = {"expected_revision": 1, "id": "p1", "pay_now": order["pay_now"],
                 "coupon_ids": [p["id"] for p in order["promotions"]], "source": "用户确认已买"}
        with self.assertRaises(w.InputError):
            w.record_purchase(state, "cheapest", index + 1, proof)
        proof["resource_usage"] = order["resource_usage"]
        state = w.record_purchase(state, "cheapest", index + 1, proof)
        self.assertEqual(state["coupon_balances"]["red"], None)
        self.assertEqual({r["id"]: r["balance"] for r in state["cart"]["resources"]}, {"red": "0.00", "coins": 0})
        self.assertEqual(w.comparison(state)["plans"]["cheapest"]["summary"]["total_spend"], "100.00")

    def test_auto_limited_discount_is_debited_after_purchase(self):
        state = w.new_state(cart([item("a", checkout_group="a"), item("b", checkout_group="b")], [percent(total_uses=1)]))
        state["latest_result"] = w.comparison(state)
        order = state["latest_result"]["plans"]["cheapest"]["orders"][0]
        self.assertTrue(order["promotions"])
        state = w.record_purchase(state, "cheapest", 1, {"expected_revision": 1, "id": "p1", "pay_now": order["pay_now"], "coupon_ids": [], "source": "已买"})
        self.assertEqual(state["coupon_balances"]["official"], 0)
        self.assertEqual(w.comparison(state)["plans"]["cheapest"]["summary"]["total_spend"], "100.00")

    def test_inactive_incomplete_resource_does_not_block_and_updates_are_versioned(self):
        state = w.new_state(cart(resources=[{"id": "unused"}]))
        self.assertEqual(w.comparison(state)["status"], "compared_under_input_rules")
        state = w.update_state(state, {"expected_revision": 1, "reason": "用户核实了红包余额", "resources": [money_resource("unused", "3")]})
        self.assertEqual(state["revision"], 2)
        self.assertEqual(state["cart"]["resources"][0]["balance"], "3")
        with self.assertRaises(w.InputError):
            w.update_state(state, {"expected_revision": 2, "reason": "误改单位", "resources": [{"id": "unused", "kind": "points", "balance": 3}]})

    def test_auto_coupon_ledger_survives_removal_and_reintroduction(self):
        state = w.new_state(cart([item("a", checkout_group="a"), item("b", checkout_group="b")], [percent(total_uses=1)]))
        state["latest_result"] = w.comparison(state)
        order = state["latest_result"]["plans"]["cheapest"]["orders"][0]
        state = w.record_purchase(state, "cheapest", 1, {"expected_revision": 1, "id": "p1", "pay_now": order["pay_now"], "coupon_ids": [], "source": "已买"})
        state = w.update_state(state, {"expected_revision": 2, "reason": "暂移除旧优惠", "remove_promotions": ["official"]})
        state = w.update_state(state, {"expected_revision": 3, "reason": "重新核对同一优惠", "promotions": [percent()]})
        self.assertEqual(state["cart"]["promotions"][0]["total_uses"], 0)
        self.assertEqual(w.comparison(state)["plans"]["cheapest"]["summary"]["total_spend"], "100.00")

    def test_search_limit_never_returns_a_partial_optimum(self):
        data = w.project(w.new_state(cart(promotions=[percent()]))["cart"], {"a"})
        with self.assertRaises(w.SearchLimit):
            d.optimize(w.compile_cart(data), state_limit=1)


def ordered_partitions(values):
    if not values:
        yield []
        return
    first, *tail = values
    for blocks in ordered_partitions(tail):
        for i in range(len(blocks) + 1):
            yield blocks[:i] + [[first]] + blocks[i:]
        for i in range(len(blocks)):
            yield [b + [first] if j == i else b[:] for j, b in enumerate(blocks)]


def oracle(prices, required, vip_budget, red_budget, rate_num, rate_den, threshold):
    """Independent integer model: auto rate budget, one coupon, selectable packet.

    Enumerates purchases, ordered partitions, coupon positions and packet choices.
    No production solver, rounding, allocation or resource helpers are used.
    """
    best = None
    optional = len(prices) - required
    for selected in itertools.product((False, True), repeat=optional):
        units = list(range(required)) + [required + i for i, yes in enumerate(selected) if yes]
        for blocks in ordered_partitions(units):
            for coupon_position in range(-1, len(blocks)):
                for red_choices in itertools.product((False, True), repeat=len(blocks)):
                    vip, red, total, valid = vip_budget, red_budget, 0, True
                    for pos, block in enumerate(blocks):
                        goods = sum(prices[i] for i in block)
                        discount = min(goods * rate_num // rate_den, vip)
                        vip -= discount; due = goods - discount
                        if pos == coupon_position:
                            if due < threshold:
                                valid = False; break
                            due = max(0, due - 10)
                        if red_choices[pos]:
                            use = min(red, due, 20)
                            red -= use; due -= use
                        total += due
                    if valid:
                        score = total, sum(selected), len(blocks)
                        if best is None or score < best:
                            best = score
    return best


class OracleTests(unittest.TestCase):
    def test_seeded_carts_match_independent_order_and_resource_oracle(self):
        rng = random.Random(20261021)
        for case in range(40):
            n = rng.randint(2, 4); required = n - rng.randint(0, 1)
            prices = [rng.randint(10, 60) for _ in range(n)]
            vip, red, threshold = rng.randint(0, 20), rng.randint(0, 30), rng.randint(20, 100)
            num, den = rng.choice([(1, 10), (1, 4), (1, 2)])
            value = lambda c: str(Decimal(c) / 100)
            rows = [item(str(i), value(p), required=i < required) for i, p in enumerate(prices)]
            rules = [percent("vip", rate=str(Decimal(num) / den), rounding="floor", rounding_scope="checkout", resource_id="vip-budget"),
                     fixed(threshold=value(threshold), discount="0.10"), balance(cap="0.20")]
            state = w.new_state(cart(rows, rules, [money_resource("vip-budget", value(vip)), money_resource("red", value(red))]))
            state["preferences"].update(allow_addons=True, approved_optional_ids=[str(i) for i in range(required, n)])
            plan = w.comparison(state)["plans"]["cheapest"]
            s = plan["summary"]
            observed = int(Decimal(s["total_spend"]) * 100), s["optional_units"], s["checkouts"]
            self.assertEqual(observed, oracle(prices, required, vip, red, num, den, threshold), f"case={case}, prices={prices}")


class DiscountCLITests(unittest.TestCase):
    def call(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "scripts/shopping.py"), *map(str, args)], capture_output=True, text=True)

    def test_discount_demo_and_persistent_purchase_roundtrip(self):
        demo = self.call("demo", "--discounts", "--format", "json")
        self.assertEqual(demo.returncode, 0, demo.stderr)
        self.assertEqual(json.loads(demo.stdout)["plans"]["cheapest"]["summary"]["total_spend"], "67.22")
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self.call("new", directory, "--input", ROOT / "fixtures/discounts.json").returncode, 0)
            out = self.call("compare", directory)
            self.assertEqual(out.returncode, 0, out.stderr)
            order = json.loads(out.stdout)["plans"]["cheapest"]["orders"][0]
            proof = {"expected_revision": 1, "id": "p1", "pay_now": order["pay_now"], "coupon_ids": ["red", "coins"],
                     "resource_usage": {"red": "10.00", "coins": 353}, "source": "已核对结算并购买"}
            path = Path(directory) / "receipt.json"; path.write_text(json.dumps(proof))
            out = self.call("purchase", directory, "--plan", "cheapest", "--order", 1, "--receipt", path)
            self.assertEqual(out.returncode, 0, out.stderr)
            saved = json.loads((Path(directory) / "session.json").read_text())
            self.assertEqual({r["id"]: r["balance"] for r in saved["cart"]["resources"]}, {"red": "0.00", "coins": 147})
            self.assertEqual(json.loads(self.call("compare", directory).stdout)["status"], "complete")

    def test_extended_search_failure_replaces_prior_success(self):
        with tempfile.TemporaryDirectory() as directory:
            self.call("new", directory, "--input", ROOT / "fixtures/discounts.json")
            self.assertEqual(self.call("compare", directory).returncode, 0)
            out = self.call("compare", directory, "--time-limit", "0.000000000001")
            self.assertEqual(out.returncode, 3, out.stderr)
            self.assertEqual(json.loads(out.stdout)["plans"], {})
            shown = self.call("show", directory)
            self.assertEqual(json.loads(shown.stdout)["latest_result"]["status"], "search_limit")


if __name__ == "__main__":
    unittest.main()
