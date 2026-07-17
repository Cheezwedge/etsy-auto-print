# etsy-auto-print

Automatically process Etsy orders as they come in. **Currently at phase 1**
(see [DESIGN.md](DESIGN.md)): polls your shop for paid, unshipped orders and
prints a packing slip for each — exactly once. Later phases add automatic
shipping-label purchase (via a postage API) and posting tracking back to Etsy.

No printer yet? The default `file` printer backend writes each slip into an
`outbox/` folder so you can run the whole pipeline today; switching to a real
printer later is a two-line config change.

## Setup

### 1. Create an Etsy app

1. Go to <https://www.etsy.com/developers/your-apps> and create a new app
   (a personal app for your own shop is fine).
2. Note the **keystring** (API key).
3. Add a **Callback URL** of exactly `http://localhost:8231/callback`
   (or another port — just match `redirect_port` in your config).
4. New apps start in "pending" state with provisional access, which is enough
   for your own shop.

### 2. Install and configure

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cp config.example.toml config.toml
# edit config.toml: set etsy.keystring
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
etsy-auto-print show 123    # details + event history for one order
etsy-auto-print reprint 123 # re-print a slip (also un-holds a held order)
```

For always-on operation on a Raspberry Pi, see
[systemd/etsy-auto-print.service](systemd/etsy-auto-print.service).

## How it stays safe

- Every order lives in a local SQLite database (`orders.db`) with a state
  machine (`new → slip_printed → … → done`, or `held`). An order is only
  processed from the `new` state, so nothing is ever printed — or, in later
  phases, *purchased* — twice.
- Anything that fails moves to `held` with a reason (`status` shows it) and
  is retried only when you say so (`reprint`).
- Every state change is recorded in an audit trail (`show <id>`).

## When you buy a printer

Any 4x6 thermal label printer with a CUPS driver works (Rollo, Munbyn,
DYMO 4XL, Zebra…). Set it up as a CUPS queue, then in `config.toml`:

```toml
[printer]
backend = "cups"
cups_queue = "label"   # your CUPS queue name
```

## Development

```bash
pytest
```
