# Changelog

Notable changes, newest first. The package version has stayed at 0.1.0, so
entries are dated; each date matches the commits in `git log`.

## 2026-10-13

### Documentation
- Added `CLAUDE.md`: architecture decisions and why, git/deploy conventions,
  Etsy/Shippo/CUPS/systemd gotchas, approaches tried and replaced, open items.
- Added this changelog.
- README: an **Updating** section (pull, then restart both services);
  Pushover setup; the `optional_scopes` escape hatch for `listings_r`; the two
  restart buttons and the installer's sudoers rule; corrected the reason the
  tool polls (a home Pi can't receive Etsy's webhook).
- Raspberry Pi guide: the update command restarts both services; troubleshooting
  rows for stale code and early "shipped" messages.
- DESIGN.md: the real state machine, ntfy/Pushover, and `carrier_scan`.

## 2026-09-26

### Changed
- **Etsy is told "shipped" on the next business day, not when the label
  prints** (`labels.mark_shipped = "next_business_day"`, from
  `mark_shipped_hour`, default 8). Weekends and USPS holidays are skipped;
  never later than Etsy's ship-by date. `ship_date` sent to Etsy is the posting
  day in local time (it was the label's UTC purchase time).

### Added
- `mark_shipped = "carrier_scan"`: post when Shippo reports a USPS scan, with
  the ship-by date as the backstop. `immediately` keeps the old behaviour.
- `mark-shipped [ids] [--all]` (also reads scanned pick-slip barcodes);
  dashboard **Shipped today** and **Mark all shipped now** buttons.
- `ship-by` shows each order's Etsy ship-by evidence; shipped orders report
  when Etsy was told, since Etsy rewrites `expected_ship_date` on shipment.
- `track <id>`: the Shippo tracking status for an order's parcel.
- The order history records *why* tracking went to Etsy when it did.
- `check` and the dashboard warn when the poller runs older code than what's
  on disk; the poller logs its version and ship policy at startup.

### Fixed
- **Restart dashboard** works, or says exactly why it can't: the installer
  writes sudoers rules for both restart commands, and the button detects a
  launcher-started dashboard and a missing service.

## 2026-09-25

### Added
- MIT `LICENSE`.

### Changed
- `.gitignore` covers every file holding secrets or buyer data
  (`config.toml.bak`, `*.bak`, `label/`, `items.csv`, …).
- README states plainly that it polls, uses the shop's own Shippo account, and
  holds on failure.

## 2026-09-07

### Added
- Push notification when an order has printed and is ready to pack
  (`notify.on_order`).
- `[etsy] optional_scopes`, so a shop can stop requesting `listings_r` when
  Etsy won't grant it.

## 2026-08-20

### Added
- International orders hold with a clear message after the slip prints.
- Orders fulfilled outside the tool (e.g. an Etsy-bought label) are
  reconciled and closed automatically.
- `print-label FILE` and a dashboard upload to print labels bought
  elsewhere. PDFs are rasterised to ZPL for raw label queues (needs
  `poppler-utils`).
- Separate **Restart poller** / **Restart dashboard** buttons with a
  self-refreshing waiting page.

### Fixed
- `auth` never opens the consent page in a text browser.
- The CLI finds the installed `config.toml` when run from another directory.
- Printer check uses `lpstat` (no `lpq` on the Pi) and flags a jammed queue.

## 2026-08-13

### Added
- Stock monitoring: alerts when a listing runs low or sells out.

## 2026-08-08 – 2026-08-10

### Added
- `listings`: cross-check live listings' SKUs against configured products.
- Instant-download orders complete without printing or shipping.
- `systemd/install-service.sh` generates the unit for the current user and
  checkout.

### Fixed
- The dashboard starts even when the config fails to load.
- Configs defining both box styles, dead parcel keys, or per-SKU box names
  that don't exist are rejected at startup.

## 2026-08-01 – 2026-08-06

### Added
- Buy the shipping service the buyer paid for (mapped USPS services; unknown
  upgrades hold).
- ZPL pick slip with a barcode before each label; `test-order`.
- Running version in the dashboard and `check`, with a stale-build warning.
- Shippo purchases tagged `Etsy order #<id>`; `refund` command.
- Box ladder: step up to a bigger box instead of holding.
- Desktop launchers (dashboard, SSH, connect) and products pasted from a
  spreadsheet.

## 2026-07-26 – 2026-07-31

### Added
- Pushover notifications; multiple box sizes per order; `dump-receipt`.
- Per-SKU shipping data in a spreadsheet; the web dashboard with Save & apply.
- Full shipment details sent to Etsy with tracking; API reachability and
  scope checks.

### Fixed
- Etsy auth sends `keystring:shared_secret` in `x-api-key`; request `shops_r`.

## 2026-07-17 – 2026-07-19

### Added
- Phase 1: poll Etsy orders and print packing slips.
- Phase 2: buy labels through Shippo (test mode enforced unless
  `allow_live = true`).
- Phase 3: post tracking to Etsy; notify on held orders; Raspberry Pi guide.
