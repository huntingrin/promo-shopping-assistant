"""Exact percentage, quantity, payment and shared-resource discount extension.

The original optimizer remains a frozen compatibility path. Every rate is a
reduction fraction; all money, resource budgets and rounding use integers.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import time

from optimize import (InputError, SearchLimit, Optimizer, Option, Plan, Shipping,
                      allocate, check, keys, string, integer, boolean, money, fmt,
                      parse_scenario, optimize as legacy_optimize)

EXT_FIELDS = {"kind", "stage", "rate", "discount_basis", "rounding", "rounding_scope",
              "min_quantity", "quantity_step", "tiers", "per_item_cap", "min_item_payable",
              "min_payable", "whole_checkout", "include_shipping", "resource_id",
              "allocation", "step_value", "units_per_step"}
RESOURCE_FIELDS = {"id", "kind", "balance", "source"}
BASES = {"price", "original_price", "threshold_price", "remaining", "payable"}


def uses_limit(row):
    return row.get("total_uses", None if row.get("kind") == "balance" or row.get("mode") == "automatic" else 1)


def rate(value):
    check(isinstance(value, (str, int, Decimal)) and not isinstance(value, bool), "rate: 使用十进制减免比例，如0.15表示减15%")
    try:
        number = Decimal(value)
        check(number.is_finite() and 0 < number <= 1 and number.as_tuple().exponent >= -6,
              "rate: 应为大于0且不超过1、最多六位小数的减免比例；95折填写0.05")
        return number.as_integer_ratio()
    except (InvalidOperation, ValueError) as exc:
        raise InputError("rate: 非法减免比例") from exc


def rounded(value, ratio, mode):
    numerator, denominator = value * ratio[0], ratio[1]
    whole, remainder = divmod(numerator, denominator)
    return whole + int((mode == "half_up" and remainder * 2 >= denominator)
                       or (mode == "ceil" and remainder > 0))


def capped_allocate(amount, weights, capacities):
    """Stable proportional allocation with hard per-component capacities."""
    parts = [0] * len(capacities)
    remaining = amount
    while remaining:
        left = [c - p for c, p in zip(capacities, parts)]
        active = [w if c > 0 else 0 for w, c in zip(weights, left)]
        if not sum(active):
            active = left
        check(sum(active) > 0, "内部折扣分摊容量不足")
        proposed = allocate(remaining, active)
        used = [min(p, c) for p, c in zip(proposed, left)]
        check(sum(used) > 0, "内部折扣分摊未取得进展")
        parts = [x + y for x, y in zip(parts, used)]
        remaining -= sum(used)
    return tuple(parts)


def required_fields(row):
    fields = {"platform", "mode", "threshold", "basis"}
    kind = row.get("kind", "fixed")
    if kind == "fixed":
        fields.add("discount")
    if kind == "percentage" or "rate" in row:
        fields.update(("discount_basis", "rounding", "rounding_scope"))
        if "tiers" not in row:
            fields.add("rate")
    if kind == "balance":
        fields.update(("resource_id", "allocation"))
    fields.update(k for k in ("min_quantity", "quantity_step", "min_item_payable", "min_payable", "step_value")
                  if k in row and row[k] is None)
    return fields


def validate_rule(row):
    kind = row.get("kind", "fixed")
    check(kind in ("fixed", "percentage", "balance"), "kind: 仅支持 fixed/percentage/balance")
    check(row.get("stage", "goods") in ("goods", "payment"), "stage: 仅支持 goods/payment")
    if row.get("basis") is not None:
        check(isinstance(row["basis"], str) and row["basis"] in BASES, "basis: 非法门槛基数")
    if row.get("discount_basis") is not None:
        check(isinstance(row["discount_basis"], str) and row["discount_basis"] in BASES, "discount_basis: 非法折扣基数")
    if row.get("rate") is not None:
        rate(row["rate"])
    for key in ("rounding", "rounding_scope", "allocation"):
        allowed = {"rounding": ("floor", "half_up", "ceil"), "rounding_scope": ("item", "checkout"),
                   "allocation": ("maximum", "flexible")}[key]
        if row.get(key) is not None:
            check(row[key] in allowed, f"{key}: 仅支持 {allowed}")
    for key in ("min_quantity", "quantity_step", "units_per_step"):
        if row.get(key) is not None:
            integer(row[key], key, 1, 1_000_000_000 if key == "units_per_step" else 12)
    for key in ("per_item_cap", "min_item_payable", "min_payable", "step_value"):
        if row.get(key) is not None:
            value = money(row[key], key)
            check(key != "step_value" or value > 0, "step_value 必须大于0")
    for key in ("whole_checkout", "include_shipping"):
        if key in row:
            boolean(row[key], key)
    if row.get("resource_id") is not None:
        string(row["resource_id"], "resource_id")
    if "tiers" in row:
        check(kind == "percentage" and "rate" not in row, "tiers 仅用于percentage，且不能同时提供rate")
        check(isinstance(row["tiers"], list) and 1 <= len(row["tiers"]) <= 12, "tiers: 需要1..12档")
        previous = 0
        for tier in row["tiers"]:
            keys(tier, {"min_quantity", "rate"}, {"min_quantity", "rate"}, "tier")
            quantity = integer(tier["min_quantity"], "tier.min_quantity", 1, 12)
            check(quantity > previous, "tiers 必须按件数门槛严格递增")
            previous = quantity
            rate(tier["rate"])
    check(not ("quantity_step" in row and (kind != "fixed" or row.get("repeat", False))),
          "quantity_step仅用于固定每N件减额，不能同时按金额repeat")
    check(not (kind != "fixed" and ("discount" in row or row.get("repeat", False))), "比例/余额规则不能再配置固定discount或repeat")
    check(not (row.get("include_shipping", False) and row.get("stage", "goods") != "payment"), "仅payment阶段可抵运费")
    check(not (kind == "balance" and row.get("mode") == "automatic" and row.get("allocation") == "flexible"),
          "automatic余额规则按最大额执行；flexible必须明确允许选择金额")
    check(not (row.get("mode") == "automatic" and row.get("exclusive_group")), "自动优惠不能配置可选择互斥组；档位请使用tiers")
    check(not (kind != "balance" and any(k in row for k in ("allocation", "step_value", "units_per_step"))), "抵扣步长/分配方式仅用于balance")
    check(not (kind == "fixed" and any(k in row for k in ("rate", "tiers", "discount_basis", "rounding", "rounding_scope", "per_item_cap"))), "固定满减不接受比例字段")


def validate_resources(rows, draft=False):
    check(isinstance(rows, list) and len(rows) <= 8, "resources: 最多8项共享余额")
    result = {}
    for row in rows:
        keys(row, RESOURCE_FIELDS, {"id"} if draft else {"id", "kind", "balance"}, "resource")
        ident = string(row["id"], "resource.id")
        check(ident not in result, "共享资源ID重复")
        kind = row.get("kind")
        check(kind in ((None, "money", "points") if draft else ("money", "points")), "resource.kind: 应为money或points")
        balance = row.get("balance")
        if balance is not None and kind is not None:
            value = money(balance, "resource.balance") if kind == "money" else integer(balance, "resource.balance", 0, 1_000_000_000)
        else:
            check(draft, f"共享资源{ident}缺少余额")
            value = None
        result[ident] = {"id": ident, "kind": kind, "balance": value}
    return result


def parse_extended(data):
    raw = deepcopy(data)
    resources = validate_resources(raw.pop("resources", []))
    extras, stub = {}, []
    payment_seen = False
    for row in raw["promotions"]:
        validate_rule(row)
        check(not [key for key in required_fields(row) if row.get(key) is None], f"优惠{row.get('id')}缺少折扣参数")
        kind, stage = row.get("kind", "fixed"), row.get("stage", "goods")
        check(not (payment_seen and stage == "goods"), "goods优惠必须在所有payment优惠之前，不能把支付抵扣放在运费计算前")
        payment_seen |= stage == "payment"
        resource = resources.get(row.get("resource_id"))
        if "resource_id" in row:
            check(resource is not None, "优惠引用了未定义共享资源")
            check(kind == "balance" or resource["kind"] == "money", "积分资源只能通过balance按已核实步长抵扣")
        if kind == "balance" and resource["kind"] == "points":
            check(row.get("step_value") is not None and row.get("units_per_step") is not None,
                  "积分抵扣必须明确每步抵扣金额和消耗积分，不能假设通兑比例")
        if kind == "balance" and resource["kind"] == "money":
            check("units_per_step" not in row, "金额资源自动按分记账，不配置units_per_step")
        base = {k: v for k, v in row.items() if k not in EXT_FIELDS}
        base["discount"] = row.get("discount", "0.01")
        if base["basis"] == "payable":
            base["basis"] = "remaining"
        if base["mode"] == "automatic":
            base["mode"] = "selectable"  # restore after parsing; finite automatic resources are supported here
        base["total_uses"] = uses_limit(row)
        stub.append(base)
        extras[row["id"]] = deepcopy(row)
    raw["promotions"] = stub
    scenario = parse_scenario(raw)
    scenario.promotions = tuple(replace(p, mode=extras[p.id]["mode"]) for p in scenario.promotions)
    return scenario, extras, resources


class DiscountOptimizer(Optimizer):
    def __init__(self, scenario, extras, resources, time_limit, state_limit):
        super().__init__(scenario, time_limit, state_limit)
        self.extra, self.resources = extras, resources
        self.count_index = dict(self.resource_index)
        self.asset_index = {ident: len(self.quota) + i for i, ident in enumerate(resources)}
        self.quota += tuple(r["balance"] for r in resources.values())
        self.options = lru_cache(maxsize=None)(self._discount_options)
        self.solve = lru_cache(maxsize=None)(self._discount_solve)
        self.optional_first = any(p.mode == "automatic" and (p.total_uses is not None or self.extra[p.id].get("resource_id")) for p in self.s.promotions)

    def component_bases(self, eligible, basis, balances, fees, include_shipping):
        values = []
        for i, item in enumerate(self.items):
            if not eligible & (1 << i):
                values.append(0)
            elif basis == "payable":
                values.append(max(0, balances[i] - item.deposit_paid))
            elif basis == "remaining":
                values.append(balances[i])
            else:
                values.append(getattr(item, "effective" if basis == "price" else basis))
        shops = {(self.items[i].platform, self.items[i].shop) for i in self.indices(eligible)}
        values.extend(money(f["pay_now"], "fee") if basis == "payable" and include_shipping
                      and (f["platform"], f["shop"]) in shops else 0 for f in fees)
        return tuple(values)

    def shipping(self, mask, balances):
        rows = []
        for platform, shop in sorted({(self.items[i].platform, self.items[i].shop) for i in self.indices(mask)}):
            config = self.s.shipping.get((platform, shop), Shipping(0, None, "price"))
            subset = sum(1 << i for i in self.indices(mask) if (self.items[i].platform, self.items[i].shop) == (platform, shop))
            base = self.base(subset, config.basis, balances)
            fee = 0 if config.free_threshold is not None and base >= config.free_threshold else config.fee
            rows.append({"platform": platform, "shop": shop, "basis": config.basis,
                         "basis_amount": fmt(base), "fee": fmt(fee), "pay_now": fmt(fee)})
        return tuple(rows)

    def asset_values(self, usage):
        return {ident: fmt(usage[index]) if self.resources[ident]["kind"] == "money" else usage[index]
                for ident, index in self.asset_index.items() if usage[index]}

    def _discount_options(self, mask, quota):
        self.tick()
        if not self.compatible(mask):
            return ()
        initial = tuple(x.effective if mask & (1 << i) else 0 for i, x in enumerate(self.items))
        applicable = [p for p in self.s.promotions if mask & self.eligible[p.id]]
        best = {}

        def visit(pos, balances, usage, occupied, applied, fees=None):
            self.tick()
            if fees is None and (pos == len(applicable) or self.extra[applicable[pos].id].get("stage", "goods") == "payment"):
                fees = self.shipping(mask, balances)
            if pos == len(applicable):
                self.variant_count += 1
                if self.variant_count > self.state_limit * 4:
                    raise SearchLimit("扩展折扣组合超过搜索上限，本次未完成")
                cost = sum(balances) - sum(self.items[i].deposit_paid for i in self.indices(mask))
                cost += sum(money(f["pay_now"], "fee") for f in fees)
                option = Option(mask, usage, balances, applied, fees, cost)
                old = best.get(usage)
                if old is None or (cost, len(applied)) < (old.pay_now, len(old.applied)):
                    best[usage] = option
                return
            p = applicable[pos]; e = self.extra[p.id]
            automatic = p.mode == "automatic"
            if not automatic:
                visit(pos + 1, balances, usage, occupied, applied, fees)
            def unavailable():
                if automatic:
                    visit(pos + 1, balances, usage, occupied, applied, fees)
            eligible = mask & self.eligible[p.id]
            quantity = eligible.bit_count()
            if (quantity < e.get("min_quantity", 1) or (e.get("whole_checkout", False) and eligible != mask)
                    or (p.id in self.count_index and usage[self.count_index[p.id]] >= quota[self.count_index[p.id]])):
                return unavailable()
            for group, scope, used_mask in occupied:
                if group == p.exclusive_group and (scope == "checkout" or p.exclusive_scope == "checkout" or used_mask & eligible):
                    return unavailable()
            current_fees = fees or ()
            include_shipping = e.get("include_shipping", False)
            basis = self.component_bases(eligible, e["basis"], balances, current_fees, include_shipping)
            base = sum(basis)
            if base < p.threshold:
                return unavailable()
            floor = money(e.get("min_item_payable", "0"), "min_item_payable")
            capacities = tuple(max(0, balances[i] - self.items[i].deposit_paid - floor) if eligible & (1 << i) else 0 for i in range(self.n))
            fee_capacity = self.component_bases(eligible, "payable", balances, current_fees, include_shipping)[self.n:]
            capacities += fee_capacity
            payable = sum(self.component_bases(eligible, "payable", balances, current_fees, include_shipping))
            maximum = min(sum(capacities), max(0, payable - money(e.get("min_payable", "0"), "min_payable")))
            if p.cap is not None:
                maximum = min(maximum, p.cap)
            kind = e.get("kind", "fixed")
            ratio, rate_text = None, None
            if "tiers" in e:
                qualified = [t for t in e["tiers"] if quantity >= t["min_quantity"]]
                if not qualified:
                    return unavailable()
                rate_text = qualified[-1]["rate"]
            elif "rate" in e:
                rate_text = e["rate"]
            if rate_text is not None:
                ratio = rate(rate_text)
                discount_bases = self.component_bases(eligible, e["discount_basis"], balances, current_fees, include_shipping)
                if e["rounding_scope"] == "item":
                    demands = tuple(min(c, rounded(b, ratio, e["rounding"])) for b, c in zip(discount_bases, capacities))
                    if e.get("per_item_cap") is not None:
                        per_item_cap = money(e["per_item_cap"], "per_item_cap")
                        demands = tuple(min(x, per_item_cap) for x in demands)
                    requested, weights = sum(demands), demands
                    capacities = demands
                else:
                    check(e.get("per_item_cap") is None, "per_item_cap需要rounding_scope=item")
                    requested = rounded(sum(discount_bases), ratio, e["rounding"])
                    weights = capacities
            else:
                requested = maximum
                weights = capacities
            cycles = 1
            if kind == "fixed":
                cycles = quantity // e["quantity_step"] if "quantity_step" in e else (base // p.threshold if p.repeat else 1)
                requested = cycles * p.discount
            asset = e.get("resource_id")
            step_value, step_units, available = 1, 1, None
            if asset:
                index = self.asset_index[asset]
                available = quota[index] - usage[index]
                if kind == "balance":
                    step_value = money(e.get("step_value", "0.01"), "step_value")
                    step_units = e["units_per_step"] if self.resources[asset]["kind"] == "points" else step_value
                maximum = min(maximum, available // step_units * step_value)
            actual = min(requested, maximum)
            if kind == "balance":
                actual = actual // step_value * step_value
            if actual <= 0:
                return unavailable()
            amounts = (range(step_value, actual + 1, step_value) if kind == "balance" and e["allocation"] == "flexible" else (actual,))
            for amount in amounts:
                self.tick()
                shares = capped_allocate(amount, weights, capacities)
                next_balances = tuple(b - d for b, d in zip(balances, shares[:self.n]))
                next_fees = None if fees is None else tuple(dict(f, pay_now=fmt(money(f["pay_now"], "fee") - shares[self.n + j])) for j, f in enumerate(fees))
                next_usage = list(usage)
                if p.id in self.count_index:
                    next_usage[self.count_index[p.id]] += 1
                asset_usage = [0] * len(usage)
                if asset:
                    used = amount // step_value * step_units
                    next_usage[self.asset_index[asset]] += used
                    asset_usage[self.asset_index[asset]] = used
                next_occupied = occupied + ((p.exclusive_group, p.exclusive_scope, eligible),) if p.exclusive_group else occupied
                names = [x.unit_id for x in self.items] + ["shipping:" + f["platform"] + "/" + f["shop"] for f in current_fees]
                trace = {"id": p.id, "name": p.name, "kind": kind, "stage": e.get("stage", "goods"),
                         "basis": e["basis"], "basis_amount": fmt(base), "threshold": fmt(p.threshold), "cycles": cycles,
                         "requested_discount": fmt(requested), "actual_discount": fmt(amount),
                         "eligible_units": [self.items[i].unit_id for i in self.indices(eligible)],
                         "allocation": {name: fmt(v) for name, v in zip(names, shares) if v},
                         "resource_usage": self.asset_values(asset_usage)}
                if ratio:
                    trace.update(rate=str(rate_text), discount_basis=e["discount_basis"], rounding=e["rounding"], rounding_scope=e["rounding_scope"])
                visit(pos + 1, next_balances, tuple(next_usage), next_occupied, applied + (trace,), next_fees)
        visit(0, initial, (0,) * len(self.quota), (), ())
        return tuple(best.values())

    def _discount_solve(self, remaining, quota, orders_left):
        self.tick(); self.state_count += 1
        if self.state_count > self.state_limit:
            raise SearchLimit("扩展折扣动态规划状态超过上限，本次未完成")
        required = remaining & self.required
        if not required:
            return Plan(0, 0, ())
        if not orders_left:
            return None
        subset, best = remaining, None
        while subset:
            self.tick()
            if (subset & required or self.optional_first) and self.compatible(subset):
                for option in self.options(subset, quota):
                    tail = self.solve(remaining ^ subset, tuple(q - u for q, u in zip(quota, option.usage)), orders_left - 1)
                    if tail is not None:
                        candidate = Plan(option.pay_now + tail.pay_now, (subset & ~self.required).bit_count() + tail.filler_count, (option,) + tail.orders)
                        if best is None or candidate.score < best.score:
                            best = candidate
            subset = (subset - 1) & remaining
        return best

    def order_dict(self, option, index):
        result = super().order_dict(option, index)
        result["resource_usage"] = self.asset_values(option.usage)
        result["coupon_usage"] = {ident: option.usage[i] for ident, i in self.count_index.items() if option.usage[i]}
        return result

    def run(self):
        plan = self.solve((1 << self.n) - 1, self.quota, min(self.s.max_orders, self.n))
        if plan is None:
            raise InputError("当前结算次数或结算组约束下没有可行方案")
        selected = 0
        for order in plan.orders:
            selected |= order.mask
        chosen = self.indices(selected)
        paid = sum(self.items[i].deposit_paid for i in chosen)
        effective = sum(self.items[i].effective for i in chosen)
        shipping = sum(money(f["fee"], "shipping") for o in plan.orders for f in o.shipping)
        cash_shipping = sum(money(f["pay_now"], "shipping") for o in plan.orders for f in o.shipping)
        final_goods = sum(sum(o.balances) for o in plan.orders)
        usage = tuple(sum(o.usage[i] for o in plan.orders) for i in range(len(self.quota)))
        orders, before = [], self.quota
        for index, option in enumerate(plan.orders):
            order = self.order_dict(option, index + 1)
            expected = {}
            for p in self.s.promotions:
                if p.mode == "automatic" and option.mask & self.eligible[p.id]:
                    if p.id in self.count_index:
                        expected["uses:" + p.id] = before[self.count_index[p.id]]
                    ident = self.extra[p.id].get("resource_id")
                    if ident:
                        value = before[self.asset_index[ident]]
                        expected["resource:" + ident] = fmt(value) if self.resources[ident]["kind"] == "money" else value
            order["automatic_resources_before"] = expected
            orders.append(order)
            before = tuple(q - u for q, u in zip(before, option.usage))
        if time.monotonic() > self.deadline:
            raise SearchLimit("扩展折扣达到精确搜索时间上限，本次未完成")
        return {"status": "optimal_under_input_rules", "version": 1, "engine": "discounts-v1", "label": self.s.label,
                "objective": ["min_pay_now", "min_optional_units", "min_checkouts"],
                "summary": {"original_price_selected": fmt(sum(self.items[i].original_price for i in chosen)),
                            "price_before_presale_discount": fmt(sum(self.items[i].price for i in chosen)),
                            "presale_discount": fmt(sum(self.items[i].presale_discount for i in chosen)),
                            "effective_price_selected": fmt(effective), "promotions_discount": fmt(effective + shipping - plan.pay_now - paid),
                            "goods_discount": fmt(effective - final_goods), "shipping_discount": fmt(shipping - cash_shipping),
                            "shipping": fmt(shipping), "deposit_paid": fmt(paid), "pay_now": fmt(plan.pay_now),
                            "total_spend": fmt(plan.pay_now + paid), "checkouts": len(plan.orders), "optional_units": plan.filler_count},
                "coupon_usage": {ident: usage[i] for ident, i in self.count_index.items() if usage[i]},
                "resource_usage": self.asset_values(usage),
                "selected_optional_units": [self.items[i].unit_id for i in chosen if not self.items[i].required],
                "skipped_optional_units": [x.unit_id for i, x in enumerate(self.items) if not x.required and not selected & (1 << i)],
                "orders": orders,
                "assumptions": self.s.assumptions,
                "warnings": ["精确最优只覆盖已输入规则、舍入、分摊和资源使用条件；不代表平台未经核实的实际最低价。",
                             "红包/积分与优惠预算为本次共享存量，方案会消耗这些权益；付费储值本金不作为折扣。"],
                "stats": {"expanded_units": self.n, "dp_states": self.state_count,
                          "checkout_subsets": self.options.cache_info().currsize, "promotion_variants": self.variant_count,
                          "elapsed_seconds": round(time.monotonic() - self.started, 4)}}


def optimize(data, time_limit=15.0, state_limit=200_000):
    check(isinstance(time_limit, (int, float)) and not isinstance(time_limit, bool) and 0 < time_limit <= 300, "time_limit: 应为0..300秒")
    integer(state_limit, "state_limit", 1, 1_000_000)
    if not data.get("resources") and not any(set(p) & EXT_FIELDS or p.get("basis") == "payable"
                                           or (p.get("mode") == "automatic" and p.get("total_uses") is not None)
                                           for p in data["promotions"]):
        legacy = deepcopy(data); legacy.pop("resources", None)
        return legacy_optimize(legacy, time_limit, state_limit)
    scenario, extras, resources = parse_extended(data)
    return DiscountOptimizer(scenario, extras, resources, time_limit, state_limit).run()
