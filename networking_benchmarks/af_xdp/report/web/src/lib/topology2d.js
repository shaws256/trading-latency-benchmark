// topology2d.js — 2D latency topology map (DOM + SVG), faithful port of the
// original topology_map.html generator. Self-contained: injects its own scoped
// styles (under .t2d-root so they never collide with the 3D app.css), builds its
// own DOM inside `container`, consumes the shared fleet.json model, returns
// { dispose() }. No external imports (kept independent from topology3d.js).
//
// Layout: SMACOF MDS where distance ∝ p50; edge color = jitter σ; node size =
// capability, node color = instance family; nested contours AZ⊂VPC⊂Region⊂
// Account (CPG shown as a per-node badge); hover = latency table, click = pin
// 1-hop cluster.

const STYLE_ID = 'topology2d-styles';
const CSS = `
.t2d-root { position: absolute; inset: 0; width: 100%; height: 100%; }
.t2d-root svg.edges { position: absolute; top: 0; left: 0; width: 100%; height: 100%; pointer-events: none; z-index: 1; }
.t2d-root .edge-label { position: absolute; z-index: 40; font-family: 'SF Mono','Fira Code',monospace;
  font-size: 13px; font-weight: 700; background: rgba(13,17,23,0.94); padding: 2px 7px; border-radius: 4px;
  border: 1px solid rgba(255,255,255,0.1); white-space: nowrap; cursor: default;
  transform: translate(-50%,-50%); transition: border-color 0.15s, background 0.15s; }
.t2d-root .edge-label:hover { border-color: rgba(88,166,255,0.5); background: rgba(22,27,34,0.98); }
.t2d-root .node { position: absolute; z-index: 20; border-radius: 50%; display: flex; flex-direction: column;
  align-items: center; justify-content: center; text-align: center; border: 2.5px solid rgba(255,255,255,0.25);
  box-shadow: 0 4px 24px rgba(0,0,0,0.6); cursor: pointer; }
.t2d-root .node.selected { box-shadow: 0 0 0 3px rgba(255,215,0,0.85), 0 0 22px 6px rgba(255,215,0,0.55); z-index: 30; }
.t2d-root .deselect-btn { position: fixed; top: 58px; left: 50%; transform: translateX(-50%); z-index: 1100;
  display: none; background: rgba(240,136,62,0.16); color: #f0883e; border: 1px solid #f0883e;
  border-radius: 6px; padding: 7px 15px; font-size: 13px; font-weight: 600; cursor: pointer; backdrop-filter: blur(8px); }
.t2d-root .deselect-btn:hover { background: rgba(240,136,62,0.3); }
.t2d-root .node .instance-type { font-size: 11px; font-weight: 700; color: #fff; white-space: nowrap; }
.t2d-root .node .ec2-name { font-size: 9px; color: #79c0ff; white-space: nowrap; margin-top: 1px; }
.t2d-root .node .ip { font-size: 9px; color: #b1bac4; font-family: 'SF Mono',monospace; }
.t2d-root .node .ip-public { color: #79c0ff; font-weight: 700; margin-top: 1px; }
.t2d-root .node .ip-private { color: #8b949e; }
.t2d-root .panel-caret { display: inline-block; width: 12px; margin-right: 4px; font-size: 10px; color: #8b949e; }
.t2d-root .stats h3, .t2d-root .vis-legend h3, .t2d-root .instance-legend h3 { cursor: move; user-select: none; }
.t2d-root .node .pg-badge { position: absolute; top: -9px; left: 50%; background: #f0883e; color: #fff;
  font-size: 11px; font-weight: 700; min-width: 22px; height: 21px; padding: 0 9px; border-radius: 11px;
  display: flex; align-items: center; justify-content: center; border: 2px solid #0d1117; white-space: nowrap; letter-spacing: 0.2px; }
.t2d-root .node-tooltip { position: absolute; z-index: 100; background: rgba(22,27,34,0.97); border: 1px solid #30363d;
  border-radius: 8px; padding: 12px 14px; font-size: 11px; pointer-events: none; backdrop-filter: blur(8px);
  box-shadow: 0 8px 32px rgba(0,0,0,0.6); white-space: nowrap; opacity: 0; transition: opacity 0.15s; min-width: 260px; }
.t2d-root .node-tooltip.visible { opacity: 1; }
.t2d-root .node-tooltip h4 { font-size: 12px; color: #58a6ff; margin-bottom: 6px; }
.t2d-root .node-tooltip table { border-collapse: collapse; width: 100%; }
.t2d-root .node-tooltip th { text-align: center; font-size: 10px; color: #8b949e; padding: 2px 6px; border-bottom: 1px solid #21262d; }
.t2d-root .node-tooltip td { text-align: center; font-family: 'SF Mono',monospace; font-size: 11px; padding: 3px 6px; color: #e6edf3; }
.t2d-root .node-tooltip td.peer-name { text-align: left; color: #79c0ff; font-family: inherit; }
.t2d-root .node-tooltip td.highlight { color: #f0883e; font-weight: 600; }
.t2d-root .node-tooltip tr.pg-group td { text-align: left; color: #8b949e; font-weight: 700; font-size: 10px; letter-spacing: 0.3px; padding: 6px 6px 2px; border-bottom: 1px solid #30363d; }
.t2d-root .node-tooltip .direction { font-size: 9px; color: #6e7681; }
.t2d-root .edge-tooltip { position: absolute; z-index: 100; background: rgba(22,27,34,0.97); border: 1px solid #30363d;
  border-radius: 8px; padding: 12px 14px; font-size: 11px; pointer-events: none; backdrop-filter: blur(8px);
  box-shadow: 0 8px 32px rgba(0,0,0,0.6); white-space: nowrap; opacity: 0; transition: opacity 0.15s; min-width: 240px; }
.t2d-root .edge-tooltip.visible { opacity: 1; }
.t2d-root .edge-tooltip h4 { font-size: 12px; color: #58a6ff; margin-bottom: 8px; }
.t2d-root .edge-tooltip .dir-block { margin-bottom: 8px; }
.t2d-root .edge-tooltip .dir-label { font-size: 10px; color: #8b949e; margin-bottom: 3px; }
.t2d-root .edge-tooltip .dir-values { display: grid; grid-template-columns: repeat(6,auto); gap: 2px 10px; }
.t2d-root .edge-tooltip .metric-label { font-size: 9px; color: #6e7681; }
.t2d-root .edge-tooltip .metric-val { font-family: 'SF Mono',monospace; font-size: 12px; color: #e6edf3; }
.t2d-root .edge-tooltip .metric-val.highlight { color: #f0883e; font-weight: 700; }
.t2d-root .edge-tooltip .asymmetry { font-size: 10px; color: #f0883e; margin-top: 4px; padding-top: 4px; border-top: 1px solid #21262d; }
.t2d-root svg.edges line.edge-line { transition: opacity 0.15s, stroke-width 0.15s; }
.t2d-root svg.edges line.edge-line.dimmed { opacity: 0.12 !important; }
.t2d-root svg.edges line.edge-line.highlighted { opacity: 1 !important; filter: drop-shadow(0 0 6px currentColor) brightness(1.3); }
.t2d-root .contour { position: absolute; z-index: 0; border-radius: 24px; border: 1.5px dashed; pointer-events: none; }
.t2d-root .contour .label { position: absolute; top: -10px; left: 16px; font-size: 10px; font-weight: 600; padding: 1px 8px; border-radius: 4px; white-space: nowrap; }
.t2d-root .contour.region { border-color: rgba(88,166,255,0.25); }
.t2d-root .contour.region .label { background: rgba(88,166,255,0.15); color: #58a6ff; }
.t2d-root .contour.az { border-color: rgba(163,113,247,0.25); }
.t2d-root .contour.az .label { background: rgba(163,113,247,0.15); color: #a371f7; }
.t2d-root .contour.vpc { border-color: rgba(57,211,83,0.2); }
.t2d-root .contour.vpc .label { background: rgba(57,211,83,0.12); color: #39d353; }
.t2d-root .contour.cpg { border-color: rgba(240,136,62,0.3); }
.t2d-root .contour.cpg .label { background: rgba(240,136,62,0.15); color: #f0883e; }
.t2d-root .contour.account { border-color: rgba(248,81,73,0.28); border-style: solid; }
.t2d-root .contour.account .label { background: rgba(248,81,73,0.15); color: #f85149; }
.t2d-root svg.edges line.peering-line { stroke: #58a6ff; stroke-width: 2.5; stroke-dasharray: 7 5; opacity: 0.75; }
.t2d-root svg.edges line.peering-hit { stroke: transparent; stroke-width: 18; pointer-events: stroke; cursor: help; }
.t2d-root .peering-label { position: absolute; z-index: 1; transform: translate(-50%,-50%); font-size: 10px; font-weight: 700; color: #58a6ff;
  background: rgba(13,17,23,0.9); border: 1px solid rgba(88,166,255,0.45); border-radius: 4px; padding: 1px 7px; white-space: nowrap; }
.t2d-root .instance-legend { position: fixed; bottom: 20px; left: 20px; z-index: 1000; background: rgba(22,27,34,0.96);
  border: 1px solid #30363d; border-radius: 8px; padding: 16px 18px; font-size: 12px; backdrop-filter: blur(8px); max-width: 360px; }
.t2d-root .instance-legend h3 { font-size: 13px; margin-bottom: 10px; color: #58a6ff; }
.t2d-root .instance-legend .type-row { display: flex; align-items: center; gap: 12px; margin: 6px 0; padding: 5px 0; border-bottom: 1px solid rgba(48,54,61,0.5); }
.t2d-root .instance-legend .type-row:last-child { border-bottom: none; }
.t2d-root .instance-legend .type-dot { border-radius: 50%; flex-shrink: 0; }
.t2d-root .instance-legend .type-info { flex: 1; }
.t2d-root .instance-legend .type-name { font-weight: 600; color: #e6edf3; font-size: 12px; }
.t2d-root .instance-legend .type-specs { font-size: 11px; color: #8b949e; }
.t2d-root .instance-legend a { color: #58a6ff; text-decoration: none; font-size: 11px; border: 1px solid rgba(88,166,255,0.3); border-radius: 3px; padding: 2px 6px; white-space: nowrap; }
.t2d-root .instance-legend a:hover { background: rgba(88,166,255,0.15); }
.t2d-root .vis-legend { position: fixed; top: 20px; right: 20px; z-index: 1000; background: rgba(22,27,34,0.96);
  border: 1px solid #30363d; border-radius: 8px; padding: 16px 18px; font-size: 12px; backdrop-filter: blur(8px); }
.t2d-root .vis-legend h3 { font-size: 13px; margin-bottom: 10px; color: #58a6ff; }
.t2d-root .vis-legend .row { display: flex; align-items: center; gap: 10px; margin: 5px 0; }
.t2d-root .vis-legend .swatch { width: 32px; height: 5px; border-radius: 2px; }
.t2d-root .vis-legend .contour-samples { margin-top: 8px; display: flex; flex-wrap: wrap; gap: 8px; }
.t2d-root .vis-legend .contour-samples span { border-radius: 4px; padding: 2px 8px; font-size: 11px; }
.t2d-root .vis-legend .ux-hint { margin-top: 10px; padding-top: 8px; border-top: 1px solid #30363d; font-size: 10px; color: #8b949e; line-height: 1.6; max-width: 300px; }
.t2d-root .vis-legend .ux-hint b { color: #e6edf3; }
.t2d-root .stats { position: fixed; top: 20px; left: 20px; z-index: 1000; background: rgba(22,27,34,0.96);
  border: 1px solid #30363d; border-radius: 8px; padding: 18px; font-size: 13px; backdrop-filter: blur(8px); min-width: 220px; }
.t2d-root .stats h3 { font-size: 14px; margin-bottom: 10px; color: #58a6ff; }
.t2d-root .stats .stat { display: flex; justify-content: space-between; margin: 3px 0; }
.t2d-root .stats .stat .val { color: #f0883e; font-weight: 600; font-family: 'SF Mono',monospace; font-size: 13px; }
.t2d-root .stats .stress { margin-top: 8px; padding-top: 8px; border-top: 1px solid #30363d; font-size: 11px; color: #8b949e; }
.t2d-root .stats .stress .val { color: #39d353; }
`;

export function mountTopology2D(container, fleet) {
  if (!document.getElementById(STYLE_ID)) {
    const style = document.createElement('style'); style.id = STYLE_ID; style.textContent = CSS; document.head.appendChild(style);
  }
  const disposers = [];
  const root = document.createElement('div'); root.className = 't2d-root'; container.appendChild(root);

  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', 'edges'); svg.classList.add('edges'); root.appendChild(svg);
  const deselectBtn = document.createElement('button'); deselectBtn.className = 'deselect-btn'; deselectBtn.textContent = 'Deselect all'; root.appendChild(deselectBtn);
  const statsEl = document.createElement('div'); statsEl.className = 'stats'; root.appendChild(statsEl);

  const W = container.clientWidth || window.innerWidth;
  const H = container.clientHeight || window.innerHeight;
  const N = fleet.nodes.length;
  const CX = W / 2, CY = H / 2;
  const matrix = fleet.matrix;

  function fmtLat(us) {
    if (us === null || us === undefined || us === '') return '\u2014';
    const v = +us; if (!isFinite(v)) return '\u2014';
    const trim = (x) => (Math.round(x * 100) / 100).toString();
    if (v >= 500000) return trim(v / 1000000) + ' s';
    if (v >= 500) return trim(v / 1000) + ' ms';
    return Math.round(v) + ' \u03bcs';
  }
  function computeNodeScore(node) {
    let s = 0;
    s += node.metal ? 40 : 0; s += (node.bw_gbps / 200) * 25; s += (node.pps_mpps / 30) * 20;
    s += (node.enis / 15) * 10; s += (node.nitro_gen / 6) * 15; s += (node.vcpus / 192) * 8; s += (node.mem_gb / 768) * 2;
    return s;
  }
  const nodeRadius = (node) => 30 + computeNodeScore(node) * 0.6;
  const familyColors = {
    'c7i':  { bg: '#1a2a40', border: '#58a6ff' }, 'c6in': { bg: '#261a3d', border: '#a371f7' },
    'c6i':  { bg: '#1a2e1a', border: '#39d353' }, 'm7i':  { bg: '#2e2415', border: '#f0883e' },
    'r7i':  { bg: '#2e1515', border: '#da3633' }, 'm6i':  { bg: '#2e2a15', border: '#d29922' },
    'r6i':  { bg: '#2e1a1a', border: '#f85149' },
  };
  const getNodeColors = (type) => familyColors[type.split('.')[0]] || { bg: '#1a2a40', border: '#58a6ff' };

  function computePositions() {
    if (N < 2) return { positions: [{ x: CX, y: CY }], stress: 0 };
    const SCALE = 7.5;
    const D = Array.from({ length: N }, () => Array(N).fill(0));
    for (let i = 0; i < N; i++) for (let j = 0; j < N; j++) if (i !== j) {
      const ab = (matrix[i][j] && matrix[i][j].p50) || 35;
      const ba = (matrix[j] && matrix[j][i] && matrix[j][i].p50) || 35;
      D[i][j] = ((ab + ba) / 2) * SCALE;
    }
    let pos = fleet.nodes.map((_, i) => ({
      x: CX + 120 * Math.cos(2 * Math.PI * i / N - Math.PI / 2),
      y: CY + 120 * Math.sin(2 * Math.PI * i / N - Math.PI / 2),
    }));
    for (let iter = 0; iter < 800; iter++) {
      const newPos = pos.map(() => ({ x: 0, y: 0 }));
      for (let i = 0; i < N; i++) {
        let wx = 0, wy = 0, wsum = 0;
        for (let j = 0; j < N; j++) {
          if (i === j) continue;
          const dx = pos[i].x - pos[j].x, dy = pos[i].y - pos[j].y;
          const dist = Math.sqrt(dx * dx + dy * dy) || 0.001, target = D[i][j], w = 1.0 / (target * target);
          wx += w * (pos[j].x + target * (dx / dist)); wy += w * (pos[j].y + target * (dy / dist)); wsum += w;
        }
        newPos[i].x = wx / wsum; newPos[i].y = wy / wsum;
      }
      pos = newPos;
    }
    const PAD = 180;
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    for (const p of pos) { minX = Math.min(minX, p.x); maxX = Math.max(maxX, p.x); minY = Math.min(minY, p.y); maxY = Math.max(maxY, p.y); }
    const rangeX = maxX - minX || 1, rangeY = maxY - minY || 1;
    const scale = Math.min((W - 2 * PAD) / rangeX, (H - 2 * PAD) / rangeY);
    const result = pos.map(p => ({ x: CX + (p.x - (minX + rangeX / 2)) * scale, y: CY + (p.y - (minY + rangeY / 2)) * scale }));
    let stressNum = 0, stressDen = 0;
    for (let i = 0; i < N; i++) for (let j = i + 1; j < N; j++) {
      const dx = result[i].x - result[j].x, dy = result[i].y - result[j].y;
      const dij = Math.sqrt(dx * dx + dy * dy), target = D[i][j] * scale;
      stressNum += (dij - target) ** 2; stressDen += target ** 2;
    }
    const stress = stressDen > 0 ? Math.sqrt(stressNum / stressDen) : 0;
    return { positions: result, stress };
  }
  const { positions, stress } = computePositions();

  const dirSigma = (d) => (d && d.p99 > d.p50) ? (d.p99 - d.p50) / 2.326 : 0;
  function edgeSigma(ab, ba) { const v = [ab, ba].filter(Boolean).map(dirSigma); return v.length ? v.reduce((a, b) => a + b, 0) / v.length : 0; }

  let allP50 = [], allP99 = [], allSigma = [];
  for (let i = 0; i < N; i++) for (let j = 0; j < N; j++) if (matrix[i] && matrix[i][j]) { allP50.push(matrix[i][j].p50); allP99.push(matrix[i][j].p99); }
  for (let i = 0; i < N; i++) for (let j = i + 1; j < N; j++) {
    const ab = matrix[i] && matrix[i][j], ba = matrix[j] && matrix[j][i];
    if (!ab && !ba) continue; allSigma.push(edgeSigma(ab, ba));
  }
  const minP50 = allP50.length ? Math.min(...allP50) : 0, maxP50 = allP50.length ? Math.max(...allP50) : 100;
  const minP99 = allP99.length ? Math.min(...allP99) : 0, maxP99 = allP99.length ? Math.max(...allP99) : 100;
  const minSigma = allSigma.length ? Math.min(...allSigma) : 0, maxSigma = allSigma.length ? Math.max(...allSigma) : 1;

  function jitterColor(sigma) {
    const t = (maxSigma === minSigma) ? 0.5 : (sigma - minSigma) / (maxSigma - minSigma);
    const stops = [[45, 212, 191], [251, 191, 36], [251, 113, 133]];
    const seg = t <= 0.5 ? 0 : 1, lt = t <= 0.5 ? t * 2 : (t - 0.5) * 2, a = stops[seg], b = stops[seg + 1];
    return 'rgb(' + Math.round(a[0] + (b[0] - a[0]) * lt) + ',' + Math.round(a[1] + (b[1] - a[1]) * lt) + ',' + Math.round(a[2] + (b[2] - a[2]) * lt) + ')';
  }
  const EDGE_WIDTH = 2.5;

  // ─── Contours (AZ ⊂ VPC ⊂ Region ⊂ Account; CPG = per-node badge) ──────────
  (function renderContours() {
    function groupBy(key) { const g = {}; fleet.nodes.forEach((node, i) => { const v = node[key] || 'unknown'; (g[v] = g[v] || []).push(i); }); return g; }
    const PAD_BASE = 12, STEP = 18;
    const contourDefs = [
      { groups: groupBy('az'),      cls: 'az',      prefix: 'AZ',      pad: PAD_BASE },
      { groups: groupBy('vpc_id'),  cls: 'vpc',     prefix: 'VPC',     pad: PAD_BASE + STEP },
      { groups: groupBy('region'),  cls: 'region',  prefix: 'Region',  pad: PAD_BASE + STEP * 2 },
      { groups: groupBy('account'), cls: 'account', prefix: 'Account', pad: PAD_BASE + STEP * 3 },
    ];
    const vpcBoxes = [];
    contourDefs.forEach(def => {
      const keys = Object.keys(def.groups); if (keys.length === 0) return;
      keys.forEach(key => {
        if (key === 'unknown') return;
        const idx = def.groups[key]; if (idx.length === 0) return;
        const xs = idx.map(i => positions[i].x), ys = idx.map(i => positions[i].y), radii = idx.map(i => nodeRadius(fleet.nodes[i]));
        const left = Math.min(...xs.map((x, k) => x - radii[k])) - def.pad;
        const right = Math.max(...xs.map((x, k) => x + radii[k])) + def.pad;
        const top = Math.min(...ys.map((y, k) => y - radii[k])) - def.pad;
        const bottom = Math.max(...ys.map((y, k) => y + radii[k])) + def.pad;
        const el = document.createElement('div'); el.className = 'contour ' + def.cls;
        el.style.left = left + 'px'; el.style.top = top + 'px'; el.style.width = (right - left) + 'px'; el.style.height = (bottom - top) + 'px';
        const label = keys.length === 1 ? def.prefix + ': ' + key : key;
        el.innerHTML = '<span class="label">' + label + '</span>';
        root.appendChild(el);
        if (def.cls === 'vpc') vpcBoxes.push({ cx: (left + right) / 2, cy: (top + bottom) / 2, hw: (right - left) / 2, hh: (bottom - top) / 2 });
      });
    });
    if (vpcBoxes.length >= 2) {
      function edgePoint(P, dx, dy) { const adx = Math.abs(dx) || 1e-6, ady = Math.abs(dy) || 1e-6, t = Math.min(P.hw / adx, P.hh / ady); return { x: P.cx + dx * t, y: P.cy + dy * t }; }
      for (let a = 0; a < vpcBoxes.length; a++) for (let b = a + 1; b < vpcBoxes.length; b++) {
        const A = vpcBoxes[a], B = vpcBoxes[b], dx = B.cx - A.cx, dy = B.cy - A.cy;
        const p1 = edgePoint(A, dx, dy), p2 = edgePoint(B, -dx, -dy);
        const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        line.setAttribute('x1', p1.x); line.setAttribute('y1', p1.y); line.setAttribute('x2', p2.x); line.setAttribute('y2', p2.y);
        line.setAttribute('class', 'peering-line'); svg.appendChild(line);
        const lbl = document.createElement('div'); lbl.className = 'peering-label';
        lbl.style.left = ((p1.x + p2.x) / 2) + 'px'; lbl.style.top = ((p1.y + p2.y) / 2) + 'px'; lbl.textContent = 'VPC Peering'; lbl.style.display = 'none';
        root.appendChild(lbl);
        const hit = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        hit.setAttribute('x1', p1.x); hit.setAttribute('y1', p1.y); hit.setAttribute('x2', p2.x); hit.setAttribute('y2', p2.y);
        hit.setAttribute('class', 'peering-hit');
        hit.addEventListener('mouseenter', () => { lbl.style.display = ''; });
        hit.addEventListener('mouseleave', () => { lbl.style.display = 'none'; });
        svg.appendChild(hit);
      }
    }
  })();

  // ─── Edges ────────────────────────────────────────────────────────────────
  const edgeElements = [];
  for (let i = 0; i < N; i++) for (let j = i + 1; j < N; j++) {
    const ab = matrix[i] && matrix[i][j], ba = matrix[j] && matrix[j][i];
    if (!ab && !ba) continue;
    const avgP50 = Math.round((((ab && ab.p50) || 0) + ((ba && ba.p50) || 0)) / 2);
    const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    line.setAttribute('x1', positions[i].x); line.setAttribute('y1', positions[i].y);
    line.setAttribute('x2', positions[j].x); line.setAttribute('y2', positions[j].y);
    line.setAttribute('stroke', jitterColor(edgeSigma(ab, ba))); line.setAttribute('stroke-width', EDGE_WIDTH); line.setAttribute('opacity', '0.55');
    line.classList.add('edge-line'); svg.appendChild(line);
    edgeElements.push({ line, i, j, avgP50, ab, ba });
  }

  // ─── Edge tooltip ───────────────────────────────────────────────────────────
  const edgeTooltip = document.createElement('div'); edgeTooltip.className = 'edge-tooltip'; root.appendChild(edgeTooltip);
  function metricRow(d) {
    return '<div class="dir-values"><span class="metric-label">p50</span><span class="metric-label">p90</span><span class="metric-label">p99</span><span class="metric-label">p99.9</span><span class="metric-label">max</span><span class="metric-label">loss</span>'
      + '<span class="metric-val highlight">' + fmtLat(d.p50) + '</span><span class="metric-val">' + (d.p90 ? fmtLat(d.p90) : '\u2014') + '</span><span class="metric-val">' + fmtLat(d.p99) + '</span><span class="metric-val">' + (d.p999 ? fmtLat(d.p999) : '\u2014') + '</span><span class="metric-val">' + (d.max ? fmtLat(d.max) : '\u2014') + '</span><span class="metric-val">' + (d.loss !== undefined ? d.loss + '%' : '\u2014') + '</span></div>';
  }
  function showEdgeTooltip(i, j) {
    const ab = matrix[i] && matrix[i][j], ba = matrix[j] && matrix[j][i], nodeA = fleet.nodes[i], nodeB = fleet.nodes[j];
    const diff = ab && ba ? Math.abs(ab.p50 - ba.p50) : 0;
    const diffPct = ab && ba && Math.min(ab.p50, ba.p50) > 0 ? ((diff / Math.min(ab.p50, ba.p50)) * 100).toFixed(1) : '0';
    let html = '<h4>' + nodeA.ec2_name + ' \u2194 ' + nodeB.ec2_name + '</h4>';
    if (ab) html += '<div class="dir-block"><div class="dir-label">\u2192 ' + nodeA.ec2_name + ' \u2192 ' + nodeB.ec2_name + '</div>' + metricRow(ab) + '</div>';
    if (ba) html += '<div class="dir-block"><div class="dir-label">\u2190 ' + nodeB.ec2_name + ' \u2192 ' + nodeA.ec2_name + '</div>' + metricRow(ba) + '</div>';
    if (diff > 0) html += '<div class="asymmetry">Asymmetry: \u0394' + diff + '\u03bcs (' + diffPct + '%)</div>';
    edgeTooltip.innerHTML = html; edgeTooltip.classList.add('visible');
  }
  function positionEdgeTooltip(e) {
    let tx = e.clientX + 16, ty = e.clientY - 10;
    const tw = edgeTooltip.offsetWidth || 260, th = edgeTooltip.offsetHeight || 150;
    if (tx + tw > W - 20) tx = e.clientX - tw - 16;
    if (ty + th > H - 20) ty = H - th - 20;
    if (ty < 10) ty = 10;
    edgeTooltip.style.left = tx + 'px'; edgeTooltip.style.top = ty + 'px';
  }
  const hideEdgeTooltip = () => edgeTooltip.classList.remove('visible');

  // ─── Edge labels ────────────────────────────────────────────────────────────
  const edgeLabelEls = [];
  for (let i = 0; i < N; i++) for (let j = i + 1; j < N; j++) {
    const ab = matrix[i] && matrix[i][j], ba = matrix[j] && matrix[j][i];
    if (!ab && !ba) continue;
    const avgP50 = Math.round((((ab && ab.p50) || 0) + ((ba && ba.p50) || 0)) / 2);
    const x1 = positions[i].x, y1 = positions[i].y, x2 = positions[j].x, y2 = positions[j].y;
    const mx = (x1 + x2) / 2, my = (y1 + y2) / 2;
    let ang = Math.atan2(y2 - y1, x2 - x1) * 180 / Math.PI;
    if (ang > 90) ang -= 180; else if (ang < -90) ang += 180;
    const el = document.createElement('div'); el.className = 'edge-label';
    el.style.left = mx + 'px'; el.style.top = my + 'px'; el.style.transform = 'translate(-50%,-50%) rotate(' + ang + 'deg)';
    el.style.color = jitterColor(edgeSigma(ab, ba)); el.textContent = fmtLat(avgP50) + ' \u00b1' + fmtLat(edgeSigma(ab, ba)); el.style.display = 'none';
    const ci = i, cj = j; edgeLabelEls.push({ el, i: ci, j: cj });
    el.addEventListener('mouseenter', () => showEdgeTooltip(ci, cj));
    el.addEventListener('mousemove', positionEdgeTooltip);
    el.addEventListener('mouseleave', hideEdgeTooltip);
    root.appendChild(el);
  }

  // ─── Selection ────────────────────────────────────────────────────────────
  const selected = new Set(), nodeEls = [];
  function neighborsOf(set) { const nb = new Set(); set.forEach(i => { nb.add(i); for (let j = 0; j < N; j++) if (j !== i && ((matrix[i] && matrix[i][j]) || (matrix[j] && matrix[j][i]))) nb.add(j); }); return nb; }
  function applySel(hover) {
    const hasSel = selected.size > 0, vis = hasSel ? neighborsOf(selected) : null;
    nodeEls.forEach((el, i) => { if (!el) return; el.style.display = (!hasSel || vis.has(i)) ? '' : 'none'; el.classList.toggle('selected', selected.has(i)); });
    edgeElements.forEach(({ line, i, j }) => {
      const touchSel = hasSel && (selected.has(i) || selected.has(j)), touchHover = hover !== -1 && (i === hover || j === hover);
      line.classList.remove('highlighted', 'dimmed');
      if (hasSel) line.classList.add(touchSel ? 'highlighted' : 'dimmed');
      else if (hover !== -1) line.classList.add(touchHover ? 'highlighted' : 'dimmed');
    });
    for (const it of edgeLabelEls) it.el.style.display = ((selected.has(it.i) || selected.has(it.j)) || (hover !== -1 && (it.i === hover || it.j === hover))) ? '' : 'none';
    deselectBtn.style.display = hasSel ? 'inline-block' : 'none';
  }

  // ─── Nodes + hover tooltip ──────────────────────────────────────────────────
  const tooltip = document.createElement('div'); tooltip.className = 'node-tooltip'; root.appendChild(tooltip);
  function buildPeerTable(i, inbound) {
    const rows = [];
    for (let j = 0; j < N; j++) { if (i === j) continue; const data = inbound ? (matrix[j] && matrix[j][i]) : (matrix[i] && matrix[i][j]); if (!data) continue; rows.push({ peer: fleet.nodes[j], data }); }
    const groups = {};
    rows.forEach(r => { const pg = (r.peer.cpg_name && r.peer.cpg_name !== 'unknown') ? r.peer.cpg_name : 'no PG'; (groups[pg] = groups[pg] || []).push(r); });
    const keys = Object.keys(groups).sort((a, b) => Math.min(...groups[a].map(r => r.data.p50)) - Math.min(...groups[b].map(r => r.data.p50)));
    let h = '<table><tr><th>Peer</th><th>p50</th><th>p90</th><th>p99</th><th>p99.9</th><th>max</th><th>loss</th></tr>';
    keys.forEach(pg => {
      h += '<tr class="pg-group"><td colspan="7">' + pg + '</td></tr>';
      groups[pg].sort((a, b) => a.data.p50 - b.data.p50).forEach(r => { const d = r.data;
        h += '<tr><td class="peer-name">' + r.peer.ec2_name + '</td><td class="highlight">' + fmtLat(d.p50) + '</td><td>' + (d.p90 ? fmtLat(d.p90) : '\u2014') + '</td><td>' + fmtLat(d.p99) + '</td><td>' + (d.p999 ? fmtLat(d.p999) : '\u2014') + '</td><td>' + (d.max ? fmtLat(d.max) : '\u2014') + '</td><td>' + (d.loss !== undefined ? d.loss + '%' : '\u2014') + '</td></tr>'; });
    });
    return h + '</table>';
  }
  fleet.nodes.forEach((node, i) => {
    const r = nodeRadius(node), colors = getNodeColors(node.type);
    const el = document.createElement('div'); el.className = 'node';
    el.style.width = el.style.height = (r * 2) + 'px';
    el.style.left = (positions[i].x - r) + 'px'; el.style.top = (positions[i].y - r) + 'px';
    el.style.background = colors.bg; el.style.borderColor = colors.border;
    el.innerHTML = '<span class="instance-type">' + node.type + '</span>'
      + '<span class="ip ip-public">' + (node.public_ip || '\u2014') + '</span>'
      + '<span class="ip ip-private">' + node.private_ip + '</span>'
      + ((node.cpg_name && node.cpg_name !== 'unknown') ? '<span class="pg-badge" title="Placement group">' + node.cpg_name + '</span>' : '');
    root.appendChild(el); nodeEls[i] = el;
    el.addEventListener('mouseenter', () => {
      let html = '<h4>' + node.ec2_name + ' \u2192 peers</h4>' + buildPeerTable(i, false);
      html += '<div class="direction" style="margin-top:6px">\u2190 Inbound (peers \u2192 this node):</div>' + buildPeerTable(i, true);
      tooltip.innerHTML = html; tooltip.classList.add('visible');
      if (selected.size === 0) applySel(i);
    });
    el.addEventListener('mousemove', (e) => {
      let tx = e.clientX + 16, ty = e.clientY - 10;
      const tw = tooltip.offsetWidth || 280, th = tooltip.offsetHeight || 200;
      if (tx + tw > W - 20) tx = e.clientX - tw - 16;
      if (ty + th > H - 20) ty = H - th - 20;
      if (ty < 10) ty = 10;
      tooltip.style.left = tx + 'px'; tooltip.style.top = ty + 'px';
    });
    el.addEventListener('mouseleave', () => { tooltip.classList.remove('visible'); if (selected.size === 0) applySel(-1); });
    el.addEventListener('click', () => { if (selected.has(i)) selected.delete(i); else selected.add(i); applySel(-1); });
  });

  // ─── Instance-type legend ───────────────────────────────────────────────────
  const region = fleet.region || 'us-east-1';
  (function renderInstanceLegend() {
    const seen = new Map(); fleet.nodes.forEach(n => { if (!seen.has(n.type)) seen.set(n.type, n); });
    let rows = '';
    for (const [type, node] of seen) {
      const colors = getNodeColors(type), r = Math.round(nodeRadius(node) * 0.35), family = type.split('.')[0];
      rows += '<div class="type-row"><div class="type-dot" style="width:' + (r * 2) + 'px;height:' + (r * 2) + 'px;background:' + colors.bg + ';border:2px solid ' + colors.border + '"></div>'
        + '<div class="type-info"><div class="type-name">' + type + '</div><div class="type-specs">' + node.vcpus + 'vCPU \u00b7 ' + node.mem_gb + 'GB \u00b7 ' + node.bw_gbps + 'Gbps \u00b7 ' + node.pps_mpps + 'Mpps \u00b7 ' + node.enis + ' ENIs \u00b7 Nitro ' + node.nitro_gen + '</div></div>'
        + '<a href="https://instances.vantage.sh/?selected=' + type + '&region=' + region + '" target="_blank">specs\u2197</a>'
        + '<a href="https://aws.amazon.com/ec2/instance-types/' + family + '/" target="_blank">family\u2197</a></div>';
    }
    const el = document.createElement('div'); el.className = 'instance-legend'; el.innerHTML = '<h3>Instance Types</h3>' + rows; root.appendChild(el);
  })();

  // ─── Visual-encoding legend ─────────────────────────────────────────────────
  (function renderVisLegend() {
    const el = document.createElement('div'); el.className = 'vis-legend';
    el.innerHTML = '<h3>Legend</h3>'
      + '<div class="row"><div class="swatch" style="background:linear-gradient(to right,#2dd4bf,#fbbf24,#fb7185)"></div><span>Edge color = jitter \u03c3 (' + fmtLat(minSigma) + ' \u2192 ' + fmtLat(maxSigma) + ')</span></div>'
      + '<div class="row"><span>Node size = f(BW, PPS, ENIs, Nitro, CPU, Mem, metal)</span></div>'
      + '<div class="row"><span>Distance \u221d p50 latency (SMACOF stress: ' + (stress * 100).toFixed(1) + '%)</span></div>'
      + '<div class="row"><span style="color:#79c0ff;font-weight:700">Public IP</span><span style="color:#8b949e">&nbsp;/&nbsp;</span><span style="color:#8b949e">Private IP</span><span>&nbsp;\u2014 shown on each node</span></div>'
      + '<div class="contour-samples">'
      + '<span style="border:1.5px dashed rgba(57,211,83,0.3);color:#39d353">VPC</span>'
      + '<span style="border:1.5px dashed rgba(163,113,247,0.3);color:#a371f7">AZ</span>'
      + '<span style="border:1.5px dashed rgba(88,166,255,0.3);color:#58a6ff">Region</span>'
      + '<span style="border:1.5px solid rgba(248,81,73,0.5);color:#f85149">Account</span></div>'
      + '<div class="ux-hint"><b>Hover</b> a node \u2014 reveal its edge latencies &amp; highlight. <b>Click</b> a node \u2014 pin/unpin those labels (click again to clear). <b>Drag</b> a panel\u2019s title to move it; <b>click</b> the title to fold.</div>';
    root.appendChild(el);
  })();

  // ─── Stats panel ────────────────────────────────────────────────────────────
  const medP50 = allP50.length ? [...allP50].sort((a, b) => a - b)[Math.floor(allP50.length / 2)] : 0;
  const uniq = (k) => [...new Set(fleet.nodes.map(n => n[k]))].filter(v => v !== 'unknown');
  const uRegions = uniq('region'), uAZs = uniq('az'), uCPGs = uniq('cpg_name'), uAccounts = uniq('account');
  let scopeHtml = '';
  const stat = (label, val) => '<div class="stat"><span>' + label + '</span><span class="val">' + val + '</span></div>';
  if (uCPGs.length === 1) scopeHtml += stat('Placement Group', uCPGs[0]); else if (uCPGs.length > 1) scopeHtml += stat('Placement Groups', uCPGs.length);
  if (uAZs.length === 1) scopeHtml += stat('AZ', uAZs[0]); else if (uAZs.length > 1) scopeHtml += stat('AZs', uAZs.join(', '));
  if (uRegions.length === 1) scopeHtml += stat('Region', uRegions[0]); else if (uRegions.length > 1) scopeHtml += stat('Regions', uRegions.join(', '));
  if (uAccounts.length === 1) scopeHtml += stat('Account', uAccounts[0]); else if (uAccounts.length > 1) scopeHtml += stat('Accounts', uAccounts.length);
  statsEl.innerHTML = '<h3>Summary</h3>'
    + stat('Nodes', N) + stat('Pairs', allP50.length)
    + stat('p50 range', fmtLat(minP50) + '\u2013' + fmtLat(maxP50))
    + stat('p99 range', fmtLat(minP99) + '\u2013' + fmtLat(maxP99))
    + stat('Jitter \u03c3', fmtLat(minSigma) + '\u2013' + fmtLat(maxSigma))
    + stat('Median p50', fmtLat(medP50))
    + stat('Spread', fmtLat(maxP50 - minP50))
    + '<div style="margin-top:8px;border-top:1px solid #30363d;padding-top:6px">' + scopeHtml + '</div>'
    + '<div class="stress">Layout fidelity (SMACOF): <span class="val">' + (100 - stress * 100).toFixed(1) + '%</span> \u2014 stress ' + (stress * 100).toFixed(1) + '%</div>';

  // ─── Foldable + draggable panels ──────────────────────────────────────────
  function enhancePanel(el) {
    const h = el.querySelector('h3'); if (!h) return;
    const body = document.createElement('div'); body.className = 'panel-body';
    while (h.nextSibling) body.appendChild(h.nextSibling); el.appendChild(body);
    const caret = document.createElement('span'); caret.className = 'panel-caret'; caret.textContent = '\u25be'; h.insertBefore(caret, h.firstChild);
    let collapsed = false, dragging = false, moved = false, sx = 0, sy = 0, ox = 0, oy = 0;
    h.addEventListener('mousedown', (e) => { dragging = true; moved = false; sx = e.clientX; sy = e.clientY;
      const r = el.getBoundingClientRect(); ox = r.left; oy = r.top; el.style.right = 'auto'; el.style.bottom = 'auto'; el.style.left = ox + 'px'; el.style.top = oy + 'px'; e.preventDefault(); });
    const onMove = (e) => { if (!dragging) return; const dx = e.clientX - sx, dy = e.clientY - sy; if (Math.abs(dx) + Math.abs(dy) > 3) moved = true; el.style.left = (ox + dx) + 'px'; el.style.top = (oy + dy) + 'px'; };
    const onUp = () => { dragging = false; };
    window.addEventListener('mousemove', onMove); window.addEventListener('mouseup', onUp);
    disposers.push(() => { window.removeEventListener('mousemove', onMove); window.removeEventListener('mouseup', onUp); });
    h.addEventListener('click', () => { if (moved) { moved = false; return; } collapsed = !collapsed; body.style.display = collapsed ? 'none' : ''; caret.textContent = collapsed ? '\u25b8' : '\u25be'; });
  }
  root.querySelectorAll('.stats, .vis-legend, .instance-legend').forEach(enhancePanel);
  deselectBtn.addEventListener('click', () => { selected.clear(); applySel(-1); });
  applySel(-1);

  return { dispose() { disposers.forEach(fn => fn()); if (root.parentNode) root.parentNode.removeChild(root); } };
}
