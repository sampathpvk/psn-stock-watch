#!/usr/bin/env python3
"""PSN wallet-card stock monitor for Indian stores.

Fetches the product pages listed in products.json, decides whether each card is
purchasable at or below its face value (MRP), and pushes a phone notification
through ntfy when one is.

Usage
    python monitor.py                 normal run (alerts + state updates)
    python monitor.py --debug         dry run: show what each page parsed to
    python monitor.py --test-notify   send one test push and exit

Environment
    NTFY_TOPIC    required for alerts (keep it long and random; it is the password)
    NTFY_SERVER   optional, default https://ntfy.sh
    NTFY_TOKEN    optional, for a protected / self-hosted ntfy server
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("PSN_CONFIG", ROOT / "products.json"))
STATE_PATH = Path(os.environ.get("PSN_STATE", ROOT / "state.json"))

IN, OUT, BLOCKED, ERROR, UNKNOWN = "in_stock", "out_of_stock", "blocked", "error", "unknown"
FAILING = (BLOCKED, ERROR, UNKNOWN)

STORE_LABEL = {
    "amazon": "Amazon India",
    "flipkart": "Flipkart",
    "jiogames": "JioGames Store",
    "gamestheshop": "Games The Shop",
    "generic": "Store",
}
PRIMARY_STORES = {"amazon", "flipkart"}

DEFAULTS = {
    "realert_hours": 6,             # re-notify this often while a card stays buyable
    "heartbeat_hours": 24,          # silent "still alive" message; 0 turns it off
    "blocked_alert_minutes": 90,    # warn when a store has failed this long
    "suspicious_price_ratio": 0.6,  # price below this share of MRP gets a "verify" note
    "notify_when_gone": False,
    "request_timeout_seconds": 25,
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
}


def log(msg: str) -> None:
    print(msg, flush=True)


@dataclass
class Result:
    status: str
    price: float | None = None
    seller: str = ""
    price_source: str = ""  # "structured" | "heuristic" | ""
    signals: list[str] = field(default_factory=list)
    note: str = ""


# --------------------------------------------------------------------------
# parsing helpers
# --------------------------------------------------------------------------
def clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def parse_price(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"\d[\d,]*(?:\.\d+)?", str(value))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


PRICE_RE = re.compile(r"(?:₹|Rs\.?|INR)\s*(\d[\d,]*(?:\.\d{1,2})?)", re.I)

BLOCK_TITLES = (
    "robot check", "access denied", "just a moment", "attention required",
    "pardon our interruption", "are you a robot", "captcha",
)
BLOCK_PHRASES = (
    "enter the characters you see below",
    "type the characters you see in this image",
    "not a robot",
    "/errors/validatecaptcha",
)
OUT_PHRASES = (
    "currently unavailable", "out of stock", "sold out", "notify me",
    "notify when available", "temporarily unavailable", "coming soon",
)
BUY_RE = re.compile(r"^(add to (cart|bag|basket)|buy( it)? now|purchase now)\b")


def looks_blocked(code: int, html: str) -> bool:
    if code in (403, 429, 503, 529):
        return True
    head = html[:60000].lower()
    m = re.search(r"<title[^>]*>(.*?)</title>", head, re.S)
    title = clean(m.group(1)) if m else ""
    if any(t in title for t in BLOCK_TITLES):
        return True
    return any(p in head for p in BLOCK_PHRASES)


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _seller_name(offer: dict) -> str:
    s = offer.get("seller")
    if isinstance(s, dict):
        return clean(s.get("name"))
    return clean(s) if isinstance(s, str) else ""


def jsonld_result(soup: BeautifulSoup) -> Result | None:
    """schema.org Product offers: the most reliable signal when a site has them."""
    in_prices: list[float | None] = []
    out_seen = False
    seller = ""
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except ValueError:
            continue
        for node in _walk(data):
            types = node.get("@type")
            types = types if isinstance(types, list) else [types]
            if "Product" not in types:
                continue
            offers = node.get("offers")
            offers = offers if isinstance(offers, list) else [offers]
            for off in offers:
                if not isinstance(off, dict):
                    continue
                avail = str(off.get("availability", "")).lower()
                price = parse_price(off.get("price", off.get("lowPrice")))
                if "instock" in avail or "limitedavailability" in avail:
                    in_prices.append(price)
                    seller = seller or _seller_name(off)
                elif any(k in avail for k in ("outofstock", "soldout", "discontinued", "preorder", "backorder")):
                    out_seen = True
    if in_prices:
        known = [p for p in in_prices if p is not None]
        price = min(known) if known else None
        return Result(IN, price, seller, "structured" if price is not None else "", ["json-ld: InStock"])
    if out_seen:
        return Result(OUT, signals=["json-ld: OutOfStock"])
    return None


def visible_text(soup: BeautifulSoup) -> str:
    for t in soup(["script", "style", "noscript", "template"]):
        t.decompose()
    return clean(soup.get_text(" "))


def buy_button(soup: BeautifulSoup) -> str:
    for el in soup.find_all(["button", "a", "input"]):
        raw = el.get("value") if el.name == "input" else el.get_text(" ")
        txt = clean(raw).lower()
        if not txt or len(txt) > 30 or not BUY_RE.match(txt):
            continue
        if el.has_attr("disabled") or el.get("aria-disabled") == "true":
            continue
        cls = " ".join(el.get("class", [])).lower()
        if "disabled" in cls or "sold" in cls:
            continue
        return txt
    return ""


def heuristic_price(soup: BeautifulSoup) -> float | None:
    """First rupee amount after the page heading. Low confidence; flagged as such."""
    h1 = soup.find("h1")
    strings = h1.find_all_next(string=True, limit=300) if h1 else soup.find_all(string=True, limit=400)
    m = PRICE_RE.search(" ".join(strings))
    return parse_price(m.group(1)) if m else None


def analyze_generic(soup: BeautifulSoup) -> Result:
    ld = jsonld_result(soup)
    text = visible_text(soup).lower()  # strips scripts, so run after JSON-LD
    btn = buy_button(soup)
    out_phrase = next((p for p in OUT_PHRASES if p in text), "")

    if ld:
        if ld.status == IN and out_phrase and not btn:
            return Result(UNKNOWN, signals=ld.signals + [f"page says '{out_phrase}'"],
                          note="JSON-LD says in stock but the page says otherwise")
        if ld.status == OUT and btn and not out_phrase:
            return Result(UNKNOWN, signals=ld.signals + [f"buy button '{btn}'"],
                          note="JSON-LD says sold out but a buy button is present")
        if ld.status == IN and ld.price is None:
            ld.price = heuristic_price(soup)
            ld.price_source = "heuristic" if ld.price is not None else ""
        return ld

    if btn and not out_phrase:
        price = heuristic_price(soup)
        return Result(IN, price, "", "heuristic" if price is not None else "", [f"buy button '{btn}'"])
    if out_phrase and not btn:
        return Result(OUT, signals=[f"page says '{out_phrase}'"])
    if btn and out_phrase:
        return Result(UNKNOWN, signals=[f"buy button '{btn}'", f"page says '{out_phrase}'"],
                      note="both buy and out-of-stock wording present")
    return Result(UNKNOWN, note="no stock signal found on page")


AMAZON_PRICE_SELECTORS = (
    "span.priceToPay span.a-offscreen",
    "#corePrice_feature_div span.a-offscreen",
    "#corePriceDisplay_desktop_feature_div span.a-offscreen",
    "#apex_desktop span.a-offscreen",
    "#price_inside_buybox",
    "#priceblock_dealprice",
    "#priceblock_ourprice",
    "#tp_price_block_total_price_ww span.a-offscreen",
)
AMAZON_SELLER_SELECTORS = (
    "#merchant-info",
    "#sellerProfileTriggerId",
    "#tabular-buybox .tabular-buybox-text[tabular-attribute-name='Sold by']",
)


def analyze_amazon(soup: BeautifulSoup) -> Result:
    avail_el = soup.select_one("#availability")
    avail = clean(avail_el.get_text(" ")) if avail_el else ""
    avail_l = avail.lower()
    out_box = soup.select_one("#outOfStock")
    cart = soup.select_one("#add-to-cart-button, #buy-now-button, input[name='submit.add-to-cart']")

    seller = ""
    for sel in AMAZON_SELLER_SELECTORS:
        el = soup.select_one(sel)
        if el and clean(el.get_text(" ")):
            seller = clean(el.get_text(" "))
            break

    price = None
    for sel in AMAZON_PRICE_SELECTORS:
        for node in soup.select(sel):
            if node.find_parent(class_=re.compile(r"a-text-price|basisPrice")):
                continue  # struck-through M.R.P., not what you pay
            price = parse_price(node.get_text())
            if price is not None:
                break
        if price is not None:
            break

    signals = [f"availability='{avail[:40]}'"] if avail else []
    if out_box or "currently unavailable" in avail_l or "out of stock" in avail_l:
        status = OUT
        signals.append("out-of-stock box" if out_box else "availability says unavailable")
    elif cart or "in stock" in avail_l:
        status = IN
        signals.append("add-to-cart button" if cart else "availability says in stock")
    else:
        status = UNKNOWN
    return Result(status, price, seller, "structured" if price is not None else "", signals)


def analyze(store: str, html: str) -> Result:
    soup = BeautifulSoup(html, "html.parser")
    if store == "amazon":
        res = analyze_amazon(soup)
        if res.status != UNKNOWN:
            return res
    return analyze_generic(soup)


# --------------------------------------------------------------------------
# network
# --------------------------------------------------------------------------
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def fetch(session: requests.Session, url: str, timeout: int) -> tuple[int, str, str]:
    last_err = ""
    for attempt in range(3):
        try:
            r = session.get(url, timeout=timeout, allow_redirects=True)
        except requests.RequestException as e:
            last_err = type(e).__name__
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code in (429, 500, 502, 503, 504, 529) and attempt < 2:
            time.sleep(3 * (attempt + 1))
            continue
        return r.status_code, r.text, ""
    return 0, "", last_err or "no response"


def try_shopify(session: requests.Session, url: str, timeout: int) -> Result | None:
    """Shopify exposes /products/<handle>.js with exact price and availability."""
    parts = urlsplit(url)
    if "/products/" not in parts.path or parts.path.endswith(".js"):
        return None
    js_url = urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/") + ".js", "", ""))
    try:
        r = session.get(js_url, timeout=timeout)
        data = r.json()
    except (requests.RequestException, ValueError):
        return None
    if r.status_code != 200 or not isinstance(data, dict) or "variants" not in data:
        return None
    avail = [v for v in data["variants"] if v.get("available")]
    if not avail:
        return Result(OUT, signals=["shopify .js: no variant available"])

    def rupees(v) -> float:
        p = v.get("price")
        return float(p) / 100 if isinstance(p, int) else (parse_price(p) or 0.0)

    return Result(IN, min(rupees(v) for v in avail), "", "structured", ["shopify .js: available"])


def check_item(session: requests.Session, item: dict, timeout: int) -> Result:
    url, store = item["url"], item["store"]
    if store in ("generic", "jiogames", "gamestheshop"):
        res = try_shopify(session, url, timeout)
        if res:
            return res
    code, html, err = fetch(session, url, timeout)
    if err:
        return Result(ERROR, note=err)
    if looks_blocked(code, html):
        return Result(BLOCKED, note=f"HTTP {code}, bot-check page")
    if code >= 400:
        return Result(ERROR, note=f"HTTP {code}")
    return analyze(store, html)


# --------------------------------------------------------------------------
# decision + notifications
# --------------------------------------------------------------------------
def evaluate(item: dict, res: Result, cfg: dict) -> tuple[bool, str]:
    """Is this card buyable at or below MRP right now?"""
    if res.status != IN:
        return False, res.status
    allowed = item.get("allowed_sellers")
    if allowed and not any(a.lower() in res.seller.lower() for a in allowed):
        return False, f"seller '{res.seller or '?'}' not in allowed_sellers"
    face = float(item["face_value"])
    if res.price is None:
        if item.get("alert_when_price_unknown", False):
            return True, "price unreadable"
        return False, "in stock but price unreadable"
    if res.price <= face + 0.01:
        return True, "at or below MRP"
    return False, f"priced ₹{res.price:,.0f}, above MRP ₹{face:,.0f}"


def deal_payload(item: dict, res: Result, cfg: dict) -> dict:
    store = STORE_LABEL.get(item["store"], item["store"])
    face = float(item["face_value"])
    lines = []
    if res.price is not None:
        lines.append(f"Price ₹{res.price:,.0f} (MRP ₹{face:,.0f})")
    else:
        lines.append("Price could not be read. Open the page and check it before paying.")
    if res.seller:
        lines.append(f"Sold by: {res.seller[:60]}")
    if res.price_source == "heuristic":
        lines.append("Price was read loosely from the page; confirm it at checkout.")
    if res.price is not None and res.price < face * cfg["suspicious_price_ratio"]:
        lines.append("Unusually low. Could be a glitch or a misread; verify first.")
    lines.append("Stock can vanish in minutes.")
    primary = item["store"] in PRIMARY_STORES
    return {
        "title": f"PSN ₹{face:,.0f} card in stock: {store}",
        "message": "\n".join(lines),
        "priority": 5 if primary else 4,
        "tags": ["video_game", "rotating_light"],
        "click": item["url"],
        "actions": [{"action": "view", "label": f"Open {store}", "url": item["url"], "clear": True}],
    }


def notify(title: str, message: str, priority: int = 3, tags=None, click=None, actions=None) -> bool:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        log("  ! NTFY_TOPIC is not set; cannot send notifications")
        return False
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    body = {"topic": topic, "title": title, "message": message, "priority": priority, "tags": tags or []}
    if click:
        body["click"] = click
    if actions:
        body["actions"] = actions
    headers = {}
    token = os.environ.get("NTFY_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = requests.post(server, json=body, headers=headers, timeout=15)
        if not r.ok:
            log(f"  ! ntfy returned HTTP {r.status_code}: {r.text[:120]}")
        return r.ok
    except requests.RequestException as e:
        log(f"  ! ntfy request failed: {type(e).__name__}")
        return False


def run_once(items, cfg, state, check, send, now, pause=lambda: None) -> dict:
    """One monitoring pass. check(item)->Result, send(payload)->bool. Mutates and returns state."""
    meta = state.setdefault("_meta", {})
    outcomes: list[tuple[dict, Result, bool]] = []

    for n, item in enumerate(items):
        if n:
            pause()
        try:
            res = check(item)
        except Exception as e:  # one bad page must not sink the whole run
            res = Result(ERROR, note=f"{type(e).__name__}: {e}")
        deal, why = evaluate(item, res, cfg)
        price = f"₹{res.price:,.0f}" if res.price is not None else "-"
        log(f"[{item['store']}] {item['id']}: {res.status} price={price} -> {why}"
            + (f" ({res.note})" if res.note else ""))
        outcomes.append((item, res, deal))

        st = state.setdefault(item["id"], {})
        prev_deal = bool(st.get("deal", False))
        if res.status in FAILING:
            st.setdefault("fail_since", int(now))
            continue
        st.pop("fail_since", None)
        if deal:
            due = (not prev_deal) or now - st.get("last_alert", 0) >= cfg["realert_hours"] * 3600
            sent = send(deal_payload(item, res, cfg)) if due else False
            if sent:
                st["last_alert"] = int(now)
            st["deal"] = prev_deal or sent
        else:
            if prev_deal and cfg["notify_when_gone"]:
                send({"title": f"PSN ₹{float(item['face_value']):,.0f} card gone: "
                               f"{STORE_LABEL.get(item['store'], item['store'])}",
                      "message": "No longer buyable at MRP.", "priority": 2, "tags": ["hourglass"]})
            st["deal"] = False

    # store-level health: every item of a store failing for a while
    health = meta.setdefault("health_alert", {})
    by_store: dict[str, list[str]] = {}
    for item, _, _ in outcomes:
        by_store.setdefault(item["store"], []).append(item["id"])
    for store, ids in by_store.items():
        since = [state[i].get("fail_since") for i in ids]
        if all(since) and now - min(since) >= cfg["blocked_alert_minutes"] * 60 \
                and now - health.get(store, 0) >= 24 * 3600:
            mins = int((now - min(since)) / 60)
            payload = {
                "title": f"{STORE_LABEL.get(store, store)} checks failing",
                "message": f"Every {STORE_LABEL.get(store, store)} page has failed for about {mins} min "
                           "(likely blocked or the page layout changed). Stock alerts from this store "
                           "are not working. Run the workflow with debug on to see why.",
                "priority": 3, "tags": ["warning"],
            }
            if send(payload):
                health[store] = int(now)

    hb = cfg["heartbeat_hours"]
    if hb and now - meta.get("last_heartbeat", 0) >= hb * 3600:
        parts = []
        for store, ids in by_store.items():
            ok = sum(1 for i in ids if "fail_since" not in state[i])
            parts.append(f"{STORE_LABEL.get(store, store)} {ok}/{len(ids)} ok")
        live = sum(1 for _, _, d in outcomes if d)
        payload = {
            "title": "PSN monitor is running",
            "message": "; ".join(parts) + f". Buyable at MRP now: {live}.",
            "priority": 2, "tags": ["white_check_mark"],
        }
        if send(payload):
            meta["last_heartbeat"] = int(now)
    return state


# --------------------------------------------------------------------------
# config / state / CLI
# --------------------------------------------------------------------------
def load_config() -> tuple[list[dict], dict]:
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    cfg = {**DEFAULTS, **raw.get("settings", {})}
    items, seen = [], set()
    for it in raw.get("items", []):
        if it.get("enabled", True) is False:
            continue
        url = str(it.get("url", "")).strip()
        if not url or url.upper().startswith("PASTE"):
            log(f"skip {it.get('id', '?')}: URL not filled in yet")
            continue
        missing = [k for k in ("id", "store", "face_value") if k not in it]
        if missing or it["id"] in seen:
            log(f"skip {it.get('id', '?')}: missing {missing or 'unique id'}")
            continue
        seen.add(it["id"])
        items.append({**it, "url": url})
    return items, cfg


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--debug", action="store_true", help="dry run: print what each page parsed to; no alerts, no state changes")
    ap.add_argument("--test-notify", action="store_true", help="send one test notification and exit")
    args = ap.parse_args(argv)

    if args.test_notify:
        ok = notify("PSN monitor test", "If you can read this on your phone, alerts will reach you.",
                    priority=4, tags=["white_check_mark"])
        log("test notification sent" if ok else "test notification FAILED")
        return 0 if ok else 1

    items, cfg = load_config()
    if not items:
        log("No usable items in products.json. Paste product URLs first.")
        return 1 if not args.debug else 0
    session = make_session()
    timeout = int(cfg["request_timeout_seconds"])

    def check(item: dict) -> Result:
        return check_item(session, item, timeout)

    if args.debug:
        for n, item in enumerate(items):
            if n:
                time.sleep(random.uniform(1, 3))
            res = check(item)
            deal, why = evaluate(item, res, cfg)
            log(f"\n{item['id']}  [{item['store']}]  {item['url']}")
            log(f"  status : {res.status}   {res.note}")
            log(f"  price  : {res.price} ({res.price_source or 'n/a'})   MRP {item['face_value']}")
            log(f"  seller : {res.seller or 'n/a'}")
            log(f"  signals: {', '.join(res.signals) or 'none'}")
            log(f"  would alert: {'YES' if deal else 'no'} ({why})")
        return 0

    if not os.environ.get("NTFY_TOPIC", "").strip():
        log("NTFY_TOPIC is not set. Add it as a repository secret (see README).")
        return 1

    state = load_state()
    before = json.dumps(state, sort_keys=True)
    state = run_once(items, cfg, state, check, lambda p: notify(**p), time.time(),
                     pause=lambda: time.sleep(random.uniform(1, 3)))
    if json.dumps(state, sort_keys=True) != before:
        save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
