"""Validate browser cart snapshots and import them into a local shopping task.

The browser itself is optional and lives outside the standard-library core.  Any
host browser tool can write the documented snapshot shape; this module performs
the deterministic, privacy-conscious conversion into the existing cart schema.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from optimize import InputError, check, integer, keys, money, string
from taobao import PRICE_KINDS
from workflow import load_json, new_state, now, readiness, update_state, validate_state

SNAPSHOT_SCHEMA = "promo-shopping-assistant/cart-snapshot"
SNAPSHOT_VERSION = 1

ITEM_KEYS = {
    "id", "name", "title", "platform", "marketplace", "shop", "sku", "spec",
    "quantity", "price", "price_text", "price_label", "price_kind",
    "original_price", "original_price_text", "required", "url", "item_url",
    "item_id", "selected", "presale", "raw_text", "source", "checkout_group",
    "category", "tags", "deposit_paid", "presale_discount", "threshold_price",
    "stock", "promotions", "warnings", "price_kind_assumed",
}
PROMO_KEYS = {
    "id", "name", "title", "platform", "mode", "threshold", "discount", "basis",
    "repeat", "cap", "total_uses", "exclusive_group", "exclusive_scope",
    "item_ids", "shops", "categories", "tags", "source", "marketplaces",
    "kind", "stage", "rate", "discount_basis", "rounding", "rounding_scope",
    "min_quantity", "quantity_step", "tiers", "per_item_cap",
    "min_item_payable", "min_payable", "whole_checkout", "include_shipping",
    "resource_id", "allocation", "step_value", "units_per_step",
    "raw_text", "url", "complete", "platform_text", "mode_text",
    "threshold_text", "discount_text",
}
SHIPPING_KEYS = {"platform", "shop", "fee", "free_threshold", "basis", "source", "raw_text"}
RESOURCE_KEYS = {"id", "kind", "balance", "source", "raw_text"}
QUESTION_KEYS = {"id", "text", "blocking", "source"}
PAGE_KEYS = {"kind", "url", "captured_at", "screenshot", "note"}
CHECKOUT_KEYS = {
    "captured", "url", "captured_at", "summary_lines", "shipping_total",
    "pay_now", "discount_total", "note",
}
SNAPSHOT_KEYS = {
    "schema", "schema_version", "captured_at", "title", "source", "login",
    "pages", "cart", "questions", "warnings", "assumptions",
    "promotion_hints", "checkout",
}
CART_KEYS = {"version", "label", "assumptions", "items", "promotions", "shipping",
             "resources", "max_orders"}

PRICE_AFTER = ("券后", "到手", "预估", "折后", "实付", "应付")
PRICE_TAIL = ("尾款", "定金")
PRICE_BEFORE = ("活动价", "售价", "原价", "标价", "页面价", "购物车展示价")
MONEY_RE = re.compile(r"[¥￥]?\s*(-?\d+(?:,\d{3})*(?:\.\d{1,2})?)")
PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
ID_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
ADDRESS_RE = re.compile(r"(收货地址|地址)\s*[:：].*")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")
SENSITIVE_KEY_RE = re.compile(r"(pass(word)?|cookie|token|session|phone|mobile|address|email|card|pay)", re.I)
SECRET_VALUE_RE = re.compile(r"(?i)\b(token|session|sessionid|sid|auth|password|passwd)\s*=\s*[^&\s]+")


def _redact(value):
    text = str(value or "")
    text = PHONE_RE.sub("[手机号已隐藏]", text)
    text = ID_RE.sub("[证件号已隐藏]", text)
    text = EMAIL_RE.sub("[邮箱已隐藏]", text)
    text = ADDRESS_RE.sub(lambda match: f"{match.group(1)}：[地址已隐藏]", text)
    text = SECRET_VALUE_RE.sub(lambda match: f"{match.group(1)}=[已隐藏]", text)
    return text


def _truncate(value, limit=1200):
    text = " ".join(_redact(value).split())
    return text[:limit]


def _stable_id(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


def _safe_ident(value, prefix="browser"):
    raw = str(value or "")
    if PHONE_RE.search(raw) or ID_RE.search(raw):
        raw = f"{prefix}-{_stable_id(raw)}"
    text = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff:]+", "-", raw).strip("-")
    return text[:120] or f"{prefix}-{_stable_id(value)}"


def _as_list(value, where):
    check(isinstance(value, list), f"{where}: 应为数组")
    return value


def _parse_amounts(text):
    values = []
    for match in MONEY_RE.finditer(str(text or "")):
        raw = match.group(1).replace(",", "")
        try:
            money(raw, "snapshot.price_text")
        except InputError:
            continue
        if raw not in values:
            values.append(raw)
    return values


def _amount(value, where):
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip().replace(",", "")
        if not value:
            return None
    money(value, where)
    return value


def _normalise_price(item, where):
    explicit = _amount(item.get("price"), f"{where}.price")
    text = item.get("price_text") or item.get("raw_text") or ""
    amounts = _parse_amounts(text)
    if explicit is not None:
        return str(explicit), amounts
    if len(amounts) == 1:
        return amounts[0], amounts
    return None, amounts


def _infer_price_kind(item):
    label = " ".join(str(item.get(key) or "") for key in ("price_label", "price_text", "raw_text"))
    explicit = item.get("price_kind")
    if explicit is not None:
        check(explicit in PRICE_KINDS, "price_kind: 未支持的价格口径")
        assumed = explicit == "before_order_promotions" and (
            "购物车展示价" in label or "页面价" in label)
        return explicit, assumed
    if any(token in label for token in PRICE_AFTER):
        return "after_coupons", False
    if any(token in label for token in PRICE_TAIL):
        return "tail_payment", False
    if any(token in label for token in PRICE_BEFORE):
        # The cart's normal line price is the pre-order-promotion activity price.
        # It is still flagged as an assumption so checkout preview can confirm it.
        return "before_order_promotions", ("购物车展示价" in label or "页面价" in label)
    return "unknown", False


def _quantity(item, where):
    value = item.get("quantity", 1)
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if type(value) is not int or value < 1:
        return 1, f"{where}: 数量无法确认，已先按1件保存"
    if value > 12:
        return value, f"{where}: 数量{value}超过12件计算上限；完整数量已保留，请缩小本次范围"
    return value, None


def _safe_url(value):
    text = str(value or "")
    try:
        parts = urlsplit(text)
    except ValueError:
        return _redact(text)
    if not parts.scheme or not parts.netloc:
        return _redact(text)
    query = [(key, item) for key, item in parse_qsl(parts.query, keep_blank_values=True)
             if not SENSITIVE_KEY_RE.search(key)]
    return _redact(urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)))


def _privacy_clean(value):
    if isinstance(value, dict):
        return {key: _privacy_clean(item) for key, item in value.items()
                if not SENSITIVE_KEY_RE.search(str(key))}
    if isinstance(value, list):
        return [_privacy_clean(item) for item in value]
    if isinstance(value, str):
        return _safe_url(value) if value.startswith(("http://", "https://")) else _redact(value)
    return value


def _source_for(snapshot, row, page="cart"):
    if "source" in row and row["source"] is not None:
        check(isinstance(row["source"], (str, dict)), "source: 应为文字或对象")
    base = snapshot.get("source")
    source = deepcopy(base) if isinstance(base, dict) else {}
    if isinstance(base, str):
        source["note"] = base
    own = row.get("source")
    if isinstance(own, dict):
        source.update(deepcopy(own))
    elif isinstance(own, str) and own.strip():
        source["note"] = own
    source.setdefault("kind", "browser")
    source.setdefault("page", page)
    source.setdefault("observed_at", snapshot.get("captured_at") or now())
    if snapshot.get("captured_at"):
        source.setdefault("captured_at", snapshot["captured_at"])
    url = row.get("url") or row.get("item_url")
    if url:
        source.setdefault("url", url)
    raw = row.get("raw_text")
    if raw:
        source["text"] = _truncate(raw)
    return _privacy_clean(source)


def _normalise_question(question, prefix="browser"):
    check(isinstance(question, dict), "questions: 每条应为对象")
    keys(question, QUESTION_KEYS, {"text"}, "question")
    text = _redact(string(question["text"], "question.text"))
    ident = question.get("id")
    if ident is None:
        ident = f"{prefix}:{_stable_id(text)}"
    ident = _safe_ident(ident, prefix)
    blocking = question.get("blocking", True)
    check(type(blocking) is bool, "question.blocking: 应为布尔值")
    result = {"id": ident, "text": text, "blocking": blocking}
    if question.get("source") is not None:
        check(isinstance(question["source"], (str, dict)), "question.source: 应为文字或对象")
        result["source"] = _privacy_clean(deepcopy(question["source"]))
    return result


def _merge_questions(*groups):
    merged = {}
    for group in groups:
        for question in group or []:
            merged[question["id"]] = question
    return list(merged.values())


def _merge_assumptions(*groups):
    result = []
    for group in groups:
        for note in group or []:
            note = _redact(note)
            if note and note not in result:
                result.append(note)
    return result


def validate_snapshot(data):
    keys(data, SNAPSHOT_KEYS, {"schema_version", "cart"}, "snapshot")
    integer(data["schema_version"], "snapshot.schema_version", 1, 1)
    if data.get("schema") is not None:
        check(data["schema"] == SNAPSHOT_SCHEMA,
              f"snapshot.schema: 应为 {SNAPSHOT_SCHEMA}")
    if data.get("captured_at") is not None:
        string(data["captured_at"], "snapshot.captured_at")
    if data.get("title") is not None:
        string(data["title"], "snapshot.title")
    if data.get("source") is not None:
        check(isinstance(data["source"], (str, dict)), "snapshot.source: 应为文字或对象")
    if data.get("login") is not None:
        login = data["login"]
        check(isinstance(login, dict), "snapshot.login: 应为对象")
        keys(login, {"required", "completed_at", "note"}, set(), "snapshot.login")
        if "required" in login:
            check(type(login["required"]) is bool, "snapshot.login.required: 应为布尔值")
    pages = _as_list(data.get("pages", []), "snapshot.pages")
    for page in pages:
        check(isinstance(page, dict), "snapshot.pages[]: 应为对象")
        keys(page, PAGE_KEYS, {"kind", "url"}, "snapshot.pages[]")
        string(page["kind"], "snapshot.pages[].kind")
        string(page["url"], "snapshot.pages[].url")

    cart = data["cart"]
    check(isinstance(cart, dict), "snapshot.cart: 应为对象")
    keys(cart, CART_KEYS, {"items"}, "snapshot.cart")
    if "version" in cart:
        integer(cart["version"], "snapshot.cart.version", 1, 1)
    for field in ("items", "promotions", "shipping", "resources"):
        if field in cart:
            _as_list(cart[field], f"snapshot.cart.{field}")
    if "assumptions" in cart:
        for note in _as_list(cart["assumptions"], "snapshot.cart.assumptions"):
            string(note, "snapshot.cart.assumptions[]")
    if "max_orders" in cart:
        integer(cart["max_orders"], "snapshot.cart.max_orders", 1, 12)

    for question in _as_list(data.get("questions", []), "snapshot.questions"):
        check(isinstance(question, dict), "snapshot.questions[]: 应为对象")
        keys(question, QUESTION_KEYS, {"text"}, "snapshot.questions[]")
    for warning in _as_list(data.get("warnings", []), "snapshot.warnings"):
        string(warning, "snapshot.warnings[]")
    for note in _as_list(data.get("assumptions", []), "snapshot.assumptions"):
        string(note, "snapshot.assumptions[]")
    for hint in _as_list(data.get("promotion_hints", []), "snapshot.promotion_hints"):
        check(isinstance(hint, (str, dict)), "snapshot.promotion_hints[]: 应为文字或对象")
        if isinstance(hint, dict):
            keys(hint, {"text", "source"}, {"text"}, "snapshot.promotion_hints[]")
            string(hint["text"], "snapshot.promotion_hints[].text")
    if data.get("checkout") is not None:
        checkout = data["checkout"]
        check(isinstance(checkout, dict), "snapshot.checkout: 应为对象")
        keys(checkout, CHECKOUT_KEYS, set(), "snapshot.checkout")
        if "summary_lines" in checkout:
            for line in _as_list(checkout["summary_lines"], "snapshot.checkout.summary_lines"):
                string(line, "snapshot.checkout.summary_lines[]")
    return data


def load_snapshot(path):
    return validate_snapshot(load_json(Path(path)))


def _normalise_item(snapshot, row, index, seen_ids):
    check(isinstance(row, dict), "snapshot.cart.items[]: 应为对象")
    keys(row, ITEM_KEYS, set(), "snapshot.cart.items[]")
    where = f"snapshot.cart.items[{index}]"

    raw_id = row.get("id") or row.get("item_id") or row.get("item_url") or row.get("url")
    if not raw_id:
        raw_id = f"{row.get('shop', '')}|{row.get('title') or row.get('name', '')}|{index}"
    ident = _safe_ident(raw_id, "item")
    while ident in seen_ids:
        ident = f"{ident}-{index + 1}"
    seen_ids.add(ident)

    platform = row.get("platform")
    check(platform in (None, "taobao", "tmall"),
          "snapshot item.platform: 仅支持 taobao/tmall")
    marketplace = row.get("marketplace")
    if platform == "tmall":
        check(marketplace in (None, "tmall"), "snapshot item: platform=tmall 与店铺类型冲突")
        marketplace = "tmall"
    if marketplace is not None:
        check(marketplace in ("taobao", "tmall"), "snapshot item.marketplace: 仅支持 taobao/tmall")

    price, amounts = _normalise_price(row, where)
    price_kind, assumed = _infer_price_kind(row)
    quantity, quantity_warning = _quantity(row, where)

    name = row.get("name") or row.get("title") or ident
    spec = row.get("spec") or row.get("sku")
    result = {
        "id": ident,
        "name": _truncate(name, 200),
        "platform": "taobao",
        "quantity": quantity,
        "required": row.get("required", True),
        "source": _source_for(snapshot, row),
    }
    if not isinstance(result["required"], bool):
        raise InputError(f"{where}.required: 应为布尔值")
    if row.get("shop") is not None:
        result["shop"] = string(row["shop"], f"{where}.shop")
    if marketplace is not None:
        result["marketplace"] = marketplace
    if spec:
        result["spec"] = _truncate(spec, 200)
    if row.get("category") is not None:
        result["category"] = string(row["category"], f"{where}.category")
    if row.get("checkout_group") is not None:
        result["checkout_group"] = string(row["checkout_group"], f"{where}.checkout_group")
    if row.get("tags") is not None:
        tags = _as_list(row["tags"], f"{where}.tags")
        for tag in tags:
            string(tag, f"{where}.tags[]")
        result["tags"] = tags

    if price is not None:
        result["price"] = price
        result["price_kind"] = price_kind
    for key in ("original_price", "threshold_price", "presale_discount", "deposit_paid"):
        value = _amount(row.get(key), f"{where}.{key}")
        if value is not None:
            result[key] = str(value)

    warnings, questions = [], []
    if quantity_warning:
        warnings.append(quantity_warning)
    if price is None:
        questions.append(_normalise_question({
            "id": f"item:{ident}:price",
            "text": f"未能从购物车确认“{name}”的订单优惠前活动价，请补充。",
            "blocking": True,
            "source": result["source"],
        }))
    elif price_kind == "unknown":
        questions.append(_normalise_question({
            "id": f"item:{ident}:price_kind",
            "text": f"“{name}”的页面价格口径无法确认（原文：{_truncate(row.get('price_text') or row.get('raw_text'), 120)}），请确认是活动价、券后价还是尾款。",
            "blocking": True,
            "source": result["source"],
        }))
    elif len(amounts) > 1 and row.get("price") is None:
        warnings.append(f"{name}页面出现多个金额，已按“{price_kind}”取其中一个，结算前请复核。")
    if not result.get("shop"):
        questions.append(_normalise_question({
            "id": f"item:{ident}:shop",
            "text": f"未能从购物车识别“{name}”的店铺，请确认店铺名称（同店运费和优惠需要）。",
            "blocking": True,
            "source": result["source"],
        }))
    if row.get("presale"):
        questions.append(_normalise_question({
            "id": f"item:{ident}:presale",
            "text": f"“{name}”看起来是预售商品，请补充完整售卖价、已付定金和尾款。",
            "blocking": True,
            "source": result["source"],
        }))
    return result, assumed, warnings, questions


def _normalise_promotion(snapshot, row, index, item_ids, seen_ids):
    check(isinstance(row, dict), "snapshot.cart.promotions[]: 应为对象")
    keys(row, PROMO_KEYS, set(), "snapshot.cart.promotions[]")
    where = f"snapshot.cart.promotions[{index}]"
    raw_id = row.get("id") or row.get("name") or row.get("title") or row.get("raw_text")
    ident = _safe_ident(raw_id, "promotion")
    while ident in seen_ids:
        ident = f"{ident}-{index + 1}"
    seen_ids.add(ident)

    platform = row.get("platform")
    check(platform in (None, "taobao", "tmall"),
          "snapshot promotion.platform: 仅支持 taobao/tmall")
    marketplaces = row.get("marketplaces")
    if platform == "tmall":
        check(marketplaces in (None, ["tmall"]), "snapshot promotion: platform=tmall 与范围冲突")
        marketplaces = ["tmall"]
    if marketplaces is not None:
        check(isinstance(marketplaces, list) and marketplaces
              and all(x in ("taobao", "tmall") for x in marketplaces),
              "snapshot promotion.marketplaces: 应为 taobao/tmall 非空数组")

    mode = row.get("mode")
    basis = row.get("basis")
    kind = row.get("kind", "fixed")
    threshold = _amount(row.get("threshold"), f"{where}.threshold")
    discount = _amount(row.get("discount"), f"{where}.discount")

    item_refs = row.get("item_ids")
    if item_refs is not None:
        _as_list(item_refs, f"{where}.item_ids")
        item_refs = [x for x in item_refs if x in item_ids]
        if not item_refs:
            return None, [_normalise_question({
                "id": f"browser:promotion:{ident}:scope",
                "text": f"优惠“{row.get('name') or ident}”指向的商品不在本次购物车，未导入；请核对适用范围。",
                "blocking": True,
                "source": _source_for(snapshot, row, "promotion"),
            })]

    incomplete = []
    if mode not in ("automatic", "selectable"):
        incomplete.append("是否自动执行")
    if basis not in ("price", "original_price", "threshold_price", "remaining", "payable"):
        incomplete.append("门槛计算口径")
    if threshold is None:
        incomplete.append("门槛金额")
    if kind == "fixed" and discount is None:
        incomplete.append("减免金额")
    if kind == "fixed" and discount is not None and money(discount, f"{where}.discount") <= 0:
        incomplete.append("大于0的减免金额")
    if row.get("repeat") and threshold is not None and money(threshold, f"{where}.threshold") <= 0:
        incomplete.append("大于0的每满门槛")
    if kind == "percentage" and row.get("rate") is None and not row.get("tiers"):
        incomplete.append("减免比例或档位")
    if kind == "balance" and not row.get("resource_id"):
        incomplete.append("共享余额来源")
    if incomplete:
        return None, [_normalise_question({
            "id": f"browser:promotion:{ident}:unparsed",
            "text": f"页面出现优惠“{row.get('name') or ident}”，但未能确认{'、'.join(incomplete)}。原文：{_truncate(row.get('raw_text') or row.get('title') or row.get('name'), 200)}",
            "blocking": True,
            "source": _source_for(snapshot, row, "promotion"),
        })]

    result = {
        "id": ident,
        "name": _truncate(row.get("name") or row.get("title") or ident, 200),
        "platform": "taobao",
        "mode": mode,
        "threshold": str(threshold),
        "basis": basis,
        "source": _source_for(snapshot, row, "promotion"),
    }
    if kind == "fixed":
        result["discount"] = str(discount)
    else:
        result["kind"] = kind
    if row.get("rate") is not None:
        result["rate"] = row["rate"]
    if row.get("tiers") is not None:
        result["tiers"] = deepcopy(row["tiers"])
    for key in ("discount_basis", "rounding", "rounding_scope", "allocation",
                "stage", "exclusive_group", "exclusive_scope", "resource_id"):
        if row.get(key) is not None:
            result[key] = row[key]
    for key in ("repeat", "whole_checkout", "include_shipping"):
        if row.get(key) is not None:
            result[key] = row[key]
    for key in ("cap", "per_item_cap", "min_item_payable", "min_payable", "step_value"):
        value = _amount(row.get(key), f"{where}.{key}")
        if value is not None:
            result[key] = str(value)
    for key in ("total_uses", "min_quantity", "quantity_step", "units_per_step"):
        if row.get(key) is not None:
            integer(row[key], f"{where}.{key}", 0 if key == "total_uses" else 1,
                    12 if key != "units_per_step" else 1_000_000_000)
            result[key] = row[key]
    for key in ("shops", "categories", "tags"):
        if row.get(key) is not None:
            values = _as_list(row[key], f"{where}.{key}")
            for value in values:
                string(value, f"{where}.{key}[]")
            result[key] = values
    if item_refs is not None:
        result["item_ids"] = item_refs
    if marketplaces is not None:
        result["marketplaces"] = marketplaces
    return result, []


def _normalise_shipping(snapshot, row, index, shop_ids):
    check(isinstance(row, dict), "snapshot.cart.shipping[]: 应为对象")
    keys(row, SHIPPING_KEYS, {"platform", "shop"}, "snapshot.cart.shipping[]")
    platform = row.get("platform", "taobao")
    check(platform in ("taobao", "tmall"), "shipping.platform: 仅支持 taobao/tmall")
    shop = string(row["shop"], "shipping.shop")
    if (("taobao", shop)) not in shop_ids:
        return None, f"运费配置的店铺“{shop}”不在购物车商品中，已跳过。"
    fee = _amount(row.get("fee"), "shipping.fee")
    basis = row.get("basis")
    if basis not in ("price", "original_price", "threshold_price", "remaining", "payable"):
        basis = None
    result = {
        "platform": "taobao",
        "shop": shop,
        "fee": str(fee) if fee is not None else None,
        "basis": basis,
        "source": _source_for(snapshot, row, "checkout" if row.get("raw_text") else "cart"),
    }
    if row.get("free_threshold") is not None:
        result["free_threshold"] = str(_amount(row["free_threshold"], "shipping.free_threshold"))
    return result, None


def _normalise_resource(snapshot, row, index):
    check(isinstance(row, dict), "snapshot.cart.resources[]: 应为对象")
    keys(row, RESOURCE_KEYS, {"id"}, "snapshot.cart.resources[]")
    ident = _safe_ident(row["id"], "resource")
    kind = row.get("kind")
    if kind is not None:
        check(kind in ("money", "points"), "resource.kind: 应为 money/points")
    balance = row.get("balance")
    if balance is not None:
        if kind == "points":
            if isinstance(balance, str) and balance.strip().isdigit():
                balance = int(balance.strip())
            integer(balance, "resource.balance", 0, 1_000_000_000)
        else:
            balance = str(_amount(balance, "resource.balance"))
    result = {"id": ident, "source": _source_for(snapshot, row, "cart")}
    if kind is not None:
        result["kind"] = kind
    if balance is not None:
        result["balance"] = balance
    return result


def _promotion_hint_question(snapshot, hints, promotions=()):
    if not hints:
        return []
    names = [p.get("name", "") for p in promotions]
    texts = []
    source = None
    for hint in hints:
        if isinstance(hint, str):
            text = _truncate(hint, 200)
        else:
            text = _truncate(hint.get("text"), 200)
            if source is None and hint.get("source") is not None:
                source = hint["source"]
        if any(name and (name in text or text in name) for name in names):
            continue
        texts.append(text)
    if not texts:
        return []
    text = "页面出现以下优惠文案，尚未确认适用范围、门槛、执行方式或叠加规则，未计入金额：" + "；".join(texts[:6])
    question = {"id": "browser:promotion-hints", "text": text, "blocking": False}
    if source is not None:
        question["source"] = deepcopy(source)
    return [_normalise_question(question)]


def import_into_state(existing_state, snapshot):
    """Return ``(state, report)`` without writing any files."""
    validate_snapshot(snapshot)
    captured_at = snapshot.get("captured_at") or now()
    cart_in = snapshot["cart"]
    item_ids = set()
    items, assumed_prices, warnings, questions = [], [], [], []
    for index, row in enumerate(cart_in.get("items", [])):
        item, assumed, item_warnings, item_questions = _normalise_item(snapshot, row, index, item_ids)
        items.append(item)
        if assumed and item.get("price") is not None:
            assumed_prices.append(item["name"])
        warnings.extend(item_warnings)
        questions.extend(item_questions)

    promotion_ids = set()
    promotions = []
    for index, row in enumerate(cart_in.get("promotions", [])):
        promotion, promotion_questions = _normalise_promotion(
            snapshot, row, index, item_ids, promotion_ids)
        if promotion is not None:
            promotions.append(promotion)
        questions.extend(promotion_questions)

    shop_ids = {("taobao", item["shop"]) for item in items if item.get("shop")}
    shipping = []
    for index, row in enumerate(cart_in.get("shipping", [])):
        row, warning = _normalise_shipping(snapshot, row, index, shop_ids)
        if row is not None:
            shipping.append(row)
        if warning:
            warnings.append(warning)

    resources = [_normalise_resource(snapshot, row, index)
                 for index, row in enumerate(cart_in.get("resources", []))]

    source_url = "购物车页面"
    if isinstance(snapshot.get("source"), dict) and snapshot["source"].get("url"):
        source_url = _privacy_clean(snapshot["source"]["url"])
    assumptions = _merge_assumptions(
        cart_in.get("assumptions", []),
        snapshot.get("assumptions", []),
        [f"浏览器采集自 {source_url}；页面内容会变化，结算前需复核。"],
        [f"“{name}”使用购物车展示价，已按订单优惠前活动价录入，结算预览需复核。" for name in assumed_prices],
        ["结算预览只用于核对金额，未提交订单、未付款。"] if (snapshot.get("checkout") or {}).get("captured") else [],
    )

    questions = _merge_questions(
        [_normalise_question(q) for q in snapshot.get("questions", [])],
        questions,
        _promotion_hint_question(snapshot, snapshot.get("promotion_hints", []), promotions),
    )
    warnings.extend(snapshot.get("warnings", []))
    for line in (snapshot.get("checkout") or {}).get("summary_lines", []):
        if "运费" in line and not shipping:
            warnings.append("结算预览显示了运费，但未能拆分到店铺；已保留原文，请按店铺补充运费。")
            break

    cart = {
        "version": 1,
        "items": items,
        "promotions": promotions,
        "shipping": shipping,
        "resources": resources,
        "assumptions": assumptions,
    }
    if cart_in.get("label") is not None:
        cart["label"] = _redact(string(cart_in["label"], "snapshot.cart.label"))
    if cart_in.get("max_orders") is not None:
        cart["max_orders"] = cart_in["max_orders"]

    if existing_state is None:
        title = _redact(snapshot.get("title") or "淘宝天猫购物车")
        state = new_state(cart, title=title)
        state["data_as_of"] = captured_at
        state["questions"] = questions
        validate_state(state)
    else:
        merged_questions = _merge_questions(existing_state.get("questions", []), questions)
        merged_assumptions = _merge_assumptions(
            existing_state.get("cart", {}).get("assumptions", []), assumptions)
        patch = {
            "expected_revision": existing_state["revision"],
            "reason": f"浏览器购物车采集：{captured_at}",
            "items": items,
            "promotions": promotions,
            "shipping": shipping,
            "resources": resources,
            "questions": merged_questions,
            "assumptions": merged_assumptions,
            "data_as_of": captured_at,
        }
        if cart.get("max_orders") is not None:
            patch["max_orders"] = cart["max_orders"]
        state = update_state(existing_state, patch)

    pending = readiness(state)
    blocking = sum(1 for q in pending if q["blocking"])
    report = {
        "status": "imported",
        "captured_at": captured_at,
        "items": len(items),
        "promotions": len(promotions),
        "shipping": len(shipping),
        "resources": len(resources),
        "blocking_questions": blocking,
        "questions": deepcopy(pending),
        "warnings": _merge_assumptions(warnings),
        "assumed_price_kind": assumed_prices,
    }
    return state, report


def render_import(report, task=None):
    lines = [f"浏览器购物车已导入：商品 {report['items']} 件，优惠 {report['promotions']} 条，"
             f"运费 {report['shipping']} 条。"]
    if task is not None:
        lines.append(f"任务目录：{task}")
    if report["blocking_questions"]:
        lines.append(f"还有 {report['blocking_questions']} 个关键问题待补充，补充后再 compare。")
    elif report["questions"]:
        lines.append(f"有 {len(report['questions'])} 条待核实信息，不阻止本次计算。")
    for question in report["questions"]:
        lines.append(("待补充：" if question["blocking"] else "待核实：") + question["text"])
    for warning in report["warnings"]:
        lines.append("提示：" + warning)
    lines.append("下一步：python3 scripts/shopping.py compare <任务目录>")
    return "\n".join(lines) + "\n"
