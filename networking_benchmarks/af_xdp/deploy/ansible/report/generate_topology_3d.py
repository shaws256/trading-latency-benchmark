#!/usr/bin/env python3
"""
generate_topology_3d.py - 3D topology visualization (three.js / WebGL).

Reads a results directory (per-node ``<ip>_metadata.json`` + ``<src>-<dst>.json``),
reuses the data loaders from ``generate_matrix_report.py``, and emits a
self-contained ``topology_3d.html``.

Ports the 2D report's visual layer into 3D:
  - nodes positioned by latency (3D SMACOF, log-compressed p50); node colour =
    instance family, size = capability score
  - edges coloured by latency jitter (sigma)
  - draggable + foldable Summary / Legend / Instance-Types panels
  - node hover: highlight incident edges, reveal that node's edge labels, and a
    latency table grouped by placement group and sorted by p50
  - click a node to pin its edge labels (click again to clear)
  - edge-label hover: directional edge tooltip (+ asymmetry)
  - latency values auto-format us -> ms -> s

three.js is loaded from a CDN via an ES import map (no build step, no WASM;
requires network access to render). View-only orbit / zoom / pan.

Usage: python3 generate_topology_3d.py <results_dir>
"""

import sys
from pathlib import Path

from generate_matrix_report import (
    load_fleet_metadata,
    build_matrix,
    _build_topology_fleet_json,
)

TEMPLATE = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>AF_XDP 3D Topology</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  html, body { width: 100%; height: 100%; overflow: hidden; background: #0d1117;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; color: #e6edf3; }
  #app { position: fixed; inset: 0; }
  .deselect-btn { position: fixed; top: 16px; left: 50%; transform: translateX(-50%); z-index: 20;
    display: none; background: rgba(240,136,62,0.16); color: #f0883e; border: 1px solid #f0883e;
    border-radius: 6px; padding: 7px 15px; font-size: 13px; font-weight: 600; cursor: pointer; backdrop-filter: blur(8px); }
  .deselect-btn:hover { background: rgba(240,136,62,0.3); }

  .label { color: #e6edf3; font-size: 12px; font-family: 'SF Mono', monospace;
    text-align: center; pointer-events: none; text-shadow: 0 1px 3px #000; white-space: nowrap; }
  .label .t { color: #fff; font-weight: 700; font-size: 14px; }
  .label .ipp { color: #79c0ff; }
  .label .ipv { color: #8b949e; }
  .label .az { color: #6e7681; font-size: 10px; }
  .boundary-label { font-size: 12px; font-weight: 700; padding: 1px 8px; border-radius: 4px;
    white-space: nowrap; pointer-events: none; background: rgba(13,17,23,0.65); }
  .boundary-label .bl-sub { font-size: 9px; font-weight: 500; opacity: 0.8; }

  .edge-label { color: #e6edf3; font-size: 14px; font-weight: 700; font-family: 'SF Mono', monospace;
    background: rgba(13,17,23,0.94); padding: 2px 8px; border-radius: 4px;
    border: 1px solid rgba(255,255,255,0.1); white-space: nowrap; cursor: default; pointer-events: auto; }
  .edge-label:hover { border-color: rgba(88,166,255,0.5); }

  .node-tooltip, .edge-tooltip { position: fixed; z-index: 100; background: rgba(22,27,34,0.97);
    border: 1px solid #30363d; border-radius: 8px; padding: 12px 14px; font-size: 12px;
    pointer-events: none; backdrop-filter: blur(8px); box-shadow: 0 8px 32px rgba(0,0,0,0.6);
    white-space: nowrap; opacity: 0; transition: opacity 0.12s; }
  .node-tooltip.visible, .edge-tooltip.visible { opacity: 1; }
  .node-tooltip h4, .edge-tooltip h4 { font-size: 13px; color: #58a6ff; margin-bottom: 6px; }
  .node-tooltip table { border-collapse: collapse; width: 100%; }
  .node-tooltip th { text-align: center; font-size: 11px; color: #8b949e; padding: 2px 6px; border-bottom: 1px solid #21262d; }
  .node-tooltip td { text-align: center; font-family: 'SF Mono', monospace; font-size: 12px; padding: 3px 6px; }
  .node-tooltip td.peer-name { text-align: left; color: #79c0ff; font-family: inherit; }
  .node-tooltip td.highlight { color: #f0883e; font-weight: 600; }
  .node-tooltip tr.pg-group td { text-align: left; color: #8b949e; font-weight: 700; font-size: 10px;
    letter-spacing: 0.3px; padding: 6px 6px 2px; border-bottom: 1px solid #30363d; }
  .node-tooltip .direction { font-size: 9px; color: #6e7681; }
  .edge-tooltip .dir-block { margin-bottom: 8px; }
  .edge-tooltip .dir-label { font-size: 10px; color: #8b949e; margin-bottom: 3px; }
  .edge-tooltip .dir-values { display: grid; grid-template-columns: repeat(6, auto); gap: 2px 10px; }
  .edge-tooltip .metric-label { font-size: 9px; color: #6e7681; }
  .edge-tooltip .metric-val { font-family: 'SF Mono', monospace; font-size: 12px; }
  .edge-tooltip .metric-val.highlight { color: #f0883e; font-weight: 700; }
  .edge-tooltip .asymmetry { font-size: 10px; color: #f0883e; margin-top: 4px; padding-top: 4px; border-top: 1px solid #21262d; }

  .panel { position: fixed; z-index: 10; background: rgba(22,27,34,0.94); border: 1px solid #30363d;
    border-radius: 8px; padding: 14px 16px; font-size: 13px; backdrop-filter: blur(8px); }
  .panel h3 { font-size: 15px; color: #58a6ff; margin-bottom: 10px; cursor: move; user-select: none; }
  .panel-caret { display: inline-block; width: 12px; margin-right: 4px; font-size: 10px; color: #8b949e; }
  #stats { top: 16px; left: 16px; min-width: 210px; }
  #stats .stat { display: flex; justify-content: space-between; margin: 3px 0; }
  #stats .stat .val { color: #f0883e; font-weight: 600; font-family: 'SF Mono', monospace; }
  #stats .scope { margin-top: 8px; border-top: 1px solid #30363d; padding-top: 6px; }
  #legend { top: 16px; right: 16px; max-width: 320px; }
  #legend .row { display: flex; align-items: center; gap: 10px; margin: 5px 0; }
  #legend .swatch { width: 32px; height: 5px; border-radius: 2px; flex-shrink: 0; }
  #legend .ux-hint { margin-top: 10px; padding-top: 8px; border-top: 1px solid #30363d;
    font-size: 10px; color: #8b949e; line-height: 1.6; }
  #legend .ux-hint b { color: #e6edf3; }
  #itypes { bottom: 16px; left: 16px; max-width: 360px; }
  #itypes .type-row { display: flex; align-items: center; gap: 12px; margin: 6px 0; padding: 5px 0;
    border-bottom: 1px solid rgba(48,54,61,0.5); }
  #itypes .type-row:last-child { border-bottom: none; }
  #itypes .type-dot { border-radius: 50%; flex-shrink: 0; }
  #itypes .type-name { font-weight: 600; }
  #itypes .type-specs { font-size: 11px; color: #8b949e; }
  #itypes a { color: #58a6ff; text-decoration: none; font-size: 11px; border: 1px solid rgba(88,166,255,0.3);
    border-radius: 3px; padding: 2px 6px; white-space: nowrap; }
  #itypes a:hover { background: rgba(88,166,255,0.15); }
</style>
<script type="importmap">
{ "imports": {
  "three": "https://unpkg.com/three@0.160.0/build/three.module.js",
  "three/addons/": "https://unpkg.com/three@0.160.0/examples/jsm/"
} }
</script>
</head><body>
<div id="app"></div>
<button class="deselect-btn" id="deselect">Deselect all</button>
<div class="panel" id="stats"></div>
<div class="panel" id="legend"></div>
<div class="panel" id="itypes"></div>
<div class="node-tooltip" id="node-tooltip"></div>
<div class="edge-tooltip" id="edge-tooltip"></div>

<script type="module">
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { CSS2DRenderer, CSS2DObject } from 'three/addons/renderers/CSS2DRenderer.js';

const fleet = __FLEET_JSON__;
const N = fleet.nodes.length;
const region = fleet.region || 'us-east-1';

// ---- latency unit formatter (us -> ms -> s at 500 thresholds) ----
function fmtLat(us) {
  if (us === null || us === undefined || us === '') return '\u2014';
  const v = +us; if (!isFinite(v) || v <= 0) return '\u2014';
  const trim = (x) => (Math.round(x*100)/100).toString();
  if (v >= 500000) return trim(v/1000000) + ' s';
  if (v >= 500) return trim(v/1000) + ' ms';
  return Math.round(v) + ' \u03bcs';
}

// ---- edge jitter palette: teal -> amber -> rose ----
function jitterColorRGB(t) {
  const stops = [[45,212,191],[251,191,36],[251,113,133]];
  const seg = t <= 0.5 ? 0 : 1, lt = t <= 0.5 ? t*2 : (t-0.5)*2;
  const a = stops[seg], b = stops[seg+1];
  return [Math.round(a[0]+(b[0]-a[0])*lt), Math.round(a[1]+(b[1]-a[1])*lt), Math.round(a[2]+(b[2]-a[2])*lt)];
}
const jitterColorCss = (t) => 'rgb(' + jitterColorRGB(t).join(',') + ')';

// ---- node colour by instance family + size by capability score ----
const familyColors = {
  'c7i': {bg:'#1a2a40', border:'#58a6ff'}, 'c6in': {bg:'#261a3d', border:'#a371f7'},
  'c6i': {bg:'#1a2e1a', border:'#39d353'}, 'm7i': {bg:'#2e2415', border:'#f0883e'},
  'r7i': {bg:'#2e1515', border:'#da3633'}, 'm6i': {bg:'#2e2a15', border:'#d29922'},
  'r6i': {bg:'#2e1a1a', border:'#f85149'},
};
const getColors = (type) => familyColors[type.split('.')[0]] || {bg:'#1a2a40', border:'#58a6ff'};
function nodeScore(n) {
  let s = 0;
  s += n.metal ? 40 : 0; s += (n.bw_gbps/200)*25; s += (n.pps_mpps/30)*20;
  s += (n.enis/15)*10; s += (n.nitro_gen/6)*15; s += (n.vcpus/192)*8; s += (n.mem_gb/768)*2;
  return s;
}
const nodeRadius3D = (n) => 2.6 + nodeScore(n) * 0.045;
// node colour by capability (absolute scale): light blue (weakest) -> deep red (metal 96x / top net)
const SCORE_MIN = 15, SCORE_MAX = 120;
const capT = (n) => Math.min(1, Math.max(0, (nodeScore(n) - SCORE_MIN) / (SCORE_MAX - SCORE_MIN)));
function capColor(n) {
  const t = Math.pow(capT(n), 0.85);
  const hue = (210 + t * 150) % 360, sat = 0.60 + t * 0.30, light = 0.75 - t * 0.42;
  return new THREE.Color().setHSL(hue / 360, sat, light);
}

// ---- jitter sigma ----
const dirSigma = (d) => (d && d.p99 > d.p50) ? (d.p99 - d.p50)/2.326 : 0;
function edgeSigma(i, j) {
  const ab = fleet.matrix[i] && fleet.matrix[i][j], ba = fleet.matrix[j] && fleet.matrix[j][i];
  const s = [ab, ba].filter(Boolean).map(dirSigma);
  return s.length ? s.reduce((a,b)=>a+b,0)/s.length : 0;
}

// ---- global ranges (for legend/stats/edge colour) ----
let allP50 = [], allP99 = [], allSig = [];
for (let i=0;i<N;i++) for (let j=0;j<N;j++) if (fleet.matrix[i] && fleet.matrix[i][j]) { allP50.push(fleet.matrix[i][j].p50); allP99.push(fleet.matrix[i][j].p99); }
for (let i=0;i<N;i++) for (let j=i+1;j<N;j++) { if ((fleet.matrix[i]&&fleet.matrix[i][j])||(fleet.matrix[j]&&fleet.matrix[j][i])) allSig.push(edgeSigma(i,j)); }
const minP50 = allP50.length?Math.min(...allP50):0, maxP50 = allP50.length?Math.max(...allP50):100;
const minP99 = allP99.length?Math.min(...allP99):0, maxP99 = allP99.length?Math.max(...allP99):100;
const minSig = allSig.length?Math.min(...allSig):0, maxSig = allSig.length?Math.max(...allSig):1;
const sigT = (s) => maxSig===minSig ? 0.5 : (s-minSig)/(maxSig-minSig);

// ---- latency -> distance (log-compressed avg p50) ----
function p50pair(i, j) {
  const ab = fleet.matrix[i]&&fleet.matrix[i][j]?fleet.matrix[i][j].p50:null;
  const ba = fleet.matrix[j]&&fleet.matrix[j][i]?fleet.matrix[j][i].p50:null;
  if (ab!=null && ba!=null) return (ab+ba)/2;
  return ab!=null?ab:(ba!=null?ba:35);
}
const LSCALE = 26;
const targetDist = (i, j) => Math.log10(p50pair(i, j) + 1) * LSCALE;

// ---- 3D SMACOF ----
function layout() {
  if (N <= 1) return [new THREE.Vector3()];
  let pos = [];
  for (let i=0;i<N;i++){ const t=Math.acos(1-2*(i+0.5)/N), p=Math.PI*(1+Math.sqrt(5))*i;
    pos.push(new THREE.Vector3(Math.sin(t)*Math.cos(p),Math.sin(t)*Math.sin(p),Math.cos(t)).multiplyScalar(40)); }
  for (let it=0; it<600; it++){
    const np = [];
    for (let i=0;i<N;i++){
      const acc = new THREE.Vector3(); let wsum = 0;
      for (let j=0;j<N;j++){ if(i===j) continue;
        const d = pos[i].distanceTo(pos[j]) || 1e-4, tgt = targetDist(i,j), w = 1/((tgt*tgt)||1);
        const dir = new THREE.Vector3().subVectors(pos[i],pos[j]).multiplyScalar(tgt/d);
        acc.add(new THREE.Vector3().addVectors(pos[j],dir).multiplyScalar(w)); wsum += w;
      }
      np.push(acc.multiplyScalar(1/(wsum||1)));
    }
    pos = np;
  }
  const c = new THREE.Vector3(); pos.forEach(p=>c.add(p)); c.multiplyScalar(1/N); pos.forEach(p=>p.sub(c));
  return pos;
}
const positions = layout();

// ---- scene / renderers ----
const app = document.getElementById('app');
const scene = new THREE.Scene(); scene.background = new THREE.Color('#0d1117');
const camera = new THREE.PerspectiveCamera(55, innerWidth/innerHeight, 0.1, 20000);
const renderer = new THREE.WebGLRenderer({antialias:true});
renderer.setSize(innerWidth, innerHeight); renderer.setPixelRatio(devicePixelRatio);
app.appendChild(renderer.domElement);
const labelRenderer = new CSS2DRenderer();
labelRenderer.setSize(innerWidth, innerHeight);
labelRenderer.domElement.style.position = 'absolute';
labelRenderer.domElement.style.top = '0';
labelRenderer.domElement.style.pointerEvents = 'none';   // canvas gets orbit; edge labels re-enable per-div
app.appendChild(labelRenderer.domElement);
const controls = new OrbitControls(camera, renderer.domElement); controls.enableDamping = true;
scene.add(new THREE.AmbientLight(0xffffff, 0.8));
const dl = new THREE.DirectionalLight(0xffffff, 0.75); dl.position.set(1,1,1); scene.add(dl);

// ---- nodes ----
const sphereGeo = new THREE.SphereGeometry(1, 24, 18);
const nodeMeshes = [];
fleet.nodes.forEach((n, i) => {
  const col = capColor(n), emissive = col.clone().multiplyScalar(0.3);
  const mat = new THREE.MeshStandardMaterial({color: col, emissive: emissive, roughness: 0.5, metalness: 0.15});
  const mesh = new THREE.Mesh(sphereGeo, mat);
  mesh.scale.setScalar(nodeRadius3D(n));
  mesh.position.copy(positions[i]);
  mesh.userData.idx = i; mesh.userData.baseEmissive = emissive.clone();
  scene.add(mesh); nodeMeshes.push(mesh);

  const div = document.createElement('div'); div.className = 'label';
  div.innerHTML = '<div class="t">' + n.type + '</div>'
    + (n.public_ip ? '<div class="ipp">' + n.public_ip + '</div>' : '')
    + '<div class="ipv">' + n.private_ip + '</div>';
  const lab = new CSS2DObject(div); lab.position.set(0, 1.45, 0); mesh.add(lab); // local units (mesh is scaled by radius)
});

// ---- edges + edge labels ----
const edges = [];        // {line, i, j, baseColor}
const edgeLabelEls = []; // {obj, i, j}
for (let i=0;i<N;i++) for (let j=i+1;j<N;j++) {
  if (!(fleet.matrix[i]&&fleet.matrix[i][j]) && !(fleet.matrix[j]&&fleet.matrix[j][i])) continue;
  const t = sigT(edgeSigma(i, j));
  const rgb = jitterColorRGB(t), col = new THREE.Color('rgb('+rgb.join(',')+')');
  const geo = new THREE.BufferGeometry().setFromPoints([positions[i], positions[j]]);
  const mat = new THREE.LineBasicMaterial({color: col, transparent: true, opacity: 0.5});
  const line = new THREE.Line(geo, mat); scene.add(line);
  edges.push({line, i, j, baseColor: col});

  const ab = fleet.matrix[i]&&fleet.matrix[i][j], ba = fleet.matrix[j]&&fleet.matrix[j][i];
  const avgP50 = Math.round(((ab?ab.p50:0)+(ba?ba.p50:0)) / ((ab?1:0)+(ba?1:0) || 1));
  const div = document.createElement('div'); div.className = 'edge-label';
  div.style.color = jitterColorCss(t);
  div.textContent = fmtLat(avgP50) + ' \u00b1' + fmtLat(edgeSigma(i, j));
  const ci = i, cj = j;
  div.addEventListener('mouseenter', () => showEdgeTooltip(ci, cj));
  div.addEventListener('mousemove', positionEdgeTooltip);
  div.addEventListener('mouseleave', hideEdgeTooltip);
  const obj = new CSS2DObject(div);
  obj.position.copy(positions[i]).add(positions[j]).multiplyScalar(0.5);
  obj.visible = false; scene.add(obj);
  edgeLabelEls.push({obj, i, j});
}

// ---- selection (click) + gold-glow halos ----
const selected = new Set();
const halos = nodeMeshes.map((m, i) => {
  const h = new THREE.Mesh(sphereGeo, new THREE.MeshBasicMaterial({color: 0xffd700, transparent: true, opacity: 0.30, blending: THREE.AdditiveBlending, depthWrite: false}));
  h.scale.setScalar(nodeRadius3D(fleet.nodes[i]) * 1.6); h.position.copy(positions[i]); h.visible = false; scene.add(h); return h;
});
function neighborsOf(set) {   // this node + 1-hop-away nodes
  const nb = new Set();
  set.forEach(i => { nb.add(i); for (let j = 0; j < N; j++) if (j !== i && ((fleet.matrix[i]&&fleet.matrix[i][j]) || (fleet.matrix[j]&&fleet.matrix[j][i]))) nb.add(j); });
  return nb;
}
function render(hover) {
  const hasSel = selected.size > 0;
  const vis = hasSel ? neighborsOf(selected) : null;
  nodeMeshes.forEach((m, i) => {
    m.visible = !hasSel || vis.has(i);
    halos[i].visible = selected.has(i);
    if (selected.has(i)) m.material.emissive.set(0xffd700).multiplyScalar(0.4);
    else m.material.emissive.copy(m.userData.baseEmissive).multiplyScalar(i === hover ? 2.2 : 1);
  });
  edges.forEach(e => {
    const touchSel = hasSel && (selected.has(e.i) || selected.has(e.j));
    const touchHover = hover !== -1 && (e.i === hover || e.j === hover);
    e.line.visible = !hasSel || touchSel;            // only links leading to the selected node(s)
    e.line.material.opacity = hasSel ? 0.95 : (hover !== -1 ? (touchHover ? 1.0 : 0.06) : 0.5);
    e.line.material.color.copy(e.baseColor);
  });
  edgeLabelEls.forEach(e => { e.obj.visible = (selected.has(e.i) || selected.has(e.j)) || (hover !== -1 && (e.i === hover || e.j === hover)); });
  document.getElementById('deselect').style.display = hasSel ? 'inline-block' : 'none';
}

// ---- latency table grouped by PG, sorted by p50 ----
function buildPeerTable(i, inbound) {
  const rows = [];
  for (let j=0;j<N;j++){ if(i===j) continue;
    const data = inbound ? (fleet.matrix[j]&&fleet.matrix[j][i]) : (fleet.matrix[i]&&fleet.matrix[i][j]);
    if (!data) continue; rows.push({peer: fleet.nodes[j], data}); }
  const groups = {};
  rows.forEach(r => { const pg = (r.peer.cpg_name && r.peer.cpg_name!=='unknown') ? r.peer.cpg_name : 'no PG'; (groups[pg]=groups[pg]||[]).push(r); });
  const keys = Object.keys(groups).sort((a,b)=> Math.min(...groups[a].map(r=>r.data.p50)) - Math.min(...groups[b].map(r=>r.data.p50)));
  let h = '<table><tr><th>Peer</th><th>p50</th><th>p90</th><th>p99</th><th>p99.9</th><th>max</th><th>loss</th></tr>';
  keys.forEach(pg => {
    h += '<tr class="pg-group"><td colspan="7">' + pg + '</td></tr>';
    groups[pg].sort((a,b)=>a.data.p50-b.data.p50).forEach(r => { const d = r.data;
      h += '<tr><td class="peer-name">' + r.peer.ec2_name + '</td><td class="highlight">' + fmtLat(d.p50) + '</td><td>' + (d.p90?fmtLat(d.p90):'\u2014') + '</td><td>' + fmtLat(d.p99) + '</td><td>' + (d.p999?fmtLat(d.p999):'\u2014') + '</td><td>' + (d.max?fmtLat(d.max):'\u2014') + '</td><td>' + (d.loss!==undefined?d.loss+'%':'\u2014') + '</td></tr>'; });
  });
  return h + '</table>';
}

// ---- tooltips ----
const nodeTip = document.getElementById('node-tooltip'), edgeTip = document.getElementById('edge-tooltip');
function positionTip(tip, ev) {
  let tx = ev.clientX + 16, ty = ev.clientY - 10;
  const tw = tip.offsetWidth || 280, th = tip.offsetHeight || 200;
  if (tx + tw > innerWidth - 20) tx = ev.clientX - tw - 16;
  if (ty + th > innerHeight - 20) ty = innerHeight - th - 20;
  if (ty < 10) ty = 10;
  tip.style.left = tx + 'px'; tip.style.top = ty + 'px';
}
function showEdgeTooltip(i, j) {
  const ab = fleet.matrix[i]&&fleet.matrix[i][j], ba = fleet.matrix[j]&&fleet.matrix[j][i];
  const a = fleet.nodes[i], b = fleet.nodes[j];
  const diff = ab&&ba ? Math.abs(ab.p50-ba.p50) : 0;
  const pct = ab&&ba&&Math.min(ab.p50,ba.p50)>0 ? ((diff/Math.min(ab.p50,ba.p50))*100).toFixed(1) : '0';
  function block(d, label) {
    if (!d) return '';
    return '<div class="dir-block"><div class="dir-label">' + label + '</div><div class="dir-values">'
      + '<span class="metric-label">p50</span><span class="metric-label">p90</span><span class="metric-label">p99</span><span class="metric-label">p99.9</span><span class="metric-label">max</span><span class="metric-label">loss</span>'
      + '<span class="metric-val highlight">' + fmtLat(d.p50) + '</span><span class="metric-val">' + (d.p90?fmtLat(d.p90):'\u2014') + '</span><span class="metric-val">' + fmtLat(d.p99) + '</span><span class="metric-val">' + (d.p999?fmtLat(d.p999):'\u2014') + '</span><span class="metric-val">' + (d.max?fmtLat(d.max):'\u2014') + '</span><span class="metric-val">' + (d.loss!==undefined?d.loss+'%':'\u2014') + '</span></div></div>';
  }
  let html = '<h4>' + a.ec2_name + ' \u2194 ' + b.ec2_name + '</h4>';
  html += block(ab, '\u2192 ' + a.ec2_name + ' \u2192 ' + b.ec2_name);
  html += block(ba, '\u2190 ' + b.ec2_name + ' \u2192 ' + a.ec2_name);
  if (diff > 0) html += '<div class="asymmetry">Asymmetry: \u0394' + fmtLat(diff) + ' (' + pct + '%)</div>';
  edgeTip.innerHTML = html; edgeTip.classList.add('visible');
}
function positionEdgeTooltip(ev) { positionTip(edgeTip, ev); }
function hideEdgeTooltip() { edgeTip.classList.remove('visible'); }

// ---- picking, hover tooltip, click-to-select ----
const raycaster = new THREE.Raycaster(); const pointer = new THREE.Vector2();
let hoverIdx = -1;
function pick(ev) {
  const rect = renderer.domElement.getBoundingClientRect();
  pointer.x = ((ev.clientX-rect.left)/rect.width)*2 - 1;
  pointer.y = -((ev.clientY-rect.top)/rect.height)*2 + 1;
  raycaster.setFromCamera(pointer, camera);
  const hit = raycaster.intersectObjects(nodeMeshes.filter(m => m.visible), false)[0];
  return hit ? hit.object.userData.idx : -1;
}
function showNodeTip(i, ev) {
  const node = fleet.nodes[i];
  let html = '<h4>' + node.ec2_name + ' \u2192 peers</h4>' + buildPeerTable(i, false);
  html += '<div class="direction" style="margin-top:6px">\u2190 Inbound (peers \u2192 this node):</div>' + buildPeerTable(i, true);
  nodeTip.innerHTML = html; nodeTip.classList.add('visible'); positionTip(nodeTip, ev);
}
renderer.domElement.addEventListener('pointermove', (ev) => {
  const i = pick(ev);
  if (i !== -1) { hoverIdx = i; showNodeTip(i, ev); render(i); }
  else if (hoverIdx !== -1) { hoverIdx = -1; nodeTip.classList.remove('visible'); render(-1); }
});
renderer.domElement.addEventListener('click', (ev) => {
  const i = pick(ev); if (i === -1) return;
  if (selected.has(i)) selected.delete(i); else selected.add(i);   // toggle 1-hop cluster
  render(hoverIdx);
});
document.getElementById('deselect').addEventListener('click', () => { selected.clear(); render(hoverIdx); });
render(-1);

// ---- boundary volumes: PG ⊂ AZ ⊂ VPC, drawn as 3D corner angles only ----
// account + region are NOT drawn as cubes; they appear as sub-rows on the VPC label.
const baseR = Math.max(...fleet.nodes.map(nodeRadius3D));
const groupsOf = (key) => { const g = {}; fleet.nodes.forEach((n, i) => { const v = n[key]; if (!v || v === 'unknown') return; (g[v] = g[v] || []).push(i); }); return g; };
const tightBox = (idxs) => new THREE.Box3().setFromPoints(idxs.map(i => positions[i]));
// largest pad that keeps every same-level box strictly disjoint (cubes never overlap)
function safePad(groupArrays, desired) {
  const tb = groupArrays.map(tightBox); let pad = desired;
  for (let a = 0; a < tb.length; a++) for (let b = a+1; b < tb.length; b++) {
    const A = tb[a], B = tb[b];
    const gap = Math.max(B.min.x-A.max.x, A.min.x-B.max.x, B.min.y-A.max.y, A.min.y-B.max.y, B.min.z-A.max.z, A.min.z-B.max.z);
    if (gap > 0) pad = Math.min(pad, gap/2 - 0.6);
  }
  return Math.max(pad, 0.5);
}
const vpcPad = safePad(Object.values(groupsOf('vpc_id')), baseR + 12);
const azPad  = Math.min(safePad(Object.values(groupsOf('az')), baseR + 6), vpcPad);   // AZ ⊂ VPC
const pgPad  = Math.min(safePad(Object.values(groupsOf('cpg_name')), baseR + 2), azPad); // PG ⊂ AZ

function cornerGeo(box, armFactor) {   // 8 corners x 3 short arms = "3D angles"
  const mn = box.min, mx = box.max, size = box.getSize(new THREE.Vector3());
  const L = Math.max(Math.min(size.x, size.y, size.z) * 0.22 * armFactor, 1.0);
  const p = [];
  for (const x of [mn.x, mx.x]) for (const y of [mn.y, mx.y]) for (const z of [mn.z, mx.z]) {
    const sx = x===mn.x?1:-1, sy = y===mn.y?1:-1, sz = z===mn.z?1:-1;
    p.push(x,y,z, x+sx*L,y,z,  x,y,z, x,y+sy*L,z,  x,y,z, x,y,z+sz*L);
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.Float32BufferAttribute(p, 3));
  return g;
}
const outerBox = new THREE.Box3().setFromPoints(positions);
function drawVol(key, prefix, color, pad, style, withSub) {
  const groups = groupsOf(key);
  Object.keys(groups).forEach(k => {
    const box = tightBox(groups[k]).expandByScalar(pad); outerBox.union(box);
    const size = box.getSize(new THREE.Vector3()), center = box.getCenter(new THREE.Vector3());
    if (style === 'angles') {                 // VPC: corner angles only (arms 3x smaller)
      scene.add(new THREE.LineSegments(cornerGeo(box, 1/3), new THREE.LineBasicMaterial({color, transparent: true, opacity: 0.8})));
    } else {                                   // AZ / PG: translucent solid + faint edges
      const geo = new THREE.BoxGeometry(size.x, size.y, size.z);
      const fill = new THREE.Mesh(geo, new THREE.MeshBasicMaterial({color, transparent: true, opacity: 0.04, depthWrite: false}));
      fill.position.copy(center); fill.renderOrder = -1; scene.add(fill);
      const ed = new THREE.LineSegments(new THREE.EdgesGeometry(geo), new THREE.LineBasicMaterial({color, transparent: true, opacity: 0.22}));
      ed.position.copy(center); scene.add(ed);
    }
    const div = document.createElement('div'); div.className = 'boundary-label';
    div.style.color = '#' + color.toString(16).padStart(6, '0');
    let html = '<div class="bl-main">' + prefix + ': ' + k + '</div>';
    if (withSub) { const n = fleet.nodes[groups[k][0]];
      html += '<div class="bl-sub">Region: ' + n.region + '</div><div class="bl-sub">Account: ' + n.account + '</div>'; }
    div.innerHTML = html;
    const lo = new CSS2DObject(div); lo.center.set(0, 0);   // anchor at the top-left corner
    lo.position.set(box.min.x, box.max.y, box.max.z); scene.add(lo);
  });
}
drawVol('cpg_name', 'PG',  0xf0883e, pgPad,  'solid',  false);
drawVol('az',       'AZ',  0xa371f7, azPad,  'solid',  false);
drawVol('vpc_id',   'VPC', 0x39d353, vpcPad, 'angles', true);

// ---- camera fit (to the outermost boundary) ----
const sphere = outerBox.getBoundingSphere(new THREE.Sphere());
const R = Math.max(sphere.radius, 20);
controls.target.copy(sphere.center);
camera.position.copy(sphere.center).add(new THREE.Vector3(R*1.6, R*1.1, R*1.8));
camera.near = R/100; camera.far = R*100; camera.updateProjectionMatrix();

// ---- panels ----
function median(a){ if(!a.length) return 0; const s=[...a].sort((x,y)=>x-y); return s[Math.floor(s.length/2)]; }
const uniq = (k) => [...new Set(fleet.nodes.map(n=>n[k]))].filter(v=>v && v!=='unknown');
(function buildStats(){
  const regs=uniq('region'), azs=uniq('az'), pgs=uniq('cpg_name'), accts=uniq('account');
  let scope = '';
  if (pgs.length===1) scope += row('Placement Group', pgs[0]); else if (pgs.length>1) scope += row('Placement Groups', pgs.length);
  if (azs.length===1) scope += row('AZ', azs[0]); else if (azs.length>1) scope += row('AZs', azs.join(', '));
  if (regs.length===1) scope += row('Region', regs[0]); else if (regs.length>1) scope += row('Regions', regs.join(', '));
  if (accts.length===1) scope += row('Account', accts[0]); else if (accts.length>1) scope += row('Accounts', accts.length);
  function row(k,v){ return '<div class="stat"><span>'+k+'</span><span class="val">'+v+'</span></div>'; }
  document.getElementById('stats').innerHTML = '<h3>Summary</h3>'
    + row('Nodes', N) + row('Edges', allSig.length)
    + row('p50 range', fmtLat(minP50)+'\u2013'+fmtLat(maxP50))
    + row('p99 range', fmtLat(minP99)+'\u2013'+fmtLat(maxP99))
    + row('Jitter \u03c3', fmtLat(minSig)+'\u2013'+fmtLat(maxSig))
    + row('Median p50', fmtLat(median(allP50)))
    + '<div class="scope">' + scope + '</div>';
})();
(function buildLegend(){
  document.getElementById('legend').innerHTML = '<h3>Legend</h3>'
    + '<div class="row"><div class="swatch" style="background:linear-gradient(to right,#2dd4bf,#fbbf24,#fb7185)"></div><span>Edge colour = jitter \u03c3 ('+fmtLat(minSig)+' \u2192 '+fmtLat(maxSig)+')</span></div>'
    + '<div class="row"><div class="swatch" style="background:linear-gradient(to right,hsl(210,60%,75%),hsl(300,78%,53%),hsl(0,90%,35%))"></div><span>Node colour = capability (weak \u2192 metal / top-net), size = capability</span></div>'
    + '<div class="row"><span>Distance \u221d log(p50 latency)</span></div>'
    + '<div class="row"><span style="color:#79c0ff;font-weight:700">Public IP</span><span style="color:#8b949e">&nbsp;/&nbsp;Private IP</span><span>&nbsp;on each node</span></div>'
    + '<div class="ux-hint"><b>Hover</b> a node \u2014 highlight edges + latency table. <b>Click</b> a node \u2014 select it + its 1-hop neighbours & links (gold glow); click again to deselect. <b>Deselect all</b> restores the full view. <b>Drag</b> = rotate, <b>scroll</b> = zoom, <b>right-drag</b> = pan; drag a panel title to move, click it to fold.</div>';
})();
(function buildITypes(){
  const seen = new Map(); fleet.nodes.forEach(n => { if(!seen.has(n.type)) seen.set(n.type, n); });
  let rows = '';
  for (const [type, n] of seen) {
    const css = capColor(n).getStyle(), r = Math.round(nodeRadius3D(n) * 2.4), fam = type.split('.')[0];
    rows += '<div class="type-row"><div class="type-dot" style="width:'+(r*2)+'px;height:'+(r*2)+'px;background:'+css+';border:2px solid '+css+'"></div>'
      + '<div style="flex:1"><div class="type-name">'+type+'</div><div class="type-specs">'+n.vcpus+'vCPU \u00b7 '+n.mem_gb+'GB \u00b7 '+n.bw_gbps+'Gbps \u00b7 '+n.pps_mpps+'Mpps \u00b7 '+n.enis+' ENIs \u00b7 Nitro '+n.nitro_gen+'</div></div>'
      + '<a href="https://instances.vantage.sh/?selected='+type+'&region='+region+'" target="_blank">specs\u2197</a>'
      + '<a href="https://aws.amazon.com/ec2/instance-types/'+fam+'/" target="_blank">family\u2197</a></div>';
  }
  document.getElementById('itypes').innerHTML = '<h3>Instance Types</h3>' + rows;
})();

// ---- foldable + draggable panels ----
function enhancePanel(el) {
  const h = el.querySelector('h3'); if (!h) return;
  const body = document.createElement('div'); body.className = 'panel-body';
  while (h.nextSibling) body.appendChild(h.nextSibling); el.appendChild(body);
  const caret = document.createElement('span'); caret.className = 'panel-caret'; caret.textContent = '\u25be';
  h.insertBefore(caret, h.firstChild);
  let collapsed = false, dragging = false, moved = false, sx=0, sy=0, ox=0, oy=0;
  h.addEventListener('mousedown', (e) => { dragging=true; moved=false; sx=e.clientX; sy=e.clientY;
    const r=el.getBoundingClientRect(); ox=r.left; oy=r.top; el.style.right='auto'; el.style.bottom='auto'; el.style.left=ox+'px'; el.style.top=oy+'px'; e.preventDefault(); });
  window.addEventListener('mousemove', (e) => { if(!dragging) return; const dx=e.clientX-sx, dy=e.clientY-sy; if(Math.abs(dx)+Math.abs(dy)>3) moved=true; el.style.left=(ox+dx)+'px'; el.style.top=(oy+dy)+'px'; });
  window.addEventListener('mouseup', () => { dragging=false; });
  h.addEventListener('click', () => { if(moved){moved=false;return;} collapsed=!collapsed; body.style.display=collapsed?'none':''; caret.textContent=collapsed?'\u25b8':'\u25be'; });
}
['stats','legend','itypes'].forEach(id => enhancePanel(document.getElementById(id)));

// ---- resize + render loop ----
addEventListener('resize', () => { camera.aspect=innerWidth/innerHeight; camera.updateProjectionMatrix(); renderer.setSize(innerWidth,innerHeight); labelRenderer.setSize(innerWidth,innerHeight); });
(function animate(){ requestAnimationFrame(animate); controls.update(); renderer.render(scene,camera); labelRenderer.render(scene,camera); })();
</script>
</body></html>"""


def main() -> None:
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <results_dir>")
        sys.exit(1)
    results_dir = Path(sys.argv[1])
    if not results_dir.is_dir():
        print(f"Error: {results_dir} is not a directory")
        sys.exit(1)

    fleet = load_fleet_metadata(results_dir)
    node_names, matrix = build_matrix(results_dir, fleet)
    if not matrix:
        print("No matrix results found.")
        sys.exit(1)

    fleet_json = _build_topology_fleet_json(node_names, matrix, fleet)
    html = TEMPLATE.replace("__FLEET_JSON__", fleet_json)
    out = results_dir / "topology_3d.html"
    out.write_text(html)
    print(f"  3D topology: {out}  ({len(node_names)} nodes)")


if __name__ == "__main__":
    main()
