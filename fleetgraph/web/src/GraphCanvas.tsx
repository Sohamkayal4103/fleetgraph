import { useEffect, useRef } from "react";
import type { GEvent, Scenario } from "./api";

type Props = {
  scenario: Scenario;
  events: GEvent[];          // events up to the current time
  highlightPath?: string[];  // e.g. ["car-B","car-C","car-A"] from an answer
};

// Relationship style + a perpendicular offset so multiple edges between the SAME two nodes fan out
// instead of stacking invisibly on one line.
const EDGE: Record<string, { color: string; dash: number[]; width: number; offset: number }> = {
  trusts:     { color: "rgba(143,160,200,0.22)", dash: [], width: 1, offset: 0 },
  observed:   { color: "#ffcf5c", dash: [], width: 2, offset: 8 },
  blind_to:   { color: "rgba(143,160,200,0.55)", dash: [5, 4], width: 1.5, offset: -8 },
  heard:      { color: "#5b8cff", dash: [], width: 2.6, offset: 0 },
  aware:      { color: "#46d17f", dash: [], width: 2, offset: 18 },
  braked_for: { color: "#ff5d6c", dash: [], width: 3, offset: -18 },
  at_risk:    { color: "#ff5d6c", dash: [3, 4], width: 2.5, offset: -26 },
};

export function GraphCanvas({ scenario, events, highlightPath }: Props) {
  const ref = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const cv = ref.current;
    if (!cv) return;
    const draw = () => paint(cv, scenario, events, highlightPath);
    draw();
    const ro = new ResizeObserver(draw);
    ro.observe(cv);
    return () => ro.disconnect();
  }, [scenario, events, highlightPath]);

  return <canvas ref={ref} />;
}

function paint(cv: HTMLCanvasElement, scenario: Props["scenario"], events: Props["events"], highlightPath?: string[]) {
  {
    const dpr = window.devicePixelRatio || 1;
    const W = cv.clientWidth, H = cv.clientHeight;
    cv.width = W * dpr; cv.height = H * dpr;
    const ctx = cv.getContext("2d")!;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);

    const cars = scenario.actors.filter((a) => a.has_radio && (a.kind === "car" || a.kind === "truck"));
    const hazards = scenario.actors.filter((a) => a.is_hazard);
    const pos: Record<string, { x: number; y: number }> = {};
    cars.forEach((c, i) => { pos[c.id] = { x: (W * (i + 1)) / (cars.length + 1), y: H * 0.26 }; });
    hazards.forEach((h, i) => { pos[h.id] = { x: (W * (i + 1)) / (hazards.length + 1), y: H * 0.8 }; });

    const edge = (a: string, b: string, type: string) => {
      const st = EDGE[type]; const p = pos[a], q = pos[b];
      if (!st || !p || !q) return;
      const dx = q.x - p.x, dy = q.y - p.y, len = Math.hypot(dx, dy) || 1;
      const nx = -dy / len, ny = dx / len;                       // perpendicular unit
      const mx = (p.x + q.x) / 2 + nx * st.offset, my = (p.y + q.y) / 2 + ny * st.offset;
      ctx.strokeStyle = st.color; ctx.lineWidth = st.width; ctx.setLineDash(st.dash);
      ctx.beginPath(); ctx.moveTo(p.x, p.y); ctx.quadraticCurveTo(mx, my, q.x, q.y); ctx.stroke();
      ctx.setLineDash([]);
      // arrowhead near the target, aimed along the curve
      const ang = Math.atan2(q.y - my, q.x - mx);
      const hx = q.x - Math.cos(ang) * 22, hy = q.y - Math.sin(ang) * 22;
      ctx.fillStyle = st.color;
      ctx.beginPath();
      ctx.moveTo(hx + Math.cos(ang) * 8, hy + Math.sin(ang) * 8);
      ctx.lineTo(hx + Math.cos(ang + 2.5) * 6, hy + Math.sin(ang + 2.5) * 6);
      ctx.lineTo(hx + Math.cos(ang - 2.5) * 6, hy + Math.sin(ang - 2.5) * 6);
      ctx.fill();
    };

    // dedupe edges (keep one per from/to/type) but preserve emission order for layering
    const seen = new Set<string>();
    const drawList: [string, string, string][] = [];
    const push = (a: string, b: string, ty: string) => {
      const k = `${a}|${b}|${ty}`; if (seen.has(k)) return; seen.add(k); drawList.push([a, b, ty]);
    };
    const relayNodes = new Set<string>();
    for (const e of events) {
      if (e.type === "trusts") push(e.a, e.b, "trusts");
      else if (e.type === "observed") push(e.car, e.hazard, "observed");
      else if (e.type === "blind_to") push(e.car, e.hazard, "blind_to");
      else if (e.type === "heard") push(e.sender, e.receiver, "heard");
      else if (e.type === "aware") push(e.car, e.hazard, "aware");
      else if (e.type === "braked_for") push(e.car, e.hazard, "braked_for");
      else if (e.type === "unaware_risk") push(e.car, e.hazard, "at_risk");
      else if (e.type === "relayed_by") relayNodes.add(e.relay);
    }
    // draw non-salient first, salient (braked/at_risk) last so they sit on top
    const order = ["trusts", "blind_to", "observed", "heard", "aware", "at_risk", "braked_for"];
    drawList.sort((a, b) => order.indexOf(a[2]) - order.indexOf(b[2]));
    for (const [a, b, ty] of drawList) edge(a, b, ty);

    if (highlightPath && highlightPath.length > 1) {
      for (let i = 0; i < highlightPath.length - 1; i++) {
        const p = pos[highlightPath[i]], q = pos[highlightPath[i + 1]]; if (!p || !q) continue;
        ctx.strokeStyle = "#fff"; ctx.lineWidth = 5; ctx.globalAlpha = 0.3;
        ctx.beginPath(); ctx.moveTo(p.x, p.y); ctx.lineTo(q.x, q.y); ctx.stroke();
        ctx.globalAlpha = 1;
      }
    }

    // nodes (with status rings)
    const brakedNodes = new Set(events.filter((e) => e.type === "braked_for").map((e) => e.car));
    const drawNode = (id: string, label: string, color: string, r = 22) => {
      const p = pos[id]; if (!p) return;
      if (relayNodes.has(id)) {
        ctx.strokeStyle = "#5b8cff"; ctx.lineWidth = 2; ctx.setLineDash([3, 3]);
        ctx.beginPath(); ctx.arc(p.x, p.y, r + 6, 0, Math.PI * 2); ctx.stroke(); ctx.setLineDash([]);
      }
      ctx.fillStyle = "#0f1526";
      ctx.beginPath(); ctx.arc(p.x, p.y, r, 0, Math.PI * 2); ctx.fill();
      ctx.strokeStyle = brakedNodes.has(id) ? "#ff5d6c" : color;
      ctx.lineWidth = brakedNodes.has(id) ? 3.5 : 2.5; ctx.stroke();
      ctx.fillStyle = "#e6ecff"; ctx.font = "11px sans-serif"; ctx.textAlign = "center";
      ctx.fillText(label, p.x, p.y + 3);
      if (relayNodes.has(id)) { ctx.fillStyle = "#8fb4ff"; ctx.font = "9px sans-serif";
        ctx.fillText("relay", p.x, p.y + r + 12); }
      ctx.textAlign = "start";
    };
    const shortName = (c: any) => (c.label || c.id).replace(/\s*\(.*\)/, "").split(" ").slice(0, 2).join(" ");
    for (const c of cars) {
      const isAmb = (c.label || "").toLowerCase().includes("ambulance");
      drawNode(c.id, shortName(c), isAmb ? "#ff5d6c" : "#5b8cff");
    }
    for (const h of hazards) drawNode(h.id, (h.label || "hazard").split(" ")[0], "#ffcf5c", 20);
  }
}
