"""Portable shopping-task state and comparison, Python 3.10+, standard library only."""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import uuid

from optimize import (InputError, SearchLimit, check, keys, string, integer, boolean,
                      money, fmt, no_duplicate_keys)
from discounts import (optimize, EXT_FIELDS, RESOURCE_FIELDS, validate_rule,
                       validate_resources, required_fields, uses_limit)
from taobao import (MARKET, PROFILE_VERSION, normalise_cart, normalise_row,
                    marketplace_matches, compile_cart)

VERSION = "0.4.0"
ITEM_FIELDS = {"id", "name", "spec", "platform", "shop", "category", "tags",
               "checkout_group", "price", "original_price", "threshold_price",
               "presale_discount", "deposit_paid", "required", "quantity", "source",
               "marketplace", "price_kind"}
PROMO_FIELDS = {"id", "name", "platform", "mode", "threshold", "discount", "basis",
                "repeat", "cap", "total_uses", "exclusive_group", "exclusive_scope",
                "item_ids", "shops", "categories", "tags", "source", "marketplaces"} | EXT_FIELDS
SHIP_FIELDS = {"platform", "shop", "fee", "free_threshold", "basis", "source"}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_json(path):
    # Preserve decimal money without ever converting it through a binary float.
    return json.loads(Path(path).read_text(encoding="utf-8"), parse_float=str,
                      object_pairs_hook=no_duplicate_keys,
                      parse_constant=lambda x: (_ for _ in ()).throw(InputError(f"非法 JSON 常量: {x}")))


def fingerprint(state):
    payload = {k: state[k] for k in ("cart", "preferences", "questions", "data_as_of", "coupon_balances")}
    payload["rule_profile"] = PROFILE_VERSION
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def validate_preferences(prefs, cart):
    keys(prefs, {"allow_addons", "approved_optional_ids", "preferred"},
         {"allow_addons", "approved_optional_ids", "preferred"}, "preferences")
    boolean(prefs["allow_addons"], "preferences.allow_addons")
    check(prefs["preferred"] in ("cheapest", "fewest_checkouts"), "preferred: 选 cheapest 或 fewest_checkouts")
    ids = prefs["approved_optional_ids"]
    check(isinstance(ids, list) and all(isinstance(i, str) for i in ids), "approved_optional_ids: 应为商品ID数组")
    check(len(ids) == len(set(ids)), "approved_optional_ids: 不得重复")
    optional = {x["id"] for x in cart["items"] if not x.get("required", True)}
    check(set(ids) <= optional, "仅能批准清单中明确的可选商品；先录入有来源的候选品")


def clean_cart(cart):
    """Remove rules/shipping made irrelevant by explicit removals or purchases."""
    ids = {x["id"] for x in cart["items"]}
    shops = {(x.get("platform"), x.get("shop")) for x in cart["items"]}
    rules = []
    for row in cart["promotions"]:
        row = deepcopy(row)
        if "item_ids" in row:
            row["item_ids"] = [i for i in row["item_ids"] if i in ids]
            if not row["item_ids"]:
                continue
        if cart["items"] and (row.get("platform") is None
                              or any(x.get("platform") is None for x in cart["items"])
                              or any(matches(row, x) for x in cart["items"])):
            rules.append(row)
    cart["promotions"] = rules
    cart["shipping"] = [x for x in cart.get("shipping", [])
                        if (x.get("platform"), x.get("shop")) in shops]


def matches(rule, item):
    return (rule.get("platform") == item.get("platform")
            and marketplace_matches(rule, item)
            and (not rule.get("item_ids") or item["id"] in rule["item_ids"])
            and (not rule.get("shops") or item.get("shop") in rule["shops"])
            and (not rule.get("categories") or item.get("category", "general") in rule["categories"])
            and (not rule.get("tags") or bool(set(rule["tags"]) & set(item.get("tags", [])))))


def validate_cart(cart):
    """Drafts may be incomplete or over size; never discard their contents."""
    keys(cart, {"version", "label", "assumptions", "items", "promotions", "shipping", "max_orders", "resources"},
         {"version", "items", "promotions"}, "cart")
    check(type(cart["version"]) is int and cart["version"] == 1, "cart.version: 仅支持1")
    for field in ("items", "promotions", "shipping"):
        check(isinstance(cart.get(field, []), list), f"{field}: 应为数组")
    validate_resources(cart.get("resources", []), draft=True)
    if "max_orders" in cart:
        integer(cart["max_orders"], "max_orders", 1, 12)
    if "label" in cart:
        string(cart["label"], "label")
    check(isinstance(cart.get("assumptions", []), list), "assumptions: 应为数组")
    for note in cart.get("assumptions", []):
        string(note, "assumptions[]")
    identities = {"items": set(), "promotions": set(), "shipping": set()}
    for field, allowed in (("items", ITEM_FIELDS), ("promotions", PROMO_FIELDS), ("shipping", SHIP_FIELDS)):
        for row in cart.get(field, []):
            required = {"platform", "shop"} if field == "shipping" else {"id"}
            keys(row, allowed, required, field)
            normalise_row(field, row)
            check(row.get("platform") in (None, "taobao"), "任务平台尚未规范化，请通过new/update导入淘宝天猫资料")
            for key in required:
                string(row[key], f"{field}.{key}")
            ident = (row["platform"], row["shop"]) if field == "shipping" else row["id"]
            check(ident not in identities[field], f"{field}: 重复ID/店铺 {ident}")
            identities[field].add(ident)
            if field == "items":
                check("#" not in row["id"], "商品ID不能包含 #")
            for key in ("price", "original_price", "threshold_price", "presale_discount", "deposit_paid",
                        "threshold", "discount", "cap", "fee", "free_threshold"):
                if key in row and row[key] is not None:
                    money(row[key], key)
            for key in ("required", "repeat"):
                if key in row:
                    boolean(row[key], key)
            if "quantity" in row:
                integer(row["quantity"], "quantity", 1)
            if row.get("total_uses") is not None:
                integer(row["total_uses"], "total_uses", 0, 12)
            for key in ("name", "spec", "platform", "shop", "category", "checkout_group", "exclusive_group"):
                if row.get(key) is not None:
                    string(row[key], key)
            for key in ("tags", "categories", "shops", "item_ids"):
                if key in row:
                    values = row[key]
                    check(isinstance(values, list) and bool(values), f"{key}: 应为非空字符串数组")
                    for value in values:
                        string(value, key)
                    check(len(set(values)) == len(values), f"{key}: 不得重复")
            if "source" in row:
                check(isinstance(row["source"], (str, dict)) or row["source"] is None,
                      "source: 应为来源文字或对象")
            if field == "promotions":
                validate_rule(row)
            elif row.get("basis") is not None:
                check(isinstance(row["basis"], str) and row["basis"] in
                      {"price", "original_price", "threshold_price", "remaining"}, "basis: 未支持的门槛基数")
            if row.get("mode") is not None:
                check(row["mode"] in ("automatic", "selectable"), "mode: 仅支持 automatic/selectable")
    for rule in cart["promotions"]:
        check(set(rule.get("item_ids", [])) <= identities["items"], "优惠 item_ids 包含未知商品")
    store_types = {}
    for item in cart["items"]:
        if item.get("shop") is not None and item.get("marketplace") is not None:
            known = store_types.setdefault(item["shop"], item["marketplace"])
            check(known == item["marketplace"], "同一店铺ID出现淘宝店/天猫店类型冲突，请核对店铺标识")


def validate_state(state):
    check(isinstance(state, dict), "任务文件应为JSON对象")
    check({"version", "revision", "cart", "preferences", "questions", "purchases", "history",
           "data_as_of", "title", "latest_result", "coupon_balances"} <= state.keys(), "任务文件缺少字段，请从备份恢复")
    check(state.get("version") == 1, "不支持的任务版本")
    integer(state["revision"], "revision", 1)
    validate_cart(state["cart"])
    validate_preferences(state["preferences"], state["cart"])
    for field in ("purchases", "history"):
        check(isinstance(state[field], list), f"{field}: 应为数组")
    string(state["data_as_of"], "data_as_of")
    check(state["latest_result"] is None or isinstance(state["latest_result"], dict), "latest_result: 应为对象或null")
    check(isinstance(state["coupon_balances"], dict), "coupon_balances: 应为对象")
    for ident, value in state["coupon_balances"].items():
        string(ident, "coupon_balances.id")
        if value is not None:
            integer(value, "coupon_balances.remaining", 0, 12)
    check(isinstance(state["questions"], list), "questions: 应为数组")
    ids = set()
    for q in state["questions"]:
        keys(q, {"id", "text", "blocking", "source"}, {"id", "text", "blocking"}, "question")
        string(q["id"], "question.id")
        string(q["text"], "question.text")
        boolean(q["blocking"], "question.blocking")
        check(q["id"] not in ids, "question.id 重复")
        ids.add(q["id"])


def new_state(cart=None, title=None):
    timestamp = now()
    state = {"version": 1, "program_version": VERSION, "id": uuid.uuid4().hex,
             "revision": 1, "title": title or "购物算账", "created_at": timestamp,
             "updated_at": timestamp, "data_as_of": timestamp,
             "cart": normalise_cart(cart) if cart is not None else
                     {"version": 1, "items": [], "promotions": [], "shipping": []},
             "preferences": {"allow_addons": False, "approved_optional_ids": [], "preferred": "cheapest"},
             "questions": [], "purchases": [], "history": [], "latest_result": None}
    validate_cart(state["cart"])
    state["coupon_balances"] = {x["id"]: uses_limit(x) for x in state["cart"]["promotions"]
                                if x.get("mode") == "selectable" or uses_limit(x) is not None}
    validate_state(state)
    return state


def project(cart, selected):
    result = deepcopy(cart)
    result["items"] = [x for x in result["items"] if x["id"] in selected]
    clean_cart(result)
    if "resources" in result:
        referenced = {p["resource_id"] for p in result["promotions"] if p.get("resource_id")}
        result["resources"] = [r for r in result["resources"] if r["id"] in referenced]
    return result


def active_ids(state, original=False):
    prefs = state["preferences"]
    approved = set(prefs["approved_optional_ids"]) if prefs["allow_addons"] and not original else set()
    return {x["id"] for x in state["cart"]["items"] if x.get("required", True) or x["id"] in approved}


def readiness(state):
    cart = project(state["cart"], active_ids(state))
    pending = deepcopy(state["questions"])
    def add(ident, text, blocking=True):
        if any(item["id"] == ident for item in pending):
            return
        pending.append({"id": ident, "text": text, "blocking": blocking})
    if not any(x.get("required", True) for x in cart["items"]):
        if not state["purchases"]:
            add("items", "请提供这次真正要买的商品；可以重新读取购物车，也可以发截图或文字。")
        return pending
    count = sum(x.get("quantity", 1) for x in cart["items"])
    if count > 12:
        add("item_limit", f"当前允许购买的商品共{count}件，超过12件计算上限；完整清单已保留，请选择本次范围。")
    if len(cart["promotions"]) > 12:
        add("promotion_limit", "优惠超过12条，已保留全部资料，请缩小本次范围。")
    for item in cart["items"]:
        for key, name in (("platform", "平台"), ("shop", "店铺"), ("price", "优惠前活动价/预售总价")):
            if item.get(key) is None:
                add(f"item:{item['id']}:{key}", f"请补充{item.get('name', item['id'])}的{name}。")
        if item.get("price_kind", "before_order_promotions") != "before_order_promotions":
            add(f"item:{item['id']}:price_kind", f"{item.get('name', item['id'])}当前是券后价、尾款或未知价格口径，请补充订单优惠前的活动价/预售总价，避免重复减券。")
        for key, label in (("deposit_paid", "已付定金"), ("presale_discount", "预售立减"),
                           ("threshold_price", "优惠门槛计入金额"), ("original_price", "原价")):
            if key in item and item[key] is None:
                add(f"item:{item['id']}:{key}", f"请核实{item.get('name', item['id'])}的{label}。")
        if not item.get("source"):
            add(f"source:item:{item['id']}", f"{item.get('name', item['id'])}的价格来源尚未记录。", False)
    for rule in cart["promotions"]:
        labels = {"platform": "适用平台", "mode": "是否自动执行", "threshold": "门槛金额",
                  "discount": "减免金额", "basis": "门槛计算口径", "rate": "减免比例",
                  "discount_basis": "比例折扣的计算基数", "rounding": "金额取整方式",
                  "rounding_scope": "逐件还是整单取整", "resource_id": "共享余额来源",
                  "allocation": "余额是否必须用到最大额", "min_quantity": "最低适用件数",
                  "quantity_step": "每次减免对应件数", "min_item_payable": "单件最低未付金额",
                  "min_payable": "适用范围最低未付金额", "step_value": "每步抵扣金额"}
        for key in sorted(required_fields(rule)):
            if rule.get(key) is None:
                add(f"promotion:{rule['id']}:{key}", f"优惠“{rule.get('name', rule['id'])}”缺少{labels[key]}，请核实使用说明。")
        if rule.get("resource_id"):
            resource = next((r for r in cart.get("resources", []) if r["id"] == rule["resource_id"]), None)
            if resource is None or resource.get("kind") is None or resource.get("balance") is None:
                add(f"resource:{rule['resource_id']}", f"请核实“{rule.get('name', rule['id'])}”可用的共享余额或积分数量。")
            else:
                if not resource.get("source"):
                    add(f"source:resource:{resource['id']}", "共享余额/积分数量的来源尚未记录。", False)
                if resource["kind"] == "points":
                    for key, label in (("step_value", "每步抵扣金额"), ("units_per_step", "每步消耗积分数")):
                        if rule.get(key) is None:
                            add(f"promotion:{rule['id']}:{key}", f"请核实{rule.get('name', rule['id'])}的{label}，不假设固定通兑比例。")
        if not rule.get("source"):
            add(f"source:promotion:{rule['id']}", f"优惠“{rule.get('name', rule['id'])}”来源尚未记录。", False)
        if rule.get("marketplaces"):
            for item in cart["items"]:
                if item.get("marketplace") is None and matches(rule, item):
                    add(f"marketplace:{rule['id']}:{item['id']}", f"请确认{item.get('name', item['id'])}来自淘宝店还是天猫店，才能判断“{rule.get('name', rule['id'])}”是否适用。")
    shipping = {(x["platform"], x["shop"]): x for x in cart.get("shipping", [])}
    for platform, shop in sorted({(x["platform"], x["shop"]) for x in cart["items"]
                                 if x.get("platform") is not None and x.get("shop") is not None}):
        row = shipping.get((platform, shop))
        if row is None or row.get("fee") is None or row.get("basis") is None:
            add(f"shipping:{platform}:{shop}", f"请核实{platform}/{shop}的运费及包邮条件；确认包邮后再填0元。")
        elif not row.get("source"):
            add(f"source:shipping:{platform}:{shop}", f"{platform}/{shop}运费来源尚未记录。", False)
    return pending


def same_revision(state, expected):
    integer(expected, "expected_revision", 1)
    check(expected == state["revision"], f"任务已变更：当前revision={state['revision']}，请先show后再修改")


def advance(state, operation, payload):
    state["history"].append({"revision": state["revision"], "saved_at": now(),
                             "operation": operation, "payload": deepcopy(payload),
                             "cart": deepcopy(state["cart"]),
                             "preferences": deepcopy(state["preferences"]),
                             "questions": deepcopy(state["questions"]),
                             "coupon_balances": deepcopy(state["coupon_balances"]),
                             "result": deepcopy(state["latest_result"])})
    state["revision"] += 1
    state["program_version"] = VERSION
    state["updated_at"] = now()
    state["latest_result"] = None


def update_state(state, patch):
    keys(patch, {"expected_revision", "reason", "items", "remove_items", "promotions",
                 "remove_promotions", "shipping", "preferences", "questions", "resolve_questions",
                 "assumptions", "data_as_of", "max_orders", "label", "resources"},
         {"expected_revision", "reason"}, "update")
    same_revision(state, patch["expected_revision"])
    string(patch["reason"], "reason")
    result = deepcopy(state)
    advance(result, "update", patch)
    for field, fields in (("items", ITEM_FIELDS), ("promotions", PROMO_FIELDS), ("shipping", SHIP_FIELDS), ("resources", RESOURCE_FIELDS)):
        incoming = patch.get(field, [])
        check(isinstance(incoming, list), f"{field}: 应为数组")
        ident_fields = ("platform", "shop") if field == "shipping" else ("id",)
        seen = set()
        for row in incoming:
            row = normalise_row(field, row)
            keys(row, fields, set(ident_fields), field)
            for key in ident_fields:
                string(row[key], key)
            ident = tuple(row[k] for k in ident_fields)
            check(ident not in seen, f"本次更新包含重复{field} ID")
            seen.add(ident)
            old = next((x for x in result["cart"].setdefault(field, [])
                        if tuple(x.get(k) for k in ident_fields) == ident), None)
            if old is None:
                added = deepcopy(row)
                if (field == "promotions" and added["id"] in result["coupon_balances"] and "total_uses" not in added):
                    added["total_uses"] = result["coupon_balances"][added["id"]]
                result["cart"][field].append(added)
            else:
                if field == "resources" and old.get("kind") is not None and row.get("kind") is not None:
                    check(old["kind"] == row["kind"], "共享资源ID的单位类型不能改变；不同权益请使用新ID")
                old.update(deepcopy(row))
                # A correction without evidence must not keep a stale source attached.
                if "source" not in row and set(row) - set(ident_fields) - {"spec", "name"}:
                    old["source"] = {"kind": "user_update", "note": patch["reason"], "observed_at": now()}
    for field in ("items", "promotions"):
        removals = patch.get(f"remove_{field}", [])
        check(isinstance(removals, list) and all(isinstance(x, str) for x in removals), f"remove_{field}: 应为ID数组")
        check(set(removals) <= {x["id"] for x in result["cart"][field]}, f"remove_{field}: 包含未知ID")
        check(not (set(removals) & {x["id"] for x in patch.get(field, [])}), "同一对象不能同时修改和删除")
        result["cart"][field] = [x for x in result["cart"][field] if x["id"] not in removals]
    if patch.get("remove_items"):
        clean_cart(result["cart"])
    for field in ("assumptions", "max_orders", "label"):
        if field in patch:
            result["cart"][field] = deepcopy(patch[field])
    if "preferences" in patch:
        check(isinstance(patch["preferences"], dict), "preferences: 应为对象")
        result["preferences"].update(deepcopy(patch["preferences"]))
    # Removed/required products cannot retain an old optional-product approval.
    optional = {x["id"] for x in result["cart"]["items"] if not x.get("required", True)}
    if "approved_optional_ids" not in patch.get("preferences", {}):
        result["preferences"]["approved_optional_ids"] = [x for x in result["preferences"]["approved_optional_ids"] if x in optional]
    if "questions" in patch:
        check(isinstance(patch["questions"], list), "questions: 应为数组（替换当前待补问列表）")
        result["questions"] = deepcopy(patch["questions"])
    resolved = patch.get("resolve_questions", [])
    check(isinstance(resolved, list) and all(isinstance(x, str) for x in resolved), "resolve_questions: 应为ID数组")
    check(set(resolved) <= {x["id"] for x in result["questions"]}, "resolve_questions: 包含未知问题")
    result["questions"] = [q for q in result["questions"] if q["id"] not in resolved]
    if "data_as_of" in patch:
        result["data_as_of"] = string(patch["data_as_of"], "data_as_of")
    else:
        result["data_as_of"] = now()
    for rule in result["cart"]["promotions"]:
        if rule.get("mode") == "selectable" or uses_limit(rule) is not None:
            result["coupon_balances"][rule["id"]] = uses_limit(rule)
    validate_state(result)
    return result


def comparison(state, time_limit=15.0, state_limit=200_000):
    check(isinstance(time_limit, (float, int)) and 0 < time_limit <= 300, "time_limit: 应为0到300秒之间")
    integer(state_limit, "state_limit", 1, 1_000_000)
    validate_state(state)
    result = {"version": 1, "market": MARKET, "rule_profile": PROFILE_VERSION,
              "revision": state["revision"], "input_fingerprint": fingerprint(state),
              "generated_at": now(), "data_as_of": state["data_as_of"], "questions": readiness(state),
              "assumptions": state["cart"].get("assumptions", []), "plans": {}, "choices": []}
    already_spent = sum(money(x["actual_total_spend"], "purchase.total") for x in state["purchases"])
    result["purchased"] = {"checkouts": len(state["purchases"]), "total_spend": fmt(already_spent)}
    result["scope"] = "remaining_cart" if state["purchases"] else "whole_cart"
    if any(q["blocking"] for q in result["questions"]):
        return dict(result, status="needs_information")
    if not any(x.get("required", True) for x in state["cart"]["items"]):
        return dict(result, status="complete", purchases=state["purchases"])
    deadline = time.monotonic() + time_limit
    for rule in state["cart"]["promotions"]:
        if rule.get("platform") is not None and all(x.get("platform") is not None for x in state["cart"]["items"]):
            check(any(matches(rule, x) for x in state["cart"]["items"]),
                  f"优惠“{rule.get('name', rule['id'])}”未匹配清单商品，请核对适用范围")
    cart = project(state["cart"], active_ids(state))
    original = project(state["cart"], active_ids(state, original=True))
    minimum = len({(x["platform"], x.get("checkout_group", "default")) for x in original["items"]})
    check(cart.get("max_orders", 12) >= minimum, f"这些商品至少需要{minimum}次结算，请调整max_orders")
    simplest = deepcopy(cart)
    simplest["max_orders"] = minimum
    # Every required checkout group can always be bought in a single checkout under v1.
    # Group count is therefore the feasible lower bound; shared coupons remain global.
    cache = {}
    for name, input_cart in (("original", original), ("cheapest", cart), ("fewest_checkouts", simplest)):
        key = json.dumps(input_cart, sort_keys=True, ensure_ascii=False)
        if key not in cache:
            left = deadline - time.monotonic()
            if left <= 0:
                raise SearchLimit("方案比较达到总时间上限，本次未完成；请缩小范围后重试")
            cache[key] = optimize(compile_cart(input_cart), time_limit=left, state_limit=state_limit)
        solved = deepcopy(cache[key])
        for order in solved["orders"]:
            for item in order["items"]:
                row = next(x for x in state["cart"]["items"] if x["id"] == item["id"])
                item["spec"] = row.get("spec") or ""
                item["source"] = row.get("source")
                item["marketplace"] = row.get("marketplace")
        result["plans"][name] = solved
    baseline = money(result["plans"]["original"]["summary"]["total_spend"], "baseline")
    signatures = {}
    for name, plan in result["plans"].items():
        savings = baseline - money(plan["summary"]["total_spend"], "total")
        plan["summary"]["net_savings_vs_original"] = ("-" if savings < 0 else "") + fmt(abs(savings))
        plan["summary"]["task_total_spend"] = fmt(already_spent + money(plan["summary"]["total_spend"], "total"))
        # Only a global, same-snapshot baseline is meaningful across objectives.
        for key in ("required_only_pay_now", "savings_vs_required_only", "required_only_single_checkout_pay_now"):
            plan["summary"].pop(key, None)
        signature = json.dumps({"summary": plan["summary"], "orders": plan["orders"]}, sort_keys=True)
        if signature in signatures:
            signatures[signature]["aliases"].append(name)
        else:
            choice = {"plan": name, "aliases": [name]}
            signatures[signature] = choice
            result["choices"].append(choice)
    result["recommended"] = state["preferences"]["preferred"]
    result["minimum_checkouts"] = minimum
    result["status"] = "compared_under_input_rules"
    return result


def current_result(state):
    value = state.get("latest_result")
    if value and (value.get("revision") != state["revision"] or value.get("input_fingerprint") != fingerprint(state)):
        return None
    return value


def record_purchase(state, plan_name, order_number, receipt):
    keys(receipt, {"expected_revision", "id", "pay_now", "coupon_ids", "source", "resource_usage"},
         {"expected_revision", "id", "pay_now", "coupon_ids", "source"}, "receipt")
    same_revision(state, receipt["expected_revision"])
    string(receipt["id"], "receipt.id")
    check(not any(x["id"] == receipt["id"] for x in state["purchases"]), "该结算记录已保存，请勿重复记账")
    check(isinstance(receipt["source"], (str, dict)) and bool(receipt["source"]), "请记录用户已购买的确认或结算凭据来源")
    actual = money(receipt["pay_now"], "receipt.pay_now")
    coupons = receipt["coupon_ids"]
    check(isinstance(coupons, list) and all(isinstance(x, str) for x in coupons)
          and len(set(coupons)) == len(coupons), "coupon_ids: 应为不重复的实际用券ID数组")
    latest = current_result(state)
    check(latest is not None and latest["status"] == "compared_under_input_rules", "请先按当前资料compare，旧方案不能用于记账")
    check(plan_name in latest["plans"], "未知方案")
    orders = latest["plans"][plan_name]["orders"]
    integer(order_number, "order", 1, len(orders))
    order = orders[order_number - 1]
    rules = {x["id"]: x for x in state["cart"]["promotions"]}
    expected = {x["id"] for x in order["promotions"] if rules[x["id"]]["mode"] == "selectable"}
    check(set(coupons) == expected, "实际用券与方案不同：请先update核对券规则并重新compare，再记录购买")
    expected_resources = order.get("resource_usage", {})
    actual_resources = receipt.get("resource_usage", {})
    check(isinstance(actual_resources, dict) and set(actual_resources) == set(expected_resources), "请核对并记录本次实际消耗的共享余额/积分")
    resources = {r["id"]: r for r in state["cart"].get("resources", [])}
    for key, expected_before in order.get("automatic_resources_before", {}).items():
        group, ident = key.split(":", 1)
        if group == "uses":
            actual_before = uses_limit(rules[ident])
        else:
            resource = resources[ident]
            actual_before = resource["balance"] if resource["kind"] == "points" else money(resource["balance"], "balance")
            if resource["kind"] == "money":
                expected_before = money(expected_before, "expected_balance")
        check(actual_before == expected_before, "该步骤依赖先前结算已消耗的自动优惠额度；请按步骤记账，或先核实实际余额并重算")
    resource_units = {}
    for ident, expected_value in expected_resources.items():
        resource = resources[ident]
        used = money(actual_resources[ident], "resource_usage") if resource["kind"] == "money" else integer(actual_resources[ident], "resource_usage", 0, 1_000_000_000)
        planned = money(expected_value, "expected_resource_usage") if resource["kind"] == "money" else expected_value
        check(used == planned, "实际余额/积分消耗与方案不同，请先核对资料并重算")
        resource_units[ident] = used
    result = deepcopy(state)
    advance(result, "purchase", receipt)
    quantities = Counter(x["id"] for x in order["items"])
    remaining = []
    for item in result["cart"]["items"]:
        item["quantity"] = item.get("quantity", 1) - quantities[item["id"]]
        if item["quantity"] > 0:
            remaining.append(item)
    result["cart"]["items"] = remaining
    applied_ids = {p["id"] for p in order["promotions"]}
    for rule in result["cart"]["promotions"]:
        if rule["id"] in applied_ids and uses_limit(rule) is not None:
            rule["total_uses"] = uses_limit(rule) - 1
            check(rule["total_uses"] >= 0, "券剩余次数不足")
            result["coupon_balances"][rule["id"]] = rule["total_uses"]
    for resource in result["cart"].get("resources", []):
        if resource["id"] in resource_units:
            balance = money(resource["balance"], "resource.balance") if resource["kind"] == "money" else resource["balance"]
            balance -= resource_units[resource["id"]]
            check(balance >= 0, "共享资源余额不足")
            resource["balance"] = fmt(balance) if resource["kind"] == "money" else balance
    clean_cart(result["cart"])
    ids = {x["id"] for x in remaining if not x.get("required", True)}
    result["preferences"]["approved_optional_ids"] = [x for x in result["preferences"]["approved_optional_ids"] if x in ids]
    paid = money(order["deposit_paid"], "deposit_paid")
    result["purchases"].append({"id": receipt["id"], "recorded_at": now(), "from_revision": state["revision"],
                                "plan": plan_name, "order": deepcopy(order), "coupon_ids": coupons,
                                "actual_pay_now": fmt(actual), "deposit_paid": fmt(paid),
                                "actual_total_spend": fmt(actual + paid), "source": receipt["source"],
                                "resource_usage": deepcopy(actual_resources)})
    if actual != money(order["pay_now"], "pay_now"):
        result["questions"].append({"id": f"purchase:{receipt['id']}:difference", "blocking": True,
                                    "text": "实际结算金额与方案不同，请核对差异是否影响剩余商品、运费或优惠，再继续计算。",
                                    "source": receipt["source"]})
    result["data_as_of"] = now()
    validate_state(result)
    return result


@contextmanager
def task_lock(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / ".shopping.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise InputError(f"任务正在被另一命令使用；锁文件：{lock}。若进程已退出，确认后移除此锁。") from exc
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(str(os.getpid()))
        yield directory
    finally:
        lock.unlink(missing_ok=True)


def read_state(directory):
    state = load_json(Path(directory) / "session.json")
    if isinstance(state, dict) and "cart" in state:
        state["cart"] = normalise_cart(state["cart"])
    validate_state(state)
    return state


def save_state(directory, state):
    validate_state(state)
    directory = Path(directory)
    fd, path = tempfile.mkstemp(prefix=".session-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(path, directory / "session.json")
    finally:
        Path(path).unlink(missing_ok=True)


def view_state(state, history=False):
    result = deepcopy(state)
    result["latest_result"] = deepcopy(current_result(state))
    result["pending"] = readiness(state)
    if not history:
        result.pop("history", None)
    return result


def render_comparison(result):
    labels = {"original": "只买原清单", "cheapest": "少花钱", "fewest_checkouts": "少操作"}
    lines = [f"资料版本 {result['revision']}；资料时间 {result['data_as_of']}"]
    if result.get("purchased", {}).get("checkouts"):
        lines.append(f"已记录{result['purchased']['checkouts']}次购买，完整支出 ¥{result['purchased']['total_spend']}；以下方案计算剩余清单。")
    status = result["status"]
    if status == "needs_information":
        lines.append("还缺关键信息，清单已保存，本次未生成金额方案。")
    elif status == "complete":
        lines.append("原清单已购完，没有剩余必买商品。")
    elif status != "compared_under_input_rules":
        lines.append("本次未完成计算：" + result.get("error", status))
    else:
        lines.append("以下为当前输入条件下的预计方案；总支出含新增商品、运费和已付定金。")
        lines.append("各方案净节省均与同份资料的原清单最优方案比较。")
        for choice in result["choices"]:
            plan = result["plans"][choice["plan"]]
            s = plan["summary"]
            title = " / ".join(labels[x] for x in choice["aliases"])
            if result["recommended"] in choice["aliases"]:
                title += "（当前偏好）"
            lines.extend(["", f"{title}：预计总支出 ¥{s['total_spend']}；{s['checkouts']}次结算；"
                          f"新增{s['optional_units']}件；净节省 ¥{s['net_savings_vs_original']}",
                          f"已付定金 ¥{s['deposit_paid']}；本次还需付 ¥{s['pay_now']}"])
            if result.get("scope") == "remaining_cart":
                lines.append(f"连同已购，整次购物预计总支出 ¥{s['task_total_spend']}")
            for order in plan["orders"]:
                lines.append(f"  第{order['order']}次结算｜淘宝/天猫｜应付 ¥{order['pay_now']}")
                grouped = Counter((x["shop"], x["id"], x["name"], x["spec"], x["required"], x["marketplace"]) for x in order["items"])
                for (shop, _, name, spec, required, marketplace), quantity in grouped.items():
                    store_type = {"taobao": "淘宝店", "tmall": "天猫店"}.get(marketplace, "店铺类型待核实")
                    lines.append(f"    {shop}（{store_type}）：{name}{' · ' + spec if spec else ''} ×{quantity}"
                                 + ("（已同意的加购品）" if not required else ""))
                for promo in order["promotions"]:
                    lines.append(f"    {promo['name']}：减 ¥{promo['actual_discount']}")
                for fee in order["shipping"]:
                    lines.append(f"    {fee['shop']}运费 ¥{fee['fee']}")
                    if fee.get("pay_now", fee["fee"]) != fee["fee"]:
                        lines.append(f"    抵扣后运费还需付 ¥{fee['pay_now']}")
                for ident, amount in order.get("resource_usage", {}).items():
                    lines.append(f"    消耗共享权益 {ident}：{amount}")
    for q in result.get("questions", []):
        lines.append(("待补充：" if q["blocking"] else "待核实：") + q["text"])
    lines.extend("条件：" + x for x in result.get("assumptions", []))
    return "\n".join(lines) + "\n"


def render_state(state):
    lines = [f"{state['title']}｜版本 {state['revision']}"]
    for item in state["cart"]["items"]:
        lines.append(f"{item['id']}：{item.get('name', item['id'])} · {item.get('spec', '')} "
                     f"×{item.get('quantity', 1)}｜活动价 {item.get('price') if item.get('price') is not None else '待补充'}元"
                     + ("（可选）" if not item.get("required", True) else ""))
    if state["purchases"]:
        spent = sum(money(x["actual_total_spend"], "purchase") for x in state["purchases"])
        lines.append(f"已记录{len(state['purchases'])}次购买，完整支出合计 ¥{fmt(spent)}；以下方案只计算剩余清单。")
    result = current_result(state)
    if result:
        lines.append(render_comparison(result))
    else:
        lines.append("当前资料尚无有效计算结果，请compare；历史结果不再作为当前购买步骤。")
        for q in readiness(state):
            lines.append(("待补充：" if q["blocking"] else "待核实：") + q["text"])
    return "\n".join(lines) + "\n"
