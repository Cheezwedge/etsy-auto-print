"""Local web dashboard: status, config editing, and debugging.

Security posture: this page shows and edits API tokens that can spend real
money, so it binds to localhost by default — reach it from a laptop with

    ssh -L 8765:localhost:8765 you@pi

and open http://localhost:8765. Binding to a LAN address (--host 0.0.0.0)
is allowed but requires a password to be set, since anything on the network
could otherwise read your credentials.

Config writes are validated before they land: the candidate text is parsed
and run through load_config() first, the previous file is kept as .bak, and
the replace is atomic. A typo in the editor can't leave the service with a
config it refuses to start on.
"""

from __future__ import annotations

import csv
import io
import os
import secrets
import subprocess
import tempfile
import tomllib
from datetime import datetime
from functools import wraps
from pathlib import Path

from markupsafe import Markup

from flask import (
    Flask,
    Response,
    flash,
    redirect,
    render_template_string,
    request,
    session,
    url_for,
)

from . import checks
from .about import running_version, version_label
from .config import (
    CSV_PARCEL_COLUMNS,
    CSV_SKU_COLUMNS,
    CSV_WEIGHT_COLUMNS,
    ConfigError,
    load_config,
)
from .notify import Notifier
from .slip import render_packing_slip
from .store import Store

SERVICE_UNIT = "etsy-auto-print"


# --------------------------------------------------------------------------
# templates
# --------------------------------------------------------------------------

BASE = """
<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ title }} · etsy-auto-print</title>
<style>
:root{--bg:#f5f4f0;--card:#fff;--ink:#22242b;--sub:#6b6e78;--line:#e2e0da;
--ok:#1f7a4d;--okbg:#e7f5ee;--warn:#8a6100;--warnbg:#fdf3e0;--fail:#b3261e;--failbg:#fdeceb;
--accent:#29335c;}
@media (prefers-color-scheme:dark){:root{--bg:#15171c;--card:#1e2128;--ink:#e9e8e4;
--sub:#9a9ca6;--line:#2e323b;--okbg:#12291f;--ok:#5fd39b;--warnbg:#2b2312;--warn:#e0b25c;
--failbg:#2c1614;--fail:#f2938c;--accent:#8fa3e0;}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.55 -apple-system,"Segoe UI",Roboto,Helvetica,sans-serif}
header{background:var(--card);border-bottom:1px solid var(--line);padding:14px 20px;
display:flex;gap:20px;align-items:center;flex-wrap:wrap;position:sticky;top:0;z-index:5}
header h1{font-size:15px;margin:0;letter-spacing:.02em}
.ver{color:var(--sub);font-weight:400;font-size:12px;margin-left:6px}
nav{display:flex;gap:4px;flex-wrap:wrap}
nav a{color:var(--sub);text-decoration:none;padding:6px 12px;border-radius:6px;font-size:14px}
nav a:hover{background:var(--bg)}
nav a.on{background:var(--accent);color:#fff}
main{max-width:1000px;margin:0 auto;padding:22px 20px 60px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:16px 18px;margin-bottom:16px}
h2{font-size:16px;margin:0 0 12px}
.check{display:flex;gap:12px;align-items:flex-start;padding:11px 13px;border-radius:8px;
margin-bottom:8px;border:1px solid transparent}
.check.ok{background:var(--okbg);border-color:var(--ok)}
.check.warn{background:var(--warnbg);border-color:var(--warn)}
.check.fail{background:var(--failbg);border-color:var(--fail)}
.dot{width:9px;height:9px;border-radius:50%;margin-top:7px;flex:none}
.ok .dot{background:var(--ok)}.warn .dot{background:var(--warn)}.fail .dot{background:var(--fail)}
.check b{display:block;font-size:14px}
.check .detail{color:var(--sub);font-size:13.5px;word-break:break-word}
.check .hint{font:12.5px ui-monospace,Menlo,Consolas,monospace;color:var(--sub);
margin-top:4px;padding:4px 7px;background:rgba(128,128,128,.12);border-radius:4px;
display:inline-block}
table{width:100%;border-collapse:collapse;font-size:14px}
th{text-align:left;color:var(--sub);font-weight:600;font-size:12px;
text-transform:uppercase;letter-spacing:.05em;padding:6px 8px;border-bottom:1px solid var(--line)}
td{padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:middle}
td input{width:100%;padding:6px 8px;border:1px solid var(--line);border-radius:5px;
background:var(--bg);color:var(--ink);font:14px inherit}
.wrap{overflow-x:auto}
textarea{width:100%;min-height:460px;padding:12px;border:1px solid var(--line);
border-radius:8px;background:var(--bg);color:var(--ink);
font:13px/1.6 ui-monospace,Menlo,Consolas,monospace;resize:vertical}
button,.btn{font:600 14px inherit;padding:9px 16px;border-radius:7px;cursor:pointer;
border:1px solid var(--line);background:var(--card);color:var(--ink);text-decoration:none;
display:inline-block}
button.primary{background:var(--accent);color:#fff;border-color:transparent}
button:hover,.btn:hover{border-color:var(--accent)}
.row{display:flex;gap:9px;flex-wrap:wrap;align-items:center;margin-top:12px}
.flash{padding:11px 14px;border-radius:8px;margin-bottom:12px;font-size:14px}
.flash.ok{background:var(--okbg);border:1px solid var(--ok)}
.flash.err{background:var(--failbg);border:1px solid var(--fail)}
.muted{color:var(--sub);font-size:13px}
pre{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:12px;
overflow-x:auto;font:12.5px/1.5 ui-monospace,Menlo,Consolas,monospace}
.pill{font-size:11px;padding:2px 8px;border-radius:99px;background:var(--bg);
border:1px solid var(--line);color:var(--sub)}
.state-held{color:var(--fail);font-weight:600}
.state-done{color:var(--ok)}
</style></head><body>
<header>
  <h1>etsy-auto-print <span class="ver">{{ version }}</span></h1>
  <nav>
    <a href="{{ url_for('status') }}" class="{{ 'on' if page=='status' }}">Status</a>
    <a href="{{ url_for('orders') }}" class="{{ 'on' if page=='orders' }}">Orders</a>
    <a href="{{ url_for('items') }}" class="{{ 'on' if page=='items' }}">Products</a>
    <a href="{{ url_for('config_edit') }}" class="{{ 'on' if page=='config' }}">Config</a>
    <a href="{{ url_for('logs') }}" class="{{ 'on' if page=='logs' }}">Logs</a>
  </nav>
</header>
<main>
{% if stale %}
<div class="flash err">This page is running <b>{{ version }}</b>, but the code on
  disk is <b>{{ stale }}</b> — you're looking at an older build. The dashboard is a
  separate process from the poller, so restarting the service doesn't update it.
  <div class="hint">sudo systemctl restart etsy-auto-print-dashboard</div>
  <div class="hint">pkill -f "etsy-auto-print dashboard"   # if it isn't a service</div>
</div>
{% endif %}
{% with msgs = get_flashed_messages(with_categories=true) %}
  {% for cat, m in msgs %}<div class="flash {{ cat }}">{{ m }}</div>{% endfor %}
{% endwith %}
{{ body }}
</main></body></html>
"""

CHECKLIST = """
<div class="card" style="border-color:var(--accent)">
  <h2>✅ Applied — what to check now</h2>
  <p class="muted">You changed <b>{{ what }}</b>{{ " and the service was restarted" if restarted else "" }}.
     Work down this list; anything already green above is confirmed good.</p>
  <ol style="margin:0;padding-left:20px;line-height:1.9">
    <li><b>Background service</b> is green above — if it isn't, the service didn't
        come back. Check the <a href="{{ url_for('logs') }}">Logs</a> tab for why.</li>
    {% if not restarted %}
    <li><b>Restart is still needed</b> for your change to take effect —
        the running service is using the old settings until you do.
        <div class="hint">sudo systemctl restart {{ unit }}</div></li>
    {% endif %}
    <li><b>Etsy</b> rows are all green — reachable, connected, and permissions —
        and <b>Shippo</b> shows the mode you expect (TEST = free fake labels,
        LIVE = real money).</li>
    <li><b>Printer</b> is green, then hit <b>Print test label</b> above and confirm a
        physical label comes out complete.</li>
    {% if what in ("products", "config") %}
    <li><b>Weights and boxes</b>: run <code>quote &lt;order-id&gt;</code> on a real
        order to see what a label would cost and which box it picks — it buys
        nothing.</li>
    {% endif %}
    <li><b>Notifications</b>: hit <b>Send test notification</b> and confirm your phone
        buzzes.</li>
    <li><b>Orders</b>: check the <a href="{{ url_for('orders') }}">Orders</a> tab for
        anything stuck in <span class="state-held">held</span> — a change you just made
        may have fixed the cause, in which case hit Retry.</li>
  </ol>
</div>
"""

STATUS = """
{{ checklist }}
<div class="card">
  <h2>System health</h2>
  {% for c in checks %}
    <div class="check {{ c.state }}"><div class="dot"></div><div>
      <b>{{ c.name }}</b>
      <div class="detail">{{ c.detail }}</div>
      {% if c.facts %}<div class="detail">{% for k, v in c.facts.items() %}
        {{ k }}: {{ v }}{% if not loop.last %} &middot; {% endif %}
      {% endfor %}</div>{% endif %}
      {% if c.hint %}<div class="hint">{{ c.hint }}</div>{% endif %}
    </div></div>
  {% endfor %}
  <div class="row"><a class="btn" href="{{ url_for('status') }}">Re-run checks</a></div>
</div>

<div class="card">
  <h2>Test &amp; actions</h2>
  <p class="muted">Safe to run any time. Test labels are free while Shippo is in TEST mode.</p>
  <div class="row">
    <form method="post" action="{{ url_for('action', name='poll') }}"><button>Poll for orders now</button></form>
    <form method="post" action="{{ url_for('action', name='test-slip') }}"><button>Print sample slip</button></form>
    <form method="post" action="{{ url_for('action', name='test-label') }}"><button>Print test label</button></form>
    <form method="post" action="{{ url_for('action', name='test-notify') }}"><button>Send test notification</button></form>
    <form method="post" action="{{ url_for('action', name='restart') }}"><button>Restart service</button></form>
  </div>
</div>

{% if output %}<div class="card"><h2>Output</h2><pre>{{ output }}</pre></div>{% endif %}
"""

ORDERS = """
<div class="card">
  <h2>Orders <span class="pill">{{ rows|length }}</span></h2>
  {% if not rows %}<p class="muted">No orders recorded yet.</p>{% else %}
  <div class="wrap"><table>
    <tr><th>Order</th><th>State</th><th>Buyer</th><th>Updated</th><th>Note</th><th></th></tr>
    {% for r in rows %}
    <tr>
      <td>{{ r.receipt_id }}</td>
      <td class="state-{{ r.state }}">{{ r.state }}</td>
      <td>{{ r.buyer or '?' }}</td>
      <td class="muted">{{ r.updated }}</td>
      <td class="muted">{{ r.error or '' }}</td>
      <td>
        {% if r.state == 'held' %}
        <form method="post" action="{{ url_for('action', name='retry') }}">
          <input type="hidden" name="receipt_id" value="{{ r.receipt_id }}">
          <button>Retry</button>
        </form>
        {% endif %}
      </td>
    </tr>
    {% endfor %}
  </table></div>
  {% endif %}
</div>
{% if output %}<div class="card"><h2>Output</h2><pre>{{ output }}</pre></div>{% endif %}
"""

ITEMS = """
<div class="card">
  <h2>Products</h2>
  {% if not csv_path %}
    <p class="muted">No spreadsheet configured. Add
      <code>items_csv = "items.csv"</code> under <code>[labels]</code> in Config,
      then reload this page to edit products here.</p>
  {% else %}
  <p class="muted">Editing <code>{{ csv_path }}</code>. The <b>sku</b> must match the SKU
     on the Etsy listing. Leave <b>parcel</b> blank to use the default box.</p>
  <form method="post">
    <div class="wrap"><table id="tbl">
      <tr><th>SKU</th><th>Weight (oz)</th><th>Parcel</th><th>Notes</th><th></th></tr>
      {% for row in rows %}
      <tr>
        <td><input name="sku" value="{{ row.sku }}"></td>
        <td><input name="weight_oz" value="{{ row.weight_oz }}" inputmode="decimal"></td>
        <td><input name="parcel" value="{{ row.parcel }}" list="parcels"></td>
        <td><input name="notes" value="{{ row.notes }}"></td>
        <td><button type="button" onclick="this.closest('tr').remove()">✕</button></td>
      </tr>
      {% endfor %}
    </table></div>
    <datalist id="parcels">
      {% for p in parcel_names %}<option value="{{ p }}">{% endfor %}
    </datalist>
    <div class="row">
      <button type="button" onclick="addRow()">Add product</button>
      <button type="submit">Save</button>
      <button class="primary" type="submit" name="apply" value="1">Save &amp; apply</button>
      <span class="muted">“Save &amp; apply” also restarts the service and re-checks status.</span>
    </div>
  </form>
  {% endif %}
</div>

{% if csv_path %}
<div class="card">
  <h2>Paste from a spreadsheet</h2>
  <p class="muted">Select your rows in Excel, LibreOffice or Google Sheets, copy, and
     paste them here — tabs or commas both work. A header row is used to find the
     columns if present; without one the order is
     <b>sku, weight&nbsp;(oz), parcel, notes</b>. Nothing is saved until you review
     the table above and press Save.</p>
  <form method="post" action="{{ url_for('items_import') }}">
    <textarea name="pasted" rows="6" spellcheck="false"
              placeholder="MUG-BLUE-12OZ&#9;14&#9;medium&#9;best seller"></textarea>
    <div class="row">
      <label><input type="radio" name="mode" value="replace" checked> Replace the list</label>
      <label><input type="radio" name="mode" value="append"> Add to it</label>
      <button class="primary" type="submit">Load into the table</button>
    </div>
  </form>
</div>
{% endif %}

{% if csv_path %}
<script>
function addRow(){
  const t=document.getElementById('tbl');
  const r=t.insertRow(-1);
  r.innerHTML='<td><input name="sku"></td><td><input name="weight_oz" inputmode="decimal"></td>'+
    '<td><input name="parcel" list="parcels"></td><td><input name="notes"></td>'+
    '<td><button type="button" onclick="this.closest(\\'tr\\').remove()">✕</button></td>';
  r.querySelector('input').focus();
}
</script>
{% endif %}
"""

CONFIG = """
<div class="card">
  <h2>config.toml</h2>
  <p class="muted">Saved only if it parses and validates — a mistake here can't break the
     running service. The previous version is kept as <code>config.toml.bak</code>.</p>
  <form method="post">
    <textarea name="text" spellcheck="false">{{ text }}</textarea>
    <div class="row">
      <button type="submit">Validate &amp; save</button>
      <button class="primary" type="submit" name="apply" value="1">Save &amp; apply</button>
      <span class="muted">“Save &amp; apply” also restarts the service and re-checks status.</span>
    </div>
  </form>
</div>
"""

LOGS = """
<div class="card">
  <h2>Service log</h2>
  <p class="muted">Last {{ n }} lines from <code>journalctl -u {{ unit }}</code>.</p>
  <pre>{{ log }}</pre>
  <div class="row"><a class="btn" href="{{ url_for('logs') }}">Refresh</a></div>
</div>
"""

LOGIN = """
<div class="card" style="max-width:380px;margin:60px auto">
  <h2>Password required</h2>
  <form method="post">
    <input type="password" name="password" autofocus
      style="width:100%;padding:10px;border:1px solid var(--line);border-radius:7px;
             background:var(--bg);color:var(--ink);font:15px inherit">
    <div class="row"><button class="primary" type="submit">Unlock</button></div>
  </form>
</div>
"""


# --------------------------------------------------------------------------
# app
# --------------------------------------------------------------------------

def create_app(config_path: Path, password: str | None = None) -> Flask:
    app = Flask(__name__)
    app.secret_key = secrets.token_hex(16)
    app.config["CONFIG_PATH"] = Path(config_path).resolve()
    app.config["PASSWORD"] = password

    def cfg():
        return load_config(app.config["CONFIG_PATH"])

    def page(template, title, name, **kw):
        # Markup(): the inner template's output is already-rendered HTML, so it
        # must not be escaped again when it lands in BASE's {{ body }}.
        body = Markup(render_template_string(template, **kw))
        # version_label() re-reads git on every call, so it reflects the code
        # on disk right now; running_version() is what this process loaded at
        # startup. A difference means you're looking at a stale build.
        on_disk = version_label()
        running = running_version()
        return render_template_string(
            BASE, title=title, page=name, body=body, version=running,
            stale=on_disk if on_disk != running else None,
        )

    def _finish(what: str):
        """After a successful save: optionally restart, then show the checklist."""
        if not request.form.get("apply"):
            flash(f"Saved {what}. Restart the service to apply.", "ok")
            return redirect(url_for("status", applied=what, restarted=0))
        try:
            _do_action("restart", app.config["CONFIG_PATH"], request.form)
            flash(f"Saved {what} and restarted the service.", "ok")
            return redirect(url_for("status", applied=what, restarted=1))
        except Exception as exc:
            flash(
                f"Saved {what}, but the restart failed: {exc}", "err"
            )
            return redirect(url_for("status", applied=what, restarted=0))

    def protected(view):
        @wraps(view)
        def wrapper(*a, **kw):
            if app.config["PASSWORD"] and not session.get("auth"):
                return redirect(url_for("login"))
            return view(*a, **kw)
        return wrapper

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            if secrets.compare_digest(
                request.form.get("password", ""), app.config["PASSWORD"] or ""
            ):
                session["auth"] = True
                return redirect(url_for("status"))
            flash("Incorrect password.", "err")
        return render_template_string(
            BASE, title="Login", page="", body=Markup(render_template_string(LOGIN))
        )

    @app.route("/")
    @protected
    def status():
        try:
            results = checks.run_all(cfg())
        except ConfigError as exc:
            results = [checks.Check("Config", checks.FAIL, str(exc), "Fix it on the Config tab")]
        applied = request.args.get("applied")
        checklist = ""
        if applied:
            checklist = Markup(render_template_string(
                CHECKLIST,
                what=applied,
                restarted=request.args.get("restarted") == "1",
                unit=SERVICE_UNIT,
            ))
        return page(STATUS, "Status", "status", checks=results, checklist=checklist,
                    output=session.pop("output", None))

    @app.route("/orders")
    @protected
    def orders():
        rows = []
        try:
            store = Store(cfg().db_path)
            for r in store.all_orders():
                rows.append({
                    "receipt_id": r["receipt_id"],
                    "state": r["state"],
                    "buyer": r["buyer_name"],
                    "updated": datetime.fromtimestamp(r["updated_at"]).strftime("%m-%d %H:%M"),
                    "error": r["error"],
                })
            rows.reverse()
        except Exception as exc:
            flash(f"Could not read orders: {exc}", "err")
        return page(ORDERS, "Orders", "orders", rows=rows, output=session.pop("output", None))

    @app.route("/items", methods=["GET", "POST"])
    @protected
    def items():
        try:
            config = cfg()
        except ConfigError as exc:
            flash(str(exc), "err")
            return page(ITEMS, "Products", "items", csv_path=None, rows=[], parcel_names=[])

        # The CSV path lives in raw TOML (load_config folds the file's contents
        # into item_weights_oz, so the path itself isn't on the Config object).
        with open(app.config["CONFIG_PATH"], "rb") as f:
            toml_raw = tomllib.load(f)
        rel = toml_raw.get("labels", {}).get("items_csv")
        if not rel:
            return page(ITEMS, "Products", "items", csv_path=None, rows=[], parcel_names=[])
        csv_path = app.config["CONFIG_PATH"].parent / rel

        if request.method == "POST":
            skus = request.form.getlist("sku")
            weights = request.form.getlist("weight_oz")
            parcels = request.form.getlist("parcel")
            notes = request.form.getlist("notes")
            buf = io.StringIO()
            writer = csv.writer(buf, lineterminator="\n")
            writer.writerow(["sku", "weight_oz", "parcel", "notes"])
            written = 0
            for i, sku in enumerate(skus):
                sku = sku.strip()
                if not sku:
                    continue
                weight = (weights[i] if i < len(weights) else "").strip()
                if weight:
                    try:
                        float(weight)
                    except ValueError:
                        flash(f"Weight {weight!r} for SKU {sku!r} is not a number.", "err")
                        return redirect(url_for("items"))
                writer.writerow([
                    sku, weight,
                    (parcels[i] if i < len(parcels) else "").strip(),
                    (notes[i] if i < len(notes) else "").strip(),
                ])
                written += 1
            _atomic_write(csv_path, buf.getvalue())
            return _finish("products")

        return page(ITEMS, "Products", "items", csv_path=csv_path,
                    rows=_read_items_csv(csv_path),
                    parcel_names=sorted(config.labels.parcels))

    @app.route("/items/import", methods=["POST"])
    @protected
    def items_import():
        """Load pasted spreadsheet rows into the editor — without saving.

        Deliberately not a write: an import that silently replaced the file
        would be the one destructive button on the page. The rows land in the
        table, the user looks at them, and Save is still Save.
        """
        try:
            config = cfg()
            with open(app.config["CONFIG_PATH"], "rb") as f:
                rel = tomllib.load(f).get("labels", {}).get("items_csv")
        except ConfigError as exc:
            flash(str(exc), "err")
            return redirect(url_for("items"))
        if not rel:
            return redirect(url_for("items"))
        csv_path = app.config["CONFIG_PATH"].parent / rel

        try:
            pasted = parse_pasted_rows(request.form.get("pasted", ""))
        except ValueError as exc:
            flash(str(exc), "err")
            return redirect(url_for("items"))

        rows = pasted
        if request.form.get("mode") == "append":
            rows = _read_items_csv(csv_path) + pasted
        flash(
            f"Loaded {len(pasted)} product(s) into the table — nothing is saved "
            "until you press Save.",
            "ok",
        )
        return page(ITEMS, "Products", "items", csv_path=csv_path, rows=rows,
                    parcel_names=sorted(config.labels.parcels))

    @app.route("/config", methods=["GET", "POST"])
    @protected
    def config_edit():
        path: Path = app.config["CONFIG_PATH"]
        if request.method == "POST":
            text = request.form.get("text", "")
            created = _ensure_items_csv(text, path)
            if created:
                flash(
                    f"Created {created} with a header row — add your products on "
                    "the Products tab.",
                    "ok",
                )
            try:
                _validate_config_text(text, path)
            except (ConfigError, Exception) as exc:
                flash(f"Not saved — {exc}", "err")
                return page(CONFIG, "Config", "config", text=text)
            if path.exists():
                path.with_suffix(path.suffix + ".bak").write_text(path.read_text())
            _atomic_write(path, text)
            return _finish("config")
        return page(CONFIG, "Config", "config", text=path.read_text() if path.exists() else "")

    @app.route("/logs")
    @protected
    def logs():
        code, out = checks._run(
            ["journalctl", "-u", SERVICE_UNIT, "-n", "200", "--no-pager"], timeout=20
        )
        return page(LOGS, "Logs", "logs", log=out or "(no output)", n=200, unit=SERVICE_UNIT)

    @app.route("/action/<name>", methods=["POST"])
    @protected
    def action(name):
        back = request.referrer or url_for("status")
        try:
            output = _do_action(name, app.config["CONFIG_PATH"], request.form)
            session["output"] = output
            flash(f"{name}: done", "ok")
        except Exception as exc:
            flash(f"{name} failed: {exc}", "err")
        return redirect(back)

    return app


_NOTES_COLUMNS = ("notes", "note", "description", "comment")


def _row_value(row: dict, candidates) -> str:
    """Case-insensitive lookup across the spellings a column might have."""
    lowered = {str(k).lower().strip(): k for k in row if k}
    for name in candidates:
        key = name.lower().strip()
        if key in lowered:
            return (row.get(lowered[key]) or "").strip()
    return ""


def _read_items_csv(path: Path) -> list[dict]:
    rows = []
    if not path.exists():
        return rows
    with open(path, newline="", encoding="utf-8-sig") as f:
        for raw in csv.DictReader(f):
            sku = _row_value(raw, CSV_SKU_COLUMNS)
            if not sku:
                continue
            rows.append({
                "sku": sku,
                "weight_oz": _row_value(raw, CSV_WEIGHT_COLUMNS),
                "parcel": _row_value(raw, CSV_PARCEL_COLUMNS),
                "notes": _row_value(raw, _NOTES_COLUMNS),
            })
    return rows


def _clean_weight(value: str) -> str:
    """Accept "14 oz" as 14. The column's unit is fixed, so a typed-out "oz"
    is noise — but anything else (g, lb) is left alone to fail loudly at save
    rather than be silently misread as ounces."""
    stripped = value.strip()
    if stripped.lower().endswith("oz"):
        stripped = stripped[:-2].strip()
    return stripped


def parse_pasted_rows(text: str) -> list[dict]:
    """Rows copied out of a spreadsheet.

    Copying cells from Excel, LibreOffice or Google Sheets puts tab-separated
    text on the clipboard, while an exported file is comma-separated; both are
    accepted. A header row is used to find the columns when present, since a
    real product sheet rarely has them in our order — without one, the order
    is assumed to be sku, weight, parcel, notes.
    """
    text = text.strip("\n\r ")
    if not text:
        raise ValueError("Nothing pasted.")

    lines = [ln for ln in text.splitlines() if ln.strip()]
    delimiter = "\t" if any("\t" in ln for ln in lines) else ","
    table = [r for r in csv.reader(lines, delimiter=delimiter) if any(c.strip() for c in r)]
    if not table:
        raise ValueError("Nothing pasted.")

    known = {c.lower() for c in (*CSV_SKU_COLUMNS, *CSV_WEIGHT_COLUMNS,
                                 *CSV_PARCEL_COLUMNS, *_NOTES_COLUMNS)}
    header = table[0]
    if any(cell.lower().strip() in known for cell in header):
        body = [dict(zip(header, r)) for r in table[1:]]
        rows = [
            {
                "sku": _row_value(r, CSV_SKU_COLUMNS),
                "weight_oz": _clean_weight(_row_value(r, CSV_WEIGHT_COLUMNS)),
                "parcel": _row_value(r, CSV_PARCEL_COLUMNS),
                "notes": _row_value(r, _NOTES_COLUMNS),
            }
            for r in body
        ]
    else:
        rows = [
            {
                "sku": (r[0] if len(r) > 0 else "").strip(),
                "weight_oz": _clean_weight(r[1] if len(r) > 1 else ""),
                "parcel": (r[2] if len(r) > 2 else "").strip(),
                "notes": (r[3] if len(r) > 3 else "").strip(),
            }
            for r in table
        ]

    rows = [r for r in rows if r["sku"]]
    if not rows:
        raise ValueError(
            "No products found in what you pasted — the first column should be "
            "the SKU, or include a header row with a 'sku' column."
        )
    return rows


ITEMS_HEADER = "sku,weight_oz,parcel,notes\n"


def _ensure_items_csv(text: str, config_path: Path) -> Path | None:
    """Create the products sheet when a saved config first names one.

    Without this the editor can't be bootstrapped: validation refuses a
    config pointing at a file that doesn't exist, and the Products tab that
    would create the file only appears once the config points at it. The
    loader still treats a missing file as an error — a typo'd path must not
    silently mean "no weights", which would hold every order later.

    Returns the path if it created one, else None. Never overwrites.
    """
    try:
        rel = tomllib.loads(text).get("labels", {}).get("items_csv")
    except tomllib.TOMLDecodeError:
        return None  # invalid TOML is the validator's error to report
    if not rel:
        return None
    path = config_path.parent / str(rel)
    if path.exists():
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ITEMS_HEADER)
    return path


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", text=True)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _validate_config_text(text: str, real_path: Path) -> None:
    """Parse + fully validate candidate config text without touching the real file.

    The probe file goes *beside* the real config, not in a temp directory:
    load_config resolves relative paths (items_csv, db, tokens) against the
    config's own directory, so validating elsewhere would spuriously fail on
    every config that references a sibling file.
    """
    import tomllib
    tomllib.loads(text)  # syntax
    fd, tmp = tempfile.mkstemp(
        dir=real_path.parent, prefix=".validate-", suffix=".toml", text=True
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        load_config(Path(tmp))  # semantics (required fields, parcel presets, ...)
    finally:
        Path(tmp).unlink(missing_ok=True)


def _do_action(name: str, config_path: Path, form) -> str:
    """Run a dashboard action. Reuses the CLI so behavior can't drift."""
    exe = Path(os.sys.executable).parent / "etsy-auto-print"
    base = [str(exe), "-c", str(config_path)]

    if name == "restart":
        code, out = checks._run(["sudo", "-n", "systemctl", "restart", SERVICE_UNIT], timeout=30)
        if code != 0:
            raise RuntimeError(
                f"{out or 'permission denied'} — run manually: "
                f"sudo systemctl restart {SERVICE_UNIT}"
            )
        return f"Restarted {SERVICE_UNIT}."

    if name == "retry":
        rid = form.get("receipt_id", "").strip()
        if not rid.isdigit():
            raise ValueError("bad receipt id")
        cmd = base + ["retry", rid]
    elif name in ("poll", "test-slip", "test-label", "test-notify"):
        cmd = base + [name]
    else:
        raise ValueError(f"unknown action {name!r}")

    code, out = checks._run(cmd, timeout=120)
    if code != 0:
        raise RuntimeError(out[:500] or f"exited {code}")
    return out or "(no output)"
