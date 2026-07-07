const BASE = (import.meta as any).env?.VITE_API_BASE ?? "http://localhost:8099";

export type Maneuver = { at_t: number; kind: "lane_change" | "stop"; to_lane?: number | null; over_m?: number };
export type Actor = {
  id: string; kind: string; label?: string | null; path: [number, number][];
  lane?: number | null; start_x?: number | null; maneuvers?: Maneuver[];
  start_y?: number | null; heading?: string | null; route?: string[];
  speed_mps: number; spawn_t: number; size: [number, number];
  is_hazard: boolean; has_radio: boolean;
  green_s?: number; yellow_s?: number; red_s?: number; phase_offset_s?: number; affects_dir?: number;
};
export type Grid = { h_roads: number[]; v_roads: number[]; lanes_each_way: number; lane_width: number };
export type TimedEvent = {
  t: number; kind: string; area?: [[number, number], [number, number]] | null;
  pos?: [number, number] | null; radius?: number | null; value?: number | null; actor?: Actor | null;
};
export type Lane = { y: number; direction: number; kind: "drive" | "shoulder"; label?: string | null };
export type World = {
  width: number; height: number; lanes: Lane[]; lane_width: number; grid?: Grid | null;
  fog_density: number; lidar_range_m: number; brake_reaction_m: number; awareness_horizon_m: number;
};
export type Scenario = {
  name: string; description?: string | null; seed: number; duration_s: number; tick_hz: number;
  world: World; actors: Actor[]; events: TimedEvent[]; trust_pairs?: [string, string][] | null;
};
export type Frame = {
  t: number; tick: number; fog: number;
  jammers: { x: number; y: number; r: number }[];
  actors: { id: string; kind: string; label?: string | null; x: number; y: number; angle?: number; size: [number, number]; is_hazard: boolean; braked?: boolean; waiting?: boolean }[];
  signals?: { id: string; x: number; y: number; red: boolean; phase?: "red" | "yellow" | "green" }[];
  links: { from: string; to: string; snr: number; delivered: boolean }[];
};
export type GEvent = { type: string; t: number; tick: number; [k: string]: any };
export type RunResult = {
  run_id: string; db_run_id?: string | null; scenario: Scenario; events: GEvent[]; frames: Frame[];
  summary: any; neo4j_synced: boolean; credits_remaining?: number | null;
};

// --- end-user auth / sim-credits (Butterbase) -------------------------------
export type BBUser = { id: string; email: string; display_name?: string | null };
export type Session = { user: BBUser; credits: number };
export type RunRow = {
  id: string; scenario_name: string; neo4j_run_id: string;
  event_count: number; brake_count: number; relay_count: number;
  synced_neo4j: boolean; created_at: string;
};

const TOKEN_KEY = "fg_token";
export const token = {
  get: () => localStorage.getItem(TOKEN_KEY),
  set: (t: string | null) => t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY),
};
function authHeaders(extra: Record<string, string> = {}): Record<string, string> {
  const t = token.get();
  return t ? { ...extra, Authorization: `Bearer ${t}` } : extra;
}

async function j<T>(r: Response): Promise<T> {
  if (!r.ok) {
    const detail = (await r.json().catch(() => ({}))).detail ?? `HTTP ${r.status}`;
    const err = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    (err as any).status = r.status;
    throw err;
  }
  return r.json();
}
const JSON_H = { "content-type": "application/json" };

export const api = {
  example: () => fetch(`${BASE}/scenarios/example`).then((r) => j<Scenario>(r)),
  scenarioCatalog: () => fetch(`${BASE}/scenarios/catalog`).then((r) => j<{ id: string; name: string }[]>(r)),
  getScenario: (id: string) => fetch(`${BASE}/scenarios/${id}`).then((r) => j<Scenario>(r)),
  generate: (prompt: string) => fetch(`${BASE}/scenarios/generate`, { method: "POST",
    headers: authHeaders(JSON_H), body: JSON.stringify({ prompt }) }).then((r) => j<Scenario>(r)),
  health: () => fetch(`${BASE}/health`).then((r) => j<{ neo4j_configured: boolean; butterbase_configured: boolean; butterbase_backend_configured: boolean }>(r)),
  run: (scenario: Scenario | null, run_id = "run-demo-1") =>
    fetch(`${BASE}/run`, { method: "POST", headers: authHeaders(JSON_H),
      body: JSON.stringify({ scenario, run_id }) }).then((r) => j<RunResult>(r)),
  askCatalog: () => fetch(`${BASE}/ask/catalog`).then((r) => j<{ id: string; question: string }[]>(r)),
  ask: (question: string, run_id = "run-demo-1") =>
    fetch(`${BASE}/ask`, { method: "POST", headers: authHeaders(JSON_H),
      body: JSON.stringify({ question, run_id }) }).then((r) => j<any>(r)),

  // auth + sim-credits
  signup: (email: string, password: string, display_name?: string) =>
    fetch(`${BASE}/auth/signup`, { method: "POST", headers: JSON_H,
      body: JSON.stringify({ email, password, display_name }) }).then((r) => j<Session & { access_token: string }>(r)),
  login: (email: string, password: string) =>
    fetch(`${BASE}/auth/login`, { method: "POST", headers: JSON_H,
      body: JSON.stringify({ email, password }) }).then((r) => j<Session & { access_token: string }>(r)),
  me: () => fetch(`${BASE}/auth/me`, { headers: authHeaders() }).then((r) => j<Session>(r)),
  buyCredits: () => fetch(`${BASE}/credits/buy`, { method: "POST", headers: authHeaders() })
    .then((r) => j<{ balance: number; granted: number }>(r)),
  runs: () => fetch(`${BASE}/runs`, { headers: authHeaders() }).then((r) => j<RunRow[]>(r)),
};
