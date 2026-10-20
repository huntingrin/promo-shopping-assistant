#!/usr/bin/env python3
"""Exact, offline promotion optimizer. Python 3.10+, standard library only.

See references/input-spec.md for the intentionally explicit promotion semantics.
Amounts are integer cents internally; no platform rules are hard-coded.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import json
from pathlib import Path
import sys
import time


class InputError(ValueError):
    pass


class SearchLimit(RuntimeError):
    pass


def check(condition, message):
    if not condition:
        raise InputError(message)


def keys(obj, allowed, required, where):
    check(isinstance(obj, dict), f"{where}: 应为对象")
    check(not (obj.keys() - allowed), f"{where}: 未知字段 {sorted(obj.keys() - allowed)}")
    check(not (required - obj.keys()), f"{where}: 缺少字段 {sorted(required - obj.keys())}")


def string(value, where):
    check(isinstance(value, str) and bool(value.strip()), f"{where}: 应为非空字符串")
    return value


def integer(value, where, low=0, high=1_000_000):
    check(type(value) is int and low <= value <= high, f"{where}: 应为 {low}..{high} 的整数")
    return value


def boolean(value, where):
    check(type(value) is bool, f"{where}: 应为布尔值")
    return value


def money(value, where):
    check(isinstance(value, (str, int, Decimal)) and not isinstance(value, bool),
          f"{where}: 金额使用十进制字符串或 JSON 数字")
    try:
        number = Decimal(value)
        check(number.is_finite() and 0 <= number <= Decimal("100000000"),
              f"{where}: 金额应为 0..100000000 的有限数")
        cents = number * 100
        check(cents == cents.to_integral_value(), f"{where}: 金额最多两位小数")
        return int(cents)
    except (InvalidOperation, ValueError, OverflowError) as exc:
        raise InputError(f"{where}: 非法金额 {value!r}") from exc


def strings(value, where):
    check(isinstance(value, list) and bool(value), f"{where}: 应为非空字符串数组")
    result = tuple(string(x, where) for x in value)
    check(len(set(result)) == len(result), f"{where}: 不得重复")
    return result


BASES = {"price", "original_price", "threshold_price", "remaining"}


@dataclass(frozen=True)
class Item:
    unit_id: str
    product_id: str
    name: str
    platform: str
    shop: str
    category: str
    tags: tuple[str, ...]
    checkout_group: str
    price: int
    original_price: int
    threshold_price: int
    presale_discount: int
    deposit_paid: int
    required: bool

    @property
    def effective(self):
        return self.price - self.presale_discount


@dataclass(frozen=True)
class Promotion:
    id: str
    name: str
    platform: str
    mode: str
    threshold: int
    discount: int
    basis: str
    repeat: bool
    cap: int | None
    total_uses: int | None
    exclusive_group: str | None
    exclusive_scope: str
    item_ids: tuple[str, ...]
    shops: tuple[str, ...]
    categories: tuple[str, ...]
    tags: tuple[str, ...]

    def matches(self, item):
        return (item.platform == self.platform
                and (not self.item_ids or item.product_id in self.item_ids)
                and (not self.shops or item.shop in self.shops)
                and (not self.categories or item.category in self.categories)
                and (not self.tags or bool(set(self.tags) & set(item.tags))))


@dataclass(frozen=True)
class Shipping:
    fee: int
    free_threshold: int | None
    basis: str


@dataclass
class Scenario:
    items: tuple[Item, ...]
    promotions: tuple[Promotion, ...]
    shipping: dict[tuple[str, str], Shipping]
    max_orders: int
    label: str
    assumptions: list[str]


def parse_scenario(data):
    keys(data, {"version", "label", "assumptions", "items", "promotions", "shipping", "max_orders"},
         {"version", "items", "promotions"}, "input")
    check(type(data["version"]) is int and data["version"] == 1, "仅支持 version=1")
    check(isinstance(data["items"], list) and 1 <= len(data["items"]) <= 12,
          "items: 需要 1..12 行商品，数量展开后也不得超过 12 件")
    items, ids = [], set()
    for row in data["items"]:
        allowed = {"id", "name", "platform", "shop", "category", "tags", "checkout_group",
                   "price", "original_price", "threshold_price", "presale_discount", "deposit_paid",
                   "required", "quantity", "source"}
        keys(row, allowed, {"id", "platform", "shop", "price"}, "item")
        pid = string(row["id"], "item.id")
        check(pid not in ids and "#" not in pid, "item.id 不得重复或包含 #")
        ids.add(pid)
        price = money(row["price"], f"{pid}.price")
        reduction = money(row.get("presale_discount", "0"), f"{pid}.presale_discount")
        paid = money(row.get("deposit_paid", "0"), f"{pid}.deposit_paid")
        check(reduction <= price and paid <= price - reduction,
              f"{pid}: 定金已付额不能超过预售价减预售立减后的金额")
        required = boolean(row.get("required", True), f"{pid}.required")
        check(not paid or required, f"{pid}: 已付定金的商品必须 required=true")
        qty = integer(row.get("quantity", 1), f"{pid}.quantity", 1, 12)
        for index in range(qty):
            items.append(Item(
                f"{pid}#{index + 1}", pid, string(row.get("name", pid), f"{pid}.name"),
                string(row["platform"], f"{pid}.platform"), string(row["shop"], f"{pid}.shop"),
                string(row.get("category", "general"), f"{pid}.category"),
                strings(row["tags"], f"{pid}.tags") if "tags" in row else (),
                string(row.get("checkout_group", "default"), f"{pid}.checkout_group"), price,
                money(row.get("original_price", row["price"]), f"{pid}.original_price"),
                money(row["threshold_price"], f"{pid}.threshold_price")
                if "threshold_price" in row else price - reduction,
                reduction, paid, required))
    check(len(items) <= 12, "商品数量展开后超过 12 件：请缩小候选范围")
    check(any(x.required for x in items), "至少需要一件 required=true 的商品")
    check(isinstance(data["promotions"], list) and len(data["promotions"]) <= 12,
          "promotions: 最多支持 12 条优惠规则")
    promotions, promo_ids = [], set()
    for row in data["promotions"]:
        allowed = {"id", "name", "platform", "mode", "threshold", "discount", "basis", "repeat",
                   "cap", "total_uses", "exclusive_group", "exclusive_scope", "item_ids", "shops",
                   "categories", "tags", "source"}
        keys(row, allowed, {"id", "platform", "mode", "threshold", "discount", "basis"}, "promotion")
        rid = string(row["id"], "promotion.id")
        check(rid not in promo_ids, f"优惠 id 重复: {rid}")
        promo_ids.add(rid)
        mode = row["mode"]
        check(mode in ("automatic", "selectable"), f"{rid}.mode: 应为 automatic 或 selectable")
        basis = row["basis"]
        check(isinstance(basis, str) and basis in BASES, f"{rid}.basis: 非法计价基数")
        uses = row.get("total_uses", 1 if mode == "selectable" else None)
        if uses is not None:
            integer(uses, f"{rid}.total_uses", 0, 12)
        group = row.get("exclusive_group")
        if group is not None:
            string(group, f"{rid}.exclusive_group")
        scope = row.get("exclusive_scope", "checkout")
        check(scope in ("checkout", "item"), f"{rid}.exclusive_scope: 应为 checkout 或 item")
        check(mode != "automatic" or (uses is None and group is None),
              f"{rid}: automatic 规则必须不限次数且没有互斥组；可选择/限次活动请用 selectable")
        threshold = money(row["threshold"], f"{rid}.threshold")
        discount = money(row["discount"], f"{rid}.discount")
        repeat = boolean(row.get("repeat", False), f"{rid}.repeat")
        check(discount > 0 and (not repeat or threshold > 0),
              f"{rid}: discount 必须大于0，每满减的 threshold 必须大于0")
        filters = {key: strings(row[key], f"{rid}.{key}") if key in row else ()
                   for key in ("item_ids", "shops", "categories", "tags")}
        check(set(filters["item_ids"]) <= ids, f"{rid}.item_ids: 包含未知商品")
        promo = Promotion(
            rid, string(row.get("name", rid), f"{rid}.name"), string(row["platform"], f"{rid}.platform"),
            mode, threshold, discount, basis, repeat,
            money(row["cap"], f"{rid}.cap") if row.get("cap") is not None else None,
            uses, group, scope, **filters)
        check(any(promo.matches(item) for item in items), f"{rid}: 规则未匹配任何输入商品，请核对范围")
        promotions.append(promo)
    check(sum(p.total_uses is not None for p in promotions) <= 8, "最多支持 8 条有账户次数限制的优惠")
    shipping = {}
    check(isinstance(data.get("shipping", []), list), "shipping: 应为数组")
    for row in data.get("shipping", []):
        keys(row, {"platform", "shop", "fee", "free_threshold", "basis", "source"},
             {"platform", "shop", "fee", "basis"}, "shipping")
        key = (string(row["platform"], "shipping.platform"), string(row["shop"], "shipping.shop"))
        check(key not in shipping, f"运费配置重复: {key}")
        check(isinstance(row["basis"], str) and row["basis"] in BASES, "shipping.basis: 非法计价基数")
        check(any((i.platform, i.shop) == key for i in items), f"运费配置未匹配店铺: {key}")
        shipping[key] = Shipping(money(row["fee"], "shipping.fee"),
                                 money(row["free_threshold"], "shipping.free_threshold")
                                 if row.get("free_threshold") is not None else None, row["basis"])
    assumptions = data.get("assumptions", [])
    check(isinstance(assumptions, list), "assumptions: 应为数组")
    for entry in assumptions:
        string(entry, "assumptions[]")
    max_orders = integer(data.get("max_orders", len(items)), "max_orders", 1, 12)
    return Scenario(tuple(items), tuple(promotions), shipping, max_orders,
                    string(data.get("label", "用户输入情景"), "label"), list(assumptions))


def allocate(amount, capacities):
    """Proportional allocation in cents with stable largest-remainder rounding."""
    total = sum(capacities)
    if total == 0:
        return tuple(0 for _ in capacities)
    parts = [amount * c // total for c in capacities]
    remainder = amount - sum(parts)
    ranking = sorted(range(len(parts)), key=lambda i: (-(amount * capacities[i] % total), i))
    for index in ranking[:remainder]:
        parts[index] += 1
    return tuple(parts)


@dataclass(frozen=True)
class Option:
    mask: int
    usage: tuple[int, ...]
    balances: tuple[int, ...]
    applied: tuple[dict, ...]
    shipping: tuple[dict, ...]
    pay_now: int


@dataclass(frozen=True)
class Plan:
    pay_now: int
    filler_count: int
    orders: tuple[Option, ...]

    @property
    def score(self):
        return self.pay_now, self.filler_count, len(self.orders)


def fmt(cents):
    return f"{cents // 100}.{cents % 100:02d}"


class Optimizer:
    def __init__(self, scenario, time_limit=15.0, state_limit=200_000):
        self.s = scenario
        self.items = scenario.items
        self.n = len(self.items)
        self.started = time.monotonic()
        self.deadline = self.started + time_limit
        self.state_limit = state_limit
        self.work = 0
        self.state_count = 0
        self.variant_count = 0
        self.required = sum(1 << i for i, x in enumerate(self.items) if x.required)
        self.limited = tuple(p.id for p in scenario.promotions if p.total_uses is not None)
        self.resource_index = {rid: i for i, rid in enumerate(self.limited)}
        self.quota = tuple(p.total_uses for p in scenario.promotions if p.total_uses is not None)
        self.eligible = {
            p.id: sum(1 << i for i, x in enumerate(self.items) if p.matches(x))
            for p in scenario.promotions}
        self.options = lru_cache(maxsize=None)(self._options)
        self.solve = lru_cache(maxsize=None)(self._solve)

    def tick(self):
        self.work += 1
        if self.work % 128 == 0 and time.monotonic() >= self.deadline:
            raise SearchLimit("达到精确搜索时间上限；未证明最优，请缩小商品/优惠范围后重试")

    def indices(self, mask):
        return tuple(i for i in range(self.n) if mask & (1 << i))

    def compatible(self, mask):
        return len({(self.items[i].platform, self.items[i].checkout_group)
                    for i in self.indices(mask)}) == 1

    def base(self, mask, basis, balances):
        if basis == "remaining":
            return sum(balances[i] for i in self.indices(mask))
        attr = "effective" if basis == "price" else basis
        return sum(getattr(self.items[i], attr) for i in self.indices(mask))

    def _options(self, mask):
        self.tick()
        if not self.compatible(mask):
            return ()
        initial = tuple(x.effective if mask & (1 << i) else 0 for i, x in enumerate(self.items))
        applicable = [p for p in self.s.promotions if mask & self.eligible[p.id] and p.total_uses != 0]
        best = {}

        def visit(pos, balances, usage, occupied, applied):
            self.tick()
            if pos == len(applicable):
                self.variant_count += 1
                if self.variant_count > self.state_limit * 4:
                    raise SearchLimit("订单优惠组合超过搜索上限；请缩小优惠范围")
                shipping = []
                shops = sorted({(self.items[i].platform, self.items[i].shop) for i in self.indices(mask)})
                for key in shops:
                    config = self.s.shipping.get(key, Shipping(0, None, "price"))
                    shop_mask = sum(1 << i for i in self.indices(mask)
                                    if (self.items[i].platform, self.items[i].shop) == key)
                    base = self.base(shop_mask, config.basis, balances)
                    fee = 0 if config.free_threshold is not None and base >= config.free_threshold else config.fee
                    shipping.append({"platform": key[0], "shop": key[1], "basis": config.basis,
                                     "basis_amount": fmt(base), "fee": fmt(fee)})
                cost = sum(balances) - sum(self.items[i].deposit_paid for i in self.indices(mask))
                cost += sum(money(x["fee"], "internal.shipping") for x in shipping)
                option = Option(mask, usage, balances, applied, tuple(shipping), cost)
                old = best.get(usage)
                if old is None or (cost, len(applied)) < (old.pay_now, len(old.applied)):
                    best[usage] = option
                return
            promo = applicable[pos]
            eligible = mask & self.eligible[promo.id]
            if promo.mode == "selectable":
                visit(pos + 1, balances, usage, occupied, applied)
            if promo.exclusive_group:
                for group, scope, used_mask in occupied:
                    if group == promo.exclusive_group and (
                            scope == "checkout" or promo.exclusive_scope == "checkout" or used_mask & eligible):
                        return
            base = self.base(eligible, promo.basis, balances)
            if base < promo.threshold:
                if promo.mode == "automatic":
                    visit(pos + 1, balances, usage, occupied, applied)
                return
            cycles = base // promo.threshold if promo.repeat else 1
            requested = cycles * promo.discount
            if promo.cap is not None:
                requested = min(requested, promo.cap)
            capacities = tuple(max(0, balances[i] - self.items[i].deposit_paid)
                               if eligible & (1 << i) else 0 for i in range(self.n))
            actual = min(requested, sum(capacities))
            if actual == 0:
                if promo.mode == "automatic":
                    visit(pos + 1, balances, usage, occupied, applied)
                return
            shares = allocate(actual, capacities)
            next_balances = tuple(x - y for x, y in zip(balances, shares))
            next_usage = list(usage)
            if promo.id in self.resource_index:
                next_usage[self.resource_index[promo.id]] += 1
            next_occupied = occupied
            if promo.exclusive_group:
                next_occupied += ((promo.exclusive_group, promo.exclusive_scope, eligible),)
            trace = {"id": promo.id, "name": promo.name, "basis": promo.basis,
                     "basis_amount": fmt(base), "threshold": fmt(promo.threshold), "cycles": cycles,
                     "requested_discount": fmt(requested), "actual_discount": fmt(actual),
                     "eligible_units": [self.items[i].unit_id for i in self.indices(eligible)],
                     "allocation": {self.items[i].unit_id: fmt(shares[i]) for i in self.indices(eligible)}}
            visit(pos + 1, next_balances, tuple(next_usage), next_occupied, applied + (trace,))

        visit(0, initial, (0,) * len(self.quota), (), ())
        return tuple(best.values())

    def _solve(self, remaining, quota, orders_left):
        self.tick()
        self.state_count += 1
        if self.state_count > self.state_limit:
            raise SearchLimit("动态规划状态超过上限；请缩小商品或限次优惠范围")
        required = remaining & self.required
        if not required:
            return Plan(0, 0, ())
        if orders_left == 0:
            return None
        pivot = required & -required
        # Only enumerate the pivot's platform/checkout group; other groups cannot combine.
        pitem = self.items[pivot.bit_length() - 1]
        compatible = sum(1 << i for i in self.indices(remaining)
                         if (self.items[i].platform, self.items[i].checkout_group)
                         == (pitem.platform, pitem.checkout_group))
        rest = compatible ^ pivot
        sub, best = rest, None
        while True:
            self.tick()
            subset = sub | pivot
            for option in self.options(subset):
                if any(used > available for used, available in zip(option.usage, quota)):
                    continue
                tail = self.solve(remaining ^ subset, tuple(q - u for q, u in zip(quota, option.usage)),
                                  orders_left - 1)
                if tail is None:
                    continue
                fillers = (subset & ~self.required).bit_count()
                candidate = Plan(option.pay_now + tail.pay_now, fillers + tail.filler_count,
                                 (option,) + tail.orders)
                if best is None or candidate.score < best.score:
                    best = candidate
            if sub == 0:
                break
            sub = (sub - 1) & rest
        return best

    def order_dict(self, option, index):
        chosen = self.indices(option.mask)
        paid = sum(self.items[i].deposit_paid for i in chosen)
        return {
            "order": index, "platform": self.items[chosen[0]].platform,
            "checkout_group": self.items[chosen[0]].checkout_group,
            "items": [{"unit_id": self.items[i].unit_id, "id": self.items[i].product_id,
                       "name": self.items[i].name, "shop": self.items[i].shop,
                       "required": self.items[i].required,
                       "effective_price": fmt(self.items[i].effective),
                       "after_promotions": fmt(option.balances[i]),
                       "deposit_paid": fmt(self.items[i].deposit_paid),
                       "pay_now": fmt(option.balances[i] - self.items[i].deposit_paid)} for i in chosen],
            "promotions": list(option.applied), "shipping": list(option.shipping),
            "deposit_paid": fmt(paid), "pay_now": fmt(option.pay_now),
            "total_spend": fmt(option.pay_now + paid)}

    def run(self):
        all_mask = (1 << self.n) - 1
        order_limit = min(self.s.max_orders, self.required.bit_count())
        plan = self.solve(all_mask, self.quota, order_limit)
        if plan is None:
            raise InputError("当前 max_orders / checkout_group 限制下没有可行方案")
        baseline = self.solve(self.required, self.quota, order_limit)
        selected = 0
        for option in plan.orders:
            selected |= option.mask
        chosen = self.indices(selected)
        paid = sum(self.items[i].deposit_paid for i in chosen)
        effective = sum(self.items[i].effective for i in chosen)
        final_goods = sum(sum(o.balances) for o in plan.orders)
        single = self.options(self.required)
        single_best = min((o for o in single if all(u <= q for u, q in zip(o.usage, self.quota))),
                          key=lambda o: o.pay_now, default=None)
        warnings = ["精确最优仅覆盖本次输入的商品、规则、固定执行顺序及分摊语义；不代表平台实际最低价。",
                    "同平台、同 checkout_group 才能合并；每个 checkout 是一次结算批次，可能包含多个店铺子单。"]
        missing_shipping = sorted({f"{x.platform}/{x.shop}" for x in self.items
                                   if (x.platform, x.shop) not in self.s.shipping})
        if missing_shipping:
            warnings.append("未配置运费的店铺按0元模拟：" + "、".join(missing_shipping))
        if any(p.basis == "remaining" for p in self.s.promotions):
            warnings.append("存在按剩余金额判断门槛的规则；结果受列表执行顺序与逐件分摊方法影响。")
        if time.monotonic() > self.deadline:
            raise SearchLimit("达到精确搜索时间上限；本次不输出最优方案")
        return {
            "status": "optimal_under_input_rules", "version": 1, "label": self.s.label,
            "objective": ["min_pay_now", "min_optional_units", "min_checkouts"],
            "summary": {
                "original_price_selected": fmt(sum(self.items[i].original_price for i in chosen)),
                "price_before_presale_discount": fmt(sum(self.items[i].price for i in chosen)),
                "presale_discount": fmt(sum(self.items[i].presale_discount for i in chosen)),
                "effective_price_selected": fmt(effective), "promotions_discount": fmt(effective - final_goods),
                "shipping": fmt(plan.pay_now + paid - final_goods), "deposit_paid": fmt(paid),
                "pay_now": fmt(plan.pay_now), "total_spend": fmt(plan.pay_now + paid),
                "checkouts": len(plan.orders), "optional_units": plan.filler_count,
                "required_only_pay_now": fmt(baseline.pay_now) if baseline else None,
                "savings_vs_required_only": fmt(baseline.pay_now - plan.pay_now) if baseline else None,
                "required_only_single_checkout_pay_now": fmt(single_best.pay_now) if single_best else None},
            "selected_optional_units": [self.items[i].unit_id for i in chosen if not self.items[i].required],
            "skipped_optional_units": [x.unit_id for i, x in enumerate(self.items)
                                       if not x.required and not selected & (1 << i)],
            "coupon_usage": dict(Counter(t["id"] for o in plan.orders for t in o.applied
                                         if t["id"] in self.resource_index)),
            "orders": [self.order_dict(o, i + 1) for i, o in enumerate(plan.orders)],
            "assumptions": self.s.assumptions, "warnings": warnings,
            "stats": {"expanded_units": self.n, "dp_states": self.state_count,
                      "checkout_subsets": self.options.cache_info().currsize,
                      "promotion_variants": self.variant_count,
                      "elapsed_seconds": round(time.monotonic() - self.started, 4)}}


def optimize(data, time_limit=15.0, state_limit=200_000):
    if not isinstance(time_limit, (int, float)) or not 0 < time_limit <= 300:
        raise InputError("time_limit 应为大于0且不超过300的秒数")
    integer(state_limit, "state_limit", 1, 1_000_000)
    return Optimizer(parse_scenario(data), time_limit, state_limit).run()


def render_text(result):
    s = result["summary"]
    lines = [result["label"], f"情景最低总支出：¥{s['total_spend']}；已付定金：¥{s['deposit_paid']}；还需付：¥{s['pay_now']}",
             f"拆成 {s['checkouts']} 次结算；选用 {s['optional_units']} 件可选凑单品。",
             f"不加凑单品的最优应付：¥{s['required_only_pay_now']}；加入凑单品实际省：¥{s['savings_vs_required_only']}"]
    if s["required_only_single_checkout_pay_now"] is not None:
        lines.append(f"必买品全部合并结算应付：¥{s['required_only_single_checkout_pay_now']}")
    for order in result["orders"]:
        lines.append(f"\n第 {order['order']} 次结算 ({order['platform']} / {order['checkout_group']})")
        for item in order["items"]:
            extra = " [凑单]" if not item["required"] else ""
            lines.append(f"  {item['unit_id']} {item['name']} ({item['shop']}){extra}：还需付 ¥{item['pay_now']}")
        for p in order["promotions"]:
            lines.append(f"  {p['name']}：计入门槛 ¥{p['basis_amount']}，减 ¥{p['actual_discount']}")
        for shipping in order["shipping"]:
            lines.append(f"  {shipping['shop']} 运费 ¥{shipping['fee']}")
        lines.append(f"  本次应付 ¥{order['pay_now']}（含此前定金总支出 ¥{order['total_spend']}）")
    lines.append("")
    lines.extend("假设：" + x for x in result["assumptions"])
    lines.extend("提示：" + x for x in result["warnings"])
    return "\n".join(lines) + "\n"


def no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InputError(f"JSON 字段重复: {key}")
        result[key] = value
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="JSON 文件路径，或 - 从 stdin 读取")
    parser.add_argument("--format", choices=("json", "text"), default="json")
    parser.add_argument("--output", type=Path, help="保存结果；不指定则写 stdout")
    parser.add_argument("--time-limit", type=float, default=15.0, help="精确搜索秒数上限，默认15，最多300")
    args = parser.parse_args(argv)
    try:
        raw = sys.stdin.read() if args.input == "-" else Path(args.input).read_text(encoding="utf-8")
        data = json.loads(raw, parse_float=Decimal, object_pairs_hook=no_duplicate_keys)
        result = optimize(data, args.time_limit)
        output = render_text(result) if args.format == "text" else json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.write_text(output, encoding="utf-8")
        else:
            sys.stdout.write(output)
        return 0
    except (InputError, SearchLimit, OSError, json.JSONDecodeError) as exc:
        status = "search_limit" if isinstance(exc, SearchLimit) else "invalid_input"
        sys.stderr.write(json.dumps({"status": status, "error": str(exc)}, ensure_ascii=False) + "\n")
        return 3 if status == "search_limit" else 2


if __name__ == "__main__":
    raise SystemExit(main())
