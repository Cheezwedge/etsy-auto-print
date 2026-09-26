# etsy-auto-print

Prints the packing slip and a prepaid shipping label for each new Etsy order,
then marks the order shipped on Etsy with its tracking number — on the next
business day by default, when the parcel is actually likely to be in the mail. Runs unattended
on a Raspberry Pi next to a label printer. See [DESIGN.md](DESIGN.md) for how
it works internally.

**How it actually runs, stated plainly:**

- **It polls; it is not instant.** Etsy offers no order webhook, so this asks
  Etsy for paid-but-unshipped orders on a timer — every 3 minutes by default
  (`poll.interval_seconds`). A new order is picked up within one interval, not
  the moment it is placed.
- **Postage comes from Shippo, not Etsy.** You need a free
  [Shippo](https://goshippo.com) account with a card on file; labels are
  charged there, not deducted from your Etsy payment account, and Shippo's
  pay-as-you-go plan adds a small per-label fee. Rates are USPS commercial
  pricing, much the same as Etsy's own. Until `labels.allow_live = true` the
  program refuses live tokens outright, so it cannot spend money by accident.
- **Anything unexpected holds the order and stops.** A failure never guesses:
  the order is marked `held`, nothing further is printed or bought, and you
  get a push notification. It is never retried automatically — a held order
  waits for you to fix the cause and run `retry <id>`. That is the deliberate
  trade: an order that sits still is recoverable, an order shipped wrong is
  not.

- **Etsy hears "shipped" the next business day, not when the label prints.**
  Telling Etsy at print time sends the buyer a "shipped" email early and
  starts Etsy's delivery estimate from a day the parcel was still on your
  shelf. So tracking is uploaded the next business day (weekends and USPS
  holidays skipped), never later than Etsy's ship-by date for the order.
  Shipping same-day? Click **Shipped today** on the dashboard, **Mark all
  shipped now** on a post-office run, or scan pick slips into
  `etsy-auto-print mark-shipped`. Or set `labels.mark_shipped =
  "carrier_scan"` to tell Etsy the moment USPS scans the parcel, with the
  ship-by date as the backstop — run `etsy-auto-print ship-by` first to
  confirm your orders carry one. `"immediately"` is the old behaviour.

Each step runs at most once per order, and a purchase *attempt* is recorded
before money moves, so a crash mid-purchase holds the order rather than
risking a second label.

No printer yet? The default `file` backend writes each slip and label into an
`outbox/` folder, so the whole pipeline runs today; switching to a real printer
is a two-line config change.

## What it does not do

- **International orders.** These need a customs declaration, which this
  program does not build — so an international order prints its slip, holds,
  and tells you to buy that one label on Etsy (which fills the customs form in
  from the order). `print-label` then prints that PDF on the same thermal
  printer, converting it to ZPL if your queue is raw. Needs `poppler-utils`.
- **Carriers other than USPS**, unless you change `allowed_providers`. Only
  USPS services are mapped out of the box.
- **Multiple boxes per order.** One order ships as one parcel; an order too
  big for the largest configured box holds.
- **Multiple shops**, or anything multi-tenant. One shop, one config, one
  SQLite file.
- **Printing over the network.** The printer must be on the machine running
  the poller, as a CUPS queue.

## Setup

### 1. Create an Etsy app

1. Go to <https://www.etsy.com/developers/your-apps> and create a new app
   (a personal app for your own shop is fine).
2. Note the **keystring** and **shared secret** (both on the same page — Etsy
   requires both in every API request as of Feb 9 2026).
3. Add a **Callback URL** of exactly `http://localhost:8231/callback`
   (or another port — just match `redirect_port` in your config).
4. New apps start in "pending" state with provisional access, which is enough
   for your own shop.

### 2. Install and configure

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cp config.example.toml config.toml
# edit config.toml: set etsy.keystring and etsy.shared_secret
```

Every `etsy-auto-print ...` command below assumes that activated venv. In a
fresh shell you either activate it again, or spell out the path —
`.venv/bin/etsy-auto-print ...` — which is what the Raspberry Pi guide does
throughout, since a service account never activates anything.

### 3. Authorize (one time)

```bash
etsy-auto-print auth
```

This opens a browser to Etsy's consent screen (use `--no-browser` on a
headless Pi and open the printed URL from any device — the final redirect to
`localhost` must be opened on the machine running the command). Tokens are
saved to `tokens.json` (gitignored, chmod 600) and refresh automatically.

### 4. Run

```bash
etsy-auto-print test-slip   # render a sample slip with fake data
etsy-auto-print poll        # check for new orders once
etsy-auto-print run         # poll every 3 minutes, forever
etsy-auto-print status      # every order and its state
etsy-auto-print check       # health-check every connection (exit 1 if broken)
etsy-auto-print show 123    # details + event history for one order
etsy-auto-print reprint 123 # re-print a slip (also un-holds a held order)
etsy-auto-print dashboard   # web UI: status, config/products, logs, tests
```

### Dashboard

`etsy-auto-print dashboard` serves a local web UI — system health checks
(service, Etsy, Shippo, printer, notifications), an order list with retry
buttons, a spreadsheet-style product editor (paste rows straight from Excel,
LibreOffice or Google Sheets), a validated config editor, and
the service log. It binds to localhost; tunnel in with
`ssh -L 8765:localhost:8765 user@host`, or set `[dashboard] password` and
pass `--host 0.0.0.0` to use it from your network. Needs
`pip install -e ".[dashboard]"`.

On a Linux desktop, `./desktop/install-shortcuts.sh YOURUSER@YOURPI` installs
three launchers: **Etsy Label Dashboard** (connects, starts the dashboard,
forwards the port, opens the browser), **Etsy Print Pi (SSH)** (a terminal on
the Pi, in the project directory), and **Connect to Pi** (a bare `ssh`).

Editors have **Save** and **Save & apply** — the latter also restarts the
service (config is read at startup, so changes are inert until then) and
drops you on Status with a checklist of what to verify. See
[docs/RASPBERRY_PI.md](docs/RASPBERRY_PI.md) for the one-line sudoers rule
that lets the restart button work without a password.

### 5. Enable shipping labels (phase 2)

1. Create a free Shippo account (Starter plan) and copy the **test** API
   token from Settings → API (it starts with `shippo_test_`).
2. In `config.toml`, set `[labels] enabled = true`, paste the token, fill in
   your `[labels.ship_from]` address, a box size under `[labels.parcel]`
   (one box for everything) or multiple named presets under
   `[labels.parcels.<name>]` + `[labels.item_parcels]` (different box sizes
   per product — see the comments in `config.example.toml`), and per-SKU
   weights in `[labels.item_weights_oz]`.
3. Verify the whole flow with fake labels (free, printable, scannable):

```bash
etsy-auto-print test-order --no-print   # one fake order end to end, to files
etsy-auto-print test-label      # buy + print a TEST label end to end
etsy-auto-print quote 123       # show rates for a real order, buy nothing
etsy-auto-print products        # your SKUs, weights and boxes
etsy-auto-print listings        # do your LIVE listings use those SKUs?
etsy-auto-print services        # Shippo service tokens for [labels.service_map]
etsy-auto-print poll            # orders now advance: slip -> label
etsy-auto-print reprint-label 123
etsy-auto-print retry 123       # re-run a held order after fixing the cause
etsy-auto-print mark-shipped --all  # tell Etsy everything printed has shipped
etsy-auto-print ship-by         # is Etsy's ship-by date readable on your orders?
etsy-auto-print track 123       # has USPS scanned this parcel yet?
etsy-auto-print refund 123      # ask for the postage back on an unused label
etsy-auto-print print-label FILE  # print a label bought elsewhere (e.g. Etsy)
```

With the test token everything behaves like production except the labels are
watermarked and free, and **tracking is never posted to Etsy for a test
label** (fake tracking must not reach a real buyer) — test-labeled orders
stop at `label_printed`. Until `allow_live` is explicitly set, the program
refuses live tokens — you cannot spend real money by accident.

### 6. Notifications (recommended before going live)

Held orders should reach your phone. Install the free [ntfy](https://ntfy.sh)
app, subscribe to a hard-to-guess topic name, then:

```toml
[notify]
ntfy_url = "https://ntfy.sh/your-secret-topic-name"
```

```bash
etsy-auto-print test-notify   # should pop up on your phone
```

You get a push when an order has printed and is ready to pack (with what to
put in the box), and when one is held or a listing sells out. Set
`on_order = false` under `[notify]` if you ship all day and only want to hear
about problems.

### 7. Going live

First, with the **test** token still in place:

```bash
etsy-auto-print listings   # every live listing's SKU has a weight
etsy-auto-print check      # every connection is green
```

`listings` is the one check fake orders can't do for you: it reads what
buyers can actually click Buy on and cross-checks each SKU against your
products. A listing with no SKU, or a SKU that differs from your products
list by a single capital letter, holds the order — and you find out with a
real buyer waiting.

Then take one real order (your own, or ask a friend) while still in test
mode. It exercises polling, the receipt, weights, rates and both printouts
for the price of Etsy's fees, and cannot buy postage or send anyone fake
tracking. Only after that:

1. In Shippo: add a payment method, copy the **live** token.
2. In `config.toml`: comment out the test token *before* uncommenting the
   live one — two `shippo_token` lines is a TOML error — and set
   `allow_live = true` in the same edit.
3. Restart `run`. From now on labels cost real postage and each completed
   order is marked shipped on Etsy with tracking (buyer gets Etsy's normal
   shipping-notification email).

### Faster shipping options

If a buyer pays for a shipping upgrade, that exact service is bought rather
than the cheapest rate — Etsy's six standard USPS services are mapped out of
the box, and `[labels.service_map]` handles profiles that name them
differently. An upgrade the map doesn't recognise **holds** the order instead
of silently shipping it slower than the buyer paid for.

Tracking is uploaded with the shipment details Etsy accepts alongside it —
service level, package weight and dimensions, what the label cost, and the
ship date — which Etsy uses to give buyers faster tracking updates. All of it
is optional: if Etsy rejects any of it, the tracking number is re-sent on its
own rather than holding an order the buyer is waiting on.

### International orders

International shipments need a customs declaration, which this program does
not build — so an international order **holds** with a message naming the
destination, after printing its packing slip and spending nothing. Buy that
label on Etsy, which fills the customs form in from the order itself, and
print it on the same thermal printer:

```bash
etsy-auto-print print-label ~/etsy-label.pdf
```

Or drop the file into **Print a label bought elsewhere** on the dashboard's
Orders tab. Choose the 4x6 format in Etsy's print dialog.

Etsy hands you a PDF, and a ZPL thermal printer is a raw CUPS queue that
can't read one — it accepts the job, reports a request id, and prints
nothing. So a PDF is rasterised to ZPL automatically whenever
`labels.file_type` is `ZPLII`. That needs `poppler-utils` installed
(`sudo apt install poppler-utils`).

Some services and destinations require the full-page multi-part customs form
instead, which a 4x6 thermal printer cannot produce — those go on ordinary
paper.

Once Etsy marks the order shipped, the poller notices it left the open-orders
list, confirms it against the receipt and closes it out here automatically.

### If a label is never going to be used

A buyer cancels, or the wrong label prints. Shippo stopped refunding unused
USPS labels automatically in 2024, so the postage is gone unless you ask:

```bash
etsy-auto-print refund 123
```

Requests must land within 90 days of purchase and are rejected once the
carrier scans the parcel, so ask early. A refunded label is rejected at the
counter — don't ship with one. Refunds show up as a credit on your next
Shippo invoice, not immediately.

For always-on operation on a Raspberry Pi, follow the full step-by-step
guide in [docs/RASPBERRY_PI.md](docs/RASPBERRY_PI.md).

## How it stays safe

- Every order lives in a local SQLite database (`orders.db`) with a state
  machine (`new → slip_printed → label_purchased → label_printed → … → done`,
  or `held`). Each step runs at most once per order — a label can never be
  purchased twice.
- A purchase *attempt* is recorded before money moves. If the process crashes
  between charging Shippo and recording the result, the order is held with
  instructions to check the Shippo dashboard — never silently re-bought
  (`clear-attempt` resumes after you've verified). Every purchase is tagged
  `Etsy order #<id>`, so the dashboard can actually be searched for it rather
  than eyeballed against a column of identical USPS charges.
- Live Shippo tokens are refused unless `allow_live = true` is set: the
  default configuration physically cannot spend money.
- Anything that fails moves to `held` with a reason (`status` shows it) and
  is retried only when you say so (`retry`).
- Every state change is recorded in an audit trail (`show <id>`).

### Secrets

Your credentials and buyer data live only on your machine and are gitignored,
never committed: `config.toml` (Etsy shared secret, live Shippo token,
dashboard password), `tokens.json` (OAuth refresh token, `chmod 600`),
`orders.db` (buyer names and addresses), `items.csv`, and the `outbox/` and
`label/` output directories. So is `config.toml.bak` — the dashboard writes
one before each save, and it holds the same secrets the live config does.

If you fork this, check `git status` before your first commit rather than
trusting the list. Nothing in the repository's history has ever contained a
credential, and it is worth keeping that true.

`etsy-auto-print dump-receipt` redacts buyer details unless you pass `--raw`;
don't paste raw output anywhere. The dashboard binds to localhost and refuses
a network-facing bind without a password, because the page shows tokens that
can spend money.

## When you buy a printer

Any 4x6 thermal label printer with a CUPS driver works (Rollo, Munbyn,
DYMO 4XL, Zebra…). Set it up as a CUPS queue, then in `config.toml`:

```toml
[printer]
backend = "cups"
cups_queue = "label"   # your CUPS queue name
slip_format = "zpl"    # print the packing slip on it too, as a 4x6 label
```

`slip_format = "zpl"` matters if the label printer is your only printer: a
shipping label carries no SKU, so each order prints a pick slip (order number
plus a scannable barcode, buyer, every SKU and quantity, gift message)
immediately before its shipping label. The two come out as a pair. With a
regular printer available, `slip_queue = "paper"` prints plain-text slips
there instead.

## Development

```bash
pytest
```

## License

MIT — see [LICENSE](LICENSE).
