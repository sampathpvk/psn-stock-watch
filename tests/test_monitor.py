"""Offline tests: mock store pages and a simulated sequence of runs.

Run:  python tests/test_monitor.py     (or: pytest tests/)
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import monitor as m  # noqa: E402

CFG = dict(m.DEFAULTS)

AMAZON_AT_MRP = """<html><head><title>Sony PlayStation Store Gift Card INR 1000</title></head><body>
<div id="availability"><span>In stock</span></div>
<div id="corePrice_feature_div">
  <span class="a-price priceToPay"><span class="a-offscreen">₹1,000.00</span></span>
  <span class="a-price a-text-price basisPrice"><span class="a-offscreen">₹1,000.00</span></span>
</div>
<div id="merchant-info">Sold by Appario Retail</div>
<input id="add-to-cart-button" type="submit" value="Add to Cart"/></body></html>"""

AMAZON_OVERPRICED = AMAZON_AT_MRP.replace('priceToPay"><span class="a-offscreen">₹1,000.00',
                                          'priceToPay"><span class="a-offscreen">₹2,099.00')

AMAZON_STRUCK_MRP_FIRST = """<html><body><div id="availability">In stock</div>
<div id="corePrice_feature_div">
  <span class="a-price a-text-price"><span class="a-offscreen">₹1,000.00</span></span>
  <span class="a-price priceToPay"><span class="a-offscreen">₹950.00</span></span>
</div><input id="add-to-cart-button"/></body></html>"""

AMAZON_UNAVAILABLE = """<html><body><div id="outOfStock"><div id="availability">
<span>Currently unavailable.</span></div></div></body></html>"""

AMAZON_CAPTCHA = """<html><head><title>Robot Check</title></head><body>
Enter the characters you see below</body></html>"""

LD_IN_STOCK = """<html><body><h1>PlayStation Gift Card 1000</h1>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Product","name":"Card",
"offers":{"@type":"Offer","price":"1000","priceCurrency":"INR","availability":"https://schema.org/InStock",
"seller":{"@type":"Organization","name":"RetailNet"}}}</script>
<button>BUY NOW</button></body></html>"""

LD_OUT = """<html><body><h1>Card</h1>
<script type="application/ld+json">{"@type":"Product","offers":[{"price":1000,"availability":"OutOfStock"}]}</script>
<span>Sold Out</span></body></html>"""

LD_STALE_IN_STOCK = """<html><body><h1>Card</h1>
<script type="application/ld+json">{"@type":"Product","offers":{"price":1000,"availability":"InStock"}}</script>
<span>Sold Out</span><button disabled>Add to cart</button></body></html>"""

TEXT_ONLY_IN_STOCK = """<html><body><h1>PSN Gift Card INR 2000</h1>
<div>₹ 2,000 <s>₹2,000</s></div><button>Add to Cart</button></body></html>"""

TEXT_ONLY_SOLD_OUT = """<html><body><h1>PSN Gift Card INR 2000</h1>
<div>Sold Out</div><button disabled>Add to cart</button><button>Notify Me</button></body></html>"""

NOTHING = "<html><body><h1>Hello</h1><p>nothing useful</p></body></html>"


def test_amazon_in_stock_at_mrp():
    r = m.analyze("amazon", AMAZON_AT_MRP)
    assert (r.status, r.price, r.seller) == (m.IN, 1000.0, "Sold by Appario Retail")
    ok, _ = m.evaluate({"face_value": 1000}, r, CFG)
    assert ok


def test_amazon_overpriced_is_not_a_deal():
    r = m.analyze("amazon", AMAZON_OVERPRICED)
    assert r.status == m.IN and r.price == 2099.0
    ok, why = m.evaluate({"face_value": 1000}, r, CFG)
    assert not ok and "above MRP" in why


def test_amazon_skips_struck_through_mrp():
    r = m.analyze("amazon", AMAZON_STRUCK_MRP_FIRST)
    assert r.price == 950.0


def test_amazon_unavailable():
    assert m.analyze("amazon", AMAZON_UNAVAILABLE).status == m.OUT


def test_captcha_detected_as_blocked():
    assert m.looks_blocked(200, AMAZON_CAPTCHA)
    assert m.looks_blocked(403, "")
    assert not m.looks_blocked(200, AMAZON_AT_MRP)


def test_jsonld_in_stock_and_out():
    r = m.analyze("flipkart", LD_IN_STOCK)
    assert (r.status, r.price, r.seller, r.price_source) == (m.IN, 1000.0, "RetailNet", "structured")
    assert m.analyze("flipkart", LD_OUT).status == m.OUT


def test_stale_jsonld_conflict_is_unknown_not_alert():
    r = m.analyze("flipkart", LD_STALE_IN_STOCK)
    assert r.status == m.UNKNOWN
    assert not m.evaluate({"face_value": 1000}, r, CFG)[0]


def test_text_heuristics():
    r = m.analyze("jiogames", TEXT_ONLY_IN_STOCK)
    assert (r.status, r.price, r.price_source) == (m.IN, 2000.0, "heuristic")
    assert m.analyze("jiogames", TEXT_ONLY_SOLD_OUT).status == m.OUT
    assert m.analyze("jiogames", NOTHING).status == m.UNKNOWN


def test_price_unreadable_only_alerts_if_opted_in():
    r = m.Result(m.IN, None)
    assert not m.evaluate({"face_value": 1000}, r, CFG)[0]
    assert m.evaluate({"face_value": 1000, "alert_when_price_unknown": True}, r, CFG)[0]


def test_allowed_sellers_filter():
    r = m.Result(m.IN, 1000.0, "Sold by Shady Traders")
    item = {"face_value": 1000, "allowed_sellers": ["Appario", "Sony"]}
    assert not m.evaluate(item, r, CFG)[0]
    r.seller = "Sold by Appario Retail"
    assert m.evaluate(item, r, CFG)[0]


def test_shopify_json_prices_are_in_paise():
    class Resp:
        status_code = 200

        def json(self):
            return {"variants": [{"available": True, "price": 100000}, {"available": False, "price": 50000}]}

    class Sess:
        def get(self, url, timeout=0):
            assert url.endswith("/products/psn-1000.js")
            return Resp()

    r = m.try_shopify(Sess(), "https://shop.example/products/psn-1000", 5)
    assert (r.status, r.price) == (m.IN, 1000.0)


def test_suspicious_low_price_is_labelled():
    item = {"id": "x", "store": "amazon", "face_value": 1000, "url": "https://x"}
    p = m.deal_payload(item, m.Result(m.IN, 5.0, "", "structured"), CFG)
    assert "Unusually low" in p["message"] and p["priority"] == 5


def test_run_sequence_alerts_once_then_realerts_then_health_and_heartbeat():
    items = [{"id": "amazon-1000", "store": "amazon", "face_value": 1000, "url": "https://a"},
             {"id": "gts-1000", "store": "gamestheshop", "face_value": 1000, "url": "https://g"}]
    cfg = {**CFG, "heartbeat_hours": 24}
    sent = []
    script = {}

    def check(item):
        return script[item["id"]]

    def send(p):
        sent.append(p["title"])
        return True

    state, t = {}, 1_000_000
    H = 3600

    # run 1: nothing in stock -> only the first heartbeat goes out
    script.update({"amazon-1000": m.Result(m.OUT), "gts-1000": m.Result(m.OUT)})
    m.run_once(items, cfg, state, check, send, t)
    assert sent == ["PSN monitor is running"]

    # run 2: Amazon appears at MRP -> one alert
    script["amazon-1000"] = m.Result(m.IN, 1000.0, "", "structured")
    m.run_once(items, cfg, state, check, send, t + 300)
    assert sent[-1].startswith("PSN ₹1,000 card in stock: Amazon India") and len(sent) == 2

    # run 3, 5 min later: still there -> silent
    m.run_once(items, cfg, state, check, send, t + 600)
    assert len(sent) == 2

    # run 4, 6h later: still there -> re-alert
    m.run_once(items, cfg, state, check, send, t + 300 + 6 * H + 1)
    assert len(sent) == 3

    # goes out of stock then back in -> alerts again immediately
    script["amazon-1000"] = m.Result(m.OUT)
    m.run_once(items, cfg, state, check, send, t + 7 * H)
    script["amazon-1000"] = m.Result(m.IN, 1000.0, "", "structured")
    m.run_once(items, cfg, state, check, send, t + 7 * H + 300)
    assert len(sent) == 4

    # Games The Shop starts failing; after 90 min it raises ONE health warning
    script["gts-1000"] = m.Result(m.BLOCKED)
    base = t + 8 * H
    m.run_once(items, cfg, state, check, send, base)
    m.run_once(items, cfg, state, check, send, base + 60 * 60)
    assert not any("checks failing" in s for s in sent)
    m.run_once(items, cfg, state, check, send, base + 100 * 60)
    m.run_once(items, cfg, state, check, send, base + 110 * 60)
    assert sum("checks failing" in s for s in sent) == 1

    # failures never wipe the "deal" state, and recovery clears the failure clock
    assert state["amazon-1000"]["deal"] is True
    script["gts-1000"] = m.Result(m.OUT)
    m.run_once(items, cfg, state, check, send, base + 120 * 60)
    assert "fail_since" not in state["gts-1000"]

    json.dumps(state)  # state must stay JSON-serialisable


def test_failed_send_is_retried_next_run():
    items = [{"id": "a", "store": "amazon", "face_value": 1000, "url": "https://a"}]
    cfg = {**CFG, "heartbeat_hours": 0}
    attempts = []

    def send(p):
        attempts.append(1)
        return len(attempts) > 1   # first attempt fails

    state = {}
    check = lambda i: m.Result(m.IN, 1000.0, "", "structured")  # noqa: E731
    m.run_once(items, cfg, state, check, send, 1000)
    assert state["a"]["deal"] is False
    m.run_once(items, cfg, state, check, send, 1300)
    assert state["a"]["deal"] is True and len(attempts) == 2


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {name}: {e!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
