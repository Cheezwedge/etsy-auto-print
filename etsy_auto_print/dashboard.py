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
import threading
import tomllib
from datetime import datetime
from functools import wraps
from pathlib import Path

from markupsafe import Markup
from werkzeug.utils import secure_filename

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
    _pick_column,
    load_config,
)
from .config_form import (
    FILE_TYPES,
    PRINTER_BACKENDS,
    SLIP_FORMATS,
    SettingsError,
    apply_boxes,
    apply_settings,
    boxes_from_labels,
    choice_list,
    default_parcel_from_labels,
    parcel_table_names,
    parse_boxes,
    provider_choices,
    read_settings,
    rows_from_box_form,
    sync_item_parcels,
)
from .labels import LabelError, format_ounces, plan_parcel
from .shipdate import POLICIES
from .notify import Notifier
from .pipeline import delay_phrase, describe_delay, format_countdown, release_timestamp, set_label_delay, waiting_to_print
from .pdf2zpl import PdfConvertError
from .printer import PrintError, get_printer, prepare_for_label_queue
from .slip import render_packing_slip
from .store import Store

# Label files only; a 4x6 label is a few hundred KB at most, so the cap is
# generous. Without one, Flask buffers whatever it is handed.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
PRINTABLE_UPLOADS = ("pdf", "png", "zpl")

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
td input,td select{width:100%;padding:6px 8px;border:1px solid var(--line);border-radius:5px;
background:var(--bg);color:var(--ink);font:14px inherit}
fieldset{border:1px solid var(--line);border-radius:8px;margin:0 0 14px;padding:12px 14px}
legend{font-weight:600;font-size:13px;padding:0 6px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px 14px}
.grid label{display:flex;flex-direction:column;gap:3px;font-size:13px;color:var(--sub);font-weight:600}
.grid input,.grid select{font-weight:400;width:100%}
.grid input[type=checkbox],.checks input{width:auto}
.checks{display:flex;flex-wrap:wrap;gap:8px 16px;margin:6px 0 2px}
.checks label{font-weight:500;color:var(--ink);display:flex;gap:6px;align-items:center}
.wrap{overflow-x:auto}
textarea{width:100%;min-height:460px;padding:12px;border:1px solid var(--line);
border-radius:8px;background:var(--bg);color:var(--ink);
font:13px/1.6 ui-monospace,Menlo,Consolas,monospace;resize:vertical}
button,.btn{font:600 14px inherit;padding:9px 16px;border-radius:7px;cursor:pointer;
border:1px solid var(--line);background:var(--card);color:var(--ink);text-decoration:none;
display:inline-block}
button.primary{background:var(--accent);color:#fff;border-color:transparent}
button:hover,.btn:hover{border-color:var(--accent)}
select{padding:6px 8px;border:1px solid var(--line);border-radius:5px;
background:var(--bg);color:var(--ink);font:14px inherit}
.row{display:flex;gap:9px;flex-wrap:wrap;align-items:center;margin-top:12px}
.actions{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.actions form{margin:0}
.countdown{font-variant-numeric:tabular-nums;font-weight:600}
.state-waiting{color:var(--warn);font-weight:600}
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
    <a href="{{ url_for('packaging') }}" class="{{ 'on' if page=='packaging' }}">Packaging</a>
    <a href="{{ url_for('config_edit') }}" class="{{ 'on' if page=='config' }}">Config</a>
    <a href="{{ url_for('logs') }}" class="{{ 'on' if page=='logs' }}">Logs</a>
  </nav>
</header>
<main>
{% if stale %}
<div class="flash err">This page is running <b>{{ version }}</b>, but the code on
  disk is <b>{{ stale }}</b> — you're looking at an older build. The dashboard is a
  separate process from the poller, so <b>Restart poller does not fix this</b>.
  <div class="row">
    <form method="post" action="{{ url_for('action', name='restart-dashboard') }}">
      <button class="primary">Restart dashboard</button>
    </form>
  </div>
  <div class="hint">…or by hand: sudo systemctl restart etsy-auto-print-dashboard</div>
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
    {% if what in ("products", "config", "packaging") %}
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
    <form method="post" action="{{ url_for('action', name='restart') }}"><button>Restart poller</button></form>
    <form method="post" action="{{ url_for('action', name='restart-dashboard') }}"><button>Restart dashboard</button></form>
  </div>
  <p class="muted">The poller processes orders; the dashboard serves this page.
     They are separate processes, so updating the code needs both restarted.</p>
</div>

{% if output %}<div class="card"><h2>Output</h2><pre>{{ output }}</pre></div>{% endif %}
"""

ORDERS = """
<div class="card">
  <h2>Waiting to print <span class="pill">{{ delayed|length }}</span></h2>
  <p class="muted">Postage is bought when this delay ends, after a fresh check
     that the order was not cancelled, refunded, already shipped, or already
     labeled. <b>0</b> prints on the next poll, as before. The value is stored
     with your orders, so it survives a restart and the poller picks it up
     without one.</p>
  <form method="post" action="{{ url_for('set_label_delay') }}">
    <div class="row" style="margin-top:0">
      <label>Wait
        <input name="amount" type="number" min="0" step="1" required
               value="{{ delay_amount }}" inputmode="numeric"
               style="width:5.5em">
      </label>
      <select name="unit" aria-label="Delay unit">
        <option value="minutes" {{ 'selected' if delay_unit == 'minutes' else '' }}>minutes</option>
        <option value="hours" {{ 'selected' if delay_unit == 'hours' else '' }}>hours</option>
      </select>
      <button type="submit">Save delay</button>
    </div>
  </form>
  {% if not delayed %}
  <p class="muted">No orders are waiting.</p>
  {% else %}
  <div class="wrap"><table>
    <tr><th>Order</th><th>Buyer</th><th>Prints</th><th></th></tr>
    {% for w in delayed %}
    <tr>
      <td>{{ w.receipt_id }}</td>
      <td>{{ w.buyer }}</td>
      <td class="countdown"{% if w.state == 'waiting' %} data-release="{{ w.release_at }}"{% endif %}>{{ w.countdown }}</td>
      <td>
        {% if w.detail %}<div class="muted">{{ w.detail }}</div>{% endif %}
        {% if w.state == 'waiting' %}
        <div class="actions">
          <form method="post" action="{{ url_for('action', name='print-now') }}"
                onsubmit="return confirm('Buy postage and print order #{{ w.receipt_id }} now?')">
            <input type="hidden" name="receipt_id" value="{{ w.receipt_id }}">
            <button class="primary">Print now</button>
          </form>
          <form method="post" action="{{ url_for('action', name='skip') }}"
                onsubmit="return confirm('Skip order #{{ w.receipt_id }}? No postage will be bought and nothing will print.')">
            <input type="hidden" name="receipt_id" value="{{ w.receipt_id }}">
            <button>Skip</button>
          </form>
        </div>
        {% else %}
        <span class="muted">printing…</span>
        {% endif %}
      </td>
    </tr>
    {% endfor %}
  </table></div>
  <script>
  function formatCountdown(seconds){
    if(seconds<=0) return "due now";
    var h=Math.floor(seconds/3600), m=Math.floor((seconds%3600)/60), s=seconds%60;
    if(h) return "in "+h+"h "+String(m).padStart(2,"0")+"m";
    if(m) return "in "+m+"m "+String(s).padStart(2,"0")+"s";
    return "in "+s+"s";
  }
  function tickCountdowns(){
    var now=Date.now()/1000;
    document.querySelectorAll(".countdown[data-release]").forEach(function(el){
      var left=Math.round(parseFloat(el.dataset.release)-now);
      el.textContent=formatCountdown(left);
    });
  }
  tickCountdowns();
  setInterval(tickCountdowns, 1000);
  </script>
  {% endif %}
</div>

<div class="card">
  <h2>Orders <span class="pill">{{ rows|length }}</span></h2>
  {% if waiting %}
  <div class="row" style="margin:0 0 12px">
    <span class="muted">{{ waiting }} printed, waiting to tell Etsy they shipped.
      Heading to the post office?</span>
    <form method="post" action="{{ url_for('action', name='mark-shipped') }}">
      <input type="hidden" name="all" value="1">
      <button class="primary">Mark all shipped now</button>
    </form>
  </div>
  {% endif %}
  {% if not rows %}<p class="muted">No orders recorded yet.</p>{% else %}
  <div class="wrap"><table>
    <tr><th>Order</th><th>State</th><th>Buyer</th><th>Updated</th><th>Note</th><th>Weight</th><th></th></tr>
    {% for r in rows %}
    <tr>
      <td>{{ r.receipt_id }}</td>
      <td class="state-{{ r.state }}">{{ r.state }}</td>
      <td>{{ r.buyer or '?' }}</td>
      <td class="muted">{{ r.updated }}</td>
      <td class="muted">{{ r.error or r.ships or r.countdown or '' }}</td>
      <td class="muted">{{ r.parcel or '' }}</td>
      <td>
        {% if r.ships %}
        <form method="post" action="{{ url_for('action', name='mark-shipped') }}">
          <input type="hidden" name="receipt_id" value="{{ r.receipt_id }}">
          <button>Shipped today</button>
        </form>
        {% endif %}
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

<div class="card">
  <h2>Print a label bought elsewhere</h2>
  <p class="muted">
    International orders hold here, because Etsy fills in the customs form and
    this program can't. Buy that label on Etsy, choose the 4x6 format, then
    drop the file in below to print it on the label printer.
  </p>
  <form method="post" action="{{ url_for('upload_label') }}"
        enctype="multipart/form-data">
    <input type="file" name="label" accept=".pdf,.png,.zpl" required>
    <button>Print it</button>
  </form>
  <p class="muted">PDF, PNG or ZPL, up to {{ max_upload_mb }} MB.</p>
</div>
{% if output %}<div class="card"><h2>Output</h2><pre>{{ output }}</pre></div>{% endif %}
"""

ITEMS = """
<div class="card">
  <h2>Products</h2>
  {% if not csv_path %}
    <p class="muted">No spreadsheet configured. Set <b>Products file</b> to
      <code>items.csv</code> on the Config tab (or add
      <code>items_csv = "items.csv"</code> under <code>[labels]</code>),
      then reload this page to edit products here.</p>
  {% else %}
  <p class="muted">Editing <code>{{ csv_path }}</code>. The <b>sku</b> must match the SKU
     on the Etsy listing. <b>Box</b> is one of the types on the Packaging tab.
     <b>Default</b> uses {% if default_parcel %}<b>{{ default_parcel }}</b>{% else %}whatever is set as the default box{% endif %}.
     <b>Label (1)</b> is this product's weight plus that box's empty weight, for a single item.
     An order with more pieces, or a mix of products, can ship in a different box — the total for that order is on the Orders tab.</p>
  <form method="post">
    <div class="wrap"><table id="tbl">
      <tr><th>SKU</th><th>Weight (oz)</th><th>Box</th><th>Label (1)</th><th>Notes</th><th></th></tr>
      {% for row in rows %}
      <tr>
        <td><input name="sku" value="{{ row.sku }}"></td>
        <td><input name="weight_oz" value="{{ row.weight_oz }}" inputmode="decimal"></td>
        <td>
          <select name="parcel" class="parcel">
            <option value="" {{ 'selected' if not row.parcel }}>Default{% if default_parcel %} ({{ default_parcel }}){% endif %}</option>
            {% for p in parcel_names %}
            <option value="{{ p }}" {{ 'selected' if row.parcel == p }}>{{ p }}</option>
            {% endfor %}
            {% if row.parcel and row.parcel not in parcel_names %}
            <option value="{{ row.parcel }}" selected>{{ row.parcel }} (not a configured box)</option>
            {% endif %}
          </select>
        </td>
        <td class="label-oz muted" title="{{ row.label_title }}">{{ row.label_oz }}</td>
        <td><input name="notes" value="{{ row.notes }}"></td>
        <td><button type="button" onclick="this.closest('tr').remove(); refreshLabels()">✕</button></td>
      </tr>
      {% endfor %}
    </table></div>
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
const PARCELS = {{ parcel_names|tojson }};
const DEFAULT_PARCEL = {{ (default_parcel or "")|tojson }};
const BOX_OZ = {{ box_oz|tojson }};
function escapeHtml(s){
  return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/"/g,"&quot;");
}
function defaultLabel(){
  return DEFAULT_PARCEL ? "Default ("+escapeHtml(DEFAULT_PARCEL)+")" : "Default";
}
function formatOz(n){
  const rounded = Math.round(n*100)/100;
  if(Math.abs(rounded-Math.round(rounded))<1e-9) return String(Math.round(rounded));
  return String(rounded);
}
function parcelSelectHtml(){
  let html = '<select name="parcel" class="parcel"><option value="">'+defaultLabel()+'</option>';
  PARCELS.forEach(function(name){
    html += '<option value="'+escapeHtml(name)+'">'+escapeHtml(name)+'</option>';
  });
  return html + '</select>';
}
function refreshLabels(){
  const rows = document.querySelectorAll("#tbl tr");
  rows.forEach(function(row, i){
    if(i===0) return;
    const weight = row.querySelector('input[name=weight_oz]');
    const parcel = row.querySelector('[name=parcel]');
    const out = row.querySelector('.label-oz');
    if(!weight || !parcel || !out) return;
    const w = parseFloat(weight.value);
    const name = parcel.value || DEFAULT_PARCEL;
    const box = BOX_OZ[name];
    if(!name || !(box>=0) || !isFinite(w)){
      out.textContent = "";
      out.title = "";
      return;
    }
    out.textContent = formatOz(w+box)+" oz";
    out.title = formatOz(w)+" oz product + "+formatOz(box)+" oz "+name;
  });
}
function addRow(){
  const t=document.getElementById('tbl');
  const r=t.insertRow(-1);
  r.innerHTML='<td><input name="sku"></td><td><input name="weight_oz" inputmode="decimal"></td>'+
    '<td>'+parcelSelectHtml()+'</td><td class="label-oz muted"></td><td><input name="notes"></td>'+
    '<td><button type="button" onclick="this.closest(\\'tr\\').remove(); refreshLabels()">✕</button></td>';
  r.querySelector('input').focus();
  refreshLabels();
}
document.getElementById('tbl').addEventListener('input', refreshLabels);
document.getElementById('tbl').addEventListener('change', refreshLabels);
refreshLabels();
</script>
{% endif %}
"""

CONFIG = """
{% if settings %}
<div class="card">
  <h2>Settings</h2>
  <p class="muted">These edit <code>config.toml</code> directly. Tokens, the
     shipping-upgrade name map, and anything not listed here stay in the file
     as they are — saving this form does not change them. Boxes are on the
     Packaging tab; which box a SKU uses is on Products.
     Saved only if the result still validates. The previous file is kept as
     <code>config.toml.bak</code>.</p>
  <form method="post">
    <input type="hidden" name="form" value="settings">

    <fieldset>
      <legend>Labels</legend>
      <div class="checks">
        <label><input type="hidden" name="labels_enabled" value="0">
          <input type="checkbox" name="labels_enabled" value="1" {{ 'checked' if settings.labels_enabled }}>
          Buy shipping labels</label>
        <label><input type="hidden" name="allow_live" value="0">
          <input type="checkbox" name="allow_live" value="1" {{ 'checked' if settings.allow_live }}>
          Allow live postage (real charges)</label>
        <label><input type="hidden" name="validate_addresses" value="0">
          <input type="checkbox" name="validate_addresses" value="1" {{ 'checked' if settings.validate_addresses }}>
          Validate buyer addresses</label>
        <label><input type="hidden" name="hold_unmapped_upgrade" value="0">
          <input type="checkbox" name="hold_unmapped_upgrade" value="1" {{ 'checked' if settings.hold_unmapped_upgrade }}>
          Hold orders with an unmapped shipping upgrade</label>
      </div>
      <div class="grid" style="margin-top:10px">
        <label>Label format
          <select name="file_type">
            {% for opt in file_types %}
            <option value="{{ opt }}" {{ 'selected' if settings.file_type == opt }}>{{ opt }}</option>
            {% endfor %}
          </select>
        </label>
        <label>Default box
          <select name="default_parcel">
            <option value="" {{ 'selected' if not settings.default_parcel }}>No default (unmapped products hold)</option>
            {% for name in parcel_names %}
            <option value="{{ name }}" {{ 'selected' if settings.default_parcel == name }}>{{ name }}</option>
            {% endfor %}
            {% if settings.default_parcel and settings.default_parcel not in parcel_names %}
            <option value="{{ settings.default_parcel }}" selected>{{ settings.default_parcel }} (missing)</option>
            {% endif %}
          </select>
        </label>
        <label>When Etsy is told it shipped
          <select name="mark_shipped">
            {% for opt in policies %}
            <option value="{{ opt }}" {{ 'selected' if settings.mark_shipped == opt }}>{{ opt }}</option>
            {% endfor %}
          </select>
        </label>
        <label>From this hour (0–23)
          <input name="mark_shipped_hour" type="number" min="0" max="23" required
                 value="{{ settings.mark_shipped_hour }}">
        </label>
        <label>Products file
          <input name="items_csv" value="{{ settings.items_csv }}" placeholder="items.csv">
        </label>
      </div>
      <p class="muted">Carriers to consider. None checked means the cheapest rate from any carrier.</p>
      <div class="checks">
        {% for name in providers %}
        <label><input type="checkbox" name="allowed_providers" value="{{ name }}"
          {{ 'checked' if name in settings.allowed_providers }}> {{ name }}</label>
        {% endfor %}
      </div>
    </fieldset>

    <fieldset>
      <legend>Ship-from address</legend>
      <div class="grid">
        <label>Name <input name="ship_name" value="{{ settings.ship_name }}"></label>
        <label>Email <input name="ship_email" value="{{ settings.ship_email }}" inputmode="email"></label>
        <label>Street <input name="ship_street1" value="{{ settings.ship_street1 }}"></label>
        <label>Street 2 <input name="ship_street2" value="{{ settings.ship_street2 }}"></label>
        <label>City <input name="ship_city" value="{{ settings.ship_city }}"></label>
        <label>State <input name="ship_state" value="{{ settings.ship_state }}"></label>
        <label>ZIP <input name="ship_zip" value="{{ settings.ship_zip }}"></label>
        <label>Country <input name="ship_country" value="{{ settings.ship_country }}"></label>
        <label>Phone <input name="ship_phone" value="{{ settings.ship_phone }}"></label>
      </div>
      <p class="muted">Required once labels are on. USPS also requires the email. Phone is optional.</p>
    </fieldset>

    <fieldset>
      <legend>Printer</legend>
      <div class="grid">
        <label>Printer
          <select name="backend">
            {% for opt in backends %}
            <option value="{{ opt }}" {{ 'selected' if settings.backend == opt }}>{{ opt }}</option>
            {% endfor %}
          </select>
        </label>
        <label>Label queue (CUPS)
          <input name="cups_queue" value="{{ settings.cups_queue }}" placeholder="required when printer is cups">
        </label>
        <label>Slip format
          <select name="slip_format">
            {% for opt in slip_formats %}
            <option value="{{ opt }}" {{ 'selected' if settings.slip_format == opt }}>{{ opt }}</option>
            {% endfor %}
          </select>
        </label>
        <label>Slip queue
          <input name="slip_queue" value="{{ settings.slip_queue }}" placeholder="blank prints slips to the outbox">
        </label>
        <label>Outbox folder
          <input name="outbox" value="{{ settings.outbox }}" placeholder="outbox">
        </label>
      </div>
      <p class="muted"><b>file</b> writes slips and labels into the outbox.
         <b>cups</b> sends them to the label queue. Slip format <b>zpl</b> prints the
         packing slip on that same label printer, so the slip queue must be empty.</p>
    </fieldset>

    <fieldset>
      <legend>Polling, stock, notifications</legend>
      <div class="grid">
        <label>Poll every (seconds)
          <input name="interval_seconds" type="number" min="1" max="86400" required
                 value="{{ settings.interval_seconds }}">
        </label>
        <label>Label delay in config (minutes)
          <input name="label_delay_minutes" type="number" min="0" max="20160" required
                 value="{{ settings.label_delay_minutes }}">
        </label>
        <label>OAuth redirect port
          <input name="redirect_port" type="number" min="1" max="65535" required
                 value="{{ settings.redirect_port }}">
        </label>
        <label>Low-stock at
          <input name="low_threshold" type="number" min="0" required value="{{ settings.low_threshold }}">
        </label>
        <label>Stock check every (minutes)
          <input name="interval_minutes" type="number" min="5" required value="{{ settings.interval_minutes }}">
        </label>
      </div>
      <div class="checks">
        <label><input type="hidden" name="stock_enabled" value="0">
          <input type="checkbox" name="stock_enabled" value="1" {{ 'checked' if settings.stock_enabled }}>
          Watch listing stock</label>
        <label><input type="hidden" name="notify_on_order" value="0">
          <input type="checkbox" name="notify_on_order" value="1" {{ 'checked' if settings.notify_on_order }}>
          Notify when an order prints</label>
        <label><input type="hidden" name="listings_r" value="0">
          <input type="checkbox" name="listings_r" value="1" {{ 'checked' if settings.listings_r }}>
          Ask Etsy for listings_r (exact stock counts)</label>
      </div>
      <p class="muted">The label delay here is the fallback in <code>config.toml</code>.
         A delay saved on the Orders page is stored with your orders and wins over this
         number, without a restart. This one is read when the poller starts.</p>
    </fieldset>

    <div class="row">
      <button type="submit">Validate &amp; save</button>
      <button class="primary" type="submit" name="apply" value="1">Save &amp; apply</button>
      <span class="muted">“Save &amp; apply” also restarts the service and re-checks status.</span>
    </div>
  </form>
</div>
{% elif parse_error %}
<div class="card">
  <h2>Settings</h2>
  <p class="muted">The settings form needs the file to parse as TOML. Fix the
     error in the editor below, save it, and the form will come back.</p>
</div>
{% endif %}

<div class="card">
  <h2>config.toml</h2>
  <p class="muted">Advanced: the whole file. Saved only if it parses and validates —
     a mistake here can't break the running service. The previous version is kept
     as <code>config.toml.bak</code>. Use this for API tokens, notification
     secrets, and <code>[labels.service_map]</code>.</p>
  <form method="post">
    <input type="hidden" name="form" value="raw">
    <textarea name="text" spellcheck="false">{{ text }}</textarea>
    <div class="row">
      <button type="submit">Validate &amp; save</button>
      <button class="primary" type="submit" name="apply" value="1">Save &amp; apply</button>
      <span class="muted">“Save &amp; apply” also restarts the service and re-checks status.</span>
    </div>
  </form>
</div>
"""

PACKAGING = """
<div class="card">
  <h2>Packaging</h2>
  {% if broken %}
  <p class="muted">config.toml isn't valid TOML, so the boxes can't be read.
     Fix it on the Config tab first.</p>
  {% else %}
  <p class="muted">Each row is a box type (<code>[labels.parcels.&lt;name&gt;]</code>,
     or the single <code>[labels.parcel]</code> block when you only have one).
     <b>Empty oz</b> is the box by itself, with nothing in it.
     A label's weight is the sum of the product weights in the order plus the
     empty weight of the one box chosen for that order.</p>
  <p class="muted">An order that mixes products ships in the largest of their
     boxes by volume. If the pieces don't fit that box's max items, the next
     larger box that holds them is used instead — and that box's empty weight
     is the one added, not the smaller box's. Leave max items blank for no
     limit. A box with no limit also counts as a place smaller boxes can step
     up into, so a special box that is larger than your small mailer will catch
     those orders.</p>
  <form method="post" id="box-form">
    <div class="wrap"><table id="boxes">
      <tr>
        <th>Name</th><th>Length (in)</th><th>Width</th><th>Height</th>
        <th>Empty oz</th><th>Max items</th><th></th>
      </tr>
      {% for box in boxes %}
      <tr>
        <td>
          <input type="hidden" name="original_name" value="{{ box.original }}">
          <input name="name" value="{{ box.name }}" required>
        </td>
        <td><input name="length_in" value="{{ box.length_in }}" inputmode="decimal" required></td>
        <td><input name="width_in" value="{{ box.width_in }}" inputmode="decimal" required></td>
        <td><input name="height_in" value="{{ box.height_in }}" inputmode="decimal" required></td>
        <td><input name="packaging_oz" value="{{ box.packaging_oz }}" inputmode="decimal" required></td>
        <td><input name="max_items" value="{{ box.max_items }}" inputmode="numeric" placeholder="no limit"></td>
        <td><button type="button" onclick="this.closest('tr').remove(); syncDefault()">✕</button></td>
      </tr>
      {% endfor %}
    </table></div>
    <div class="row">
      <label>Default box
        <select name="default_parcel" id="default_parcel">
          <option value="" {{ 'selected' if not default_parcel }}>No default (unmapped products hold)</option>
          {% for box in boxes %}
          <option value="{{ box.name }}" {{ 'selected' if default_parcel == box.name }}>{{ box.name }}</option>
          {% endfor %}
        </select>
      </label>
    </div>
    <div class="row">
      <button type="button" onclick="addBox()">Add box</button>
      <button type="submit">Save</button>
      <button class="primary" type="submit" name="apply" value="1">Save &amp; apply</button>
      <span class="muted">“Save &amp; apply” also restarts the service and re-checks status.</span>
    </div>
  </form>
  <script>
  function syncDefault(){
    const sel = document.getElementById('default_parcel');
    if(!sel) return;
    const current = sel.value;
    const pairs = Array.from(document.querySelectorAll('#boxes tr')).slice(1).map(function(row){
      const nameEl = row.querySelector('input[name=name]');
      const origEl = row.querySelector('input[name=original_name]');
      return {
        name: nameEl ? nameEl.value.trim() : "",
        original: origEl ? origEl.value.trim() : ""
      };
    }).filter(function(p){ return p.name; });
    let keep = current;
    if(!pairs.some(function(p){ return p.name === current; })){
      const renamed = pairs.find(function(p){ return p.original && p.original === current; });
      keep = renamed ? renamed.name : "";
    }
    let html = '<option value="">No default (unmapped products hold)</option>';
    pairs.forEach(function(p){
      const esc = p.name.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/"/g,"&quot;");
      html += '<option value="'+esc+'"'+(p.name===keep?' selected':'')+'>'+esc+'</option>';
    });
    sel.innerHTML = html;
  }
  function addBox(){
    const t = document.getElementById('boxes');
    const r = t.insertRow(-1);
    r.innerHTML = '<td><input type="hidden" name="original_name" value="">'+
      '<input name="name" required></td>'+
      '<td><input name="length_in" inputmode="decimal" required></td>'+
      '<td><input name="width_in" inputmode="decimal" required></td>'+
      '<td><input name="height_in" inputmode="decimal" required></td>'+
      '<td><input name="packaging_oz" inputmode="decimal" required></td>'+
      '<td><input name="max_items" inputmode="numeric" placeholder="no limit"></td>'+
      '<td><button type="button" onclick="this.closest(\\'tr\\').remove(); syncDefault()">✕</button></td>';
    r.querySelector('input[name=name]').focus();
    syncDefault();
  }
  document.getElementById('boxes').addEventListener('input', function(ev){
    if(ev.target && ev.target.name === 'name') syncDefault();
  });
  document.getElementById('box-form').addEventListener('submit', syncDefault);
  </script>
  {% endif %}
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
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

    def cfg():
        return load_config(app.config["CONFIG_PATH"])

    def page(template, title, name, **kw):
        # Markup(): the inner template's output is already-rendered HTML, so it
        # must not be escaped again when it lands in BASE's {{ body }}.
        failure = take_restart_failure()
        if failure:
            flash(f"Restart dashboard failed after the page was sent: {failure}. "
                  f"This is still the old build.", "err")
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

    def _commit_text(text: str, what: str):
        """Validate, back up, and write config.toml. None if it was refused."""
        path: Path = app.config["CONFIG_PATH"]
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
            return None
        if path.exists():
            path.with_suffix(path.suffix + ".bak").write_text(path.read_text())
        _atomic_write(path, text)
        return _finish(what)

    def _render_config(text: str):
        try:
            raw = tomllib.loads(text) if text.strip() else {}
        except tomllib.TOMLDecodeError:
            return page(CONFIG, "Config", "config", text=text, settings=None, parse_error=True)
        if not isinstance(raw, dict):
            raw = {}
        settings = read_settings(raw)
        labels = raw.get("labels") if isinstance(raw.get("labels"), dict) else {}
        names = parcel_table_names(labels)
        return page(
            CONFIG, "Config", "config", text=text, settings=settings, parse_error=False,
            parcel_names=names,
            providers=provider_choices(settings["allowed_providers"]),
            file_types=choice_list(settings["file_type"], FILE_TYPES),
            backends=list(PRINTER_BACKENDS),
            slip_formats=choice_list(settings["slip_format"], SLIP_FORMATS),
            policies=choice_list(settings["mark_shipped"], POLICIES),
        )

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
        waiting = 0
        delayed = []
        delay_amount, delay_unit = 0, "minutes"
        try:
            config = cfg()
            store = Store(config.db_path)
            delay_amount, delay_unit, delay_seconds = describe_delay(
                store, config.label_delay_minutes
            )
            now_ts = datetime.now().timestamp()
            delayed = waiting_to_print(store, delay_seconds, now_ts)
            for r in store.all_orders():
                ships = ""
                if r["state"] == "label_printed":
                    ships = _ship_note(store, r["receipt_id"], config)
                    waiting += bool(ships)
                countdown = ""
                if r["state"] == "waiting":
                    remaining = int(release_timestamp(r, delay_seconds) - now_ts)
                    countdown = "due now" if remaining <= 0 else format_countdown(remaining)
                elif r["state"] == "releasing":
                    countdown = "printing…"
                rows.append({
                    "receipt_id": r["receipt_id"],
                    "state": r["state"],
                    "buyer": r["buyer_name"],
                    "updated": datetime.fromtimestamp(r["updated_at"]).strftime("%m-%d %H:%M"),
                    "error": r["error"],
                    "ships": ships,
                    "countdown": countdown,
                    "parcel": _parcel_summary(config, store.get_receipt_json(r["receipt_id"])),
                })
            rows.reverse()
        except Exception as exc:
            flash(f"Could not read orders: {exc}", "err")
        return page(ORDERS, "Orders", "orders", rows=rows, waiting=waiting,
                    delayed=delayed, delay_amount=delay_amount, delay_unit=delay_unit,
                    max_upload_mb=MAX_UPLOAD_BYTES // (1024 * 1024),
                    output=session.pop("output", None))

    @app.route("/orders/delay", methods=["POST"], endpoint="set_label_delay")
    @protected
    def set_label_delay_view():
        try:
            config = cfg()
        except ConfigError as exc:
            flash(str(exc), "err")
            return redirect(url_for("orders"))
        raw = request.form.get("amount", "").strip()
        unit = request.form.get("unit", "minutes")
        try:
            amount = int(raw)
        except ValueError:
            flash("Delay must be a whole number of minutes or hours.", "err")
            return redirect(url_for("orders"))
        store = Store(config.db_path)
        try:
            try:
                seconds = set_label_delay(store, amount, unit)
            except ValueError as exc:
                flash(str(exc), "err")
                return redirect(url_for("orders"))
        finally:
            store.close()
        if seconds == 0:
            flash("Label delay is off — orders print on the next poll.", "ok")
        else:
            flash(
                f"Label delay set to {delay_phrase(amount, unit)}. "
                "It applies on the next poll.",
                "ok",
            )
        return redirect(url_for("orders"))

    @app.route("/print-label", methods=["POST"])
    @protected
    def upload_label():
        """Print a label file this program didn't buy.

        The alternative is scp plus a hand-written lp command, which is a lot
        of ceremony for something that happens on every international order.
        """
        upload = request.files.get("label")
        if not upload or not upload.filename:
            flash("No file chosen.", "err")
            return redirect(url_for("orders"))

        name = secure_filename(upload.filename)
        ext = Path(name).suffix.lstrip(".").lower()
        if ext not in PRINTABLE_UPLOADS:
            flash(
                f"Cannot print {upload.filename} — expected a "
                f"{' / '.join(sorted(PRINTABLE_UPLOADS))} file.",
                "err",
            )
            return redirect(url_for("orders"))

        data = upload.read()
        if not data:
            # A cancelled or truncated download would otherwise reach the
            # printer as nothing at all and look like a hardware fault.
            flash(f"{upload.filename} is empty — did the download finish?", "err")
            return redirect(url_for("orders"))

        try:
            config = cfg()
            data, ext, note = prepare_for_label_queue(data, ext, config)
            destination = get_printer(config).print_bytes(Path(name).stem, data, ext)
        except (ConfigError, PrintError, PdfConvertError) as exc:
            flash(f"Print failed: {exc}", "err")
            return redirect(url_for("orders"))
        detail = f" ({note})" if note else f" (as {ext.upper()}, unchanged)"
        flash(f"Sent {upload.filename} to {destination}{detail}", "ok")
        return redirect(url_for("orders"))

    def _product_context(config, toml_raw):
        labels_raw = (toml_raw or {}).get("labels") or {}
        if not isinstance(labels_raw, dict):
            labels_raw = {}
        if config is not None:
            boxes = config.labels.parcels
            default = config.labels.default_parcel or ""
            mapping = config.labels.item_parcels
            weights = config.labels.item_weights_oz
            names = sorted(boxes)
        else:
            boxes = _raw_boxes(labels_raw)
            default = default_parcel_from_labels(labels_raw)
            mapping = labels_raw.get("item_parcels") or {}
            weights = labels_raw.get("item_weights_oz") or {}
            if not isinstance(mapping, dict):
                mapping = {}
            if not isinstance(weights, dict):
                weights = {}
            names = sorted(parcel_table_names(labels_raw))
        box_oz = {
            name: box["packaging_oz"]
            for name, box in boxes.items()
            if isinstance(box, dict) and box.get("packaging_oz") is not None
        }
        return labels_raw, boxes, default, mapping, weights, names, box_oz

    def _items_page(csv_path, rows, names, default, box_oz):
        return page(
            ITEMS, "Products", "items", csv_path=csv_path, rows=rows,
            parcel_names=names, default_parcel=default, box_oz=box_oz,
        )

    @app.route("/items", methods=["GET", "POST"])
    @protected
    def items():
        # A bad parcel name in the CSV makes load_config fail, and this page
        # is where you fix it — so it has to render without one. The box
        # dropdown then comes from the raw file rather than an empty list
        # that would blank every row's parcel on save.
        config = None
        try:
            config = cfg()
        except ConfigError as exc:
            flash(str(exc), "err")

        # The CSV path lives in raw TOML (load_config folds the file's contents
        # into item_weights_oz, so the path itself isn't on the Config object).
        try:
            with open(app.config["CONFIG_PATH"], "rb") as f:
                toml_raw = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError):
            return _items_page(None, [], [], "", {})
        labels_raw, boxes, default, mapping, weights, names, box_oz = _product_context(
            config, toml_raw
        )
        rel = labels_raw.get("items_csv")
        if not rel:
            return _items_page(None, [], names, default, box_oz)
        csv_path = app.config["CONFIG_PATH"].parent / rel

        if request.method == "POST":
            before = _merge_product_rows(_read_items_csv(csv_path), mapping, weights)
            previous = {row["sku"] for row in before}
            skus = request.form.getlist("sku")
            weights_in = request.form.getlist("weight_oz")
            parcels = request.form.getlist("parcel")
            notes = request.form.getlist("notes")
            allowed = set(names)
            buf = io.StringIO()
            writer = csv.writer(buf, lineterminator="\n")
            writer.writerow(["sku", "weight_oz", "parcel", "notes"])
            saved: dict[str, str] = {}
            for i, sku in enumerate(skus):
                sku = sku.strip()
                if not sku:
                    continue
                weight = (weights_in[i] if i < len(weights_in) else "").strip()
                if weight:
                    try:
                        float(weight)
                    except ValueError:
                        flash(f"Weight {weight!r} for SKU {sku!r} is not a number.", "err")
                        return redirect(url_for("items"))
                parcel = (parcels[i] if i < len(parcels) else "").strip()
                if parcel and parcel not in allowed:
                    flash(
                        f"SKU {sku!r} is set to box {parcel!r}, which is not a "
                        "packaging type. Pick one from the list.",
                        "err",
                    )
                    return redirect(url_for("items"))
                writer.writerow([
                    sku, weight, parcel,
                    (notes[i] if i < len(notes) else "").strip(),
                ])
                saved[sku] = parcel
            new_csv = buf.getvalue()
            old_csv = csv_path.read_text() if csv_path.exists() else None
            _atomic_write(csv_path, new_csv)
            path: Path = app.config["CONFIG_PATH"]
            old_toml = path.read_text()
            try:
                new_toml = sync_item_parcels(old_toml, saved, previous - set(saved))
            except (tomllib.TOMLDecodeError, Exception) as exc:
                if old_csv is None:
                    csv_path.unlink(missing_ok=True)
                else:
                    _atomic_write(csv_path, old_csv)
                flash(f"Not saved — {exc}", "err")
                return redirect(url_for("items"))
            if new_toml != old_toml:
                try:
                    _validate_config_text(new_toml, path)
                except (ConfigError, Exception) as exc:
                    if old_csv is None:
                        csv_path.unlink(missing_ok=True)
                    else:
                        _atomic_write(csv_path, old_csv)
                    flash(f"Not saved — {exc}", "err")
                    return redirect(url_for("items"))
                path.with_suffix(path.suffix + ".bak").write_text(old_toml)
                _atomic_write(path, new_toml)
            return _finish("products")

        rows = _merge_product_rows(_read_items_csv(csv_path), mapping, weights)
        _annotate_label_preview(rows, boxes, default)
        return _items_page(csv_path, rows, names, default, box_oz)

    @app.route("/items/import", methods=["POST"])
    @protected
    def items_import():
        """Load pasted spreadsheet rows into the editor — without saving.

        Deliberately not a write: an import that silently replaced the file
        would be the one destructive button on the page. The rows land in the
        table, the user looks at them, and Save is still Save.
        """
        config = None
        try:
            config = cfg()
        except ConfigError as exc:
            flash(str(exc), "err")
        try:
            with open(app.config["CONFIG_PATH"], "rb") as f:
                toml_raw = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError):
            return redirect(url_for("items"))
        labels_raw, boxes, default, mapping, weights, names, box_oz = _product_context(
            config, toml_raw
        )
        rel = labels_raw.get("items_csv")
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
            rows = _merge_product_rows(_read_items_csv(csv_path), mapping, weights) + pasted
        _annotate_label_preview(rows, boxes, default)
        flash(
            f"Loaded {len(pasted)} product(s) into the table — nothing is saved "
            "until you press Save.",
            "ok",
        )
        return _items_page(csv_path, rows, names, default, box_oz)

    @app.route("/packaging", methods=["GET", "POST"])
    @protected
    def packaging():
        path: Path = app.config["CONFIG_PATH"]
        labels = _read_labels_section(path)
        if labels is None:
            flash("config.toml is not valid TOML — fix it on the Config tab first.", "err")
            return page(
                PACKAGING, "Packaging", "packaging", boxes=[], default_parcel="", broken=True,
            )

        def show(boxes, default):
            return page(
                PACKAGING, "Packaging", "packaging",
                boxes=boxes, default_parcel=default, broken=False,
            )

        if request.method == "POST":
            posted_default = request.form.get("default_parcel", "")
            try:
                boxes, default_parcel = parse_boxes(request.form)
            except SettingsError as exc:
                flash(str(exc), "err")
                return show(rows_from_box_form(request.form), posted_default)
            text = path.read_text() if path.exists() else ""
            try:
                new_text, rename = apply_boxes(text, boxes, default_parcel)
            except (SettingsError, Exception) as exc:
                flash(f"Not saved — {exc}", "err")
                return show(rows_from_box_form(request.form), posted_default)
            csv_path = _items_csv_path(path, labels)
            csv_backup = None
            if rename and csv_path is not None and csv_path.exists():
                csv_backup = csv_path.read_text()
                _rename_csv_parcels(csv_path, rename)
            try:
                created = _ensure_items_csv(new_text, path)
                if created:
                    flash(
                        f"Created {created} with a header row — add your products on "
                        "the Products tab.",
                        "ok",
                    )
                _validate_config_text(new_text, path)
            except (ConfigError, Exception) as exc:
                if csv_backup is not None and csv_path is not None:
                    _atomic_write(csv_path, csv_backup)
                flash(f"Not saved — {exc}", "err")
                return show(rows_from_box_form(request.form), posted_default)
            if path.exists():
                path.with_suffix(path.suffix + ".bak").write_text(text)
            _atomic_write(path, new_text)
            return _finish("packaging")

        return show(boxes_from_labels(labels), default_parcel_from_labels(labels))

    @app.route("/config", methods=["GET", "POST"])
    @protected
    def config_edit():
        path: Path = app.config["CONFIG_PATH"]
        if request.method == "POST":
            if request.form.get("form") == "settings":
                current = path.read_text() if path.exists() else ""
                try:
                    updated = apply_settings(current, request.form)
                except SettingsError as exc:
                    flash(f"Not saved — {exc}", "err")
                    return _render_config(current)
                saved = _commit_text(updated, "config")
                if saved is None:
                    return _render_config(updated)
                return saved
            text = request.form.get("text", "")
            saved = _commit_text(text, "config")
            if saved is None:
                return _render_config(text)
            return saved
        current = path.read_text() if path.exists() else ""
        return _render_config(current)

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
        if name == "restart-dashboard":
            # Redirecting is the wrong shape here: this action kills the
            # server that would serve the redirect, so the browser lands on
            # a connection error at the POST URL and the user has to find
            # their way back by hand. Send a page that waits instead.
            try:
                _restart_dashboard(delay=1.5)
            except Exception as exc:
                flash(f"{name} failed: {exc}", "err")
                return redirect(back)
            return Response(RESTARTING, mimetype="text/html")
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


def _parcel_summary(config, receipt: dict | None) -> str:
    """Label weight for an order: products plus the box that was chosen."""
    if receipt is None or config is None:
        return ""
    try:
        plan = plan_parcel(receipt, config.labels)
    except LabelError as exc:
        return str(exc)
    except Exception:
        return ""
    return (
        f"{format_ounces(plan.total_oz)} oz in {plan.preset} "
        f"({format_ounces(plan.item_weight_oz)} product + "
        f"{format_ounces(plan.packaging_oz)} box)"
    )


def _raw_boxes(labels: dict) -> dict:
    parcels = labels.get("parcels")
    if isinstance(parcels, dict) and parcels:
        return parcels
    if isinstance(labels.get("parcel"), dict) and labels.get("parcel"):
        return {"default": labels["parcel"]}
    return {}


def _read_labels_section(path: Path) -> dict | None:
    """The [labels] table, or None when the file is not TOML."""
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    labels = raw.get("labels") or {}
    return labels if isinstance(labels, dict) else {}


def _items_csv_path(config_path: Path, labels: dict) -> Path | None:
    rel = labels.get("items_csv")
    if not rel:
        return None
    return config_path.parent / str(rel)


def _merge_product_rows(rows: list[dict], item_parcels: dict, extra_weights: dict) -> list[dict]:
    """Show the box the loader will actually use.

    ``[labels.item_parcels]`` wins over the CSV parcel column. A SKU that
    exists only in that table is still a product the dropdown should edit.
    """
    item_parcels = item_parcels or {}
    extra_weights = extra_weights or {}
    seen = set()
    for row in rows:
        seen.add(row["sku"])
        if row["sku"] in item_parcels:
            row["parcel"] = str(item_parcels[row["sku"]])
        if not str(row.get("weight_oz") or "").strip() and row["sku"] in extra_weights:
            row["weight_oz"] = format_ounces(extra_weights[row["sku"]])
    for sku, preset in item_parcels.items():
        if not str(sku).strip() or sku in seen:
            continue
        weight = format_ounces(extra_weights[sku]) if sku in extra_weights else ""
        rows.append({
            "sku": str(sku),
            "weight_oz": weight,
            "parcel": str(preset),
            "notes": "",
        })
    return rows


def _annotate_label_preview(rows: list[dict], boxes: dict, default: str) -> None:
    for row in rows:
        row["label_oz"] = ""
        row["label_title"] = ""
        raw = str(row.get("weight_oz") or "").strip()
        if not raw:
            continue
        try:
            weight = float(raw)
        except ValueError:
            continue
        name = (row.get("parcel") or "").strip() or default
        box = boxes.get(name) if name else None
        if not isinstance(box, dict) or box.get("packaging_oz") is None:
            continue
        packaging = float(box["packaging_oz"])
        row["label_oz"] = f"{format_ounces(weight + packaging)} oz"
        row["label_title"] = (
            f"{format_ounces(weight)} oz product + {format_ounces(packaging)} oz {name}"
        )


def _rename_csv_parcels(path: Path, rename: dict[str, str]) -> None:
    """Point the parcel column at a box that was just renamed."""
    if not rename or not path.exists():
        return
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    if not fieldnames:
        return
    parcel_col = _pick_column({name: 1 for name in fieldnames}, CSV_PARCEL_COLUMNS)
    if not parcel_col:
        return
    changed = False
    for row in rows:
        current = (row.get(parcel_col) or "").strip()
        if current in rename:
            row[parcel_col] = rename[current]
            changed = True
    if not changed:
        return
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, lineterminator="\n", extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    _atomic_write(path, buf.getvalue())


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


DASHBOARD_UNIT = f"{SERVICE_UNIT}-dashboard"


RESTARTING = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Restarting · etsy-auto-print</title>
<meta http-equiv="refresh" content="6;url=/">
<style>
body{margin:0;min-height:100vh;display:flex;align-items:center;
justify-content:center;background:#f5f4f0;color:#22242b;
font:15px/1.6 -apple-system,"Segoe UI",Roboto,Helvetica,sans-serif}
@media (prefers-color-scheme:dark){body{background:#15171c;color:#e9e8e4}}
.box{max-width:26rem;padding:2rem;text-align:center}
a{color:inherit}
</style></head><body><div class="box">
<h1>Restarting the dashboard…</h1>
<p>This page reloads itself in a few seconds. The version in the header
should change once it comes back.</p>
<p><a href="/">Reload now</a></p>
</div></body></html>
"""


# Exactly what sudo is asked to run. The sudoers rule that allows it has to
# name this same command line, word for word — sudo matches exactly.
RESTART_DASHBOARD = ["systemctl", "restart", "--no-block", DASHBOARD_UNIT]

# Set by the deferred restart if it fails. By then the response has gone, so
# the only way to tell anyone is on the next page load — which, since the
# restart failed, is served by this same process.
_restart_failure: str | None = None


def take_restart_failure() -> str | None:
    global _restart_failure
    failure, _restart_failure = _restart_failure, None
    return failure


def _dashboard_service_pid() -> int | None:
    code, out = checks._run(
        ["systemctl", "show", "-p", "MainPID", "--value", DASHBOARD_UNIT]
    )
    out = out.strip()
    return int(out) if code == 0 and out.isdigit() and int(out) > 0 else None


def _restart_dashboard(delay: float = 0.0) -> str:
    """Restart the process serving this page.

    The poller and the dashboard are separate long-lived processes, so a
    `git pull` plus "Restart poller" leaves this page on the old build.

    Everything that could stop the restart is checked before anything is
    promised, because a "Restarting…" page that comes back unchanged looks
    exactly like a restart that worked and a fix that didn't.
    """
    if checks._run(["systemctl", "cat", DASHBOARD_UNIT])[0] != 0:
        raise RuntimeError(
            "the dashboard isn't installed as a service, so nothing would "
            "start it again after stopping it. Install it with "
            "./systemd/install-service.sh dashboard — or close the launcher "
            "window and reopen it."
        )

    # The service restarting is no use if this page isn't the service. The
    # desktop launcher starts its own copy, which also holds port 8765 — so
    # the service copy can't even start while it runs.
    service_pid = _dashboard_service_pid()
    if service_pid != os.getpid():
        state = ("isn't running — probably because this copy holds its port"
                 if service_pid is None else f"is a different process ({service_pid})")
        raise RuntimeError(
            f"this page is served by a dashboard started outside the service "
            f"(pid {os.getpid()}, most likely by the desktop launcher), and the "
            f"{DASHBOARD_UNIT} service {state}. Restarting the service wouldn't "
            f"touch this page. Close the launcher window and reopen it."
        )

    # Ask sudo about the exact command, without running it or prompting.
    code, _ = checks._run(["sudo", "-n", "-l", *RESTART_DASHBOARD])
    if code != 0:
        raise RuntimeError(
            "sudo won't run the restart without a password. Allow it with "
            "./systemd/install-service.sh dashboard — or run it by hand: "
            f"sudo systemctl restart {DASHBOARD_UNIT}"
        )

    command = ["sudo", "-n", *RESTART_DASHBOARD]
    if delay:
        # Fire after the response has been written: --no-block only stops
        # systemctl waiting; systemd can still stop this process mid-response.
        def fire():
            global _restart_failure
            code, out = checks._run(command, timeout=30)
            if code != 0:
                _restart_failure = out or f"systemctl exited {code}"

        threading.Timer(delay, fire).start()
        return f"Restarting {DASHBOARD_UNIT}…"

    code, out = checks._run(command, timeout=30)
    if code != 0:
        raise RuntimeError(
            f"{out or 'permission denied'} — run manually: "
            f"sudo systemctl restart {DASHBOARD_UNIT}"
        )
    return (
        f"Restarting {DASHBOARD_UNIT} — wait a few seconds and reload this page. "
        "The version in the header should change."
    )


def _ship_note(store: Store, rid: int, config) -> str:
    """"Etsy told Mon Sep 28" for an order waiting on its ship day, else ""."""
    from .shipdate import IMMEDIATELY, ship_day, waits_for_carrier

    label = store.get_label(rid)
    if (label is None or label["is_test"]
            or config.labels.mark_shipped == IMMEDIATELY):
        return ""
    receipt = store.get_receipt_json(rid)
    day = ship_day(label["created_at"], receipt, config.labels.mark_shipped)
    if waits_for_carrier(config.labels.mark_shipped, receipt):
        return f"Etsy told on USPS scan, by {day:%a %b %-d}"
    return f"Etsy told {day:%a %b %-d}"


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

    if name == "restart-dashboard":
        return _restart_dashboard()

    if name == "retry":
        rid = form.get("receipt_id", "").strip()
        if not rid.isdigit():
            raise ValueError("bad receipt id")
        cmd = base + ["retry", rid]
    elif name in ("print-now", "skip"):
        rid = form.get("receipt_id", "").strip()
        if not rid.isdigit():
            raise ValueError("bad receipt id")
        cmd = base + [name, rid]
    elif name == "mark-shipped":
        if form.get("all"):
            cmd = base + ["mark-shipped", "--all"]
        else:
            rid = form.get("receipt_id", "").strip()
            if not rid.isdigit():
                raise ValueError("bad receipt id")
            cmd = base + ["mark-shipped", rid]
    elif name in ("poll", "test-slip", "test-label", "test-notify"):
        cmd = base + [name]
    else:
        raise ValueError(f"unknown action {name!r}")

    code, out = checks._run(cmd, timeout=120)
    if code != 0:
        raise RuntimeError(out[:500] or f"exited {code}")
    return out or "(no output)"
