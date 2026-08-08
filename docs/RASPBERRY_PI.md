# Running etsy-auto-print on a Raspberry Pi

The end state: a Pi sits next to your label printer, polls Etsy every few
minutes, and every new order comes out of the printer as a prepaid label —
with tracking posted back to Etsy and failures pushed to your phone. You
only touch it when your phone tells you to.

Prerequisites: your Etsy app is approved, `test-label` works somewhere, and
you have a label printer. Steps 1–5 can be done before either arrives.

## What to buy

- Raspberry Pi 4 or 5 (any RAM size; a Pi 3 or Zero 2 W also works), power
  supply, microSD card (16 GB+).
- Your label printer + USB cable, generic 4x6 direct-thermal fanfold labels.

## 1. Install the OS

1. On your desktop, install the Raspberry Pi Imager
   (<https://www.raspberrypi.com/software/>).
2. Choose **Raspberry Pi OS Lite (64-bit)** — no desktop needed.
3. In the Imager's settings (gear icon): set hostname (e.g. `printpi`),
   **enable SSH**, set a username/password, and enter your Wi-Fi details
   (skip if using Ethernet).
4. Flash the card, boot the Pi, then from your desktop:

```bash
ssh youruser@printpi.local
```

## 2. Install system packages

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y git python3-venv cups
sudo usermod -aG lpadmin $USER    # allow managing printers
# log out and back in so the group change applies
```

## 3. Install etsy-auto-print

```bash
cd ~
git clone https://github.com/Cheezwedge/etsy-auto-print.git
cd etsy-auto-print
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/pytest        # optional sanity check

# put the command on your PATH, so it works from any directory
mkdir -p ~/.local/bin
ln -sf ~/etsy-auto-print/.venv/bin/etsy-auto-print ~/.local/bin/
hash -r                 # forget the shell's memory of "not found"
etsy-auto-print --help  # should print the command list
```

If that last line still says `command not found`, `~/.local/bin` isn't on
your PATH — Raspberry Pi OS only adds it at login, and it didn't exist when
you logged in. Log out and back in, or for this shell:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

The rest of this guide spells out `.venv/bin/etsy-auto-print` so it works
whether or not you did the symlink step. With the symlink you can drop the
`.venv/bin/` prefix everywhere.

## 4. Copy your state from the machine where you tested

The three private files are the whole state. From your **desktop**:

```bash
scp ~/etsy-auto-print/{config.toml,tokens.json,orders.db} youruser@printpi.local:~/etsy-auto-print/
```

(No `orders.db` yet? Skip it — it's created on first poll.)

If you haven't run `etsy-auto-print auth` anywhere yet, do it on the Pi via
an SSH tunnel — the OAuth redirect goes to `localhost:8231`, so forward that
port and complete the browser step on your desktop:

```bash
ssh -L 8231:localhost:8231 youruser@printpi.local
cd etsy-auto-print && .venv/bin/etsy-auto-print auth --no-browser
# open the printed URL in your DESKTOP browser and approve
```

## 5. Verify the pipeline (file backend, no printer needed)

```bash
cd ~/etsy-auto-print
.venv/bin/etsy-auto-print test-slip
.venv/bin/etsy-auto-print test-label
.venv/bin/etsy-auto-print poll && .venv/bin/etsy-auto-print status
```

Everything should behave exactly as it did on your desktop, with output in
`outbox/`.

## 6. Set up the label printer in CUPS

Plug the printer into the Pi over USB and find its URI:

```bash
lpinfo -v | grep usb
# e.g.  direct usb://Zebra%20Technologies/ZTC%20GX420d?serial=...
```

**Zebra (GX420d/GK420d/ZD…): use a raw queue** — Shippo sends ZPL that the
printer speaks natively, no driver involved:

```bash
lpadmin -p label -E -v 'usb://Zebra%20Technologies/ZTC%20GX420d?serial=...' -m raw
```

Then in `config.toml`:

```toml
[printer]
backend = "cups"
cups_queue = "label"

[labels]
file_type = "ZPLII"      # raw ZPL straight to the printer
```

**Non-Zebra (Rollo/Munbyn/iDPRT/DYMO): use the vendor/CUPS driver** and keep
`file_type = "PDF_4x6"`. Install the driver per the vendor's Linux
instructions, create the queue in the CUPS web UI
(`http://printpi.local:631` → Administration → Add Printer), and set
`cups_queue` to the queue name you chose. If the vendor only ships x86
drivers, the printer may still work in raw mode if it accepts TSPL/ZPL —
test with a small file before giving up.

**Packing slips — how you tell one label from another.** A shipping label
carries the buyer's address and the postage barcode; it says nothing about
what goes in the box. With several orders printing unattended you'd have a
stack of labels and no way to pack from them. Pick one of:

```toml
[printer]
slip_format = "zpl"    # slip prints on the LABEL printer, as a 4x6 label
```

Each order then produces **two labels back to back**: a pick slip (order
number as text *and* a scannable Code 128 barcode, buyer, every SKU with its
quantity, variations, gift message, buyer note), then that order's shipping
label. They come out as a pair, in order — pack straight off the stack. This
is the right choice when the label printer is the only printer you own.

```toml
[printer]
slip_queue = "paper"   # slip prints as plain text on a second CUPS queue
```

Use this instead if you have a regular printer and want a full-page slip.
The two settings are mutually exclusive and the config editor rejects both.

With neither set, slips are written to `outbox/` as `.txt` files and never
printed — a raw ZPL label queue can't render plain text.

Test the physical printer:

```bash
.venv/bin/etsy-auto-print test-order   # a whole fake order: slip, then label
```

This runs one fake order through the same code a real order takes, so what
comes out is exactly what you'd get unattended — with `slip_format = "zpl"`,
two labels back to back. It uses the first SKU in your product list (or
`--sku MUG-1` to test a specific one), so it also checks that product's
weight and box are configured. Nothing is written to your order database and
nothing reaches Etsy; it refuses to run on a live Shippo token.

`test-slip` and `test-label` still exist if you want one or the other alone.

Check the barcode scans with any phone barcode-scanner app.

## 7. Notifications

Either or both channels below may be configured — every configured channel
gets every alert.

**ntfy (free, no account):**
```toml
[notify]
ntfy_url = "https://ntfy.sh/some-long-random-topic-name"
```
Install the ntfy app on your phone, subscribe to the same topic name.

**Pushover ($5 one-time per platform, private):**
```toml
[notify]
pushover_user_key = "your-pushover-user-key"      # from your pushover.net dashboard
pushover_api_token = "your-pushover-app-api-token" # from pushover.net/apps/build
```
Install the Pushover app, log into your account.

Then test whichever you configured:

```bash
.venv/bin/etsy-auto-print test-notify
```

## 7b. Web dashboard (optional but handy)

A local web UI for status, editing config and products, viewing logs, and
running the test actions — no SSH commands needed day to day.

```bash
.venv/bin/pip install -e ".[dashboard]"
.venv/bin/etsy-auto-print dashboard
```

It binds to localhost. From your laptop:

```bash
ssh -L 8765:localhost:8765 youruser@<pi-ip>
```

then open <http://localhost:8765>. To skip the tunnel and use it from any
device on your home network, set a password first (the page shows API
tokens), then bind wide:

```toml
[dashboard]
password = "pick-something-long"
```
```bash
.venv/bin/etsy-auto-print dashboard --host 0.0.0.0
```

### Getting your products in

The **Products** tab edits the per-SKU spreadsheet. It only appears once the
file is named in your config, so first add this under `[labels]` on the
**Config** tab and hit **Save & apply**:

```toml
items_csv = "items.csv"
```

Saving creates the file with its header row for you. (A missing products
file is otherwise a hard config error, deliberately: a typo'd path that
quietly loaded no weights would hold every order later with a confusing
message instead of naming the real problem.)

Then either type rows with **Add product**, or, if you already keep your
products in a spreadsheet, select the cells in Excel / LibreOffice / Google
Sheets, copy, and paste them into **Paste from a spreadsheet**. Tabs and
commas both work, a header row is used to find the columns if you include one
(so your sheet's column order doesn't have to match), and extra columns are
ignored. Pasted rows land in the table for you to check — nothing is written
until you press **Save**.

Only `sku` and `weight_oz` really matter: the SKU must match the SKU on the
Etsy listing exactly, and the weight is what the carrier gets billed on.
Leave `parcel` blank to use the default box.

### After adding products — what to test

First see what you have — these are your own SKUs, not examples to copy:

```bash
.venv/bin/etsy-auto-print products
```

Then run three tests, substituting SKUs from *that* output — the words below
are placeholders, not values to paste:

```bash
.venv/bin/etsy-auto-print test-order --no-print --sku ONE-OF-YOUR-SKUS
.venv/bin/etsy-auto-print test-order --no-print --sku ONE --sku ANOTHER
.venv/bin/etsy-auto-print test-order --no-print --sku ONE-OF-YOUR-SKUS --qty 4
```

`--no-print` writes the slip and label into `outbox/` instead of the printer,
which is what you want while checking weights — no label media spent per
attempt. Drop it once the numbers are right and you want to see the physical
slip-then-label pairing.

A SKU that isn't on your list stops before anything prints or is bought, so
a mistyped name costs a message rather than a label.

(With no `--sku` it uses the first product on your list, which is the fastest
way to confirm the list loaded at all.)

Each run prints what the carrier was told:

```
Declared to the carrier: 26.5 oz in a 10.0 x 7.0 x 4.0 in box
```

1. **Weigh a packed one.** That number should match your scale, including
   box and padding. Under-declaring is the expensive mistake — USPS bills the
   difference back to your Shippo account weeks later, and nothing in the
   pipeline fails, so you won't notice.
2. **Test every SKU.** A missing weight holds the order; better to find that
   now than on a real one.
3. **Test a mixed order** if you use more than one box preset. Everything
   ships in the largest box involved, and the weight sums across items — this
   is where a wrong preset shows up.
4. **Test a bulk quantity.** Weight scales with quantity, box dimensions do
   not. Set `max_items` on each preset to say how many fit: an order that
   outgrows its box steps up to the smallest configured box that holds it,
   and holds only when nothing does. Either way a label never ships with
   dimensions that are a lie. Test one order past the step-up point and
   confirm the label shows the bigger box's size.
5. **Check the slip.** The SKUs and quantities on the printed pick slip
   should match what you asked for — that's what you'll pack from.

To have it always running, copy the systemd unit and change `run` to
`dashboard` in `ExecStart` (use a distinct unit name, e.g.
`etsy-auto-print-dashboard.service`).

**After a `git pull`, restart the dashboard too.** It is a separate
long-lived process from the poller, so `systemctl restart etsy-auto-print`
does not touch it — and the desktop launcher deliberately reuses a dashboard
that is already running rather than starting a new one. The version in the
page header tells you what is actually loaded; compare it with
`git rev-parse --short HEAD`.

```bash
sudo systemctl restart etsy-auto-print-dashboard   # if it runs as a service
pkill -f "etsy-auto-print dashboard"               # otherwise; then relaunch
```

### One-click launchers on your Linux desktop

Three shortcuts, so you never have to remember an SSH command:

- **Etsy Label Dashboard** — connects, starts the dashboard if it isn't
  already running, forwards the port, and opens the page in your browser.
- **Etsy Print Pi (SSH)** — a terminal on the Pi, already in the project
  directory, with the commands you actually use printed in front of you. For
  anything the dashboard doesn't cover.
- **Connect to Pi** — just `ssh youruser@yourpi` and nothing else, for when
  you want a bare session.

From the repo on your **desktop** (not the Pi):

```bash
./desktop/install-shortcuts.sh YOURUSER@YOURPI
```

Replace **both** halves with your own values — `whoami` on the Pi gives the
username, `hostname -I` gives its IP. Prefer the IP: `.local` names only work
if your laptop has mDNS (avahi/nss-mdns) set up, and many don't. The installer
checks that the name resolves before it writes anything.

Both go into your applications menu and onto your desktop. On GNOME,
right-click each desktop icon once and choose **Allow Launching**. Re-run the
installer any time to point them at a different Pi.

For the dashboard, the terminal window it opens *is* the connection — leave it
open while you use the dashboard, and close it (or Ctrl-C) to disconnect.
Because everything goes through the tunnel, the dashboard stays bound to
localhost on the Pi and needs no dashboard password.

It's safe to click when a dashboard is already running on the Pi (its own
systemd unit, say): it reuses that one rather than trying to start a second.
And if you click it twice, the second click just re-opens the browser tab.

Both SSH shortcuts are ordinary login sessions — type `exit` to close them.
"Connect to Pi" is a bare `ssh` with no wrapper around it, so if the
connection fails its window closes with the error still on screen; the other
two keep the window open and log the reason.

Don't have a graphical desktop, or want them from a terminal? Both scripts
work directly:

```bash
./desktop/etsy-dashboard YOURUSER@YOURPI
./desktop/etsy-pi-shell  YOURUSER@YOURPI
```

Override the defaults with `ETSY_DASHBOARD_PORT` (default 8765) or
`ETSY_PI_DIR` (default `etsy-auto-print`).

**If the window opens and closes again straight away**, something failed
before the connection got going. Every run is appended to a log, so the
reason survives even when the window doesn't:

```bash
cat ~/.cache/etsy-auto-print/launcher.log
```

An empty or missing log means the launcher never reached the script at all —
usually a stale copy in `~/.local/bin` (re-run the installer) or a desktop
that ignores `Terminal=true`. Running it straight from a terminal you opened
yourself sidesteps both and shows the error directly:

```bash
~/.local/bin/etsy-dashboard YOURUSER@YOURPI
```

### Let the dashboard restart the service

The **Save & apply** and **Restart service** buttons run `systemctl restart`,
which normally needs a password. Grant just that one command:

```bash
echo "$USER ALL=(root) NOPASSWD: /bin/systemctl restart etsy-auto-print" \
  | sudo tee /etc/sudoers.d/etsy-auto-print
sudo chmod 0440 /etc/sudoers.d/etsy-auto-print
```

This permits exactly one command and nothing else. Without it the buttons
still save your changes — they just report that you need to run
`sudo systemctl restart etsy-auto-print` yourself.

## After you change settings — what to check

The dashboard shows this checklist automatically after every save, but for
reference:

1. **Restart happened.** Config and products are read once at startup, so a
   change does nothing until the service restarts. "Save & apply" does it for
   you; plain "Save" does not.
2. **Background service is green** on Status. If not, the service failed to
   come back — the Logs tab says why (usually a config problem, though the
   editor's validation makes that unlikely).
3. **Etsy is green on all three rows** — reachable, connected, and
   permissions. **Shippo is green** and shows the mode you expect:
   `TEST` = free fake labels, `LIVE` = real money per label.
4. **Printer is green**, then **Print test label** and confirm a complete
   physical label comes out.
5. **After changing weights or boxes**, run `quote <order-id>` against a real
   order — it shows the rate and chosen box without buying anything.
6. **After changing notifications**, hit **Send test notification** and
   confirm your phone buzzes.
7. **Check Orders** for anything `held`. If your change fixed the cause (a
   missing weight, say), hit Retry on that order.

## 8. Run as a service (starts on boot, restarts on failure)

Edit `systemd/etsy-auto-print.service` — set `User=` to your username and
fix both paths to `/home/YOURUSER/etsy-auto-print` — then:

```bash
sudo cp systemd/etsy-auto-print.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now etsy-auto-print
systemctl status etsy-auto-print          # should be active (running)
journalctl -u etsy-auto-print -f          # live log
```

## 9. Go live

1. At Shippo: add a payment method, copy the **live** API token.
2. In `config.toml`: replace the test token, set `allow_live = true`.
3. `sudo systemctl restart etsy-auto-print`
4. Place or wait for a real order and watch it: label prints, order flips
   to shipped on Etsy, buyer gets the tracking email.

## Day-to-day

- Your phone notifies you when an order needs attention; the message
  includes the exact `retry` command.
- `etsy-auto-print status` / `show <id>` — inspect anything, any time.
- `etsy-auto-print check` — the dashboard's Status page as terminal output:
  service, Etsy reachability, token permissions, Shippo, printer,
  notifications. Exits non-zero if anything is failing, so it works as the
  health command behind a desktop shortcut or a cron alert.
- Update: `cd ~/etsy-auto-print && git pull && sudo systemctl restart etsy-auto-print`
- Back up the three private files occasionally:

```bash
# in crontab -e, weekly:
0 3 * * 0 tar czf $HOME/backup-etsy-auto-print-$(date +\%F).tgz -C $HOME/etsy-auto-print config.toml tokens.json orders.db
```

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `etsy-auto-print: command not found` | It lives in the venv. Either run `~/etsy-auto-print/.venv/bin/etsy-auto-print`, or do the symlink step in section 3 to put the bare name on your PATH |
| Nothing prints, no errors | `lpstat -p` — printer paused? `cupsenable label` |
| `lp failed for queue` in log | Queue name in config matches `lpstat -p`; user in `lp`/`lpadmin` group |
| Garbage characters printed | Raw queue + PDF file type mismatch: Zebra wants `ZPLII`, driver queues want `PDF_4x6` |
| Poll errors after weeks of uptime | Etsy refresh token expired (90 days idle) — rerun `auth` via the SSH tunnel |
| Etsy calls suddenly 403 | `etsy-auto-print check` — "Etsy permissions" names any scope the token is missing; "Etsy API reachable" separates an Etsy outage or a bad keystring from a bad token |
| Service dead after power cut | `journalctl -u etsy-auto-print -b` for the reason; it auto-restarts on failure |
