# PSN card stock watch (India)

Checks PlayStation Store wallet / gift-card pages on **Amazon India, Flipkart, JioGames Store and Games The Shop**
every ~5 minutes from GitHub's cloud (no laptop needed) and pushes an alert to your phone the moment a card is
buyable **at or below its face value**. Anything priced above MRP is ignored.

It only alerts. You still buy by hand, so payment stays with you.

## Set-up (about 15 minutes)

**1. Phone alerts (ntfy).** Install the free *ntfy* app (Android / iOS). Make up a long random topic name, for
example by running `python3 -c "import secrets; print('psn-' + secrets.token_urlsafe(12))"`, and subscribe to it in
the app. The topic name is effectively the password, so don't share it.

**2. GitHub repo.** Create a new **public** repository and upload everything in this folder, including the
`.github/workflows/monitor.yml` file. Public matters: Actions minutes are unlimited for public repos, while a private
repo's 2,000 free minutes a month would run out in about a week at one run per 5 minutes. If your file manager hides
`.github`, use GitHub's *Add file → Create new file*, type `.github/workflows/monitor.yml` as the name and paste the
contents.

**3. Secret.** Repo → *Settings → Secrets and variables → Actions → New repository secret*. Name `NTFY_TOPIC`,
value = your topic.

**4. Fill in `products.json`.** For each card you care about, open the listing on each store, copy the page URL into
`url`, and set `face_value` (e.g. 1000). Delete rows you don't need; rows still saying `PASTE_...` are skipped.
- Amazon: use the clean form `https://www.amazon.in/dp/<ASIN>`. Several sellers list the same card at inflated
  prices; the script reads the price on the page and ignores anything above face value.
- Games The Shop's PSN category page (from a 2022 article, may have moved) was
  `https://www.gamestheshop.com/bucket/psn-gift-cards--wallet-funds--instant-e-mail-delivery-24x7/31`.
  Open it and copy the URL of each denomination's own page.
- Optional per-row fields: `"allowed_sellers": ["Appario", "Sony"]` (only alert for these sellers; check the
  seller text in the debug output first) and `"alert_when_price_unknown": true`.

**5. Verify before trusting it.** *Actions → PSN stock monitor → Run workflow*:
1. tick **test_notify** → your phone should buzz. If not, the topic or secret is wrong.
2. tick **debug** → the log shows, for every URL, the status, price, seller and the signals it used, plus whether it
   *would* alert. Check that out-of-stock pages read as out of stock and prices look right. This is the step that
   proves each store's page is being read correctly.

After that it runs on its own. Alerts arrive at high/urgent priority (Amazon and Flipkart urgent, the two backups
high). A silent "monitor is running" message arrives daily, so if those stop, the schedule has stopped. In the ntfy
app you can allow urgent notifications to override Do Not Disturb if you want it to wake you.

## What it does and doesn't do

- **Alert rule:** in stock **and** price ≤ face value. One alert when a card becomes buyable, a repeat every 6 hours
  while it stays buyable (`realert_hours`), and a fresh alert whenever it goes out and comes back.
- **Blocked or broken pages:** if every page of a store fails for 90 minutes you get one warning per day for that
  store, rather than silent nothing.
- **Conflicting signals** (e.g. schema says in stock but the page says sold out) count as "unknown": no alert, and
  they feed the failure warning.
- **A checkout fee isn't visible:** a page can show ₹1,000 and still add a convenience fee at payment. Look before
  you pay.
- **Not instant:** GitHub's scheduler is best-effort. 5 minutes is the minimum and runs are often late by several
  minutes. Sony reportedly caps how much wallet credit Indian retailers can sell per week, so stock can appear and
  vanish quickly. If minutes matter, run the same script from a small always-on machine instead:
  ```
  */2 * * * * cd /path/to/psn-stock-watch && NTFY_TOPIC=your-topic /usr/bin/python3 monitor.py >> run.log 2>&1
  ```
  (Locally, state is just kept in `state.json`; nothing to commit.)
- **Bot blocking:** Amazon and Flipkart sometimes serve captcha pages to cloud/datacenter IP addresses. That shows up
  as `blocked` in the debug run and as a health warning later. If it happens, running the script from a home
  connection (Raspberry Pi, old PC) usually works better.
- **GitHub housekeeping:** GitHub can pause scheduled workflows in a repo with no activity for 60 days. The daily
  state commit may count as activity, and the daily heartbeat tells you if the schedule ever stops. Re-enable under
  *Actions* if so.

## Files

| File | Purpose |
|---|---|
| `monitor.py` | the checker and notifier |
| `products.json` | your URLs and settings |
| `.github/workflows/monitor.yml` | the 5-minute cloud schedule |
| `state.json` | remembers what was already alerted (auto-updated) |
| `tests/test_monitor.py` | offline tests: `python tests/test_monitor.py` |

Settings in `products.json` → `settings`: `realert_hours` (6), `heartbeat_hours` (24, 0 = off),
`blocked_alert_minutes` (90), `notify_when_gone` (false).
