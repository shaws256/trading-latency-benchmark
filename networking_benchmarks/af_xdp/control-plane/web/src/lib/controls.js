// controls.js — a shared, framework-agnostic control panel mounted ONCE as a
// persistent overlay above whichever topology view (2D or 3D) is active. It
// survives the viz dispose/remount that live updates trigger, so it's the
// single source of UI for: view mode, real-time monitoring on/off, which
// kind/variation to render, and launching test campaigns. It is pure DOM +
// callbacks — the host (App) wires the callbacks to the live model + backend.

const STYLE_ID = 'cp-controls-styles';
const CSS = `
.cp-panel{position:fixed;top:14px;left:14px;z-index:9999;width:360px;
  min-width:360px;max-width:720px;min-height:40px;max-height:90vh;
  background:#161b22;border:1px solid #30363d;border-radius:10px;
  padding:0;color:#e6edf3;
  font:13px -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  box-shadow:0 8px 24px rgba(0,0,0,.5);resize:horizontal;overflow:hidden}
.cp-panel-title{display:flex;align-items:center;gap:6px;padding:8px 12px;cursor:move;
  user-select:none;border-bottom:1px solid #30363d;background:#0d1117}
.cp-panel-title:hover{background:#1c2128}
.cp-panel-caret{font-size:14px;color:#8b949e;margin-right:6px;transition:transform .15s;cursor:pointer;
  padding:2px 4px;border-radius:4px}
.cp-panel-caret:hover{background:rgba(88,166,255,.15);color:#58a6ff}
.cp-panel-caret.collapsed{transform:rotate(-90deg)}
.cp-panel-fold-btn{font-size:9px;color:#8b949e;cursor:pointer;user-select:none;padding:0 2px}
.cp-panel-fold-btn:hover{color:#e6edf3}
.cp-panel-body{padding:6px 12px 10px;background:#161b22}
.cp-panel .row{display:flex;align-items:center;gap:6px;margin:6px 0;flex-wrap:wrap}
.cp-panel .row.center{justify-content:center}
.cp-panel .grow{flex:1}
.cp-seg{display:flex;border:1px solid #30363d;border-radius:7px;overflow:hidden}
.cp-seg button{background:transparent;color:#8b949e;border:none;padding:5px 12px;cursor:pointer;font:600 12px inherit}
.cp-seg button.on{background:rgba(88,166,255,.18);color:#58a6ff}
.cp-live{background:transparent;color:#8b949e;border:1px solid #30363d;border-radius:7px;
  padding:5px 12px;cursor:pointer;font:600 12px inherit}
.cp-live.on{background:rgba(248,81,73,.16);color:#f85149;border-color:#da3633}
.cp-foldall{margin-left:auto;background:transparent;color:#8b949e;border:1px solid #30363d;
  border-radius:7px;padding:5px 10px;cursor:pointer;font:600 13px inherit;line-height:1}
.cp-foldall:hover{color:#e6edf3;border-color:#8b949e;background:rgba(88,166,255,.12)}
.cp-lbl{color:#6e7681;font:600 11px inherit;text-transform:uppercase;letter-spacing:.4px;flex-shrink:0}
.cp-section{color:#e6edf3;font:700 12px inherit;text-transform:uppercase;letter-spacing:.5px}
.cp-btn-group{display:flex;gap:4px;flex-wrap:nowrap;margin-left:4px}
.cp-stats{color:#8b949e;font:12px inherit;font-variant-numeric:tabular-nums}
.cp-stats b{color:#e6edf3}
.cp-sel{background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:4px 6px;font:12px inherit;flex:1}
.cp-btn{background:#21262d;color:#adbac7;border:1px solid #30363d;border-radius:6px;
  padding:4px 8px;cursor:pointer;font:600 12px inherit;transition:background .15s,border-color .15s}
.cp-btn:hover{background:#30363d;color:#fff}
.cp-btn:active,.cp-btn.running{background:rgba(240,136,62,.22);color:#f0883e;border-color:#f0883e}
.cp-num{width:58px;background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:3px 5px;font:12px inherit}
.cp-dim{color:#6e7681;font:11px inherit;margin-left:2px}
.cp-status{margin-top:6px;padding-top:8px;border-top:1px solid #21262d;color:#58a6ff;
  font:12px inherit;font-variant-numeric:tabular-nums;min-height:16px;word-break:break-word}
.cp-hr{height:1px;background:#21262d;margin:4px -12px}
`;

import { enhancePanel, foldAllPanels, resetAllPanels } from './2d/panels.js';
import { esc } from './2d/palette.js';

export function mountControls(host, opts = {}) {
  const { onSetMode, onToggleLive, onSelectView, onRun, onPickResult } = opts;
  if (!document.getElementById(STYLE_ID)) {
    const s = document.createElement('style'); s.id = STYLE_ID; s.textContent = CSS; document.head.appendChild(s);
  }
  const el = document.createElement('div');
  el.className = 'cp-panel';
  el.innerHTML = `
    <h3 class="cp-panel-title">
      <div class="cp-seg" data-seg>
        <button data-mode="2d">2D</button><button data-mode="3d">3D</button>
      </div>
      <button class="cp-live" data-live>Live</button>
      <button class="cp-foldall" data-foldall title="Fold / unfold all panels">\u29C9</button>
    </h3>
    <div class="cp-panel-body" data-body>
      <div class="row"><span class="cp-stats" data-stats>—</span></div>
      <div class="cp-hr"></div>
      <div class="row"><span class="cp-lbl">Show</span>
        <select class="cp-sel" data-view><option value="">(connect Live or load a fleet.json)</option></select>
      </div>
      <div class="cp-hr"></div>
      <div class="row center"><span class="cp-section">Run Tests</span></div>
      <div class="row"><span class="cp-lbl">ucast</span></div>
      <div class="row">
        <span class="cp-btn-group">
          <button class="cp-btn" data-run-ucast="kernel" title="Round-trip through kernel sendto (tuned busy-poll baseline)">kernel</button>
          <button class="cp-btn" data-run-ucast="xdp-tx" title="AF_XDP zero-copy TX + kernel RX">xdp-tx</button>
          <button class="cp-btn" data-run-ucast="xdp-rx" title="Kernel TX + XDP-stamped RX (instrumented)">xdp-rx</button>
          <button class="cp-btn" data-run-ucast="xdp-txrx" title="AF_XDP TX + XDP-stamped RX">txrx</button>
          <button class="cp-btn" data-run-ucast="all" title="Run all 4 ucast variations sequentially">all</button>
        </span>
      </div>
      <div class="row">
        <span class="cp-lbl">mcast</span>
      </div>
      <div class="row">
        <span class="cp-btn-group">
          <button class="cp-btn" data-run-mcast="copy" title="Replicator copies frame to new TX buffer per destination">copy</button>
          <button class="cp-btn" data-run-mcast="inplace" title="Replicator patches RX frame headers in-place (zero-copy last dest)">inplace</button>
          <button class="cp-btn" data-run-mcast="kernel" title="XDP_TX forward in the kernel (single destination only)">kernel</button>
          <button class="cp-btn" data-run-mcast="all" title="Run all 3 mcast modes sequentially">all</button>
        </span>
      </div>
      <div class="cp-hr"></div>
      <div class="row">
        <span class="cp-lbl">Packets</span>
        <input class="cp-num" data-count value="5000" title="Number of measurement packets per pair">
      </div>
      <div class="row">
        <span class="cp-lbl">Rate</span>
        <input class="cp-num" data-rate value="20000" title="Ucast send rate">
        <span class="cp-dim">pps</span>
      </div>
      <div class="row">
        <span class="cp-lbl">Interval</span>
        <input class="cp-num" data-interval value="100" title="Mcast inter-packet interval">
        <span class="cp-dim">µs</span>
      </div>
      <div class="cp-status" data-status></div>
    </div>
  `;
  host.appendChild(el);

  // Use the shared enhancePanel for drag/fold/proportional-resize.
  const ctx = { disposers: [] };
  enhancePanel(ctx, el, false);


  const $ = (sel) => el.querySelector(sel);
  const segBtns = [...el.querySelectorAll('[data-mode]')];
  const liveBtn = $('[data-live]');
  const foldAllBtn = $('[data-foldall]');
  // Toggle: 1st click folds EVERY panel (including this control panel); 2nd click
  // restores them ALL to their default position/size/expanded state, regardless
  // of whatever the user changed in between.
  let allFolded = false;
  foldAllBtn.addEventListener('click', () => {
    if (allFolded) { resetAllPanels(); allFolded = false; foldAllBtn.textContent = '\u29C9'; }
    else { foldAllPanels(true); allFolded = true; foldAllBtn.textContent = '\u29C7'; }
  });
  const viewSel = $('[data-view]');
  const statsEl = $('[data-stats]');
  const statusEl = $('[data-status]');
  const num = (s) => Math.max(1, parseInt($(s).value, 10) || 0);

  let mode = opts.initialMode || '2d';
  let liveOn = !!opts.initialLive;
  const paintMode = () => segBtns.forEach((b) => b.classList.toggle('on', b.dataset.mode === mode));
  const paintLive = () => { liveBtn.classList.toggle('on', liveOn); liveBtn.textContent = liveOn ? '\u25CF Live' : 'Live'; };
  paintMode(); paintLive();

  segBtns.forEach((b) => b.addEventListener('click', () => { mode = b.dataset.mode; paintMode(); onSetMode && onSetMode(mode); }));
  liveBtn.addEventListener('click', () => { liveOn = !liveOn; paintLive(); onToggleLive && onToggleLive(liveOn); });
  viewSel.addEventListener('change', () => {
    // A browse-result option carries data-run (a results/ subdir path); a live
    // combo option carries a "kind|variation" value. The Show dropdown hosts both.
    const opt = viewSel.selectedOptions[0];
    if (opt && opt.dataset.run !== undefined) { onPickResult && onPickResult(opt.dataset.run); return; }
    const v = viewSel.value; if (!v) return;
    const [kind, variation] = v.split('|');
    onSelectView && onSelectView({ kind, variation });
  });
  // Track the currently running button so we can clear it on job completion.
  let activeRunBtn = null;
  const startRun = (btn, payload) => {
    if (activeRunBtn === btn) {
      // Second click = cancel (visually; the backend doesn't support cancel yet).
      btn.classList.remove('running');
      activeRunBtn = null;
      return;
    }
    if (activeRunBtn) activeRunBtn.classList.remove('running');
    btn.classList.add('running');
    activeRunBtn = btn;
    onRun && onRun(payload);
  };

  el.querySelectorAll('[data-run-ucast]').forEach((b) => b.addEventListener('click', () => {
    const v = b.dataset.runUcast;
    if (v === 'all') {
      // Run all 4 variations — the backend's one-at-a-time constraint means they
      // queue; the App's doRun fires them sequentially via the onRun callback.
      startRun(b, { kind: 'ucast', variation: 'all', count: num('[data-count]'), rate: num('[data-rate]'), warmup: 1000 });
    } else {
      startRun(b, { kind: 'ucast', variation: v, count: num('[data-count]'), rate: num('[data-rate]'), warmup: 1000 });
    }
  }));
  el.querySelectorAll('[data-run-mcast]').forEach((b) => b.addEventListener('click', () => {
    const mcastMode = b.dataset.runMcast;
    const modes = mcastMode === 'all' ? ['copy', 'inplace', 'kernel'] : [mcastMode];
    startRun(b, { kind: 'mcast', modes, count: num('[data-count]'), interval_us: num('[data-interval]'), timeout_sec: 25 });
  }));

  return {
    setMode(m) { mode = m; paintMode(); },
    setLive(on) { liveOn = on; paintLive(); },
    setStatus(text) {
      statusEl.textContent = text || '';
      // Auto-clear the running button ONLY on terminal orchestrator states.
      // "started", "running", "progress" must NOT clear it.
      if (activeRunBtn && /\bstatus.*(done|error|rejected|failed)|^(done|error|rejected|failed)\b/i.test(text)) {
        activeRunBtn.classList.remove('running');
        activeRunBtn = null;
      }
    },
    setStats({ nodes = 0, online = 0, edges = 0, updated } = {}) {
      statsEl.innerHTML = `<b>${online}</b>/${nodes} online &middot; <b>${edges}</b> edges` +
        (updated ? ` &middot; ${updated}` : '');
    },
    // Populate the Show dropdown with saved-run browse results (dev-only API).
    // Used when NOT live; setCombos overrides it with live kind/variation combos.
    setResults(runs) {
      if (!runs || !runs.length) return;
      viewSel.innerHTML = ['<option value="" disabled selected>Browse results\u2026 (' + runs.length + ')</option>']
        .concat(runs.map((r) => '<option data-run="' + esc(r.path) + '" value="run:' + esc(r.path) + '">' + esc(r.path) + '</option>'))
        .join('');
    },
    // combos: [{kind,variation}]; sel: {kind,variation} currently shown
    setCombos(combos, sel) {
      const cur = sel ? `${sel.kind}|${sel.variation}` : viewSel.value;
      if (!combos || !combos.length) { viewSel.innerHTML = '<option value="">(no data yet)</option>'; return; }
      viewSel.innerHTML = combos.map((c) => {
        const v = `${c.kind}|${c.variation}`;
        return `<option value="${v}"${v === cur ? ' selected' : ''}>${c.kind} / ${c.variation}</option>`;
      }).join('');
    },
    dispose() { el.remove(); },
  };
}
