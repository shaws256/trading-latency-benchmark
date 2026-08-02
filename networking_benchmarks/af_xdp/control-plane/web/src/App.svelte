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
  let selected = '';

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
    handle = (mode === '2d' ? mountTopology2D : mountTopology3D)(container, fleet);
  }

  function statsFromFleet(f) {
    if (!f) return { nodes: 0, online: 0, edges: 0 };
    let edges = 0;
    (f.matrix || []).forEach((row) => row && row.forEach((c) => { if (c) edges++; }));
    const online = f.nodes.filter((n) => n.online !== false).length;
    return { nodes: f.nodes.length, online, edges };
  }

  async function load(url, label) {
    loading = true; error = '';
    try {
      const res = await fetch(url);
      if (!res.ok) throw new Error(`GET ${url} -> ${res.status}`);
      fleet = await res.json();
      loading = false; remount();
      panel && panel.setStats(statsFromFleet(fleet));
      panel && panel.setStatus(`loaded ${label || url}`);
    } catch (e) { loading = false; error = e.message || String(e); panel && panel.setStatus('load failed: ' + error); }
  }

  // Rebuild the viz from live state for the current kind+variation (debounced).
  function liveRerender() {
    if (!live) return;
    const combos = live.combos();
    panel && panel.setCombos(combos, { kind, variation });
    fleet = live.toFleet(kind, variation);
    loading = false; error = ''; remount();
    const s = live.stats();
    panel && panel.setStats({ ...s, updated: new Date().toLocaleTimeString() });
  }
  function scheduleRerender() {
    if (rerenderTimer) return;
    rerenderTimer = setTimeout(() => { rerenderTimer = null; liveRerender(); }, 500);
  }

  function startLive() {
    if (live) return;
    liveOn = true; panel && panel.setLive(true); panel && panel.setStatus('connecting to control plane…');
    live = createLive({
      onUpdate: scheduleRerender,
      onJob: (d) => {
        const t = `${d.status || ''} ${d.kind || ''} ${d.mode || d.variation || ''}` +
          (d.total ? ` ${d.done ?? 0}/${d.total}` : '') + (d.err || d.reason ? ` — ${d.err || d.reason}` : '');
        panel && panel.setStatus(t.trim());
      },
    });
    liveRerender();
  }
  function stopLive() { liveOn = false; panel && panel.setLive(false); if (live) { live.close(); live = null; } panel && panel.setStatus('live stopped'); }

  async function doRun(body) {
    if (!liveOn) startLive();               // launching implies you want to watch it
    panel && panel.setStatus(`starting ${body.kind}/${body.variation || (body.modes || []).join('+')}…`);
    try { const r = await runCampaign(body); panel && panel.setStatus(`started ${r.kind}/${r.variation || (r.modes || []).join('+')}`); }
    catch (e) { panel && panel.setStatus('run failed: ' + e); }
  }

  onMount(async () => {
    panel = mountControls(controlsHost, {
      initialMode: mode,
      onSetMode: (m) => { if (m !== mode) { mode = m; remount(); } },
      onToggleLive: (on) => { on ? startLive() : stopLive(); },
      onSelectView: ({ kind: k, variation: v }) => { kind = k; variation = v; liveRerender(); },
      onRun: doRun,
    });

    const params = new URLSearchParams(location.search);
    if (params.get('live') === '1') startLive();
    else await load(params.get('data') || 'fleet.json', params.get('data') ? params.get('data') : 'bundled sample');

    try { const res = await fetch('/api/results'); if (res.ok) runs = await res.json(); } catch { /* no dev API */ }
  });
  onDestroy(() => { if (handle) handle.dispose(); stopLive(); if (panel) panel.dispose(); });

  function onPick(e) { const p = e.target.value; if (p) load(`/api/fleet?path=${encodeURIComponent(p)}`, p); }
</script>

<div class="controls-host" bind:this={controlsHost}></div>
<div class="root" bind:this={container}></div>

{#if runs.length && !liveOn}
  <div class="toolbar">
    <select class="browse" bind:value={selected} on:change={onPick} title="Load a saved run's fleet.json">
      <option value="" disabled selected>Browse results… ({runs.length})</option>
      {#each runs as r}<option value={r.path}>{r.path}</option>{/each}
    </select>
  </div>
{/if}
{#if loading}<div class="msg">Loading topology…</div>{/if}
{#if error}<div class="msg err">Failed to load: {error}</div>{/if}

<style>
  .root { position: fixed; inset: 0; }
  .controls-host { position: fixed; inset: 0; pointer-events: none; z-index: 3000; }
  .controls-host :global(.cp-panel) { pointer-events: auto; }
  .toolbar { position: fixed; top: 16px; left: 50%; transform: translateX(-50%); z-index: 1500;
    background: rgba(22,27,34,.9); border: 1px solid #30363d; border-radius: 8px; padding: 4px; }
  .toolbar .browse { background: transparent; color: #8b949e; border: none; border-radius: 6px;
    padding: 5px 10px; max-width: 260px; font: 600 13px sans-serif; cursor: pointer; outline: none; }
  .toolbar .browse option { background: #161b22; color: #e6edf3; }
  .msg { position: fixed; bottom: 16px; right: 16px; z-index: 2000; color: #e6edf3;
    background: rgba(22,27,34,.92); border: 1px solid #30363d; border-radius: 6px; padding: 8px 12px;
    font: 13px -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }
  .msg.err { color: #f85149; border-color: #f85149; }
</style>
