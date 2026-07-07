import { useEffect, useMemo, useRef, useState } from "react";
import { api, type RunResult, type Scenario } from "./api";
import { WorldCanvas, type CarStatus } from "./WorldCanvas";
import { GraphCanvas } from "./GraphCanvas";
import { AuthBar } from "./AuthBar";

type Tool = null | "car" | "truck" | "obstacle" | "pedestrian" | "jammer" | "signal";

function nearestLane(world: Scenario["world"], y: number): number {
  const lanes = world.lanes || [];
  if (!lanes.length) return 0;
  let best = 0, bd = Infinity;
  lanes.forEach((l, i) => { const d = Math.abs(l.y - y); if (d < bd) { bd = d; best = i; } });
  return best;
}

// on a grid, pick an entry heading from where the vehicle was dropped (nearest road + which side)
function headingForDrop(grid: NonNullable<Scenario["world"]["grid"]>, x: number, y: number): string {
  let dh = Infinity, hy = 0; for (const gy of grid.h_roads) { const d = Math.abs(y - gy); if (d < dh) { dh = d; hy = gy; } }
  let dv = Infinity, vx = 0; for (const gx of grid.v_roads) { const d = Math.abs(x - gx); if (d < dv) { dv = d; vx = gx; } }
  if (dh <= dv) return y >= hy ? "E" : "W";
  return x <= vx ? "S" : "N";
}

export function App() {
  const [scenario, setScenario] = useState<Scenario | null>(null);
  const [run, setRun] = useState<RunResult | null>(null);
  const [ti, setTi] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);   // playback speed (1 = real-time)
  const [tool, setTool] = useState<Tool>(null);
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false);
  const [catalog, setCatalog] = useState<{ id: string; question: string }[]>([]);
  const [scenarios, setScenarios] = useState<{ id: string; name: string }[]>([]);
  const [scenarioId, setScenarioId] = useState("blank-road");
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState<any>(null);
  const [genPrompt, setGenPrompt] = useState("");
  const [generating, setGenerating] = useState(false);
  const [bbConfigured, setBbConfigured] = useState(false);
  const raf = useRef<number>();

  useEffect(() => { api.health().then((h) => setBbConfigured(h.butterbase_configured)).catch(() => {}); }, []);

  const generate = async () => {
    if (!genPrompt.trim()) return;
    setGenerating(true); setRun(null); setAnswer(null);
    try { setScenario(await api.generate(genPrompt)); }
    catch (e: any) { alert("Generate failed: " + e.message + (bbConfigured ? "" : "\n(Butterbase AI gateway not configured yet.)")); }
    finally { setGenerating(false); }
  };

  useEffect(() => {
    api.getScenario("blank-road").then(setScenario);
    api.askCatalog().then(setCatalog).catch(() => {});
    api.scenarioCatalog().then(setScenarios).catch(() => {});
  }, []);

  const loadScenario = async (id: string) => {
    setScenarioId(id); setRun(null); setAnswer(null); setPlaying(false);
    setScenario(await api.getScenario(id));
  };

  useEffect(() => {
    if (!playing || !run) return;
    let last = performance.now();
    const step = (now: number) => {
      if (now - last > 1000 / (run.scenario.tick_hz * speed)) {   // real-time at 1x
        last = now;
        setTi((i) => (i + 1 >= run.frames.length ? (setPlaying(false), i) : i + 1));
      }
      raf.current = requestAnimationFrame(step);
    };
    raf.current = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf.current!);
  }, [playing, run, speed]);

  const frame = run ? run.frames[Math.min(ti, run.frames.length - 1)] : null;
  const tNow = frame ? frame.t : 0;
  const eventsUpTo = useMemo(
    () => (run ? run.events.filter((e) => e.t <= tNow + 1e-6) : []), [run, tNow]);

  // per-car status. AWARE_via_radio ONLY when the knowledge came over the mesh (direct/relay);
  // a car that saw the hazard with its own lidar is "saw" — never mislabelled as radio.
  const carStatus: Record<string, CarStatus> = useMemo(() => {
    const st: Record<string, CarStatus> = {};
    const sawLidar: Record<string, boolean> = {};
    for (const e of eventsUpTo) {
      if (e.type === "observed") sawLidar[e.car] = true;
      if (e.type === "blind_to") st[e.car] = st[e.car] ?? "blind";
      if (e.type === "aware" && (e.via === "direct" || e.via === "relay")) st[e.car] = "aware_radio";
      if (e.type === "braked_for") st[e.car] = "braking";
    }
    return st;
  }, [eventsUpTo]);
  const labelOf = (id: string) => scenario?.actors.find((a) => a.id === id)?.label || id;

  const doRun = async (sc: Scenario | null) => {
    setBusy(true); setAnswer(null);
    try {
      const rid = `run-${scenarioId}`;
      const r = await api.run(sc, rid);
      setRun(r); setTi(0); setPlaying(true);
      setScenario(sc ?? r.scenario);
      if (r.credits_remaining != null)
        window.dispatchEvent(new CustomEvent("fg:credits", { detail: r.credits_remaining }));
    } catch (e: any) {
      if (e.status === 402) alert("Out of sim credits — click “＋ Buy 20” in the header to keep running.");
      else alert("Run failed: " + e.message);
    } finally { setBusy(false); }
  };

  const place = (x: number, y: number, kind?: string) => {
    const k = (kind ?? tool) as Tool;
    if (scenario && k) addActor(k, x, y);
  };

  const addActor = (kind: Exclude<Tool, null>, x: number, y: number) => {
    if (!scenario) return;
    const sc = structuredClone(scenario);
    const n = sc.actors.length + 1;
    const lane = nearestLane(sc.world, y);
    const dir = sc.world.lanes[lane]?.direction ?? 1;
    if (kind === "jammer") {
      sc.events.push({ t: 0, kind: "jammer_on", pos: [Math.round(x), Math.round(y)], radius: 40, value: 0.85 });
    } else if (kind === "signal") {
      const g = sc.world.grid
        ? { start_x: Math.round(x), start_y: Math.round(y), affects_dir: 0 }
        : { lane, start_x: Math.round(x), affects_dir: dir };
      sc.actors.push({ id: `sig-${n}`, kind: "signal", label: `Traffic light ${n}`, is_hazard: false,
        has_radio: false, ...g, speed_mps: 0, spawn_t: 0, size: [1.2, 1.2],
        path: [], green_s: 15, yellow_s: 2, red_s: 5, phase_offset_s: 0 });
    } else if (kind === "pedestrian") {
      const cy = sc.world.height / 2;
      sc.actors.push({ id: `ped-${n}`, kind: "pedestrian", label: `Pedestrian ${n}`, is_hazard: true,
        has_radio: false, path: [[Math.round(x), cy - 12], [Math.round(x), cy + 12]], speed_mps: 2, spawn_t: 2, size: [0.7, 0.7] });
    } else if (kind === "obstacle") {
      sc.actors.push({ id: `obs-${n}`, kind: "obstacle", label: `Obstacle ${n}`, is_hazard: false,
        has_radio: false, path: [[Math.round(x), y]], lane, start_x: Math.round(x), speed_mps: 0, spawn_t: 0, size: [3, 3] });
    } else if (kind === "truck") {
      // radio ON by default so the truck joins the V2V mesh and appears as a graph node
      const g = sc.world.grid ? { start_x: Math.round(x), start_y: Math.round(y), heading: headingForDrop(sc.world.grid, x, y), route: [] as string[] } : { lane, start_x: Math.round(x) };
      sc.actors.push({ id: `truck-${n}`, kind: "truck", label: `Truck ${n}`, is_hazard: false,
        has_radio: true, ...g, speed_mps: sc.world.grid ? 8 : 0, spawn_t: 0, size: [9, 2.6], path: [] });
    } else {
      const g = sc.world.grid ? { start_x: Math.round(x), start_y: Math.round(y), heading: headingForDrop(sc.world.grid, x, y), route: [] as string[] } : { lane, start_x: Math.round(x) };
      sc.actors.push({ id: `car-${n}`, kind: "car", label: `Car ${n}`, is_hazard: false,
        has_radio: true, ...g, speed_mps: 11, spawn_t: 0, size: [4.5, 2], path: [], maneuvers: [] });
    }
    setScenario(sc); setRun(null);
  };

  const updateActor = (id: string, patch: any) => {
    if (!scenario) return;
    setScenario({ ...scenario, actors: scenario.actors.map((a) => a.id === id ? { ...a, ...patch, path: [] } : a) });
    setRun(null);
  };
  const removeActor = (id: string) => {
    if (!scenario) return;
    setScenario({ ...scenario, actors: scenario.actors.filter((a) => a.id !== id) }); setRun(null);
  };
  const addLaneChange = (id: string) => {
    const a = scenario?.actors.find((x) => x.id === id); if (!a || a.lane == null || !scenario) return;
    const target = a.lane > 0 ? a.lane - 1 : a.lane + 1;
    updateActor(id, { maneuvers: [...(a.maneuvers || []), { at_t: 3, kind: "lane_change", to_lane: target, over_m: 12 }] });
  };
  const setFog = (v: number) => scenario && setScenario({ ...scenario, world: { ...scenario.world, fog_density: v } });

  const ask = async (q: string) => {
    setAnswer({ loading: true });
    try { setAnswer(await api.ask(q, `run-${scenarioId}`)); } catch (e: any) { setAnswer({ error: e.message }); }
  };
  const answerPath: string[] | undefined = answer?.rows?.[0]?.relay_path ?? answer?.rows?.[0]?.path ?? undefined;

  const vehicles = scenario?.actors.filter((a) => a.kind === "car" || a.kind === "truck") ?? [];
  const signalActors = scenario?.actors.filter((a) => a.kind === "signal") ?? [];

  return (
    <div className="app">
      <header>
        <h1>🚗📡 FleetGraph</h1>
        <span className="tag">When sensors go blind, the mesh sees — V2V safety on a live graph</span>
        <div className="spacer" />
        <span className={"badge " + (run?.neo4j_synced ? "on" : "")}>{run?.neo4j_synced ? "Neo4j ✓ synced" : "Neo4j —"}</span>
        <AuthBar />
      </header>

      <div className="toolbar">
        <button className="primary" disabled={busy} onClick={() => doRun(scenario)}>▶ Run scenario</button>
        <select value={scenarioId} onChange={(e) => loadScenario(e.target.value)}
          style={{ background: "var(--panel)", color: "var(--text)", border: "1px solid var(--line)", borderRadius: 8, padding: "6px 8px" }}>
          {scenarios.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
        </select>
        <div className="sep" />
        <span className="chip">Drag onto road →</span>
        {(["car", "truck", "pedestrian", "obstacle", "signal", "jammer"] as Tool[]).map((tk) => (
          <button key={tk} className={"tool " + (tool === tk ? "active" : "")} draggable
            onDragStart={(e) => { e.dataTransfer.setData("kind", tk as string); e.dataTransfer.effectAllowed = "copy"; }}
            onClick={() => setTool(tool === tk ? null : tk)}>⠿ {tk}</button>
        ))}
        <button className={"tool " + (editing ? "active" : "")} onClick={() => setEditing((v) => !v)}>✎ vehicles</button>
        <div className="sep" />
        <span className="chip">Fog</span>
        <input type="range" min={0} max={1} step={0.05} value={scenario?.world.fog_density ?? 0}
          onChange={(e) => setFog(parseFloat(e.target.value))} style={{ width: 100 }} />
        <span className="chip">{(scenario?.world.fog_density ?? 0).toFixed(2)}</span>
        {tool && <span className="chip">now click a lane to drop a {tool}</span>}
        <div className="spacer" style={{ flex: 1 }} />
        <input type="text" value={genPrompt} onChange={(e) => setGenPrompt(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && generate()}
          placeholder={bbConfigured ? "Describe a scenario in words…" : "Describe a scenario (needs Butterbase gateway)"}
          style={{ minWidth: 220, background: "var(--panel)", color: "var(--text)", border: "1px solid var(--line)", borderRadius: 8, padding: "6px 10px" }} />
        <button className="primary" disabled={generating} onClick={generate}>{generating ? "✨…" : "✨ Generate"}</button>
      </div>

      <div className="main">
        <div className="pane">
          <h2>Road — click a lane to place · lidar reach (blue) · radio (green pulse)</h2>
          <div className="canvas-wrap">
            {scenario && <WorldCanvas scenario={scenario} frame={frame} carStatus={carStatus}
              onPlace={tool ? place : undefined} onDropActor={(x, y, k) => place(x, y, k)} />}
            {scenario && scenario.actors.length === 0 && !run && (
              <div className="empty-hint">Empty road — drag a vehicle from the toolbar onto a lane, or click a tool then click a lane. Then press ▶ Run.</div>
            )}
            <div className="hud">
              <div>t = <b>{tNow.toFixed(1)}s</b> · fog {frame ? frame.fog.toFixed(2) : (scenario?.world.fog_density ?? 0)}</div>
              {Object.entries(carStatus).map(([c, s]) => (
                <div key={c}>{labelOf(c)}: <b>{s === "aware_radio" ? "AWARE via radio" : s === "braking" ? "BRAKING" : s.toUpperCase()}</b></div>
              ))}
              {!run && <div className="chip">Press Run to simulate</div>}
            </div>
            {editing && scenario && (
              <div className="editor">
                <div className="chip" style={{ marginBottom: 6 }}>Vehicles ({vehicles.length}) — edit & Run</div>
                {vehicles.map((a) => (
                  <div key={a.id} className="veh">
                    <input value={a.label ?? a.id} onChange={(e) => updateActor(a.id, { label: e.target.value })} />
                    {scenario.world.grid ? (
                      <span className="chip" title="entry heading (set by where you dropped it)">↦ {a.heading}</span>
                    ) : (
                      <label>lane
                        <select value={a.lane ?? 0} onChange={(e) => updateActor(a.id, { lane: parseInt(e.target.value) })}>
                          {scenario.world.lanes.map((l, i) => <option key={i} value={i}>{i}:{l.label}</option>)}
                        </select>
                      </label>
                    )}
                    <label>speed
                      <input type="number" min={0} max={30} value={a.speed_mps}
                        onChange={(e) => updateActor(a.id, { speed_mps: parseFloat(e.target.value) || 0 })} style={{ width: 44 }} />
                    </label>
                    <label title="has V2V radio (appears in the graph)">📡
                      <input type="checkbox" checked={a.has_radio}
                        onChange={(e) => updateActor(a.id, { has_radio: e.target.checked })} /></label>
                    {scenario.world.grid ? (
                      <>
                        <span className="chip">route:</span>
                        {(a.route ?? []).length === 0 && <span className="chip">(straight)</span>}
                        {(a.route ?? []).map((t, i) => <span key={i} className="chip" style={{ color: "var(--text)" }}>{t[0].toUpperCase()}</span>)}
                        <button title="add left turn" onClick={() => updateActor(a.id, { route: [...(a.route ?? []), "left"] })}>↰L</button>
                        <button title="add straight" onClick={() => updateActor(a.id, { route: [...(a.route ?? []), "straight"] })}>↑S</button>
                        <button title="add right turn" onClick={() => updateActor(a.id, { route: [...(a.route ?? []), "right"] })}>↱R</button>
                        <button title="clear route" onClick={() => updateActor(a.id, { route: [] })}>clr</button>
                      </>
                    ) : (
                      <button title="change lane after 3s" onClick={() => addLaneChange(a.id)}>⇄ lane</button>
                    )}
                    <button title="remove" onClick={() => removeActor(a.id)}>✕</button>
                    {!scenario.world.grid && (a.maneuvers?.length ?? 0) > 0 && <span className="chip">↳ change→{a.maneuvers![0].to_lane} @{a.maneuvers![0].at_t}s</span>}
                  </div>
                ))}
                {signalActors.length > 0 && <div className="chip" style={{ marginTop: 8 }}>Signals — green/yellow/red seconds</div>}
                {signalActors.map((a) => (
                  <div key={a.id} className="veh">
                    <input value={a.label ?? a.id} onChange={(e) => updateActor(a.id, { label: e.target.value })} />
                    {(["green_s", "yellow_s", "red_s"] as const).map((k) => (
                      <label key={k}>{k[0].toUpperCase()}
                        <input type="number" min={0} max={60} value={(a as any)[k] ?? 0}
                          onChange={(e) => updateActor(a.id, { [k]: parseFloat(e.target.value) || 0 })} style={{ width: 40 }} /></label>
                    ))}
                    <button title="remove" onClick={() => removeActor(a.id)}>✕</button>
                  </div>
                ))}
              </div>
            )}
          </div>
          <div className="legend">
            <span style={{ color: "#ff5d6c" }}>ambulance</span>
            <span style={{ color: "#5b8cff" }}>car</span>
            <span style={{ color: "#8fa0c8" }}>truck</span>
            <span style={{ color: "#ffcf5c" }}>hazard</span>
            <span style={{ color: "#46d17f" }}>radio delivered</span>
          </div>
        </div>

        <div className="pane">
          <h2>Knowledge graph — built live from the same events (mirrors Neo4j)</h2>
          <div className="canvas-wrap">
            {scenario && <GraphCanvas scenario={scenario} events={eventsUpTo} highlightPath={answerPath} />}
          </div>
          <div className="legend">
            <span style={{ color: "rgba(143,160,200,0.7)" }}>TRUSTS</span>
            <span style={{ color: "#ffcf5c" }}>OBSERVED</span>
            <span style={{ color: "#8fa0c8" }}>BLIND_TO</span>
            <span style={{ color: "#5b8cff" }}>BEACONED / relay</span>
            <span style={{ color: "#46d17f" }}>AWARE_OF</span>
            <span style={{ color: "#ff5d6c" }}>BRAKED_FOR / AT_RISK</span>
          </div>
        </div>
      </div>

      {run && (
        <div className="timeline">
          <button onClick={() => setPlaying((p) => !p)}>{playing ? "⏸" : "▶"}</button>
          <button onClick={() => { setTi(0); setPlaying(false); }}>⟲</button>
          <input type="range" min={0} max={run.frames.length - 1} value={ti}
            onChange={(e) => { setPlaying(false); setTi(parseInt(e.target.value)); }} />
          <span className="chip">{ti + 1}/{run.frames.length}</span>
          <span className="chip">speed</span>
          {[0.5, 1, 2].map((sp) => (
            <button key={sp} className={"tool " + (speed === sp ? "active" : "")}
              onClick={() => setSpeed(sp)}>{sp}×</button>
          ))}
        </div>
      )}

      <div className="ask">
        <div className="row">
          {catalog.map((c) => (
            <button key={c.id} onClick={() => { setQuestion(c.question); ask(c.question); }}>{c.question}</button>
          ))}
        </div>
        <div className="row">
          <input type="text" placeholder="Ask the graph… e.g. why did the ambulance brake?"
            value={question} onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && ask(question)} />
          <button className="primary" onClick={() => ask(question)}>Ask</button>
        </div>
        {answer?.loading && <div className="chip">querying the graph…</div>}
        {answer?.error && <div className="answer" style={{ color: "#ff5d6c" }}>{answer.error}</div>}
        {answer?.rows && (
          <div className="answer">
            <div className="chip">{answer.question}{answer.source === "rocketride" ? " · via RocketRide ☁️" : ""}</div>
            {answer.answer && <div style={{ fontSize: 15, margin: "4px 0" }}>{answer.answer}</div>}
            {answerPath && <div className="path">{answerPath.map((p: string) => labelOf(p)).join("  →  ")}</div>}
            <div>{answer.rows.length} result(s) — grounded in a real graph traversal:</div>
            <pre>{JSON.stringify(answer.rows, null, 2)}</pre>
            <details><summary className="chip">Cypher</summary><pre>{answer.cypher}</pre></details>
          </div>
        )}
      </div>
    </div>
  );
}
