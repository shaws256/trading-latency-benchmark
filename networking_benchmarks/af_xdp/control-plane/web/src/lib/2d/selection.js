// 2d/selection.js — hover/click selection state applied to nodes/edges/labels.
// Edge labels:
//   - appear (opacity:1) for edges touching the currently hovered node
//   - stay visible (pinned) for edges touching any clicked/selected node
//   - hidden otherwise (opacity:0)

function neighborsOf(ctx, set) {
  const { N, matrix } = ctx;
  const nb = new Set();
  set.forEach(i => { nb.add(i); for (let j = 0; j < N; j++) if (j !== i && ((matrix[i] && matrix[i][j]) || (matrix[j] && matrix[j][i]))) nb.add(j); });
  return nb;
}

// hover: node index currently hovered, or -1.
export function applySel(ctx, hover) {
  const { selected, nodeEls, edgeElements, edgeLabelEls } = ctx;
  const hasSel = selected.size > 0, vis = hasSel ? neighborsOf(ctx, selected) : null;
  nodeEls.forEach((el, i) => { if (!el) return; el.style.display = (!hasSel || vis.has(i)) ? '' : 'none'; el.classList.toggle('selected', selected.has(i)); });
  edgeElements.forEach(({ line, i, j }) => {
    const touchSel = hasSel && (selected.has(i) || selected.has(j)), touchHover = hover !== -1 && (i === hover || j === hover);
    const baseOp = line.dataset.baseOp || '0.4';
    line.classList.remove('highlighted', 'dimmed');
    if (hasSel || hover !== -1) {
      const active = touchSel || touchHover;
      if (active) {
        line.classList.add('highlighted');
        line.style.opacity = '';
      } else {
        line.classList.add('dimmed');
        line.style.opacity = '';
      }
    } else {
      line.style.opacity = baseOp;
    }
  });
  for (const it of edgeLabelEls) {
    const pinned  = selected.has(it.i) || selected.has(it.j);   // stays after click
    const hovered = hover !== -1 && (it.i === hover || it.j === hover); // transient on hover
    const show = pinned || hovered;
    it.el.style.opacity = show ? '1' : '0';
    it.el.style.pointerEvents = show ? 'auto' : 'none';
  }
}
