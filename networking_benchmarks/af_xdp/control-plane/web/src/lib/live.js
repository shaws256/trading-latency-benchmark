// live.js — Live control-plane client. Consumes the backend SSE stream
// (/api/events: snapshot + node/edge/job deltas), keeps the fleet {nodes,edges}
// state, and ADAPTS it into the afxdp.topology/v1 `fleet.json` schema the
// existing 2D/3D viz renders — for a chosen kind (ucast|mcast) + variation.
// Also exposes run-campaign + ad-hoc command POST helpers.

export function createLive({ onUpdate, onJob } = {}) {
  let nodes = [];
  const edges = new Map(); // key: kind|variation|src|dst -> edge
  let es = null;

  const key = (e) => `${e.kind}|${e.variation}|${e.src}|${e.dst}`;

  function apply(msg) {
    switch (msg.type) {
      case 'snapshot':
        nodes = msg.data.nodes || [];
        edges.clear();
        (msg.data.edges || []).forEach((e) => edges.set(key(e), e));
        onUpdate && onUpdate();
        break;
      case 'node': {
        const n = msg.data;
        const i = nodes.findIndex((x) => x.instance_id === n.instance_id);
        if (i >= 0) nodes[i] = n; else nodes.push(n);
        onUpdate && onUpdate();
        break;
      }
      case 'edge':
        edges.set(key(msg.data), msg.data);
        onUpdate && onUpdate();
        break;
      case 'job':
        onJob && onJob(msg.data);
        break;
    }
  }

  function connect() {
    es = new EventSource('/api/events');
    es.onmessage = (ev) => { try { apply(JSON.parse(ev.data)); } catch (_) { /* ignore */ } };
    // EventSource auto-reconnects on error; nothing to do.
  }
  connect();

  return {
    close() { if (es) es.close(); },
    nodes: () => nodes,

    // Live counts for the control panel's real-time readout.
    stats() {
      let online = 0;
      for (const n of nodes) if (n.online) online++;
      return { nodes: nodes.length, online, edges: edges.size };
    },

    // Distinct {kind,variation} combos present in the data (drives the selector).
    combos() {
      const s = new Set();
      for (const e of edges.values()) s.add(`${e.kind}|${e.variation}`);
      return [...s].sort().map((x) => { const [kind, variation] = x.split('|'); return { kind, variation }; });
    },

    // Adapt current state into the fleet.json schema for one kind+variation.
    toFleet(kind, variation) {
      const order = [...nodes].sort((a, b) => (a.private_ip || '').localeCompare(b.private_ip || ''));
      const idx = new Map(order.map((n, i) => [n.private_ip, i]));
      const fnodes = order.map((n, i) => ({
        index: i,
        name: n.private_ip,
        ec2_name: n.role || n.instance_id || n.private_ip,
        type: n.instance_type || 'unknown',
        private_ip: n.private_ip,
        public_ip: n.public_ip || '',
        az: n.az || 'unknown',
        region: n.region || 'us-east-1',
        cpg_name: n.placement_group || 'unknown',
        pg_type: n.placement_group ? 'cluster' : 'unknown',
        role: n.role || '',
        online: !!n.online,
        metal: false,
      }));
      const N = order.length;
      const matrix = Array.from({ length: N }, () => Array(N).fill(null));
      for (const e of edges.values()) {
        if (e.kind !== kind || e.variation !== variation) continue;
        const i = idx.get(e.src), j = idx.get(e.dst);
        if (i == null || j == null) continue;
        const m = e.metrics.service_rtt_us;
        matrix[i][j] = {
          p50: m.p50, p90: m.p90, p99: m.p99, p999: m.p999, max: m.max,
          loss: +(e.metrics.loss_pct || 0).toFixed(3),
        };
      }
      return {
        schema: 'afxdp.topology/v1',
        region: (fnodes[0] && fnodes[0].region) || 'us-east-1',
        generated_at: new Date().toISOString(),
        nodes: fnodes, matrix,
      };
    },
  };
}

export async function runCampaign(body) {
  const res = await fetch('/api/run', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  return res.json();
}

export async function sendCommand(instanceId, command) {
  const res = await fetch('/api/cmd', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ instance_id: instanceId, command }),
  });
  return res.json();
}
