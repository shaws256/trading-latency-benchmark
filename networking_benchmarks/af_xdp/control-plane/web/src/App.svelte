<script>
  import { onMount, onDestroy } from 'svelte';
  import { mountTopology2D } from './lib/2d/index.js';
  import { mountTopology3D } from './lib/topology3d.js';
  import { createLive, runCampaign } from './lib/live.js';
  import { mountControls } from './lib/controls.js';

  let container;        // viz host (wiped on remount)
  let controlsHost;     // persistent overlay host for the shared panel
  let panel = null;

  let fleet = null;
  let error = '';
  let loading = true;
  let mode = '2d';
  let handle = null;

  let runs = [];

  // ── live state ─────────────────────────────────────────────────────────────
  let live = null;
  let liveOn = false;
  let kind = 'ucast';
  let variation = 'kernel';
  let rerenderTimer = null;

  function remount() {
    if (!fleet || !container) return;
    if (handle) handle.dispose();
    container.innerHTML = '';
    try {
      handle = (mode === '2d' ? mountTopology2D : mountTopology3D)(container, fleet);
    } catch (e) {
      console.error('Viz mount failed:', e);
      container.innerHTML = '<pre style="color:#f85149;padding:20px;font:12px monospace;white-space:pre-wrap;max-height:80vh;overflow:auto">'
        + 'Render error:\n' + (e.stack || e.message || String(e)) + '</pre>';
      handle = null;
    }
  }

  function statsFromFleet(f) {
    if (!f || !Array.isArray(f.nodes)) return { nodes: 0, online: 0, edges: 0 };
    let edges = 0;
    (f.matrix || []).forEach((row) => row && row.forEach((c) => { if (c) edges++; }));
    const online = f.nodes.filter((n) => n.online !== false).length;
    return { nodes: f.nodes.length, online, edges };
  }

  // Minimal shape check so a malformed fleet.json fails with a clear message
  // instead of throwing deep inside a renderer.
  function validateFleet(f) {
    if (!f || typeof f !== 'object') throw new Error('fleet is not an object');
    if (!Array.isArray(f.nodes)) throw new Error('fleet.nodes must be an array');
    if (!Array.isArray(f.matrix)) throw new Error('fleet.matrix must be an array');
    return f;
  }

  async function load(url, label) {
    loading = true; error = '';
    try {
      const res = await fetch(url);
      if (!res.ok) throw new Error(`GET ${url} -> ${res.status}`);
      fleet = validateFleet(await res.json());
      loading = false; remount();
      panel?.setStats(statsFromFleet(fleet));
      panel?.setStatus(`loaded ${label || url}`);
    } catch (e) { loading = false; error = e.message || String(e); panel?.setStatus('load failed: ' + error); }
  }

  // Rebuild the viz from live state for the current kind+variation (debounced).
  function liveRerender() {
    if (!live) return;
    const combos = live.combos();
    panel?.setCombos(combos, { kind, variation });
    fleet = live.toFleet(kind, variation);
    loading = false; error = ''; remount();
    const s = live.stats();
    panel?.setStats({ ...s, updated: new Date().toLocaleTimeString() });
  }
  function scheduleRerender() {
    if (rerenderTimer) return;
    rerenderTimer = setTimeout(() => { rerenderTimer = null; liveRerender(); }, 500);
  }

  function startLive() {
    if (live) return;
    liveOn = true; panel?.setLive(true); panel?.setStatus('connecting to control plane…');
    live = createLive({
      onUpdate: scheduleRerender,
      onJob: (d) => {
        const t = `${d.status || ''} ${d.kind || ''} ${d.mode || d.variation || ''}` +
          (d.total ? ` ${d.done ?? 0}/${d.total}` : '') + (d.err || d.reason ? ` — ${d.err || d.reason}` : '');
        panel?.setStatus(t.trim());
      },
    });
    liveRerender();
  }
  function stopLive() {
    liveOn = false;
    panel?.setLive(false);
    // Deselecting Live cancels any running test and returns its button to default.
    panel?.setStatus('done (live stopped)');
    if (live) { live.close(); live = null; }
    // Re-render with the latest fleet data so the viewport isn't stale, and
    // restore the browse-results list in the Show dropdown (live combos replaced it).
    if (fleet) remount();
    panel?.setResults(runs);
  }

  async function doRun(body) {
    if (!liveOn) startLive();
    panel?.setStatus(`launching ${body.kind}/${body.variation || (body.modes || []).join('+')}…`);
    try {
      if (body.kind === 'ucast' && body.variation === 'all') {
        // Run all 4 ucast variations sequentially.
        for (const v of ['kernel', 'xdp-tx', 'xdp-rx', 'xdp-txrx']) {
          panel?.setStatus(`running ucast/${v}…`);
          await runCampaign({ ...body, variation: v });
        }
        panel?.setStatus('done ucast/all');
      } else {
        const r = await runCampaign(body);
        panel?.setStatus(`running ${r.kind}/${r.variation || (r.modes || []).join('+')}`);
      }
    } catch (e) { panel?.setStatus('run failed: ' + e); }
  }

  onMount(async () => {
    panel = mountControls(controlsHost, {
      initialMode: mode,
      onSetMode: (m) => { if (m !== mode) { mode = m; remount(); } },
      onToggleLive: (on) => { on ? startLive() : stopLive(); },
      onSelectView: ({ kind: k, variation: v }) => { kind = k; variation = v; liveRerender(); },
      onPickResult: (p) => { if (p) load(`/api/fleet?path=${encodeURIComponent(p)}`, p); },
      onRun: doRun,
    });

    const params = new URLSearchParams(location.search);
    if (params.get('live') === '1') startLive();
    else await load(params.get('data') || 'fleet.json', params.get('data') ? params.get('data') : 'bundled sample');

    try { const res = await fetch('/api/results'); if (res.ok) { runs = await res.json(); panel?.setResults(runs); } } catch { /* no dev API */ }
  });
  onDestroy(() => { if (handle) handle.dispose(); stopLive(); if (panel) panel.dispose(); });
</script>

<div class="controls-host" bind:this={controlsHost}></div>
<div class="root" bind:this={container}></div>

{#if loading}<div class="msg">Loading topology…</div>{/if}
{#if error}<div class="msg err">Failed to load: {error}</div>{/if}

<style>
  .root { position: fixed; inset: 0; }
  /* controls-host MUST be highest z-index — above the 3D CSS2DRenderer overlay
     (which is position:absolute with high z-index) and above all canvas elements.
     pointer-events:none lets clicks through to the canvas; .cp-panel is auto. */
  .controls-host { position: fixed; inset: 0; pointer-events: none; z-index: 9999; }
  .controls-host :global(.cp-panel) { pointer-events: auto; }
  .msg { position: fixed; bottom: 16px; right: 16px; z-index: 9999; color: #e6edf3;
    background: rgba(22,27,34,.92); border: 1px solid #30363d; border-radius: 6px; padding: 8px 12px;
    font: 13px -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }
  .msg.err { color: #f85149; border-color: #f85149; }
</style>
