/**
 * awm drawio integration — replaces the stock PostConfig.js.
 *
 * Copyright (c) 2006-2024, JGraph Holdings Ltd
 * Copyright (c) 2006-2024, draw.io AG
 *
 * Everything above the rule below is upstream's stub, verbatim. Below it is
 * the one addition: `ellipticArcEdgeStyle`, a custom edge style the diagrams
 * in this store already depend on — `prokaryotic_metabolism.drawio` carries
 * ~225 edges styled with it. An unregistered edge style is not an error in
 * mxGraph: the edge silently falls back to the default router, so the arcs
 * render straight (or as a plain bezier where the style also sets `curved=1`).
 * That is a rendering regression with nothing logged and no failed save, so
 * this file has to ship with the client, not alongside it.
 *
 * bootstrap.js loads js/PostConfig.js after the app bundle, which is why the
 * style registration lives here rather than in PreConfig.js — mxEdgeStyle and
 * mxStyleRegistry do not exist yet at PreConfig time.
 */
// null'ing of global vars need to be after init.js
window.ICONSEARCH_PATH = null;

// ---------------------------------------------------------------------------
// Elliptic-arc edge style
// ---------------------------------------------------------------------------
// Renders an edge as a circular arc connecting two cell centres.  Only
// intermediate arc points are pushed to the waypoint array; drawio's view
// computes the source/target perimeter-snapped endpoints naturally, so the
// curve flows smoothly from shape boundary onto the arc.
//
// The arc is a true circular arc through both cell centres with sagitta h
// (perpendicular bulge at the chord midpoint).  The bulge is controlled by one
// of three style properties, evaluated in priority order so an explicit sagitta
// can override a baked-in arcRadius without deleting it first:
//
//   1. arcSagitta=PX        — fixed sagitta in px
//   2. arcSagittaFraction=F — sagitta = F × chordLen   (F=0.3 → 30% bulge)
//   3. arcRadius=N          — circle radius R = N × chordLen  (default N=1.5)
//
// arcRadius is a TRUE radius multiple of the node distance: larger N → flatter
// edge, smaller N → tighter curve.  N=1.5 (the default) is a ~9% bulge; N=2 →
// ~6%, N=6 → ~2%, N=12 → ~1%, N≈1 → ~13% bulge, N≈0.7 → ~25%.
//
// Usage (select edge → Edit Style → paste):
//   edgeStyle=ellipticArcEdgeStyle;arcRadius=12;curved=1
//
// Live-tune the default via browser console (no server restart):
//   ELLIPTIC_ARC_CONFIG.radiusFactor = 1     // stronger curve on un-tagged edges
// ---------------------------------------------------------------------------
(function () {
  if (typeof mxEdgeStyle === 'undefined') return;

  // Expose global config for live console tuning (no server restart).
  window.ELLIPTIC_ARC_CONFIG = window.ELLIPTIC_ARC_CONFIG || {};

  // Sagitta (perpendicular bulge) for a circular arc whose radius is
  // `factor × chordLen`.  Larger factor → flatter arc.
  function sagittaFromRadiusFactor(factor, chordLen) {
    var R = Math.max(factor * chordLen, chordLen / 2); // R ≥ chord/2 (semicircle cap)
    return R - Math.sqrt(Math.max(0, R * R - chordLen * chordLen / 4));
  }

  mxEdgeStyle.EllipticArc = function (state, source, target, points, result) {
    if (source == null || target == null) return;

    var style = state.style;

    function terminalCenter(terminal) {
      if (terminal == null) return null;
      var s = state.view.getState(terminal);
      return s ? { x: s.x + s.width / 2, y: s.y + s.height / 2 } : null;
    }

    var srcCell = state.cell.getTerminal(true);
    var tgtCell = state.cell.getTerminal(false);
    var sc = terminalCenter(srcCell);
    var tc = terminalCenter(tgtCell);
    if (!sc || !tc) return;

    var dx = tc.x - sc.x;
    var dy = tc.y - sc.y;
    var chordLen = Math.sqrt(dx * dx + dy * dy);
    if (chordLen < 1) return;

    // --- target sagitta h (px) ---
    // priority: arcSagitta > arcSagittaFraction > arcRadius > config > default
    var cfg = window.ELLIPTIC_ARC_CONFIG || {};
    var fixedH = style['arcSagitta'] != null ? parseFloat(style['arcSagitta']) : undefined;
    var frac   = style['arcSagittaFraction'] != null ? parseFloat(style['arcSagittaFraction']) : undefined;
    var radiusFactor = style['arcRadius'] != null ? parseFloat(style['arcRadius']) : undefined;

    var h;
    if (fixedH !== undefined && !isNaN(fixedH)) {
      h = fixedH;
    } else if (frac !== undefined && !isNaN(frac)) {
      h = frac * chordLen;
    } else if (radiusFactor !== undefined && !isNaN(radiusFactor)) {
      h = sagittaFromRadiusFactor(radiusFactor, chordLen);
    } else if (cfg.sagittaPx != null) {
      h = cfg.sagittaPx;
    } else if (cfg.sagittaFraction != null) {
      h = cfg.sagittaFraction * chordLen;
    } else if (cfg.radiusFactor != null) {
      h = sagittaFromRadiusFactor(cfg.radiusFactor, chordLen);
    } else {
      h = sagittaFromRadiusFactor(1.5, chordLen); // default arcRadius=1.5
    }

    if (!(h > 0)) return; // zero/negative/NaN sagitta → straight line

    // True circular arc through both centres with this sagitta:
    //   R = circumradius, centre sits at distance d = R − h from the chord
    //   midpoint on the side OPPOSITE the bulge.
    var R = (chordLen * chordLen / 4 + h * h) / (2 * h);
    var d = R - h;

    // Perpendicular unit vector (90° CW from chord) — bulge direction.
    var perpX =  dy / chordLen;
    var perpY = -dx / chordLen;

    var mx = (sc.x + tc.x) / 2;
    var my = (sc.y + tc.y) / 2;
    var cx = mx - d * perpX;
    var cy = my - d * perpY;

    // Angular positions of cell centres around arc centre.
    var a1 = Math.atan2(sc.y - cy, sc.x - cx);
    var a2 = Math.atan2(tc.y - cy, tc.x - cx);

    // Shortest (minor) arc.
    var da = a2 - a1;
    if (da > Math.PI) da -= 2 * Math.PI;
    else if (da < -Math.PI) da += 2 * Math.PI;

    // Sample intermediate arc points (skip source/target positions).
    // drawio's view adds the perimeter-snapped endpoints, so the spline
    // transitions smoothly from shape boundary onto the arc.
    var steps = Math.max(16, Math.ceil(Math.abs(da) / 0.1));
    for (var i = 1; i < steps; i++) {
      var a = a1 + da * (i / steps);
      result.push(new mxPoint(cx + R * Math.cos(a), cy + R * Math.sin(a)));
    }
  };

  mxStyleRegistry.putValue(
    'ellipticArcEdgeStyle', mxEdgeStyle.EllipticArc
  );

  if (window.ui && window.ui.editor && window.ui.editor.graph) {
    window.ui.editor.graph.refresh();
  }
})();
