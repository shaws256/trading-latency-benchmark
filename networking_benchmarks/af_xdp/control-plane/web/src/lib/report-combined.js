// report-combined.js - ONE self-contained HTML report covering every mode that
// has been measured, with per-cell mode metadata.
//
// Runs in parallel with report.js buildReportHTML(), which still produces a
// single-mode document and is unchanged. This module composes the same shape
// per mode and adds a mode-annotated overview.
//
// Two properties are deliberately preserved:
//   - Each mode keeps its OWN heatmap, so colour stays comparable within a mode.
//     A grid mixing a kernel p50 with an mcast one-way would make colour a
//     function of mode rather than of network position.
//   - The overview grid, which does mix modes to show the freshest value per
//     cell, says so, and badges every cell with the mode that produced it.

import { fmtLat, latencyColor, esc } from './2d/palette.js';

/** Short per-mode badge: K/X for ucast kernel/xdp, C/I/K for mcast fwd modes. */
export const MODE_BADGE = {
  'ucast/kernel': 'K',
  'ucast/xdp': 'X',
  'mcast/copy': 'C',
  'mcast/inplace': 'I',
  'mcast/kernel': 'MK',
};

const modeKey = (v) => `${v.kind}/${v.variation}`;
const badgeOf = (key) => MODE_BADGE[key] || key.split('/')[1].slice(0, 2).toUpperCase();
const label = (n) => esc(n.private_ip || n.ec2_name || '#' + n.index);
const relAge = (unix) => {
  if (!unix) return 'unknown age';
  const s = Math.max(0, Math.floor(Date.now() / 1000 - unix));
  if (s < 90) return `${s}s ago`;
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  return `${Math.round(s / 3600)} h ago`;
};

/**
 * Flatten every view into one measurement list plus a per-cell freshest index.
 * A cell can be measured by several modes; the overview shows the newest.
 */
function collate(views) {
  const rows = [];   // one per measured pair, per mode
  const best = new Map(); // "src|dst" -> { unix, key, cell }
  for (const v of views) {
    const key = modeKey(v);
    const { nodes, matrix } = v.fleet;
    for (let i = 0; i < nodes.length; i++) {
      for (let j = 0; j < nodes.length; j++) {
        const c = matrix[i] && matrix[i][j];
        if (!c) continue;
        const src = nodes[i], dst = nodes[j];
        const unix = c.unix || v.unix || 0;
        rows.push({ key, kind: v.kind, variation: v.variation, src, dst, cell: c, unix });
        const ck = `${src.private_ip}|${dst.private_ip}`;
        const prev = best.get(ck);
        if (!prev || unix >= prev.unix) best.set(ck, { unix, key, cell: c });
      }
    }
  }
  return { rows, best };
}

/** Mode-annotated overview: freshest value per cell, badged with its mode. */
function overviewGrid(nodes, best) {
  const vals = [...best.values()].map((b) => b.cell.p50).filter((v) => v != null);
  const mn = vals.length ? Math.min(...vals) : 0;
  const mx = vals.length ? Math.max(...vals) : 1;
  let h = '<table class="heat" id="overview-table"><tr><th>src \\ dst</th>'
    + nodes.map((n) => `<th data-col-ip="${esc(n.private_ip || '')}">${label(n)}</th>`).join('')
    + '</tr>';
  nodes.forEach((rn) => {
    const rip = esc(rn.private_ip || '');
    h += `<tr data-ip="${rip}"><th data-row-ip="${rip}">${label(rn)}</th>`;
    nodes.forEach((cn) => {
      const cip = esc(cn.private_ip || '');
      const dat = ` data-row-ip="${rip}" data-col-ip="${cip}"`;
      if (rn === cn) { h += `<td class="na"${dat}>\u00b7</td>`; return; }
      const b = best.get(`${rn.private_ip}|${cn.private_ip}`);
      if (!b) { h += `<td class="na"${dat}>\u00b7</td>`; return; }
      const tip = `${fmtLat(b.cell.p50)} \u00b7 ${b.key} \u00b7 ${relAge(b.unix)}`;
      h += `<td${dat} style="background:${latencyColor(b.cell.p50, mn, mx)};color:#0d1117;font-weight:700"`
        + ` title="${esc(tip)}">${fmtLat(b.cell.p50)}`
        + `<span class="mode-badge">${badgeOf(b.key)}</span></td>`;
    });
    h += '</tr>';
  });
  return h + '</table>';
}

/** Per-mode heatmap, so colour remains comparable inside that mode. */
function modeHeatmap(v) {
  const { nodes, matrix } = v.fleet;
  const key = modeKey(v);
  const vals = [];
  matrix.forEach((r) => r && r.forEach((c) => c && c.p50 != null && vals.push(c.p50)));
  const mn = vals.length ? Math.min(...vals) : 0;
  const mx = vals.length ? Math.max(...vals) : 1;
  let h = `<table class="heat" data-mode="${esc(key)}"><tr><th>src \\ dst</th>`
    + nodes.map((n) => `<th data-col-ip="${esc(n.private_ip || '')}">${label(n)}</th>`).join('')
    + '</tr>';
  nodes.forEach((rn, i) => {
    const rip = esc(rn.private_ip || '');
    h += `<tr data-ip="${rip}"><th data-row-ip="${rip}">${label(rn)}</th>`;
    nodes.forEach((cn, j) => {
      const cip = esc(cn.private_ip || '');
      const dat = ` data-row-ip="${rip}" data-col-ip="${cip}"`;
      const c = matrix[i] && matrix[i][j];
      if (!c) { h += `<td class="na"${dat}>\u00b7</td>`; return; }
      h += `<td${dat} style="background:${latencyColor(c.p50, mn, mx)};color:#0d1117;font-weight:700"`
        + ` title="${esc(fmtLat(c.p50) + ' \u00b7 ' + key)}">${fmtLat(c.p50)}</td>`;
    });
    h += '</tr>';
  });
  return h + '</table>';
}

/** Per-mode methodology. With both kinds in one document this cannot be global. */
function methodology(v) {
  const isMcast = v.kind === 'mcast';
  const metric = isMcast
    ? 'Reported value is a <b>ONE-WAY</b> delay: source \u2192 replicator \u2192 destination. It is not a round trip, and is not comparable with the ucast RTT figures.'
    : 'Reported value is a <b>ROUND-TRIP TIME</b> (RTT) through the remote replicator\u2019s echo, at queue depth 1.';
  const detail = isMcast
    ? `<dt>Clock</dt><dd><code>CLOCK_REALTIME</code> on all three nodes \u2014 necessarily, since a one-way delay spans hosts. chrony disciplines each node to the <b>ENA PHC hardware clock</b> (<code>refclock PHC /dev/ptp0</code>); AWS Time Sync is the fallback. Observed RMS offset is tens of nanoseconds.</dd>
       <dt>Stamps</dt><dd><code>ts_ns</code> at the source before TX ring submit, <code>replicator_ns</code> at replicator RX, <code>rx_ns</code> at destination RX. One-way = <code>rx_ns \u2212 ts_ns</code>.</dd>
       <dt>Fwd mode</dt><dd><code>${esc(v.variation)}</code>. <code>XDP_TX</code> (kernel) is a single-destination passthrough, not a fan-out.</dd>`
    : `<dt>Clock</dt><dd>A single <code>CLOCK_REALTIME</code> domain on one host, so <b>no inter-node clock sync is required</b> and none of its error enters the result. No TSC and no PHC are used for RTT.</dd>
       <dt>Stamps</dt><dd>TX <code>CLOCK_REALTIME</code> immediately before the send; RX a kernel software timestamp recorded in the NAPI receive path, before the socket queue.</dd>
       <dt>Variation</dt><dd><code>${esc(v.variation)}</code>. <code>--xdp-rx</code> is instrumented kernel RX, NOT a bypass receive.</dd>`;
  return `<div class="metric-kind">${metric}</div>
  <details class="method"><summary>How this was measured</summary><dl>${detail}
    <dt>Statistic</dt><dd>Service-time RTT excludes coordinated omission; warmup datagrams are discarded and percentiles derive only from datagrams that arrived. A run over the loss ceiling is rejected rather than published.</dd>
  </dl></details>`;
}

/** Combined latency table: every mode, one table, with a mode column. */
function latencyTable(rows) {
  const head = '<tr><th>mode</th><th>src IP</th><th>src role</th><th>dst IP</th><th>dst role</th>'
    + '<th>dst AZ</th><th>src PG</th><th>dst PG</th><th>p50</th><th>p90</th><th>p99</th>'
    + '<th>p99.9</th><th>max</th><th>loss</th><th>age</th></tr>';
  const pg = (n) => esc(n.cpg_name && n.cpg_name !== 'unknown' ? n.cpg_name : '\u2014');
  const body = rows
    .slice()
    .sort((a, b) => a.key.localeCompare(b.key)
      || (a.src.private_ip || '').localeCompare(b.src.private_ip || ''))
    .map((r) => {
      const c = r.cell;
      return `<tr data-src="${esc(r.src.private_ip || '')}" data-dst="${esc(r.dst.private_ip || '')}"`
        + ` data-mode="${esc(r.key)}">`
        + `<td><span class="mode-badge">${badgeOf(r.key)}</span> ${esc(r.key)}</td>`
        + `<td>${label(r.src)}</td><td>${esc(r.src.role || '\u2014')}</td>`
        + `<td>${label(r.dst)}</td><td>${esc(r.dst.role || '\u2014')}</td>`
        + `<td>${esc(r.dst.az || '\u2014')}</td><td>${pg(r.src)}</td><td>${pg(r.dst)}</td>`
        + `<td>${fmtLat(c.p50)}</td><td>${fmtLat(c.p90)}</td><td>${fmtLat(c.p99)}</td>`
        + `<td>${fmtLat(c.p999)}</td><td>${fmtLat(c.max)}</td><td>${esc(c.loss ?? 0)}%</td>`
        + `<td>${esc(relAge(r.unix))}</td></tr>`;
    })
    .join('');
  return `<table class="sortable" id="lat-table">${head}${body}</table>`;
}

function inventory(nodes) {
  let t = '<table class="inv sortable" id="inv-table"><tr><th>#</th><th>Private IP</th>'
    + '<th>Public IP</th><th>Role</th><th>VPC ID</th><th>AZ</th><th>PG</th><th>Type</th></tr>';
  nodes.forEach((n, i) => {
    const u = (v) => esc(v && v !== 'unknown' ? v : '\u2014');
    t += `<tr data-ip="${esc(n.private_ip || '')}"><td>${i}</td><td>${label(n)}</td>`
      + `<td>${esc(n.public_ip || '\u2014')}</td>`
      + `<td class="role-${esc(n.role || '')}">${esc(n.role || '\u2014')}</td>`
      + `<td>${u(n.vpc_id)}</td><td>${esc(n.az || '\u2014')}</td><td>${u(n.cpg_name)}</td>`
      + `<td>${esc(n.type || '\u2014')}</td></tr>`;
  });
  return t + '</table>';
}

function ages(rows) {
  if (!rows.length) return '';
  const us = rows.map((r) => r.unix).filter(Boolean);
  if (!us.length) return '';
  const newest = Math.max(...us), oldest = Math.min(...us);
  const stale = rows.filter((r) => r.unix && newest - r.unix > 300).length;
  return '<div class="coverage"><h3 style="margin:0 0 4px;font-size:13px;color:#58a6ff">Measurement ages</h3>'
    + `newest ${esc(relAge(newest))} \u00b7 oldest ${esc(relAge(oldest))}`
    + (stale ? ` \u00b7 <b>${stale}</b> measurement(s) more than 5 min older than the newest` : '')
    + ' \u2014 a scoped-run grid is a mosaic of measurement ages, not one snapshot.</div>';
}

/**
 * Build the combined report.
 * @param {Array<{kind:string,variation:string,unix?:number,fleet:object}>} views
 */
export function buildCombinedReportHTML(views) {
  const vs = (views || []).filter((v) => v && v.fleet && (v.fleet.nodes || []).length);
  const gen = new Date().toISOString();
  if (!vs.length) {
    return `<!doctype html><html><head><meta charset="utf-8"><title>Latency Report</title></head>`
      + `<body style="background:#0d1117;color:#e6edf3;font-family:system-ui;padding:24px">`
      + `<h1>Latency Report</h1><p>No measurements yet \u2014 run a campaign first.</p></body></html>`;
  }
  const nodes = vs[0].fleet.nodes;
  const region = vs[0].fleet.region || '?';
  const { rows, best } = collate(vs);
  const modeList = vs.map((v) => modeKey(v));

  const sections = vs.map((v) => `
  <h2>${esc(modeKey(v))} \u2014 ${v.kind === 'mcast' ? 'one-way' : 'round-trip (RTT)'}</h2>
  ${methodology(v)}
  ${modeHeatmap(v)}`).join('\n');

  return `<!doctype html><html><head><meta charset="utf-8">
  <title>Latency Report \u2014 all modes</title>
  <style>
  body{background:#0d1117;color:#e6edf3;font-family:system-ui,-apple-system,sans-serif;padding:22px;margin:0}
  h1{font-size:19px;margin:0 0 4px}h2{font-size:15px;margin:22px 0 6px;color:#e6edf3}
  h3{font-size:13px}
  .meta{color:#8b949e;font-size:12px;margin-bottom:10px}
  table{border-collapse:collapse;margin:8px 0 4px;font-size:12px}
  th,td{border:1px solid #30363d;padding:3px 7px;text-align:right;white-space:nowrap}
  th{background:#161b22;color:#8b949e;font-weight:600;cursor:pointer;user-select:none}
  .inv td,.inv th{text-align:left}
  .heat td{font-family:'SF Mono',monospace;color:#0d1117;font-weight:700}
  .heat th{font-family:'SF Mono',monospace}
  td.na{background:#161b22;color:#484f58;font-weight:400}
  #lat-table td{color:#e6edf3}
  #lat-table td:first-child,.inv td{text-align:left}
  .mode-badge{display:inline-block;margin-left:4px;padding:0 3px;border-radius:3px;
    background:#0d1117;color:#79c0ff;font:9px 'SF Mono',monospace;font-weight:700;vertical-align:top}
  #lat-table .mode-badge{background:#161b22}
  .coverage{font-size:12px;margin:10px 0;padding:9px 11px;background:#1c1810;color:#e3b341;
    border:1px solid #30363d;border-radius:6px}
  .warn{font-size:12px;margin:6px 0 2px;padding:8px 10px;background:#161b22;color:#adbac7;
    border:1px solid #30363d;border-left:3px solid #d29922;border-radius:6px}
  .method{font-size:12px;margin:6px 0 12px;padding:9px 11px;background:#161b22;
    border:1px solid #30363d;border-left:3px solid #58a6ff;border-radius:6px;color:#adbac7;line-height:1.6}
  .method summary{font-size:13px;color:#58a6ff;cursor:pointer;font-weight:600;list-style:none}
  .method summary::-webkit-details-marker{display:none}
  .method summary::before{content:'\\u25b6';display:inline-block;margin-right:6px;font-size:10px}
  .method[open] summary::before{transform:rotate(90deg)}
  .method dt{color:#e6edf3;font-weight:600;margin-top:6px}.method dd{margin:0 0 0 14px}
  .method code{background:#0d1117;padding:1px 4px;border-radius:3px;color:#79c0ff}
  .metric-kind{font-size:13px;color:#e6edf3;margin:2px 0 6px}.metric-kind b{color:#f0883e}
  .selbar{font-size:12px;color:#8b949e;margin:10px 0 2px}
  .selbar button{background:#21262d;color:#c9d1d9;border:1px solid #30363d;border-radius:5px;
    padding:1px 7px;font-size:11px;cursor:pointer;margin-left:6px}
  .inv tr.sel{background:#1f2937;outline:2px solid #d29922;outline-offset:-2px}
  .heat td.sel-row,.heat td.sel-col{outline:2px solid #d29922;outline-offset:-2px}
  .heat th.sel-row,.heat th.sel-col{background:#243b53;color:#e6edf3}
  #lat-table tr.sel-src{background:#132a3f}#lat-table tr.sel-dst{background:#12301c}
  #lat-table tr.sel-both{background:#3a2d10}
  </style></head><body>
  <h1>Latency Report \u2014 all modes</h1>
  <div class="meta">Region: ${esc(region)} \u00b7 Nodes: ${nodes.length} \u00b7 Modes: ${esc(modeList.join(', '))} \u00b7 Measurements: ${rows.length} \u00b7 Generated: ${esc(gen)}</div>
  ${ages(rows)}

  <h2>Overview \u2014 freshest measurement per pair, badged by mode</h2>
  <div class="warn">Colour here is <b>not comparable across modes</b>: a ucast RTT and an mcast
  one-way measure different quantities, so a cell's colour partly reflects its mode rather than
  its network position. Compare within a mode using the per-mode heatmaps below; this grid answers
  "what is the latest figure for this pair, and where did it come from".</div>
  ${overviewGrid(nodes, best)}

  <div class="selbar"><span id="selinfo">Click an IP anywhere to highlight that instance everywhere.</span><button id="selclear">Clear</button></div>
  <h2>Fleet inventory</h2>
  ${inventory(nodes)}
  ${sections}

  <h2>All measured latencies \u2014 every mode</h2>
  ${latencyTable(rows)}

  <script>
  (function () {
    // Sorting: unit-aware. Backslashes are doubled because this lives inside a
    // template literal, where a lone \\d would be eaten by escape processing.
    function sortKey(s) {
      const u = s.match(/^([\\d.]+)\\s*(ms|s|\u00b5s|\u03bcs)$/);
      if (u) { const v = parseFloat(u[1]); return u[2] === 's' ? v*1e6 : u[2] === 'ms' ? v*1e3 : v; }
      const p = s.match(/^([\\d.]+)%$/); if (p) return parseFloat(p[1]);
      if (/^\\d+(\\.\\d+)?$/.test(s)) return parseFloat(s);
      return NaN;
    }
    document.querySelectorAll('table.sortable').forEach((table) => {
      const dir = {};
      table.querySelectorAll('tr:first-child th').forEach((th, col) => {
        th.addEventListener('click', () => {
          const rows = [...table.querySelectorAll('tr')].slice(1);
          const asc = !(dir[col] = !dir[col]);
          rows.sort((a, b) => {
            const x = (a.cells[col] || {}).textContent?.trim() ?? '';
            const y = (b.cells[col] || {}).textContent?.trim() ?? '';
            const nx = sortKey(x), ny = sortKey(y);
            const c = (!isNaN(nx) && !isNaN(ny)) ? nx - ny
              : x.localeCompare(y, undefined, { numeric: true });
            return asc ? c : -c;
          });
          rows.forEach((r) => table.appendChild(r));
        });
      });
    });

    // Cross-table selection by instance IP, spanning every mode section.
    const sel = new Set();
    function paint() {
      document.querySelectorAll('#inv-table tr[data-ip]').forEach((tr) =>
        tr.classList.toggle('sel', sel.has(tr.dataset.ip)));
      document.querySelectorAll('table.heat td, table.heat th').forEach((el) => {
        el.classList.toggle('sel-row', !!el.dataset.rowIp && sel.has(el.dataset.rowIp));
        el.classList.toggle('sel-col', !!el.dataset.colIp && sel.has(el.dataset.colIp));
      });
      document.querySelectorAll('#lat-table tr[data-src]').forEach((tr) => {
        const s = sel.has(tr.dataset.src), d = sel.has(tr.dataset.dst);
        tr.classList.toggle('sel-both', s && d);
        tr.classList.toggle('sel-src', s && !d);
        tr.classList.toggle('sel-dst', d && !s);
      });
      const info = document.getElementById('selinfo');
      info.textContent = sel.size
        ? sel.size + ' instance' + (sel.size > 1 ? 's' : '') + ' selected: ' + [...sel].join(', ')
        : 'Click an IP anywhere to highlight that instance everywhere.';
    }
    const toggle = (ip) => { if (!ip) return; sel.has(ip) ? sel.delete(ip) : sel.add(ip); paint(); };
    document.querySelectorAll('#inv-table tr[data-ip]').forEach((tr) => {
      tr.style.cursor = 'pointer';
      tr.addEventListener('click', () => toggle(tr.dataset.ip));
    });
    document.querySelectorAll('table.heat th[data-row-ip], table.heat th[data-col-ip]').forEach((th) => {
      th.style.cursor = 'pointer';
      th.addEventListener('click', () => toggle(th.dataset.rowIp || th.dataset.colIp));
    });
    document.querySelectorAll('#lat-table tr[data-src]').forEach((tr) => {
      tr.style.cursor = 'pointer';
      tr.addEventListener('click', (ev) => {
        // Column order: 0 mode, 1 src IP, 2 src role, 3 dst IP, ...
        toggle(ev.target.cellIndex === 3 ? tr.dataset.dst : tr.dataset.src);
      });
    });
    document.getElementById('selclear').addEventListener('click', () => { sel.clear(); paint(); });
    paint();
  })();
  </script></body></html>`;
}
