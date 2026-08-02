# etsy-auto-print

Automatically process Etsy orders as they come in (see [DESIGN.md](DESIGN.md)):
polls your shop for paid, unshipped orders, prints a packing slip, buys a
shipping label via Shippo and prints it, then posts the tracking number back
to Etsy — which marks the order shipped and emails your buyer. Exactly once
per order, with every failure held for review (and pushed to your phone via
ntfy if configured) instead of guessed at.

The full loop is: **order placed → slip + prepaid label printed → order
marked shipped with tracking — hands off.**

No printer yet? The default `file` printer backend writes each slip into an
`outbox/` folder so you can run the whole pipeline today; switching to a real
printer later is a two-line config change.

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
etsy-auto-print test-label      # buy + print a TEST label end to end
etsy-auto-print quote 123       # show rates for a real order, buy nothing
etsy-auto-print services        # Shippo service tokens for [labels.service_map]
etsy-auto-print poll            # orders now advance: slip -> label
etsy-auto-print reprint-label 123
etsy-auto-print retry 123       # re-run a held order after fixing the cause
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

### 7. Going live

1. In Shippo: add a payment method, copy the **live** token.
2. In `config.toml`: paste the live token and set `allow_live = true`.
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
  (`clear-attempt` resumes after you've verified).
- Live Shippo tokens are refused unless `allow_live = true` is set: the
  default configuration physically cannot spend money.
- Anything that fails moves to `held` with a reason (`status` shows it) and
  is retried only when you say so (`retry`).
- Every state change is recorded in an audit trail (`show <id>`).

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
