"""Observable price invariants and an independent exhaustive small-cart oracle."""
import copy
from decimal import Decimal
import importlib.util
import itertools
import json
from pathlib import Path
import random
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/optimize.py"
spec = importlib.util.spec_from_file_location("promo_optimizer", SCRIPT)
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)


def item(id, price, **kwargs):
    return dict(id=id, platform="demo", shop="A", price=str(price), **kwargs)


def promo(id="c", threshold="200", discount="30", **kwargs):
    return dict(id=id, platform="demo", mode="selectable", threshold=str(threshold),
                discount=str(discount), basis="price", **kwargs)


def scenario(items, promotions=(), **kwargs):
    return dict(version=1, items=items, promotions=list(promotions), **kwargs)


def partitions(values):
    if not values:
        yield []
        return
    first, *rest = values
    for blocks in partitions(rest):
        yield [[first]] + blocks
        for i in range(len(blocks)):
            yield [block + [first] if j == i else block[:] for j, block in enumerate(blocks)]


def independent_oracle(prices, required_count, coupons, auto_threshold, auto_discount, auto_cap):
    """Enumerate optional purchases, Bell partitions and account-coupon assignments.

    Only this oracle's test domain: all items eligible, parallel thresholds,
    one use of each coupon, no shipping/deposits. Uses no optimizer primitives.
    """
    best = None
    for choose in itertools.product((False, True), repeat=len(prices) - required_count):
        selected = list(range(required_count)) + [required_count + j for j, yes in enumerate(choose) if yes]
        for blocks in partitions(selected):
            totals = [sum(prices[i] for i in block) for block in blocks]
            for assignments in itertools.product(range(-1, len(blocks)), repeat=len(coupons)):
                due = [max(0, amount - min(amount // auto_threshold * auto_discount, auto_cap)) for amount in totals]
                valid = True
                for (threshold, discount), assigned in zip(coupons, assignments):
                    if assigned < 0:
                        continue
                    if totals[assigned] < threshold:
                        valid = False
                        break
                    due[assigned] = max(0, due[assigned] - discount)
                if valid:
                    score = sum(due), sum(choose), len(blocks)
                    if best is None or score < best:
                        best = score
    return best


class OptimizerTests(unittest.TestCase):
    def run_case(self, data):
        return m.optimize(data)

    def test_complete_example(self):
        data = json.loads((ROOT / "fixtures/original-example.json").read_text())
        out = self.run_case(data)
        self.assertEqual(out["summary"]["pay_now"], "345.00")
        self.assertEqual(out["summary"]["required_only_pay_now"], "390.00")
        self.assertEqual(out["summary"]["checkouts"], 2)
        self.assertEqual(out["selected_optional_units"], ["cloth#1"])

    def test_parallel_and_sequential_thresholds(self):
        data = scenario([item("x", 300)], [promo("shop", 300, 50), promo("cat", 300, 20)])
        self.assertEqual(self.run_case(data)["summary"]["pay_now"], "230.00")
        data["promotions"][1]["basis"] = "remaining"
        self.assertEqual(self.run_case(data)["summary"]["pay_now"], "250.00")

    def test_account_coupon_cannot_be_reused(self):
        out = self.run_case(scenario([item("x", 200, quantity=2)], [promo(discount=50)]))
        self.assertEqual(out["summary"]["pay_now"], "350.00")
        self.assertEqual(out["coupon_usage"], {"c": 1})

    def test_global_coupon_assignment_and_split(self):
        a = promo("a", 200, 50, exclusive_group="coupons")
        b = promo("b", 250, 80, exclusive_group="coupons")
        out = self.run_case(scenario([item("x", 250), item("y", 200)], [a, b]))
        self.assertEqual(out["summary"]["pay_now"], "320.00")
        self.assertEqual(out["summary"]["checkouts"], 2)

    def test_filler_actual_cost_and_rejection(self):
        data = scenario([item("x", 199), item("extra", 1, required=False)], [promo()])
        out = self.run_case(data)
        self.assertEqual(out["summary"]["pay_now"], "170.00")
        self.assertEqual(out["summary"]["savings_vs_required_only"], "29.00")
        data["items"][1]["price"] = "100"
        out = self.run_case(data)
        self.assertEqual(out["summary"]["pay_now"], "199.00")
        self.assertEqual(out["selected_optional_units"], [])

    def test_shipping_can_reverse_coupon_benefit(self):
        shipping = [dict(platform="demo", shop="A", fee="20", free_threshold="100", basis="remaining")]
        out = self.run_case(scenario([item("x", 100)], [promo(threshold=100, discount=10)], shipping=shipping))
        self.assertEqual(out["summary"]["pay_now"], "100.00")
        self.assertEqual(out["coupon_usage"], {})

    def test_shipping_filler(self):
        shipping = [dict(platform="demo", shop="A", fee="15", free_threshold="200", basis="price")]
        out = self.run_case(scenario([item("x", 190), item("extra", 10, required=False)], shipping=shipping))
        self.assertEqual(out["summary"]["pay_now"], "200.00")
        self.assertEqual(out["summary"]["savings_vs_required_only"], "5.00")

    def test_presale_total_and_tail_are_separate(self):
        data = json.loads((ROOT / "fixtures/presale.json").read_text())
        s = self.run_case(data)["summary"]
        self.assertEqual((s["pay_now"], s["deposit_paid"], s["total_spend"]), ("620.00", "100.00", "720.00"))

    def test_discount_cannot_refund_deposit(self):
        out = self.run_case(scenario([item("x", 100, deposit_paid="90")], [promo(threshold=100, discount=50)]))
        self.assertEqual(out["summary"]["pay_now"], "0.00")
        self.assertEqual(out["summary"]["total_spend"], "90.00")
        self.assertEqual(out["summary"]["promotions_discount"], "10.00")

    def test_deposit_is_protected_per_item(self):
        out = self.run_case(scenario([item("x", 100, deposit_paid="90"), item("y", 100)],
                                    [promo(threshold=200, discount=150)]))
        self.assertEqual(out["summary"]["pay_now"], "0.00")
        self.assertEqual(out["summary"]["promotions_discount"], "110.00")

    def test_automatic_discount_is_mandatory(self):
        auto = promo("auto", 100, 10)
        auto["mode"] = "automatic"
        c = promo("coupon", 100, 50)
        c["basis"] = "remaining"
        out = self.run_case(scenario([item("x", 100)], [auto, c]))
        self.assertEqual(out["summary"]["pay_now"], "90.00")

    def test_item_exclusivity(self):
        a = promo("a", 200, 20, exclusive_group="dong", exclusive_scope="item")
        b = promo("b", 200, 30, exclusive_group="dong", exclusive_scope="item")
        out = self.run_case(scenario([item("x", 200)], [a, b]))
        self.assertEqual(out["summary"]["pay_now"], "170.00")
        a["item_ids"], b["item_ids"] = ["x"], ["y"]
        out = self.run_case(scenario([item("x", 200), item("y", 200)], [a, b], max_orders=1))
        self.assertEqual(out["summary"]["pay_now"], "350.00")

    def test_scope_does_not_count_ineligible_items(self):
        c = promo(categories=["beauty"])
        out = self.run_case(scenario([item("x", 100, category="beauty"), item("y", 100, category="home")], [c]))
        self.assertEqual(out["summary"]["pay_now"], "200.00")

    def test_platform_and_checkout_groups(self):
        x, y = item("x", 100), item("y", 100)
        y["platform"] = "other"
        out = self.run_case(scenario([x, y], [promo()]))
        self.assertEqual(out["summary"]["checkouts"], 2)
        with self.assertRaises(m.InputError):
            self.run_case(scenario([x, y], max_orders=1))
        y["platform"] = "demo"
        y["checkout_group"] = "later"
        self.assertEqual(self.run_case(scenario([x, y], [promo()]))["summary"]["pay_now"], "200.00")

    def test_repeat_cycles_consume_one_coupon(self):
        out = self.run_case(scenario([item("x", 600)], [promo(repeat=True)]))
        self.assertEqual(out["summary"]["pay_now"], "510.00")
        self.assertEqual(out["coupon_usage"], {"c": 1})

    def test_two_copies_of_coupon(self):
        c = promo(discount=50, total_uses=2, exclusive_group="coupon")
        out = self.run_case(scenario([item("x", 200, quantity=2)], [c]))
        self.assertEqual(out["summary"]["pay_now"], "300.00")
        self.assertEqual(out["coupon_usage"], {"c": 2})

    def test_cent_allocation_conservation_and_floor(self):
        out = self.run_case(scenario([item("x", "0.01", quantity=3)], [promo(threshold=0, discount="0.02")]))
        self.assertEqual(out["summary"]["pay_now"], "0.01")
        allocations = out["orders"][0]["promotions"][0]["allocation"]
        self.assertEqual(sum(Decimal(v) for v in allocations.values()), Decimal("0.02"))
        self.assertTrue(all(Decimal(i["pay_now"]) >= 0 for o in out["orders"] for i in o["items"]))

    def test_ties_prefer_no_filler_then_fewer_orders(self):
        out = self.run_case(scenario([item("x", 100, quantity=2), item("free", 0, required=False)]))
        self.assertEqual(out["summary"]["optional_units"], 0)
        self.assertEqual(out["summary"]["checkouts"], 1)

    def test_rejects_ambiguous_or_invalid_inputs(self):
        base = scenario([item("x", 100)], [promo(threshold=100)])
        cases = []
        d = copy.deepcopy(base); d["items"][0]["prcie"] = "50"; cases.append(d)
        d = copy.deepcopy(base); d["items"][0]["price"] = "1.001"; cases.append(d)
        d = copy.deepcopy(base); d["items"][0]["quantity"] = 13; cases.append(d)
        d = copy.deepcopy(base); d["promotions"][0]["basis"] = "unknown"; cases.append(d)
        d = copy.deepcopy(base); d["promotions"][0]["basis"] = []; cases.append(d)
        d = copy.deepcopy(base); d["shipping"] = [dict(platform="demo", shop="A", fee="0", basis={})]; cases.append(d)
        d = copy.deepcopy(base); d["items"][0]["price"] = "NaN"; cases.append(d)
        d = copy.deepcopy(base); d["items"][0]["deposit_paid"] = "101"; cases.append(d)
        d = copy.deepcopy(base); d["items"][0]["deposit_paid"] = "1"; d["items"][0]["required"] = False; cases.append(d)
        d = copy.deepcopy(base); d["promotions"][0]["item_ids"] = ["unknown"]; cases.append(d)
        d = copy.deepcopy(base); d["promotions"][0]["mode"] = "automatic"; d["promotions"][0]["total_uses"] = 1; cases.append(d)
        for case in cases:
            with self.subTest(case=case), self.assertRaises(m.InputError):
                self.run_case(case)

    def test_limits_never_claim_optimal(self):
        with self.assertRaises(m.SearchLimit):
            m.optimize(scenario([item("x", 100, quantity=4)]), state_limit=1)
        with self.assertRaises(m.SearchLimit):
            m.optimize(scenario([item("x", 100, quantity=4)]), time_limit=1e-12)

    def test_cli_stdin_decimal_and_json_error_protocol(self):
        valid = '{"version":1,"items":[{"id":"x","platform":"d","shop":"a","price":0.29}],"promotions":[]}'
        proc = subprocess.run([sys.executable, str(SCRIPT), "-"], input=valid, text=True, capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["summary"]["pay_now"], "0.29")
        proc = subprocess.run([sys.executable, str(SCRIPT), "-"], input=valid.replace('"version":1', '"version":1,"version":1'), text=True, capture_output=True)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(json.loads(proc.stderr)["status"], "invalid_input")

    def test_random_carts_against_independent_exhaustive_oracle(self):
        rng = random.Random(20261020)
        for case in range(50):
            required_count = rng.randint(1, 4)
            optional_count = rng.randint(0, min(2, 5 - required_count))
            prices = [rng.randint(1, 30) * 10 for _ in range(required_count + optional_count)]
            coupons = [(rng.choice([100, 200, 300]), rng.choice([10, 30, 80])) for _ in range(2)]
            auto_threshold, auto_discount, auto_cap = 200, 30, rng.choice([30, 60, 999])
            rows = [item(str(i), price, required=i < required_count) for i, price in enumerate(prices)]
            auto = promo("auto", auto_threshold, auto_discount, repeat=True, cap=str(auto_cap))
            auto["mode"] = "automatic"
            rules = [auto] + [promo(str(i), threshold, discount) for i, (threshold, discount) in enumerate(coupons)]
            out = self.run_case(scenario(rows, rules))["summary"]
            expected = independent_oracle(prices, required_count, coupons, auto_threshold, auto_discount, auto_cap)
            observed = (int(Decimal(out["pay_now"])), out["optional_units"], out["checkouts"])
            self.assertEqual(observed, expected, f"random cart {case}: {prices}, {coupons}")


if __name__ == "__main__":
    unittest.main()
