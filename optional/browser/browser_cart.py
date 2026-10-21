#!/usr/bin/env python3
"""Optional Playwright adapter: read a Taobao/Tmall cart like a careful human.

The core shopping assistant stays standard-library only.  This adapter is an
optional companion for hosts that allow a local browser.  It never submits an
order, pays, changes quantities, deletes items, adds items or claims coupons.
It may select items and open the read-only checkout preview when the user has
allowed that; the previous selection is restored afterwards.

Usage:
    python3 optional/browser/browser_cart.py capture --task-dir ./task
    python3 optional/browser/browser_cart.py capture --output ./cart-snapshot.json --no-checkout

The output is a cart snapshot understood by:
    python3 scripts/shopping.py import-cart TASK_DIR --snapshot SNAPSHOT.json
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time

SCHEMA = "promo-shopping-assistant/cart-snapshot"
SCHEMA_VERSION = 1
CART_URL = "https://cart.taobao.com/cart.htm"
LOGIN_HOSTS = ("login.taobao.com", "login.tmall.com", "passport.taobao.com",
               "passport.tmall.com", "login.m.taobao.com")
RISK_PATTERN = re.compile(r"(安全验证|滑块|访问过于频繁|验证码|请完成验证|拖动滑块)")
LOGIN_PATTERN = re.compile(r"(扫码登录|密码登录|请登录|亲，请登录)")
PRICE_PATTERN = re.compile(r"[¥￥]\s*(\d+(?:,\d{3})*(?:\.\d{1,2})?)")
LABELED_PRICE_PATTERN = re.compile(
    r"(活动价|券后价?|到手价?|预估到手价?|折后价?|尾款|定金|原价|售价|标价)"
    r"\s*[:：]?\s*[¥￥]?\s*(\d+(?:,\d{3})*(?:\.\d{1,2})?)"
)
PHONE_PATTERN = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
ID_PATTERN = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
ADDRESS_PATTERN = re.compile(r"(收货地址|地址)\s*[:：].*")
EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")
SECRET_PATTERN = re.compile(r"(?i)\b(token|session|sessionid|sid|auth|password|passwd)\s*=\s*[^&\s]+")


class AdapterError(RuntimeError):
    def __init__(self, message, code=2):
        super().__init__(message)
        self.code = code


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_profile_dir():
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    return base / "promo-shopping-assistant" / "browser-profile"


def ensure_private_dir(path):
    path = Path(path).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def stable_id(*parts):
    raw = "|".join(str(p or "") for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def safe_ident(value, prefix="item"):
    raw = str(value or "")
    if PHONE_PATTERN.search(raw) or ID_PATTERN.search(raw):
        raw = f"{prefix}-{stable_id(raw)}"
    text = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff:]+", "-", raw).strip("-")
    return text[:120] or f"{prefix}-{stable_id(value)}"


def parse_price(text):
    if text is None:
        return None
    match = PRICE_PATTERN.search(str(text)) or LABELED_PRICE_PATTERN.search(str(text))
    if not match:
        return None
    raw = match.group(1) if match.re is PRICE_PATTERN else match.group(2)
    raw = raw.replace(",", "")
    try:
        number = Decimal(raw)
        if not number.is_finite() or number < 0 or number.as_tuple().exponent < -2:
            return None
        return raw
    except (InvalidOperation, ValueError):
        return None


def parse_amounts(text):
    values = []
    for match in PRICE_PATTERN.finditer(str(text or "")):
        raw = match.group(1).replace(",", "")
        try:
            number = Decimal(raw)
            if not number.is_finite() or number.as_tuple().exponent < -2:
                continue
        except (InvalidOperation, ValueError):
            continue
        if raw not in values:
            values.append(raw)
    return values


def infer_price_kind(label, explicit=None):
    if explicit:
        return explicit, False
    text = str(label or "")
    if any(token in text for token in ("券后", "到手", "预估", "折后", "实付", "应付")):
        return "after_coupons", False
    if any(token in text for token in ("尾款", "定金")):
        return "tail_payment", False
    if any(token in text for token in ("活动价", "售价", "原价", "标价", "页面价", "购物车展示价")):
        return "before_order_promotions", ("购物车展示价" in text or "页面价" in text)
    return "unknown", False


def redact_text(text):
    value = str(text or "")
    value = PHONE_PATTERN.sub("[手机号已隐藏]", value)
    value = ID_PATTERN.sub("[证件号已隐藏]", value)
    value = EMAIL_PATTERN.sub("[邮箱已隐藏]", value)
    value = ADDRESS_PATTERN.sub(lambda m: f"{m.group(1)}：[地址已隐藏]", value)
    value = SECRET_PATTERN.sub(lambda m: f"{m.group(1)}=[已隐藏]", value)
    return " ".join(value.split())


def relevant_lines(text, keywords, limit=80, max_length=240):
    lines, seen = [], set()
    for line in str(text or "").splitlines():
        line = redact_text(line)
        if not line or len(line) > max_length:
            continue
        if not any(keyword in line for keyword in keywords):
            continue
        if line in seen:
            continue
        seen.add(line)
        lines.append(line)
        if len(lines) >= limit:
            break
    return lines


def merge_texts(*groups):
    result = []
    for group in groups:
        for value in group or []:
            value = redact_text(value)
            if value and value not in result:
                result.append(value)
    return result


def write_json(path, data):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".browser-cart-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return path


EXTRACT_CART_JS = r"""
() => {
  const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
  const abs = (href) => { try { return new URL(href, location.href).href; } catch (e) { return href || ""; } };
  const isItemLink = (a) => {
    const href = a.href || "";
    return /(item\.taobao\.com|detail\.tmall\.com|item\.tmall\.com|chaoshi\.detail\.tmall\.com)/.test(href);
  };
  const itemLinkCount = (el) => {
    try { return [...el.querySelectorAll("a[href]")].filter(isItemLink).length; } catch (e) { return 0; }
  };
  const rowFor = (link) => {
    let el = link;
    for (let i = 0; i < 14 && el && el.parentElement; i++) {
      el = el.parentElement;
      const text = el.innerText || "";
      const hasPrice = /[¥￥]\s*\d/.test(text);
      const hasControl = !!(el.querySelector('input[type="checkbox"], input[type="number"]'));
      if (hasPrice && hasControl && itemLinkCount(el) <= 2) return el;
    }
    return link.closest("div") || link.parentElement;
  };
  const shopFor = (row, text) => {
    const link = row.querySelector('a[href*="shop"], a[href*="taobao.com/shop"], a[href*="tmall.com/shop"]');
    let shop = link ? clean(link.innerText || link.title || "") : "";
    if (!shop) {
      const match = text.match(/(?:店铺|商家)\s*[:：]\s*([^\s¥￥]{1,40})/);
      if (match) shop = match[1];
    }
    return shop || null;
  };
  const marketFor = (url, text) => {
    if (/tmall\.com/.test(url) || /天猫/.test(text)) return "tmall";
    if (/taobao\.com/.test(url) || /淘宝/.test(text)) return "taobao";
    return null;
  };
  const skuFor = (text) => {
    const match = text.match(/(?:颜色分类|颜色|规格|尺码|分类|套餐|版本|型号)\s*[:：]\s*([^¥￥]{1,40}?)(?=\s*(?:活动价|券后|到手|预估|原价|售价|标价|折后|数量|¥|￥)|$)/);
    return match ? clean(match[1]) : null;
  };
  const qtyFor = (row, text) => {
    const input = row.querySelector('input[type="number"], input[class*="quantity"], input[class*="num"]');
    if (input && input.value) {
      const value = parseInt(input.value, 10);
      if (value > 0) return value;
    }
    const match = text.match(/(?:×|x|X|数量\s*[:：]?\s*)\s*(\d{1,2})(?!\d)/);
    return match ? parseInt(match[1], 10) : null;
  };
  const seen = new Map();
  const links = [...document.querySelectorAll("a[href]")].filter(isItemLink);
  for (const link of links) {
    const row = rowFor(link);
    if (!row) continue;
    const text = clean(row.innerText || "");
    if (!text || text.length > 4000) continue;
    const url = abs(link.getAttribute("href") || link.href);
    const rawTitle = clean(link.innerText) || clean((row.querySelector("img[alt]") || {}).alt || "");
    const title = rawTitle
      .replace(/[¥￥]\s*[0-9]+(?:\.[0-9]{1,2})?/g, "")
      .replace(/(活动价|券后价?|到手价?|预估到手价?|折后价?)/g, "")
      .trim();
    const labeled = text.match(/(活动价|券后价?|到手价?|预估到手价?|折后价?|尾款|定金|原价|售价|标价)\s*[:：]?\s*[¥￥]?\s*([0-9]+(?:\.[0-9]{1,2})?)/);
    const prices = [...text.matchAll(/[¥￥]\s*([0-9]+(?:\.[0-9]{1,2})?)/g)].map((m) => m[1]);
    const price = labeled ? labeled[2] : (prices.length === 1 ? prices[0] : null);
    const priceLabel = labeled ? labeled[1] : "购物车展示价";
    const checkbox = row.querySelector('input[type="checkbox"]');
    const selected = checkbox ? !!checkbox.checked : /(selected|checked)/.test(String(row.className || ""));
    const idMatch = url.match(/[?&]id=(\d+)/) || url.match(/\/(\d+)\.htm/);
    const item = {
      id: idMatch ? idMatch[1] : null,
      url: url,
      title: title || null,
      name: title || null,
      shop: shopFor(row, text),
      marketplace: marketFor(url, text),
      spec: skuFor(text),
      quantity: qtyFor(row, text),
      price: price,
      price_label: priceLabel,
      price_text: labeled ? labeled[0] : (prices.length ? "¥" + prices.join(" / ¥") : null),
      price_candidates: prices,
      selected: selected,
      presale: /定金|尾款/.test(text),
      raw_text: text.slice(0, 1200)
    };
    const key = (item.url || item.title || "") + "|" + (item.shop || "") + "|" +
      (item.spec || "") + "|" + (item.raw_text || "").slice(0, 100);
    const old = seen.get(key);
    if (!old || (item.raw_text || "").length > (old.raw_text || "").length) seen.set(key, item);
  }
  const bodyText = document.body ? document.body.innerText || "" : "";
  const promoLines = [...new Set(bodyText.split("\n").map(clean).filter((line) =>
    line && line.length <= 220 &&
    /(满\s*\d|每满|立减|\d+(?:\.\d+)?\s*折|券后|优惠|红包|淘金币|定金|尾款|包邮)/.test(line)
  ))];
  return {
    url: location.href,
    title: document.title,
    login_markers: /扫码登录|密码登录|请登录/.test(bodyText),
    risk_markers: /安全验证|滑块|访问过于频繁|验证码|请完成验证/.test(bodyText),
    items: [...seen.values()],
    promotion_lines: promoLines.slice(0, 80)
  };
}
"""

CHECKOUT_JS = r"""
() => {
  const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
  const redact = (s) => (s || "")
    .replace(/(?<!\d)1[3-9]\d{9}(?!\d)/g, "[手机号已隐藏]")
    .replace(/(?<!\d)\d{17}[\dXx](?!\d)/g, "[证件号已隐藏]")
    .replace(/(收货地址|地址)\s*[:：].*/g, "$1：[地址已隐藏]")
    .replace(/[\w.+-]+@[\w.-]+\.\w+/g, "[邮箱已隐藏]");
  const lines = [...new Set((document.body ? document.body.innerText : "").split("\n")
    .map(clean)
    .filter((line) => line && line.length <= 240 &&
      /(合计|应付|实付|运费|优惠|折扣|红包|淘金币|定金|尾款|包邮)/.test(line))
    .map(redact))];
  return {url: location.href, title: document.title, lines: lines.slice(0, 100)};
}
"""

CHECKBOX_STATES_JS = r"""
() => [...document.querySelectorAll('input[type="checkbox"]')]
  .filter((cb) => cb.offsetParent !== null)
  .map((cb) => !!cb.checked)
"""

SELECT_ALL_JS = r"""
() => {
  const boxes = [...document.querySelectorAll('input[type="checkbox"]')].filter((cb) => cb.offsetParent !== null);
  for (const box of boxes) if (!box.checked) box.click();
  return boxes.length;
}
"""

RESTORE_CHECKBOXES_JS = r"""
(states) => {
  const boxes = [...document.querySelectorAll('input[type="checkbox"]')].filter((cb) => cb.offsetParent !== null);
  if (boxes.length !== states.length) return false;
  boxes.forEach((box, index) => { if (box.checked !== states[index]) box.click(); });
  return true;
}
"""

CLICK_CHECKOUT_JS = r"""
() => {
  const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
  const elements = [...document.querySelectorAll("button, a")].filter((element) =>
    /^(结算|去结算|立即结算)(\s*\(\d+\))?$/.test(clean(element.innerText)) &&
    element.offsetParent !== null);
  if (!elements.length) return null;
  elements[0].click();
  return clean(elements[0].innerText);
}
"""

CLICK_DETAIL_JS = r"""
() => {
  const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
  const wanted = /^(使用说明|活动规则|优惠详情|查看明细|规则)$/;
  const elements = [...document.querySelectorAll("a, button, span, div")].filter((element) =>
    wanted.test(clean(element.innerText)) && element.offsetParent !== null);
  if (!elements.length) return null;
  elements[0].click();
  return clean(elements[0].innerText);
}
"""


def page_text(page):
    try:
        return page.inner_text("body", timeout=3000)
    except Exception:
        return ""


def login_required(page, data=None):
    if data and data.get("login_markers"):
        return True
    url = (page.url or "").lower()
    if any(host in url for host in LOGIN_HOSTS):
        return True
    return bool(LOGIN_PATTERN.search(page_text(page)))


def risk_control(page, data=None):
    if data and data.get("risk_markers"):
        return True
    return bool(RISK_PATTERN.search(page_text(page)))


def wait_for_manual(page, args, reason):
    print(f"{reason}。请在打开的浏览器里完成扫码/验证；完成后我会继续，不会读取密码或绕过验证。",
          file=sys.stderr)
    deadline = time.monotonic() + args.login_timeout
    while time.monotonic() < deadline:
        try:
            data = page.evaluate(EXTRACT_CART_JS)
        except Exception:
            data = None
        if data is not None and not data.get("login_markers") and not data.get("risk_markers"):
            if "cart.taobao.com" not in (page.url or ""):
                try:
                    page.goto(args.cart_url, wait_until="domcontentloaded",
                              timeout=args.page_timeout * 1000)
                    page.wait_for_timeout(2000)
                except Exception:
                    pass
            return True
        if not login_required(page, data) and not risk_control(page, data):
            return True
        page.wait_for_timeout(2000)
    return False


def goto_cart(page, args):
    page.goto(args.cart_url, wait_until="domcontentloaded", timeout=args.page_timeout * 1000)
    page.wait_for_timeout(2500)
    required = login_required(page)
    completed_at = None
    if required:
        if not wait_for_manual(page, args, "购物车需要登录"):
            raise AdapterError("等待登录超时；浏览器仍可继续手动操作，稍后重试采集。", 4)
        completed_at = now_iso()
        if "cart.taobao.com" not in (page.url or ""):
            page.goto(args.cart_url, wait_until="domcontentloaded", timeout=args.page_timeout * 1000)
            page.wait_for_timeout(2500)
    if risk_control(page):
        if not wait_for_manual(page, args, "页面出现安全验证"):
            raise AdapterError("等待安全验证超时；未绕过验证，请稍后重试。", 4)
        completed_at = completed_at or now_iso()
    # Give the cart's asynchronous rendering a moment, then wait briefly for
    # item links.  An empty cart is still a legitimate snapshot.
    page.wait_for_timeout(2000)
    data = page.evaluate(EXTRACT_CART_JS)
    for _ in range(4):
        if data.get("items"):
            break
        text = page_text(page)
        if any(marker in text for marker in ("购物车还是空的", "购物车是空的", "还没有商品", "去逛逛")):
            break
        page.wait_for_timeout(1500)
        data = page.evaluate(EXTRACT_CART_JS)
    if data.get("risk_markers"):
        raise AdapterError("页面仍处于安全验证状态；未继续读取。", 4)
    return data, required, completed_at


def launch_browser(pw, args):
    if args.cdp_url:
        browser = pw.chromium.connect_over_cdp(args.cdp_url)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()
        return context, page, "cdp", "cdp", False

    profile = ensure_private_dir(Path(args.profile).expanduser() if args.profile else default_profile_dir())
    if args.browser == "auto":
        candidates = ["chrome", "msedge", None]
    elif args.browser == "chromium":
        candidates = [None]
    else:
        candidates = [args.browser]
    errors = []
    for candidate in candidates:
        kwargs = {
            "user_data_dir": str(profile),
            "headless": args.headless,
            "viewport": {"width": 1440, "height": 960},
        }
        if candidate:
            kwargs["channel"] = candidate
        try:
            context = pw.chromium.launch_persistent_context(**kwargs)
            page = context.pages[0] if context.pages else context.new_page()
            return context, page, candidate or "chromium", "persistent", True
        except Exception as exc:
            errors.append(f"{candidate or 'chromium'}: {exc}")
    raise AdapterError(
        "未找到可用浏览器。请安装 Google Chrome 或 Microsoft Edge，或执行 "
        "`python3 -m playwright install chromium` 后重试。尝试结果：" + "；".join(errors),
        3,
    )


def screenshot(page, args, kind):
    if not args.screenshot:
        return None
    try:
        directory = evidence_dir(args)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{kind}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
        page.screenshot(path=str(path), full_page=True)
        base = Path(args.task_dir).expanduser() if args.task_dir else Path(args.output).expanduser().parent
        try:
            return str(path.relative_to(base))
        except ValueError:
            return str(path)
    except Exception as exc:
        print(f"截图失败，不影响结构化采集：{exc}", file=sys.stderr)
        return None


def evidence_dir(args):
    if args.task_dir:
        return Path(args.task_dir).expanduser() / "evidence"
    return Path(args.output).expanduser().parent / "evidence"


def capture_item_details(context, items, args):
    pages, hints, warnings = [], [], []
    seen = set()
    for item in items[: args.detail_limit]:
        url = item.get("url")
        if not url or url in seen:
            continue
        seen.add(url)
        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=args.detail_timeout * 1000)
            page.wait_for_timeout(1500)
            if login_required(page) or risk_control(page):
                warnings.append(f"商品详情页需要登录或验证，已跳过：{url}")
                continue
            text = page_text(page)
            lines = relevant_lines(
                text,
                ("活动价", "券后", "到手", "预估", "定金", "尾款", "满", "立减",
                 "折扣", "规格", "颜色", "尺码", "包邮", "运费"),
                limit=40,
            )
            if lines:
                hints.extend(lines)
                pages.append({"kind": "item", "url": page.url,
                              "captured_at": now_iso(),
                              "note": "；".join(lines[:12])})
        except Exception as exc:
            warnings.append(f"商品详情读取失败：{url}（{exc}）")
        finally:
            page.close()
    return pages, merge_texts(hints), warnings


def capture_dialogs(page, args):
    hints, warnings = [], []
    for _ in range(3):
        before_url = page.url
        try:
            trigger = page.evaluate(CLICK_DETAIL_JS)
        except Exception:
            break
        if not trigger:
            break
        page.wait_for_timeout(700)
        try:
            text = page_text(page)
            hints.extend(relevant_lines(
                text, ("满", "减", "立减", "折扣", "券", "红包", "淘金币",
                       "定金", "尾款", "包邮", "运费", "叠加", "互斥"),
                limit=30,
            ))
        except Exception as exc:
            warnings.append(f"优惠说明读取失败：{exc}")
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
            if page.url != before_url:
                page.go_back(wait_until="domcontentloaded", timeout=args.page_timeout * 1000)
                page.wait_for_timeout(500)
        except Exception as exc:
            warnings.append(f"关闭优惠说明失败：{exc}")
    return merge_texts(hints), warnings


def capture_checkout(context, page, args, item_count):
    if not args.checkout:
        return {"captured": False, "note": "用户或参数关闭了结算预览。"}, []
    if item_count == 0:
        return {"captured": False, "note": "购物车没有可结算商品。"}, []
    print("将进入只读结算预览核对金额；不会提交订单或付款。", file=sys.stderr)
    warnings = []
    try:
        states = page.evaluate(CHECKBOX_STATES_JS)
    except Exception:
        states = []
    if not states:
        return {"captured": False, "note": "未找到购物车勾选框，跳过结算预览。"}, ["未找到购物车勾选框，未进入结算预览。"]
    try:
        page.evaluate(SELECT_ALL_JS)
        page.wait_for_timeout(600)
        clicked = page.evaluate(CLICK_CHECKOUT_JS)
        if not clicked:
            warnings.append("未找到“结算”按钮，未进入结算预览。")
            return {"captured": False, "note": "未找到结算按钮。"}, warnings
        page.wait_for_timeout(1000)
        checkout_page = None
        for _ in range(12):
            for candidate in context.pages:
                if candidate is page:
                    continue
                url = (candidate.url or "")
                try:
                    title = candidate.title()
                except Exception:
                    title = ""
                try:
                    content_marker = "提交订单" in candidate.content()
                except Exception:
                    content_marker = False
                if ("buy.taobao.com" in url or "buy.tmall.com" in url
                        or "订单" in title or content_marker):
                    checkout_page = candidate
                    break
            if checkout_page is None and "cart.taobao.com" not in (page.url or "") and (
                    "buy.taobao.com" in (page.url or "")
                    or "buy.tmall.com" in (page.url or "")
                    or "提交订单" in page.content()):
                checkout_page = page
            if checkout_page is not None:
                break
            page.wait_for_timeout(1000)
        if checkout_page is None:
            warnings.append("点击结算后未进入可识别的结算预览，已停止。")
            return {"captured": False, "note": "未进入结算预览。"}, warnings
        checkout_page.wait_for_timeout(1500)
        data = checkout_page.evaluate(CHECKOUT_JS)
        result = {
            "captured": True,
            "url": data.get("url") or checkout_page.url,
            "captured_at": now_iso(),
            "summary_lines": [redact_text(line) for line in data.get("lines", [])],
            "note": "只读结算预览；未提交订单、未付款。",
        }
        if checkout_page is not page:
            checkout_page.close()
        else:
            try:
                page.go_back(wait_until="domcontentloaded", timeout=args.page_timeout * 1000)
                page.wait_for_timeout(1500)
            except Exception as exc:
                warnings.append(f"从结算预览返回购物车失败：{exc}")
        return result, warnings
    except Exception as exc:
        warnings.append(f"结算预览读取失败：{exc}")
        return {"captured": False, "note": "结算预览读取失败。"}, warnings
    finally:
        if "states" in locals() and states:
            try:
                if "cart.taobao.com" not in (page.url or ""):
                    page.goto(args.cart_url, wait_until="domcontentloaded",
                              timeout=args.page_timeout * 1000)
                    page.wait_for_timeout(1500)
                restored = page.evaluate(RESTORE_CHECKBOXES_JS, states)
                if not restored:
                    warnings.append("未能恢复原购物车勾选状态，请手动确认后再操作。")
            except Exception as exc:
                warnings.append(f"恢复购物车勾选状态失败：{exc}")


def make_item(raw, warnings, questions):
    url = raw.get("url") or ""
    base_id = raw.get("id") or url or f"{raw.get('shop')}|{raw.get('title')}"
    if raw.get("spec"):
        base_id = f"{base_id}|{raw['spec']}"
    ident = safe_ident(base_id, "item")
    price = raw.get("price")
    candidates = raw.get("price_candidates") or []
    if price is None:
        if len(candidates) == 1:
            price = candidates[0]
        elif not candidates:
            price = parse_price(raw.get("price_text"))
    price_kind, assumed = infer_price_kind(raw.get("price_label"))
    quantity = raw.get("quantity")
    if not isinstance(quantity, int) or quantity < 1:
        if quantity is not None:
            warnings.append(f"{raw.get('title') or ident}数量无法确认，已先按1件保存。")
        quantity = 1
    elif quantity > 12:
        warnings.append(f"{raw.get('title') or ident}数量{quantity}超过12件计算上限；完整数量已保留。")
    item = {
        "id": ident,
        "name": raw.get("name") or raw.get("title") or ident,
        "platform": "taobao",
        "quantity": quantity,
        "required": True,
        "selected": bool(raw.get("selected")),
        "url": url,
        "price": price,
        "price_text": redact_text(raw.get("price_text")),
        "price_label": raw.get("price_label") or "购物车展示价",
        "price_kind": price_kind,
        "raw_text": redact_text(raw.get("raw_text"))[:1200],
        "source": {
            "kind": "browser",
            "page": "cart",
            "url": url,
            "text": redact_text(raw.get("raw_text"))[:1200],
            "observed_at": now_iso(),
        },
    }
    if raw.get("shop"):
        item["shop"] = raw["shop"]
    else:
        questions.append({
            "id": f"item:{ident}:shop",
            "text": f"未能识别“{item['name']}”的店铺，请确认店铺名称。",
            "blocking": True,
            "source": item["source"],
        })
    if raw.get("marketplace"):
        item["marketplace"] = raw["marketplace"]
    if raw.get("spec"):
        item["spec"] = raw["spec"]
    if price is None:
        questions.append({
            "id": f"item:{ident}:price",
            "text": f"未能识别“{item['name']}”的活动价，请补充。",
            "blocking": True,
            "source": item["source"],
        })
    if price_kind == "unknown":
        questions.append({
            "id": f"item:{ident}:price_kind",
            "text": f"“{item['name']}”的页面价格口径无法确认，请确认是活动价、券后价还是尾款。",
            "blocking": True,
            "source": item["source"],
        })
    if raw.get("presale"):
        questions.append({
            "id": f"browser:item:{ident}:presale",
            "text": f"“{item['name']}”看起来是预售商品，请补充完整售卖价、已付定金和尾款。",
            "blocking": True,
            "source": item["source"],
        })
    if assumed:
        item["price_kind"] = "before_order_promotions"
        item["price_kind_assumed"] = True
    return item


def build_snapshot(args, cart_data, browser_label, browser_mode,
                   login_required_flag, login_completed_at, pages, hints, checkout, warnings):
    questions = []
    items = [make_item(raw, warnings, questions) for raw in cart_data.get("items", [])]
    if len(items) > 12:
        warnings.append(f"购物车有{len(items)}件商品，超过12件计算上限；完整快照已保存，请选择本次范围。")
    if not items:
        warnings.append("购物车页面未识别到商品；可能为空购物车、页面结构变化或尚未加载完成。")
    assumptions = [
        "购物车页面由浏览器读取；商品价格、优惠和运费会变化，结算前需复核。",
        "未在页面确认的优惠、账户权益和运费未写入规则，缺失时会提出补问。",
        "结算预览只用于核对金额，未提交订单、未付款。",
    ]
    if any(item.get("price_kind_assumed") for item in items):
        assumptions.append("部分商品使用购物车展示价，已按订单优惠前活动价录入，需在结算预览复核。")
    snapshot = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "captured_at": now_iso(),
        "title": redact_text(args.title or "淘宝天猫购物车"),
        "source": {
            "kind": "browser",
            "url": cart_data.get("url") or args.cart_url,
            "browser": browser_label,
            "mode": browser_mode,
        },
        "login": {
            "required": bool(login_required_flag),
            "completed_at": login_completed_at,
        },
        "pages": pages,
        "cart": {
            "version": 1,
            "label": redact_text(args.title or "浏览器购物车"),
            "assumptions": assumptions,
            "items": items,
            "promotions": [],
            "shipping": [],
        },
        "promotion_hints": merge_texts(cart_data.get("promotion_lines", []), hints),
        "checkout": checkout,
        "questions": questions,
        "warnings": merge_texts(warnings),
    }
    return snapshot


def parser():
    root = argparse.ArgumentParser(description=__doc__,
                                   formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = root.add_subparsers(dest="command", required=True)
    capture = sub.add_parser("capture", help="打开购物车并输出快照")
    capture.add_argument("--task-dir", type=Path, help="任务目录；快照默认写入该目录")
    capture.add_argument("--output", type=Path, help="快照输出路径；与--task-dir二选一")
    capture.add_argument("--title", help="任务标题")
    capture.add_argument("--profile", type=Path, help="独立浏览器配置目录（默认在用户数据目录）")
    capture.add_argument("--cdp-url", help="附加到已开启调试端口的 Chrome，如 http://127.0.0.1:9222")
    capture.add_argument("--browser", choices=("auto", "chrome", "msedge", "chromium"), default="auto",
                         help="优先使用的系统浏览器")
    capture.add_argument("--headless", action="store_true", help="无头运行；默认可见浏览器")
    capture.add_argument("--cart-url", default=CART_URL)
    capture.add_argument("--login-timeout", type=float, default=300,
                         help="等待用户登录/验证的秒数")
    capture.add_argument("--page-timeout", type=float, default=30)
    capture.add_argument("--detail-timeout", type=float, default=15)
    capture.add_argument("--detail-limit", type=int, default=12)
    capture.add_argument("--no-details", dest="details", action="store_false", default=True,
                         help="不打开商品详情页")
    capture.add_argument("--no-dialogs", dest="dialogs", action="store_false", default=True,
                         help="不打开券/活动说明")
    capture.add_argument("--no-checkout", dest="checkout", action="store_false", default=True,
                         help="不进入只读结算预览")
    capture.add_argument("--screenshot", action="store_true", help="保存页面截图到任务目录")
    capture.add_argument("--keep-open", dest="keep_open", action="store_true", default=None,
                         help="采集后等待回车再关闭浏览器")
    capture.add_argument("--no-keep-open", dest="keep_open", action="store_false",
                         help="采集后立即关闭浏览器")
    return root


def run(args):
    if not args.task_dir and not args.output:
        raise AdapterError("请指定 --task-dir 或 --output。", 2)
    if args.task_dir and args.output:
        raise AdapterError("--task-dir 与 --output 只能指定一个。", 2)
    if args.detail_limit < 0:
        raise AdapterError("--detail-limit 不能为负数。", 2)
    if args.login_timeout <= 0 or args.page_timeout <= 0 or args.detail_timeout <= 0:
        raise AdapterError("超时参数必须大于0。", 2)
    output = Path(args.output).expanduser() if args.output else Path(args.task_dir).expanduser() / "browser-cart.json"
    args.output = output
    if output.exists() and output.is_dir():
        raise AdapterError(f"输出路径是目录：{output}", 2)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise AdapterError(
            "未安装 Playwright。核心程序不需要它；启用浏览器适配器请执行 "
            "`python3 -m pip install playwright`，再按需执行 "
            "`python3 -m playwright install chromium`。", 3) from exc

    with sync_playwright() as pw:
        context, page, browser_label, browser_mode, owns_context = launch_browser(pw, args)
        try:
            cart_data, login_flag, login_completed_at = goto_cart(page, args)
            pages = [{
                "kind": "cart",
                "url": cart_data.get("url") or page.url,
                "captured_at": now_iso(),
                "screenshot": screenshot(page, args, "cart"),
            }]
            warnings = []
            hints = []
            if args.details:
                detail_pages, detail_hints, detail_warnings = capture_item_details(
                    context, cart_data.get("items", []), args)
                pages.extend(detail_pages)
                hints.extend(detail_hints)
                warnings.extend(detail_warnings)
            if args.dialogs:
                dialog_hints, dialog_warnings = capture_dialogs(page, args)
                hints.extend(dialog_hints)
                warnings.extend(dialog_warnings)
            checkout, checkout_warnings = capture_checkout(
                context, page, args, len(cart_data.get("items", [])))
            warnings.extend(checkout_warnings)
            if checkout.get("captured"):
                pages.append({
                    "kind": "checkout",
                    "url": checkout.get("url"),
                    "captured_at": checkout.get("captured_at"),
                    "screenshot": None,
                    "note": checkout.get("note"),
                })
            snapshot = build_snapshot(
                args, cart_data, browser_label, browser_mode,
                login_flag, login_completed_at, pages, hints, checkout, warnings)
            write_json(output, snapshot)
            summary = {
                "status": "captured",
                "output": str(output),
                "items": len(snapshot["cart"]["items"]),
                "promotion_hints": len(snapshot["promotion_hints"]),
                "checkout_captured": bool(snapshot["checkout"].get("captured")),
                "blocking_questions": sum(1 for q in snapshot["questions"] if q["blocking"]),
                "warnings": snapshot["warnings"],
            }
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            keep_open = args.keep_open if args.keep_open is not None else sys.stdin.isatty()
            if keep_open and owns_context:
                try:
                    input("采集完成。浏览器保持打开；按回车关闭。")
                except EOFError:
                    pass
            return 0
        finally:
            if owns_context:
                try:
                    context.close()
                except Exception:
                    pass


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return run(args)
    except AdapterError as exc:
        print(json.dumps({"status": "error", "code": exc.code, "error": str(exc)},
                         ensure_ascii=False), file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        print(json.dumps({"status": "cancelled", "error": "用户中断"}, ensure_ascii=False),
              file=sys.stderr)
        return 130
    except Exception as exc:
        print(json.dumps({"status": "error", "code": 2, "error": str(exc)},
                         ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
