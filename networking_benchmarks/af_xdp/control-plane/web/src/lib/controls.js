// controls.js — a shared, framework-agnostic control panel mounted ONCE as a
// persistent overlay above whichever topology view (2D or 3D) is active. It
// survives the viz dispose/remount that live updates trigger, so it's the
// single source of UI for: view mode, real-time monitoring on/off, which
// kind/variation to render, and launching test campaigns. It is pure DOM +
// callbacks — the host (App) wires the callbacks to the live model + backend.

const STYLE_ID = 'cp-controls-styles';
const CSS = `
.cp-panel{position:fixed;top:14px;left:14px;z-index:3000;width:288px;
  background:rgba(22,27,34,.94);border:1px solid #30363d;border-radius:10px;
  padding:10px 12px;backdrop-filter:blur(8px);color:#e6edf3;
  font:13px -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;box-shadow:0 8px 24px rgba(0,0,0,.35)}
.cp-panel .row{display:flex;align-items:center;gap:6px;margin:6px 0;flex-wrap:wrap}
.cp-panel .grow{flex:1}
.cp-seg{display:flex;border:1px solid #30363d;border-radius:7px;overflow:hidden}
.cp-seg button{background:transparent;color:#8b949e;border:none;padding:5px 12px;cursor:pointer;font:600 12px inherit}
.cp-seg button.on{background:rgba(88,166,255,.18);color:#58a6ff}
.cp-live{background:transparent;color:#8b949e;border:1px solid #30363d;border-radius:7px;
  padding:5px 12px;cursor:pointer;font:600 12px inherit}
.cp-live.on{background:rgba(63,185,80,.16);color:#3fb950;border-color:#238636}
.cp-lbl{color:#6e7681;font:600 11px inherit;text-transform:uppercase;letter-spacing:.4px}
.cp-stats{color:#8b949e;font:12px inherit;font-variant-numeric:tabular-nums}
.cp-stats b{color:#e6edf3}
.cp-sel{background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:4px 6px;font:12px inherit;flex:1}
.cp-btn{background:#21262d;color:#adbac7;border:1px solid #30363d;border-radius:6px;
  padding:4px 8px;cursor:pointer;font:600 12px inherit}
.cp-btn:hover{background:#30363d;color:#fff}
.cp-btn.mcast{border-color:#8957e5;color:#d2a8ff}
.cp-num{width:58px;background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:3px 5px;font:12px inherit}
.cp-status{margin-top:8px;padding-top:8px;border-top:1px solid #21262d;color:#58a6ff;
  font:12px inherit;font-variant-numeric:tabular-nums;min-height:16px;word-break:break-word}
.cp-hr{height:1px;background:#21262d;margin:8px -12px}
`;

export function mountControls(host, opts = {}) {
  const { onSetMode, onToggleLive, onSelectView, onRun } = opts;
  if (!document.getElementById(STYLE_ID)) {
    const s = document.createElement('style'); s.id = STYLE_ID; s.textContent = CSS; document.head.appendChild(s);
  }
  const el = document.createElement('div');
  el.className = 'cp-panel';
  el.innerHTML = `
    <div class="row">
      <div class="cp-seg" data-seg>
        <button data-mode="2d">2D</button><button data-mode="3d">3D</button>
      </div>
      <button class="cp-live" data-live>Live</button>
      <span class="grow"></span>
    </div>
    <div class="row"><span class="cp-stats" data-stats>—</span></div>
    <div class="cp-hr"></div>
    <div class="row"><span class="cp-lbl">Show</span>
      <select class="cp-sel" data-view><option value="">(no data)</option></select>
    </div>
    <div class="row"><span class="cp-lbl">Launch</span></div>
    <div class="row" data-ucast>
      <span class="cp-lbl" style="width:38px">ucast</span>
      <button class="cp-btn" data-run-ucast="kernel">kernel</button>
      <button class="cp-btn" data-run-ucast="xdp-tx">xdp-tx</button>
      <button class="cp-btn" data-run-ucast="xdp-rx">xdp-rx</button>
      <button class="cp-btn" data-run-ucast="xdp-txrx">txrx</button>
    </div>
    <div class="row">
      <span class="cp-lbl" style="width:38px">mcast</span>
      <button class="cp-btn mcast" data-run-mcast>copy+inplace+kernel</button>
    </div>
    <div class="row">
      <span class="cp-lbl">n</span><input class="cp-num" data-count value="5000" title="message count">
      <span class="cp-lbl">rate</span><input class="cp-num" data-rate value="20000" title="ucast rate (msg/s)">
      <span class="cp-lbl">iv&#181;s</span><input class="cp-num" data-interval value="100" title="mcast interval (us)">
    </div>
    <div class="cp-status" data-status></div>
  `;
  host.appendChild(el);

  const $ = (sel) => el.querySelector(sel);
  const segBtns = [...el.querySelectorAll('[data-mode]')];
  const liveBtn = $('[data-live]');
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
    const v = viewSel.value; if (!v) return;
    const [kind, variation] = v.split('|');
    onSelectView && onSelectView({ kind, variation });
  });
  el.querySelectorAll('[data-run-ucast]').forEach((b) => b.addEventListener('click', () => {
    onRun && onRun({ kind: 'ucast', variation: b.dataset.runUcast, count: num('[data-count]'), rate: num('[data-rate]'), warmup: 1000 });
  }));
  $('[data-run-mcast]').addEventListener('click', () => {
    onRun && onRun({ kind: 'mcast', modes: ['copy', 'inplace', 'kernel'], count: num('[data-count]'), interval_us: num('[data-interval]'), timeout_sec: 25 });
  });

  return {
    setMode(m) { mode = m; paintMode(); },
    setLive(on) { liveOn = on; paintLive(); },
    setStatus(text) { statusEl.textContent = text || ''; },
    setStats({ nodes = 0, online = 0, edges = 0, updated } = {}) {
      statsEl.innerHTML = `<b>${online}</b>/${nodes} online &middot; <b>${edges}</b> edges` +
        (updated ? ` &middot; ${updated}` : '');
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
