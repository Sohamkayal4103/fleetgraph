"""Headless end-to-end proof: run a scenario, print the graph-event stream + the demo narrative.

    .venv/bin/python run_scenario.py                 # built-in fog-occluded-pedestrian scene
    .venv/bin/python run_scenario.py scenarios/x.json # any scenario JSON
"""

import json
import sys

from fleetgraph.engine import run_scenario
from fleetgraph.scenario import example_fog_pedestrian, load_scenario

ICON = {
    "trusts": "🤝", "observed": "👁️ ", "blind_to": "🌫️ ", "heard": "📡", "relayed_by": "🔁",
    "aware": "💡", "braked_for": "🛑", "unaware_risk": "⚠️ ",
}


def main() -> None:
    if len(sys.argv) > 1:
        scenario = load_scenario(json.load(open(sys.argv[1])))
    else:
        scenario = example_fog_pedestrian()

    result = run_scenario(scenario)

    print("=" * 78)
    print(f"SCENARIO: {scenario.name} — {scenario.description or ''}")
    print(f"{len(scenario.cars())} radio cars, {len(scenario.hazards())} hazard(s), "
          f"fog={scenario.world.fog_density}, {len(result.frames)} ticks, "
          f"{result.summary['n_events']} graph events")
    print("=" * 78)

    # Print the semantic events (skip the per-tick link spam, which lives in frames for the viewer).
    last_t = None
    for e in result.events:
        if e["type"] in ("trusts",) and e["t"] == 0.0:
            print(f"  {ICON['trusts']} trust pinned: {e['a']} <-> {e['b']}")
    print("-" * 78)
    for e in result.events:
        if e["type"] == "trusts":
            continue
        if e["t"] != last_t:
            print(f"\n  t={e['t']:>5.1f}s")
            last_t = e["t"]
        ic = ICON.get(e["type"], "· ")
        if e["type"] == "observed":
            print(f"    {ic} {e['car']} SAW {e['hazard']} by lidar ({e['distance']} m)")
        elif e["type"] == "blind_to":
            print(f"    {ic} {e['car']} is BLIND to {e['hazard']} — {e['reason']} ({e['distance']} m)")
        elif e["type"] == "heard":
            print(f"    {ic} {e['receiver']} heard {e['sender']} about {e['hazard']} "
                  f"(SNR {e['snr']} dB, {e['hop']})")
        elif e["type"] == "relayed_by":
            print(f"    {ic} {e['relay']} RELAYED {e['hazard']} from {e['source']} -> {e['dest']}")
        elif e["type"] == "aware":
            chain = " -> ".join(e["path"])
            print(f"    {ic} {e['car']} now AWARE of {e['hazard']} via {e['via']} [{chain}]")
        elif e["type"] == "braked_for":
            print(f"    {ic} {e['car']} BRAKED for {e['hazard']} ({e['distance']} m) via {e['source']}")
        elif e["type"] == "unaware_risk":
            print(f"    {ic} {e['car']} UNAWARE of {e['hazard']} at {e['distance']} m — collision risk")

    print("\n" + "=" * 78)
    print("NARRATIVE")
    for car, hz in result.summary["braked"].items():
        via = result.summary["aware_via_radio"].get(car, [])
        for h in hz:
            how = "the mesh (radio)" if h in via else "its own lidar"
            print(f"  • {car} avoided {h} — learned about it from {how}.")
    if not result.summary["braked"]:
        print("  • No braking events (adjust the scenario).")
    print("=" * 78)


if __name__ == "__main__":
    main()
