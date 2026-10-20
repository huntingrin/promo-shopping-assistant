"""Taobao/Tmall product scope, shared checkout domain and coupon eligibility."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import workflow as w


def item(ident, price="100", platform="taobao", **extra):
    return dict(id=ident, platform=platform, shop=ident, price=price, **extra)


def coupon(platform="taobao", **extra):
    return dict(id="coupon", platform=platform, mode="selectable", threshold="200", discount="30", basis="price", **extra)


def cart(items=None, promotions=None):
    rows = items if items is not None else [item("a", marketplace="taobao"), item("b", platform="tmall")]
    return {"version": 1, "items": rows, "promotions": promotions or [],
            "shipping": [{"platform": x["platform"], "shop": x["shop"], "fee": "0", "basis": "price"} for x in rows]}


class TaobaoTests(unittest.TestCase):
    def test_other_platforms_and_tmall_global_are_rejected_in_all_sections(self):
        for platform in ("jd", "pdd", "amazon", "tmall.hk", "tmall_global", "demo"):
            for section in ("items", "promotions", "shipping"):
                data = cart(promotions=[coupon()])
                data[section][0]["platform"] = platform
                with self.subTest(platform=platform, section=section), self.assertRaises(w.InputError):
                    w.new_state(data)

    def test_unsupported_optional_item_is_not_silently_ignored(self):
        data = cart()
        data["items"].append(item("foreign", platform="jd", required=False))
        with self.assertRaises(w.InputError):
            w.new_state(data)

    def test_taobao_and_tmall_can_share_verified_checkout_group_and_coupon(self):
        state = w.new_state(cart(promotions=[coupon()]))
        out = w.comparison(state)
        self.assertEqual(out["market"], "taobao-tmall")
        self.assertEqual(out["rule_profile"], "taobao-tmall-v2")
        self.assertEqual(out["minimum_checkouts"], 1)
        self.assertEqual(out["plans"]["cheapest"]["summary"]["total_spend"], "170.00")
        self.assertEqual({x["platform"] for x in state["cart"]["items"]}, {"taobao"})
        self.assertEqual({x["marketplace"] for x in out["plans"]["cheapest"]["orders"][0]["items"]}, {"taobao", "tmall"})

    def test_tmall_only_coupon_does_not_count_taobao_goods_towards_threshold(self):
        for scoped in (coupon(platform="tmall"), coupon(marketplaces=["tmall"])):
            out = w.comparison(w.new_state(cart(promotions=[scoped])))
            self.assertEqual(out["plans"]["cheapest"]["summary"]["total_spend"], "200.00")
            self.assertEqual(out["plans"]["cheapest"]["coupon_usage"], {})

    def test_marketplace_filter_combines_with_tags_and_item_scope_using_and(self):
        rows = [item("a", "20", marketplace="taobao", tags=["red"]),
                item("b", "20", platform="tmall", tags=["red"]),
                item("c", "50", platform="tmall", tags=["blue"])]
        rule = coupon(marketplaces=["tmall"], tags=["red"], item_ids=["a", "b"])
        rule.update(threshold="20", discount="5")
        out = w.comparison(w.new_state(cart(rows, [rule])))
        plan = out["plans"]["cheapest"]
        self.assertEqual(plan["summary"]["total_spend"], "85.00")
        traces = [p for order in plan["orders"] for p in order["promotions"]]
        self.assertEqual(traces[0]["eligible_units"], ["b#1"])

    def test_unknown_store_type_blocks_scoped_coupon_until_resolved(self):
        data = cart([item("a")], [dict(coupon(platform="tmall"), threshold="100")])
        state = w.new_state(data)
        out = w.comparison(state)
        self.assertEqual(out["status"], "needs_information")
        self.assertEqual(out["plans"], {})
        self.assertIn("marketplace:coupon:a", [q["id"] for q in out["questions"]])
        state = w.update_state(state, {"expected_revision": 1, "reason": "用户确认这是天猫店", "items": [{"id": "a", "marketplace": "tmall"}]})
        self.assertEqual(w.comparison(state)["plans"]["cheapest"]["summary"]["total_spend"], "70.00")

    def test_unrelated_unknown_store_type_does_not_block_shop_scoped_rule(self):
        rule = dict(coupon(platform="tmall", shops=["b"]), threshold="100")
        out = w.comparison(w.new_state(cart([item("a"), item("b", platform="tmall")], [rule])))
        self.assertEqual(out["status"], "compared_under_input_rules")
        self.assertEqual(out["plans"]["cheapest"]["summary"]["total_spend"], "170.00")

    def test_coupon_counts_are_shared_across_store_types_and_checkout_groups(self):
        rows = [item("a", "200", marketplace="taobao", checkout_group="now"),
                item("b", "200", platform="tmall", checkout_group="later")]
        out = w.comparison(w.new_state(cart(rows, [coupon(total_uses=1)])))
        self.assertEqual(out["minimum_checkouts"], 2)
        self.assertEqual(out["plans"]["cheapest"]["summary"]["total_spend"], "370.00")
        self.assertEqual(out["plans"]["cheapest"]["coupon_usage"], {"coupon": 1})

    def test_coupon_after_price_or_tail_payment_is_not_discounted_again(self):
        for kind in ("after_coupons", "tail_payment", "unknown"):
            state = w.new_state(cart([item("a", price_kind=kind)]))
            out = w.comparison(state)
            self.assertEqual(out["status"], "needs_information")
            self.assertEqual(out["plans"], {})
            state = w.update_state(state, {"expected_revision": 1, "reason": "核对到优惠前活动价", "items": [{"id": "a", "price_kind": "before_order_promotions", "price": "120"}]})
            self.assertEqual(w.comparison(state)["plans"]["cheapest"]["summary"]["total_spend"], "120.00")

    def test_tmall_alias_update_merges_shipping_and_preserves_store_type(self):
        state = w.new_state(cart())
        state = w.update_state(state, {"expected_revision": 1, "reason": "天猫店确认有6元运费", "shipping": [{"platform": "tmall", "shop": "b", "fee": "6"}]})
        self.assertEqual(len(state["cart"]["shipping"]), 2)
        self.assertEqual(next(x for x in state["cart"]["items"] if x["id"] == "b")["marketplace"], "tmall")
        self.assertEqual(w.comparison(state)["plans"]["cheapest"]["summary"]["total_spend"], "206.00")

    def test_platform_and_store_type_conflicts_are_rejected(self):
        with self.assertRaises(w.InputError):
            w.new_state(cart([item("a", platform="tmall", marketplace="taobao")]))
        with self.assertRaises(w.InputError):
            w.new_state(cart(promotions=[coupon(platform="tmall", marketplaces=["taobao"])]))

    def test_same_shop_id_cannot_represent_two_different_store_types(self):
        data = cart()
        data["items"][1]["shop"] = "a"
        data["shipping"] = data["shipping"][:1]
        with self.assertRaises(w.InputError):
            w.new_state(data)

    def test_scope_changes_do_not_modify_task_on_failed_update(self):
        state = w.new_state(cart()); before = deepcopy(state)
        with self.assertRaises(w.InputError):
            w.update_state(state, {"expected_revision": 1, "reason": "其他平台", "items": [{"id": "a", "platform": "jd"}]})
        self.assertEqual(state, before)

    def test_pre_profile_cached_result_is_invalidated(self):
        state = w.new_state(cart())
        cached = w.comparison(state)
        legacy_payload = {k: state[k] for k in ("cart", "preferences", "questions", "data_as_of", "coupon_balances")}
        cached["input_fingerprint"] = hashlib.sha256(json.dumps(legacy_payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        state["latest_result"] = cached
        self.assertIsNone(w.current_result(state))


if __name__ == "__main__":
    unittest.main()
