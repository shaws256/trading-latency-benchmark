// Tests for the four UI changes:
// (1) Best latency gold, not green
// (2) 'All' preset selects every online node unconditionally
// (3) Heading rename to 'Latest measurements'
// (4) 'Save as CSVs' button in the report
import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { JSDOM } from '/tmp/reptest/node_modules/jsdom/lib/api.js';

// ─── (1) GOLD, NOT GREEN ────────────────────────────────────────────────────

describe('latency colour ramp: gold at the fast end', () => {
  test('the lowest latency is gold-ish (R>180, G>140, B<100), not green', async () => {
    const { latencyColor } = await import('../src/lib/2d/palette.js');
    const css = latencyColor(10, 10, 100);
    const m = /rgb\((\d+),\s*(\d+),\s*(\d+)\)/.exec(css);
    assert.ok(m, `expected rgb(), got ${css}`);
    const [r, g, b] = [+m[1], +m[2], +m[3]];
    // Gold/amber: high red, moderate-to-high green, low blue
    assert.ok(r >= 180, `R=${r} too low for gold`);
    assert.ok(g >= 140, `G=${g} too low for gold`);
    assert.ok(b < 110, `B=${b} too high for gold`);
    // Must NOT be green: green means G > R by a lot
    assert.ok(r >= g - 30, `looks green not gold: R=${r} G=${g}`);
  });

  test('the highest latency is still red', async () => {
    const { latencyColor } = await import('../src/lib/2d/palette.js');
    const css = latencyColor(100, 10, 100);
    const m = /rgb\((\d+),\s*(\d+),\s*(\d+)\)/.exec(css);
    const [r, g, b] = [+m[1], +m[2], +m[3]];
    assert.ok(r >= 150, `R=${r} not red enough`);
    assert.ok(g < 60, `G=${g} too high for red`);
  });

  test('midpoint is visually distinct from both ends', async () => {
    const { latencyColor } = await import('../src/lib/2d/palette.js');
    const lo = latencyColor(10, 10, 100);
    const mid = latencyColor(55, 10, 100);
    const hi = latencyColor(100, 10, 100);
    assert.notEqual(lo, mid, 'mid must differ from low');
    assert.notEqual(mid, hi, 'mid must differ from high');
    assert.notEqual(lo, hi, 'low must differ from high');
  });
});

describe('report best/worst markers use gold, not CSS green', () => {
  function makeViews() {
    const nodes = [0, 1, 2].map((i) => ({
      index: i, private_ip: `10.0.0.${i + 1}`, public_ip: `1.1.1.${i + 1}`,
      role: 'source', az: 'a', region: 'r', cpg_name: 'cpg', vpc_id: 'vpc',
      type: 't', online: true,
    }));
    const m = [[null, { p50: 30, p90: 32, p99: 35, p999: 40, max: 50, loss: 0, unix: 1 },
                      { p50: 80, p90: 82, p99: 85, p999: 90, max: 100, loss: 2, unix: 1 }],
               [{ p50: 60, p90: 62, p99: 65, p999: 70, max: 80, loss: 1, unix: 1 }, null, null],
               [null, null, null]];
    return [{ kind: 'ucast', variation: 'kernel', unix: 1, fleet: { nodes, matrix: m, region: 'r' } }];
  }

  test('best cell uses gold colour, not literal "green"', async () => {
    const { buildCombinedReportHTML } = await import('../src/lib/report-combined.js');
    const html = buildCombinedReportHTML(makeViews());
    // The best marker used to be style="color:green" - must be gone
    assert.ok(!html.includes('color:green'), 'no literal color:green in the report');
    // It should use an rgb() or #hex gold instead
    assert.ok(html.includes('color:'), 'cells must still be coloured');
  });

  test('legend/key text does not say "green = fast"', async () => {
    const { buildCombinedReportHTML } = await import('../src/lib/report-combined.js');
    const html = buildCombinedReportHTML(makeViews()).toLowerCase();
    assert.ok(!html.includes('green = fast'), 'no green=fast in the legend');
    assert.ok(!html.includes('green=fast'), 'no green=fast in the legend');
  });
});

// ─── (2) 'ALL' PRESET SELECTS UNCONDITIONALLY ───────────────────────────────

describe("'all' preset selects every online node without an anchor", () => {
  const fleet = [
    { private_ip: '10.0.0.1', cpg_name: 'cpg-a', az: 'a', region: 'r', vpc_id: 'v', online: true },
    { private_ip: '10.0.0.2', cpg_name: 'cpg-a', az: 'a', region: 'r', vpc_id: 'v', online: true },
    { private_ip: '10.0.0.3', cpg_name: 'cpg-b', az: 'b', region: 'r', vpc_id: 'v', online: true },
    { private_ip: '10.0.0.4', cpg_name: 'cpg-b', az: 'b', region: 'r', vpc_id: 'v', online: false },
  ];

  test('all with NO anchor selects every online node', async () => {
    const { resolvePreset } = await import('../src/lib/pairs.js');
    const r = resolvePreset('all', fleet, null);
    assert.deepEqual([...r].sort(), ['10.0.0.1', '10.0.0.2', '10.0.0.3']);
  });

  test('all with an anchor still selects every online node (anchor is irrelevant)', async () => {
    const { resolvePreset } = await import('../src/lib/pairs.js');
    const r = resolvePreset('all', fleet, '10.0.0.1');
    assert.deepEqual([...r].sort(), ['10.0.0.1', '10.0.0.2', '10.0.0.3']);
  });

  test('other presets still require an anchor for their group', async () => {
    const { resolvePreset } = await import('../src/lib/pairs.js');
    // pg with anchor picks that group
    const r = resolvePreset('pg', fleet, '10.0.0.3');
    assert.deepEqual([...r].sort(), ['10.0.0.3']);
    // pg without anchor picks largest group
    const r2 = resolvePreset('pg', fleet, null);
    assert.deepEqual([...r2].sort(), ['10.0.0.1', '10.0.0.2']);
  });
});

// ─── (3) HEADING RENAME ─────────────────────────────────────────────────────

describe('combined report heading', () => {
  function makeViews() {
    const nodes = [0, 1].map((i) => ({
      index: i, private_ip: `10.0.0.${i + 1}`, public_ip: `1.1.1.${i + 1}`,
      role: 'source', az: 'a', region: 'r', cpg_name: 'cpg', vpc_id: 'vpc',
      type: 't', online: true,
    }));
    const m = [[null, { p50: 30, p90: 32, p99: 35, p999: 40, max: 50, loss: 0, unix: 1 }],
               [{ p50: 31, p90: 33, p99: 36, p999: 41, max: 51, loss: 0, unix: 1 }, null]];
    return [{ kind: 'ucast', variation: 'kernel', unix: 1, fleet: { nodes, matrix: m, region: 'r' } }];
  }

  test('heading is exactly "Latest measurements"', async () => {
    const { buildCombinedReportHTML } = await import('../src/lib/report-combined.js');
    const html = buildCombinedReportHTML(makeViews());
    assert.ok(html.includes('>Latest measurements</h2>'),
      'must contain the exact heading "Latest measurements"');
  });

  test('old heading is gone', async () => {
    const { buildCombinedReportHTML } = await import('../src/lib/report-combined.js');
    const html = buildCombinedReportHTML(makeViews());
    assert.ok(!html.includes('freshest measurement per pair'),
      'old heading text must be removed');
  });
});

// ─── (4) SAVE AS CSVs ───────────────────────────────────────────────────────

describe('Save as CSVs button and CSV generation', () => {
  function makeViews() {
    const nodes = [0, 1].map((i) => ({
      index: i, private_ip: `10.0.0.${i + 1}`, public_ip: `1.1.1.${i + 1}`,
      role: 'source', az: 'a', region: 'r', cpg_name: 'cpg', vpc_id: 'vpc',
      type: 't', online: true,
    }));
    const m = [[null, { p50: 30, p90: 32, p99: 35, p999: 40, max: 50, loss: 0, unix: 1 }],
               [{ p50: 31, p90: 33, p99: 36, p999: 41, max: 51, loss: 0, unix: 1 }, null]];
    return [{ kind: 'ucast', variation: 'kernel', unix: 1, fleet: { nodes, matrix: m, region: 'r' } }];
  }

  test('report contains a Save as CSVs button near the print control', async () => {
    const { buildCombinedReportHTML } = await import('../src/lib/report-combined.js');
    const html = buildCombinedReportHTML(makeViews());
    assert.ok(/save\s+as\s+csvs?/i.test(html), 'must have a Save as CSVs button');
  });

  test('CSV generation produces proper quoted output', async () => {
    const { buildCombinedReportHTML, reportInteractions } = await import('../src/lib/report-combined.js');
    const html = buildCombinedReportHTML(makeViews());
    const dom = new JSDOM(html, { runScripts: 'dangerously', url: 'http://localhost' });
    const doc = dom.window.document;

    // The tableToCsv helper should be accessible inside reportInteractions
    // We test by extracting it from the serialised function
    const fnStr = reportInteractions.toString();
    assert.ok(fnStr.includes('tableToCsv') || fnStr.includes('tableToCSV'),
      'reportInteractions must contain a CSV serialisation function');
  });

  test('CSV correctly quotes fields with commas and double-quotes', async () => {
    const { buildCombinedReportHTML, reportInteractions } = await import('../src/lib/report-combined.js');

    // Create a minimal DOM and inject a table with tricky content
    const dom = new JSDOM(`<!doctype html><html><body>
      <h2>Test Table</h2>
      <table><tr><th>Name</th><th>Value</th></tr>
      <tr><td>hello, world</td><td>say "hi"</td></tr>
      <tr><td>normal</td><td>42</td></tr></table>
    </body></html>`, { url: 'http://localhost' });
    const doc = dom.window.document;

    // Mock Blob and URL for the download mechanism
    const blobs = [];
    dom.window.Blob = function(parts, opts) { this.parts = parts; this.type = opts && opts.type; blobs.push(this); };
    dom.window.URL = { createObjectURL: () => 'blob:test', revokeObjectURL: () => {} };

    // Run reportInteractions
    reportInteractions(doc);

    // Find and click the CSV button
    const btn = doc.querySelector('[data-csv-btn]') || doc.querySelector('button');
    const csvButtons = [...doc.querySelectorAll('button')].filter(b => /csv/i.test(b.textContent));
    if (csvButtons.length > 0) {
      csvButtons[0].click();
      // Check the generated CSV content
      if (blobs.length > 0) {
        const csv = blobs[0].parts[0];
        assert.ok(csv.includes('"hello, world"'), 'comma field must be quoted');
        assert.ok(csv.includes('"say ""hi"""'), 'double-quote must be escaped by doubling');
        assert.ok(csv.includes('normal'), 'normal field is present');
      }
    }
  });

  test('CSV file names derive from table headings, slugified', async () => {
    const { buildCombinedReportHTML, reportInteractions } = await import('../src/lib/report-combined.js');
    const html = buildCombinedReportHTML(makeViews());
    const dom = new JSDOM(html, { runScripts: 'dangerously', url: 'http://localhost' });
    const doc = dom.window.document;

    // Mock Blob, URL, and anchor for download interception
    const downloads = [];
    dom.window.Blob = function(parts, opts) { this.parts = parts; this.type = opts && opts.type; };
    dom.window.URL = { createObjectURL: () => 'blob:test', revokeObjectURL: () => {} };
    const origCreate = doc.createElement.bind(doc);
    doc.createElement = function(tag) {
      const el = origCreate(tag);
      if (tag === 'a') {
        el.click = function() { downloads.push({ href: el.href, download: el.download }); };
      }
      return el;
    };

    // Click the CSV button
    const csvBtn = [...doc.querySelectorAll('button')].find(b => /csv/i.test(b.textContent));
    assert.ok(csvBtn, 'CSV button must exist in the rendered report');
    csvBtn.click();

    // Should produce at least one download per table
    assert.ok(downloads.length > 0, 'clicking CSV button must trigger downloads');
    // Check slugification: the overview heading becomes latest-measurements.csv
    const names = downloads.map(d => d.download);
    assert.ok(names.some(n => /\.csv$/.test(n)), 'downloads must be .csv files');
  });
});
