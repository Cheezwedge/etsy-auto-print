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
```

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

**Packing slips:** with a raw label queue, plain-text slips can't go to the
label printer, so by default they stay in `outbox/` as files. If you have a
regular paper printer, add it as a second CUPS queue and set
`slip_queue = "paper"` in `[printer]` — slips will print there. (You can
also print ZPL-rendered slips on the label printer; open an issue if you
want that — not built yet.)

Test the physical printer:

```bash
.venv/bin/etsy-auto-print test-label   # should come out of the printer
```

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

To have it always running, copy the systemd unit and change `run` to
`dashboard` in `ExecStart` (use a distinct unit name, e.g.
`etsy-auto-print-dashboard.service`).

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
- Update: `cd ~/etsy-auto-print && git pull && sudo systemctl restart etsy-auto-print`
- Back up the three private files occasionally:

```bash
# in crontab -e, weekly:
0 3 * * 0 tar czf $HOME/backup-etsy-auto-print-$(date +\%F).tgz -C $HOME/etsy-auto-print config.toml tokens.json orders.db
```

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Nothing prints, no errors | `lpstat -p` — printer paused? `cupsenable label` |
| `lp failed for queue` in log | Queue name in config matches `lpstat -p`; user in `lp`/`lpadmin` group |
| Garbage characters printed | Raw queue + PDF file type mismatch: Zebra wants `ZPLII`, driver queues want `PDF_4x6` |
| Poll errors after weeks of uptime | Etsy refresh token expired (90 days idle) — rerun `auth` via the SSH tunnel |
| Service dead after power cut | `journalctl -u etsy-auto-print -b` for the reason; it auto-restarts on failure |
