"""Taobao/Tmall scope and metadata compilation; no hard-coded campaign discounts."""
from __future__ import annotations

from copy import deepcopy

from optimize import check

MARKET = "taobao-tmall"
PROFILE_VERSION = "taobao-tmall-v2"
MARKETPLACES = ("taobao", "tmall")
PRICE_KINDS = ("before_order_promotions", "after_coupons", "tail_payment", "unknown")


def normalise_row(section, row):
    check(isinstance(row, dict), f"{section}: 每条资料应为对象")
    result = deepcopy(row)
    platform = result.get("platform")
    if platform is not None:
        check(isinstance(platform, str) and platform in MARKETPLACES,
              "本产品仅支持淘宝、天猫国内购物（platform=taobao 或 tmall）；其他平台、天猫国际暂不支持")
        result["platform"] = "taobao"
        if section == "items" and platform == "tmall":
            check(result.get("marketplace") in (None, "tmall"), "platform=tmall 与商品店铺类型冲突")
            result["marketplace"] = "tmall"
        elif section == "promotions" and platform == "tmall":
            check(result.get("marketplaces") in (None, ["tmall"]), "platform=tmall 的优惠仅适用天猫店")
            result["marketplaces"] = ["tmall"]
    if section == "items":
        if result.get("marketplace") is not None:
            check(result["marketplace"] in MARKETPLACES, "marketplace: 仅支持 taobao/tmall，天猫国际暂不支持")
        if "price_kind" in result:
            check(result["price_kind"] in PRICE_KINDS, "price_kind: 未支持的价格口径")
    elif section == "promotions" and "marketplaces" in result:
        values = result["marketplaces"]
        check(isinstance(values, list) and bool(values)
              and all(isinstance(x, str) and x in MARKETPLACES for x in values),
              "marketplaces: 应为 taobao/tmall 组成的非空数组")
        check(len(set(values)) == len(values), "marketplaces: 不得重复")
    return result


def normalise_cart(cart):
    check(isinstance(cart, dict), "cart: 应为JSON对象")
    result = deepcopy(cart)
    for section in ("items", "promotions", "shipping"):
        if section in result:
            check(isinstance(result[section], list), f"{section}: 应为数组")
            result[section] = [normalise_row(section, row) for row in result[section]]
    return result


def marketplace_matches(rule, item):
    # An unknown store type remains a possible match until readiness asks about it.
    return (not rule.get("marketplaces") or item.get("marketplace") is None
            or item["marketplace"] in rule["marketplaces"])


def compile_cart(cart):
    """Compile the store-type AND filter without changing the frozen solver."""
    result = deepcopy(cart)
    for rule in result["promotions"]:
        if "marketplaces" in rule:
            selected = {x["id"] for x in result["items"] if marketplace_matches(rule, x)}
            if "item_ids" in rule:
                selected &= set(rule["item_ids"])
            check(bool(selected), "优惠没有匹配的店铺类型")
            rule["item_ids"] = sorted(selected)
            del rule["marketplaces"]
    for item in result["items"]:
        for key in ("spec", "marketplace", "price_kind"):
            item.pop(key, None)
    return result
