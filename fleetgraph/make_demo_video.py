"""Render a FleetGraph demo MP4 from the engine — world + knowledge graph, side by side, with
captions and title/sponsor cards. Silent (no voiceover), but demonstrates the whole flow.

    .venv/bin/python make_demo_video.py   ->  fleetgraph_demo.mp4
"""

import math

import imageio
import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Circle, FancyArrow, Rectangle  # noqa: E402

from fleetgraph.engine import run_scenario  # noqa: E402
from fleetgraph.scenario import get_scenario  # noqa: E402

BG = "#0b1020"; ROAD = "#1a1f2b"; TXT = "#e6ecff"; MUT = "#8fa0c8"
AMB = "#ff5d6c"; CAR = "#5b8cff"; TRK = "#8fa0c8"; PED = "#ffcf5c"; OK = "#46d17f"
EDGE_COL = {"observed": PED, "blind_to": "#5b6b8f", "heard": CAR, "aware": OK, "braked_for": AMB}
W, H, DPI = 1280, 600, 100


def _fig():
    fig = plt.figure(figsize=(W / DPI, H / DPI), dpi=DPI)
    fig.patch.set_facecolor(BG)
    return fig


def _to_rgb(fig):
    fig.canvas.draw()
    buf = np.asarray(fig.canvas.buffer_rgba())[:, :, :3].copy()
    plt.close(fig)
    return buf


def caption(t: float) -> str:
    if t < 2.2:
        return "Heavy fog — the ambulance's lidar is nearly blind."
    if t < 6.6:
        return "A parked car SEES the pedestrian → beacons it. A middle car RELAYS it onward."
    if t < 9.0:
        return "Warned by RADIO, the ambulance brakes ~28 m early — before its own lidar sees the pedestrian."
    return "Every hop is a real signed BPSK radio beacon. Every fact lands in the Neo4j graph."


def title_card(lines, colors=None, sub=None):
    fig = _fig()
    ax = fig.add_axes([0, 0, 1, 1]); ax.set_facecolor(BG); ax.axis("off")
    colors = colors or [TXT] * len(lines)
    n = len(lines)
    for i, (ln, c) in enumerate(zip(lines, colors)):
        y = 0.62 - i * 0.13
        sz = 30 if i == 0 else 20
        ax.text(0.5, y, ln, ha="center", va="center", color=c, fontsize=sz,
                fontweight="bold" if i == 0 else "normal", transform=ax.transAxes)
    if sub:
        ax.text(0.5, 0.62 - n * 0.13 - 0.02, sub, ha="center", va="center", color=MUT,
                fontsize=13, transform=ax.transAxes)
    return _to_rgb(fig)


def car_status(events, t):
    st = {}
    for e in events:
        if e["t"] > t:
            break
        if e["type"] == "blind_to":
            st.setdefault(e["car"], "blind")
        if e["type"] == "aware" and e.get("via") in ("direct", "relay"):
            st[e["car"]] = "aware"
        if e["type"] == "braked_for":
            st[e["car"]] = "braked"
    return st


def draw_world(ax, scenario, frame, st):
    wd = scenario.world
    ax.set_facecolor(BG); ax.set_xlim(0, wd.width); ax.set_ylim(wd.height, 0)
    ax.set_xticks([]); ax.set_yticks([]); ax.set_title("The road (V2V simulation)", color=MUT, fontsize=12)
    ys = [l.y for l in wd.lanes]
    top, bot = min(ys) - wd.lane_width / 2, max(ys) + wd.lane_width / 2
    ax.add_patch(Rectangle((0, top), wd.width, bot - top, color=ROAD, zorder=0))
    ax.axhline((min(ys) + max(ys)) / 2, color="#f0cd5a", lw=0.8, alpha=0.5, zorder=1)
    fog = frame["fog"]
    if fog > 0:
        ax.add_patch(Rectangle((0, 0), wd.width, wd.height, color="#c8d2e6",
                               alpha=0.06 + fog * 0.28, zorder=5))
    pos = {a["id"]: (a["x"], a["y"]) for a in frame["actors"]}
    for l in frame["links"]:
        if l["delivered"] and l["from"] in pos and l["to"] in pos:
            (x0, y0), (x1, y1) = pos[l["from"]], pos[l["to"]]
            ax.plot([x0, x1], [y0, y1], color=OK, lw=1.2, alpha=0.55, zorder=3)
    for a in frame["actors"]:
        lbl = (a.get("label") or a["id"])
        isamb = "ambulance" in lbl.lower()
        if a["kind"] == "pedestrian":
            ax.add_patch(Circle((a["x"], a["y"]), 1.6, color=PED, zorder=7))
        else:
            c = TRK if a["kind"] == "truck" else (AMB if isamb else CAR)
            ln, wi = a["size"]
            ang = a.get("angle", 0.0)
            ax.add_patch(Rectangle((a["x"] - ln / 2, a["y"] - wi / 2), ln, wi, color=c,
                                   angle=math.degrees(ang), rotation_point="center", zorder=8))
            s = st.get(a["id"])
            if a.get("braked") or s == "braked":
                ax.add_patch(Circle((a["x"], a["y"]), max(ln, wi) / 2 + 2, fill=False,
                                    edgecolor=AMB, lw=2, zorder=9))
            elif s == "aware":
                ax.add_patch(Circle((a["x"], a["y"]), max(ln, wi) / 2 + 2, fill=False,
                                    edgecolor=OK, lw=1.6, zorder=9))
        ax.text(a["x"], a["y"] - 4, lbl, ha="center", color=TXT, fontsize=7, zorder=10)


def draw_graph(ax, scenario, events, t):
    ax.set_facecolor(BG); ax.set_xlim(0, 10); ax.set_ylim(0, 10)
    ax.set_xticks([]); ax.set_yticks([]); ax.set_title("The knowledge graph (Neo4j)", color=MUT, fontsize=12)
    cars = scenario.cars(); hz = scenario.hazards()
    pos = {}
    for i, c in enumerate(cars):
        pos[c.id] = ((i + 1) * 10 / (len(cars) + 1), 7.6)
    for i, h in enumerate(hz):
        pos[h.id] = ((i + 1) * 10 / (len(hz) + 1), 2.2)
    seen = set()
    for e in events:
        if e["t"] > t:
            break
        ty = e["type"]
        if ty not in EDGE_COL:
            continue
        a = e.get("car") or e.get("sender"); b = e.get("hazard") or e.get("receiver")
        if a not in pos or b not in pos or (a, b, ty) in seen:
            continue
        seen.add((a, b, ty))
        (x0, y0), (x1, y1) = pos[a], pos[b]
        off = {"observed": 0.25, "blind_to": -0.25, "heard": 0, "aware": 0.5, "braked_for": -0.5}[ty]
        ax.annotate("", xy=(x1, y1), xytext=(x0, y0),
                    arrowprops=dict(arrowstyle="-|>", color=EDGE_COL[ty], lw=2,
                                    connectionstyle=f"arc3,rad={off/3}", alpha=0.9))
    braked = {e["car"] for e in events if e["type"] == "braked_for" and e["t"] <= t}
    for nid, (x, y) in pos.items():
        meta = next((a for a in scenario.actors if a.id == nid), None)
        isamb = meta and "ambulance" in (meta.label or "").lower()
        col = AMB if (isamb or nid in braked) else (PED if meta and meta.is_hazard else CAR)
        ax.add_patch(Circle((x, y), 0.62, facecolor="#0f1526", edgecolor=col, lw=2.5, zorder=5))
        short = (meta.label if meta else nid).replace(" (relay)", "").replace(" (by crossing)", "")
        short = short.replace("Pedestrian", "PED")[:10]
        ax.text(x, y, short.split(" ")[0][:6], ha="center", va="center", color=TXT, fontsize=7, zorder=6)


def sim_frame(scenario, frame, events):
    fig = _fig()
    fig.suptitle(caption(frame["t"]), color=TXT, fontsize=14, fontweight="bold", y=0.965)
    st = car_status(events, frame["t"])
    axw = fig.add_axes([0.02, 0.05, 0.46, 0.82]); draw_world(axw, scenario, frame, st)
    axg = fig.add_axes([0.52, 0.05, 0.46, 0.82]); draw_graph(axg, scenario, events, frame["t"])
    fig.text(0.5, 0.015, f"t = {frame['t']:.1f}s", ha="center", color=MUT, fontsize=10)
    return _to_rgb(fig)


def main():
    scenario = get_scenario("fog-occluded-pedestrian")
    result = run_scenario(scenario)
    frames, events = result.frames, result.events

    out = []
    hold = lambda img, n: out.extend([img] * n)  # noqa: E731

    hold(title_card(["FleetGraph",
                     "When sensors go blind, the mesh sees"],
                    [TXT, OK], sub="HackwithBay 3.0 — V2V road safety on a live graph"), 28)
    hold(title_card(["The problem", "A car's lidar is blinded by fog or an obstacle —",
                     "so it can't see the pedestrian ahead."], [AMB, TXT, MUT]), 22)
    for f in frames:
        out.append(sim_frame(scenario, f, events))
    hold(out[-1], 12)
    hold(title_card(["Ask the graph:  “Why did the ambulance brake?”",
                     "Parked car  →  Middle car  →  Ambulance"],
                    [TXT, OK], sub="A real multi-hop Cypher traversal in Neo4j — not a guess."), 34)
    hold(title_card(["Powered by", "Neo4j  ·  RocketRide  ·  Butterbase"],
                    [MUT, TXT], sub="graph brain · deployed AI pipeline · backend + AI gateway + payments"), 30)
    hold(title_card(["FleetGraph", "github.com/Sohamkayal4103/fleetgraph"], [OK, MUT]), 20)

    path = "fleetgraph_demo.mp4"
    with imageio.get_writer(path, fps=10, codec="libx264", quality=8,
                            macro_block_size=8) as w:
        for img in out:
            w.append_data(img)
    print(f"wrote {path}  ({len(out)} frames, ~{len(out)/10:.0f}s)")


if __name__ == "__main__":
    main()
