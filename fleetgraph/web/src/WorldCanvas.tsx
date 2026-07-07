import { useEffect, useRef } from "react";
import type { Frame, Scenario } from "./api";

export type CarStatus = "normal" | "blind" | "aware_radio" | "braking";

type Props = {
  scenario: Scenario;
  frame: Frame | null;
  carStatus: Record<string, CarStatus>;
  onPlace?: (x: number, y: number, kind?: string) => void;   // click-place (kind from armed tool)
  onDropActor?: (x: number, y: number, kind: string) => void; // drag-and-drop from the palette
};

const COLORS: Record<string, string> = {
  ambulance: "#ff5d6c", car: "#5b8cff", truck: "#8fa0c8", pedestrian: "#ffcf5c", obstacle: "#6b7794",
};

export function WorldCanvas({ scenario, frame, carStatus, onPlace, onDropActor }: Props) {
  const ref = useRef<HTMLCanvasElement>(null);
  const st = useRef({ scenario, frame, carStatus });
  st.current = { scenario, frame, carStatus };

  useEffect(() => {
    const cv = ref.current;
    if (!cv) return;
    let raf = 0;

    const draw = (now: number) => {
      const { scenario, frame, carStatus } = st.current;
      const world = scenario.world;
      const lanes = world.lanes || [];
      const lw = world.lane_width || 5;
      const dpr = window.devicePixelRatio || 1;
      const W = cv.clientWidth, H = cv.clientHeight;
      if (cv.width !== W * dpr || cv.height !== H * dpr) { cv.width = W * dpr; cv.height = H * dpr; }
      const ctx = cv.getContext("2d")!;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, W, H);

      const pad = 20;
      const s = Math.min((W - 2 * pad) / world.width, (H - 2 * pad) / world.height);
      const ox = (W - world.width * s) / 2, oy = (H - world.height * s) / 2;
      const px = (x: number) => ox + x * s, py = (y: number) => oy + y * s;

      // grass / verge
      ctx.fillStyle = "#0c1122"; ctx.fillRect(0, 0, W, H);

      // --- road geometry: a grid network OR the straight-lane road ---
      let roadTop: number, roadBot: number;
      if (world.grid) {
        const g = world.grid, hr = g.lanes_each_way * g.lane_width;
        roadTop = Math.min(...g.h_roads) - hr; roadBot = Math.max(...g.h_roads) + hr;
        drawGrid(ctx, g, world.width, world.height, px, py, s);
      } else {
        const ys = lanes.map((l) => l.y);
        roadTop = Math.min(...ys) - lw / 2; roadBot = Math.max(...ys) + lw / 2;
        ctx.fillStyle = "#1a1f2b";
        ctx.fillRect(px(0), py(roadTop), world.width * s, (roadBot - roadTop) * s);
        for (const l of lanes) if (l.kind === "shoulder") {
          ctx.fillStyle = "#141926";
          ctx.fillRect(px(0), py(l.y - lw / 2), world.width * s, lw * s);
        }
        ctx.strokeStyle = "rgba(220,225,240,0.5)"; ctx.lineWidth = 1.5;
        for (const yy of [roadTop, roadBot]) {
          ctx.beginPath(); ctx.moveTo(px(0), py(yy)); ctx.lineTo(px(world.width), py(yy)); ctx.stroke();
        }
        for (let i = 0; i < lanes.length - 1; i++) {
          const yb = (lanes[i].y + lanes[i + 1].y) / 2;
          const opposing = lanes[i].direction !== lanes[i + 1].direction;
          const shoulderEdge = lanes[i].kind !== lanes[i + 1].kind;
          if (opposing) {
            ctx.strokeStyle = "rgba(240,205,90,0.6)"; ctx.lineWidth = 1.2; ctx.setLineDash([]);
            for (const d of [-1.2, 1.2]) {
              ctx.beginPath(); ctx.moveTo(px(0), py(yb) + d); ctx.lineTo(px(world.width), py(yb) + d); ctx.stroke();
            }
          } else if (shoulderEdge) {
            ctx.strokeStyle = "rgba(220,225,240,0.35)"; ctx.lineWidth = 1; ctx.setLineDash([]);
            ctx.beginPath(); ctx.moveTo(px(0), py(yb)); ctx.lineTo(px(world.width), py(yb)); ctx.stroke();
          } else {
            ctx.strokeStyle = "rgba(220,225,240,0.4)"; ctx.lineWidth = 1; ctx.setLineDash([14, 12]);
            ctx.beginPath(); ctx.moveTo(px(0), py(yb)); ctx.lineTo(px(world.width), py(yb)); ctx.stroke();
            ctx.setLineDash([]);
          }
        }
        for (const l of lanes) if (l.kind === "drive") {
          ctx.strokeStyle = "rgba(180,195,230,0.18)"; ctx.lineWidth = 1.5;
          for (let x = 12; x < world.width; x += 28) {
            const cxp = px(x), cyp = py(l.y), a = 4 * s * l.direction;
            ctx.beginPath();
            ctx.moveTo(cxp - a, cyp - 3); ctx.lineTo(cxp, cyp); ctx.lineTo(cxp - a, cyp + 3); ctx.stroke();
          }
        }
      }

      // traffic signals: a stop line across the road + a red/green light housing on the verge
      const PHASE_COL: Record<string, string> = { red: "#ff5d6c", yellow: "#ffcf5c", green: "#46d17f" };
      const signals = frame ? (frame.signals || [])
        : scenario.actors.filter((a) => a.kind === "signal").map((a) => ({
            id: a.id, x: a.start_x ?? (a.path?.[0]?.[0] ?? 0),
            y: a.start_y != null ? a.start_y
               : (a.lane != null && lanes[a.lane] ? lanes[a.lane].y : (a.path?.[0]?.[1] ?? roadTop)),
            phase: "red" as const, red: true }));
      for (const sig of signals) {
        const phase = (sig as any).phase || (sig.red ? "red" : "green");
        const col = PHASE_COL[phase] || "#46d17f";
        const lx = px(sig.x);
        let hx: number, hy: number;              // light-housing position
        if (world.grid) {
          // compact light + short stop mark at the junction point
          const gy = py(sig.y);
          ctx.fillStyle = col; ctx.beginPath(); ctx.arc(lx, gy, 3.5, 0, Math.PI * 2); ctx.fill();
          ctx.strokeStyle = phase === "green" ? "rgba(70,209,127,0.5)" : `${col}cc`; ctx.lineWidth = 2;
          ctx.beginPath(); ctx.moveTo(lx, gy - 7); ctx.lineTo(lx, gy + 7); ctx.stroke();
          hx = lx + 10; hy = gy - 14;
        } else {
          ctx.strokeStyle = phase === "green" ? "rgba(70,209,127,0.5)" : `${col}dd`; ctx.lineWidth = 2;
          ctx.beginPath(); ctx.moveTo(lx, py(roadTop)); ctx.lineTo(lx, py(roadBot)); ctx.stroke();
          hx = lx; hy = py(roadTop) - 24;
        }
        // 3-light housing with the active lamp lit
        ctx.fillStyle = "#0a0f1e"; roundRect(ctx, hx - 5, hy - 12, 10, 24, 3);
        ctx.strokeStyle = "rgba(180,195,230,0.4)"; ctx.lineWidth = 1; roundRectStroke(ctx, hx - 5, hy - 12, 10, 24, 3);
        for (const [i, p] of ["red", "yellow", "green"].entries()) {
          ctx.fillStyle = p === phase ? PHASE_COL[p] : "rgba(255,255,255,0.12)";
          ctx.beginPath(); ctx.arc(hx, hy - 6 + i * 6, 2.8, 0, Math.PI * 2); ctx.fill();
        }
      }

      const fog = frame ? frame.fog : world.fog_density;
      if (fog > 0) { ctx.fillStyle = `rgba(200,210,230,${0.05 + fog * 0.26})`; ctx.fillRect(0, 0, W, H); }

      if (frame) for (const j of frame.jammers) {
        const g = ctx.createRadialGradient(px(j.x), py(j.y), 2, px(j.x), py(j.y), j.r * s);
        g.addColorStop(0, "rgba(255,93,108,0.3)"); g.addColorStop(1, "rgba(255,93,108,0)");
        ctx.fillStyle = g; ctx.beginPath(); ctx.arc(px(j.x), py(j.y), j.r * s, 0, Math.PI * 2); ctx.fill();
      }

      // actors: from the running frame, or a static preview (start positions) before Run
      const actors = frame ? frame.actors : previewActors(scenario);
      const posOf: Record<string, { x: number; y: number }> = {};
      for (const a of actors) posOf[a.id] = { x: a.x, y: a.y };
      const laneDir = (id: string) => {
        const m = scenario.actors.find((x) => x.id === id);
        return m?.lane != null && lanes[m.lane] ? lanes[m.lane].direction : 1;
      };

      // lidar reach
      const effRange = world.lidar_range_m * Math.exp(-2.3 * fog);
      for (const a of actors) {
        const m = scenario.actors.find((x) => x.id === a.id);
        if (!m?.has_radio || (a.kind !== "car" && a.kind !== "truck")) continue;
        ctx.strokeStyle = "rgba(91,140,255,0.14)"; ctx.setLineDash([3, 4]);
        ctx.beginPath(); ctx.arc(px(a.x), py(a.y), effRange * s, 0, Math.PI * 2); ctx.stroke(); ctx.setLineDash([]);
      }

      // radio links + moving pulse
      const tt = (now % 1100) / 1100;
      if (frame) for (const l of frame.links) {
        const p = posOf[l.from], q = posOf[l.to]; if (!p || !q) continue;
        if (l.delivered) {
          ctx.strokeStyle = "rgba(70,209,127,0.5)"; ctx.lineWidth = 1.6;
          ctx.beginPath(); ctx.moveTo(px(p.x), py(p.y)); ctx.lineTo(px(q.x), py(q.y)); ctx.stroke();
          const mx = p.x + (q.x - p.x) * tt, my = p.y + (q.y - p.y) * tt;
          ctx.fillStyle = "#7bffb0"; ctx.beginPath(); ctx.arc(px(mx), py(my), 2.6, 0, Math.PI * 2); ctx.fill();
        }
      }

      // actors (with de-collision label placement)
      const placed: { x: number; y1: number; y2: number }[] = [];
      const drawLabel = (text: string, cx: number, topY: number, color: string) => {
        ctx.font = "10px -apple-system, sans-serif";
        const w = ctx.measureText(text).width + 8; let y = topY;
        for (let guard = 0; guard < 8; guard++) {
          const clash = placed.some((b) => Math.abs(b.x - cx) < (w + 40) / 2 && Math.abs((y) - b.y1) < 13);
          if (!clash) break; y -= 13;
        }
        placed.push({ x: cx, y1: y, y2: y + 13 });
        const x = cx - w / 2;
        ctx.fillStyle = "rgba(9,13,26,0.8)"; ctx.beginPath();
        const r = 3; ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + 13, r);
        ctx.arcTo(x + w, y + 13, x, y + 13, r); ctx.arcTo(x, y + 13, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.fill();
        ctx.fillStyle = color; ctx.textAlign = "center"; ctx.textBaseline = "middle";
        ctx.fillText(text, cx, y + 6.5); ctx.textAlign = "start"; ctx.textBaseline = "alphabetic";
      };

      for (const a of actors) {
        const m = scenario.actors.find((x) => x.id === a.id);
        const isAmb = (m?.label || "").toLowerCase().includes("ambulance");
        const cx = px(a.x), cy = py(a.y);
        const braked = (a as any).braked as boolean | undefined;

        if (a.kind === "pedestrian" && !((m?.size?.[0] ?? 0) > 2)) {
          const r = Math.max(5, a.size[0] * s * 0.7);
          ctx.fillStyle = a.is_hazard ? "#ffcf5c" : "#cbd5f5";
          ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2); ctx.fill();
          if (a.is_hazard) {
            ctx.strokeStyle = `rgba(255,207,92,${0.4 + 0.4 * Math.abs(Math.sin(now / 400))})`;
            ctx.lineWidth = 2.5; ctx.beginPath(); ctx.arc(cx, cy, r + 5, 0, Math.PI * 2); ctx.stroke();
          }
          drawLabel(a.label || "hazard", cx, cy + r + 6, "#ffcf5c");
          continue;
        }
        if (a.kind === "obstacle") {
          const w = a.size[0] * s, h = a.size[1] * s;
          ctx.fillStyle = COLORS.obstacle; ctx.fillRect(cx - w / 2, cy - h / 2, w, h);
          drawLabel(a.label || "obstacle", cx, cy - h / 2 - 4, "#a9b4d0"); continue;
        }
        // car / truck / stalled-car — rotated to its heading (works for grid turns AND straight roads)
        const angle = (a as any).angle != null ? (a as any).angle : (laneDir(a.id) < 0 ? Math.PI : 0);
        const len = Math.max(10, a.size[0] * s), wid = Math.max(6, a.size[1] * s);
        const baseColor = a.kind === "truck" ? COLORS.truck : (isAmb ? COLORS.ambulance
                          : a.is_hazard ? "#ffcf5c" : COLORS.car);
        ctx.save(); ctx.translate(cx, cy); ctx.rotate(angle);
        ctx.fillStyle = baseColor;
        roundRect(ctx, -len / 2, -wid / 2, len, wid, 2);
        ctx.fillStyle = "rgba(255,255,255,0.6)";               // windshield at the front (+x local)
        ctx.fillRect(len / 2 - 4, -wid / 2 + 1.5, 3, wid - 3);
        ctx.restore();
        // status rings
        if (braked) {
          ctx.strokeStyle = "#ff5d6c"; ctx.lineWidth = 3;
          ctx.beginPath(); ctx.arc(cx, cy, Math.max(len, wid) / 2 + 5, 0, Math.PI * 2); ctx.stroke();
          drawLabel(`${a.label || a.id} · STOPPED`, cx, cy - wid / 2 - 4, "#ff9aa4");
        } else if ((a as any).waiting) {
          ctx.strokeStyle = "#ffcf5c"; ctx.lineWidth = 2.2;
          roundRectStroke(ctx, cx - len / 2 - 3, cy - wid / 2 - 3, len + 6, wid + 6, 3);
          drawLabel(`${a.label || a.id} · waiting`, cx, cy - wid / 2 - 4, "#ffe08a");
        } else {
          if (carStatus[a.id] === "aware_radio") {
            ctx.strokeStyle = "#46d17f"; ctx.lineWidth = 2.2;
            roundRectStroke(ctx, cx - len / 2 - 3, cy - wid / 2 - 3, len + 6, wid + 6, 3);
          }
          drawLabel(a.label || a.id, cx, cy - wid / 2 - 4, "#c7d2f5");
        }
      }
      raf = requestAnimationFrame(draw);
    };
    raf = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(raf);
  }, []);

  const toWorld = (clientX: number, clientY: number) => {
    const cv = ref.current!; const rect = cv.getBoundingClientRect();
    const world = scenario.world;
    const W = cv.clientWidth, H = cv.clientHeight, pad = 20;
    const s = Math.min((W - 2 * pad) / world.width, (H - 2 * pad) / world.height);
    const ox = (W - world.width * s) / 2, oy = (H - world.height * s) / 2;
    return { x: (clientX - rect.left - ox) / s, y: (clientY - rect.top - oy) / s };
  };
  const handleClick = (e: React.MouseEvent) => {
    if (!onPlace) return; const w = toWorld(e.clientX, e.clientY); onPlace(w.x, w.y);
  };
  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    const kind = e.dataTransfer.getData("kind"); if (!kind || !onDropActor) return;
    const w = toWorld(e.clientX, e.clientY); onDropActor(w.x, w.y, kind);
  };

  return <canvas ref={ref} onClick={handleClick}
    onDragOver={(e) => { if (onDropActor) e.preventDefault(); }} onDrop={handleDrop}
    style={{ cursor: onPlace ? "crosshair" : "default" }} />;
}

// static start-of-scenario positions, so placed vehicles are visible before you press Run
const HEAD_ANGLE: Record<string, number> = { E: 0, W: Math.PI, S: Math.PI / 2, N: -Math.PI / 2 };
function previewActors(scenario: Scenario) {
  const lanes = scenario.world.lanes || [];
  const out: any[] = [];
  for (const a of scenario.actors) {
    let x: number | undefined, y: number | undefined, angle = 0;
    if ((a.kind === "car" || a.kind === "truck") && scenario.world.grid && a.start_x != null && a.start_y != null) {
      x = a.start_x; y = a.start_y; angle = HEAD_ANGLE[a.heading || "E"] ?? 0;   // grid vehicle
    } else if ((a.kind === "car" || a.kind === "truck" || a.kind === "obstacle") && a.lane != null && lanes[a.lane]) {
      const dir = lanes[a.lane].direction;
      x = a.start_x ?? (dir > 0 ? 0 : scenario.world.width); y = lanes[a.lane].y; angle = dir < 0 ? Math.PI : 0;
    } else if (a.path && a.path.length) { x = a.path[0][0]; y = a.path[0][1]; }
    if (x == null || y == null) continue;
    out.push({ id: a.id, kind: a.kind, label: a.label, x, y, angle, size: a.size, is_hazard: a.is_hazard, braked: false });
  }
  return out;
}

// draw a 2D grid road network (horizontal + vertical roads, intersections, lane markings)
function drawGrid(ctx: CanvasRenderingContext2D, g: any, W: number, H: number,
                  px: (x: number) => number, py: (y: number) => number, s: number) {
  const gw = g.lane_width, hr = g.lanes_each_way * gw;
  ctx.fillStyle = "#1a1f2b";
  for (const gy of g.h_roads) ctx.fillRect(px(0), py(gy - hr), W * s, 2 * hr * s);
  for (const gx of g.v_roads) ctx.fillRect(px(gx - hr), py(0), 2 * hr * s, H * s);
  const edge = "rgba(220,225,240,0.4)", yellow = "rgba(240,205,90,0.5)", dash = "rgba(220,225,240,0.3)";
  for (const gy of g.h_roads) {
    ctx.setLineDash([]); ctx.lineWidth = 1.2; ctx.strokeStyle = edge;
    for (const e of [gy - hr, gy + hr]) { ctx.beginPath(); ctx.moveTo(px(0), py(e)); ctx.lineTo(px(W), py(e)); ctx.stroke(); }
    ctx.strokeStyle = yellow; for (const d of [-1.1, 1.1]) { ctx.beginPath(); ctx.moveTo(px(0), py(gy) + d); ctx.lineTo(px(W), py(gy) + d); ctx.stroke(); }
    ctx.strokeStyle = dash; ctx.setLineDash([12, 10]);
    for (let i = 1; i < g.lanes_each_way; i++) for (const yy of [gy - i * gw, gy + i * gw]) { ctx.beginPath(); ctx.moveTo(px(0), py(yy)); ctx.lineTo(px(W), py(yy)); ctx.stroke(); }
  }
  for (const gx of g.v_roads) {
    ctx.setLineDash([]); ctx.lineWidth = 1.2; ctx.strokeStyle = edge;
    for (const e of [gx - hr, gx + hr]) { ctx.beginPath(); ctx.moveTo(px(e), py(0)); ctx.lineTo(px(e), py(H)); ctx.stroke(); }
    ctx.strokeStyle = yellow; for (const d of [-1.1, 1.1]) { ctx.beginPath(); ctx.moveTo(px(gx) + d, py(0)); ctx.lineTo(px(gx) + d, py(H)); ctx.stroke(); }
    ctx.strokeStyle = dash; ctx.setLineDash([12, 10]);
    for (let i = 1; i < g.lanes_each_way; i++) for (const xx of [gx - i * gw, gx + i * gw]) { ctx.beginPath(); ctx.moveTo(px(xx), py(0)); ctx.lineTo(px(xx), py(H)); ctx.stroke(); }
  }
  ctx.setLineDash([]);
  // clean asphalt squares over each intersection (covers the crossing markings)
  ctx.fillStyle = "#1c2230";
  for (const gy of g.h_roads) for (const gx of g.v_roads) ctx.fillRect(px(gx - hr), py(gy - hr), 2 * hr * s, 2 * hr * s);
}

function roundRect(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
  ctx.beginPath();
  ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.fill();
}
function roundRectStroke(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
  ctx.beginPath();
  ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.stroke();
}
