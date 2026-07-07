"""Feasibility proof: a car state-beacon really rides Aithernet's software radio, and whether it
arrives is decided by world conditions (distance + fog + jammer) through a real CRC — not random().

Run:  .venv/bin/python smoke_radio.py
"""

from fleetgraph.radio import LinkConditions, RadioMesh, make_car_identity, snr_db_for


def beacon_payload() -> dict:
    return {
        "type": "state_beacon",
        "pos": [40.0, 12.0],
        "vel": [0.0, 8.5],
        "heading_deg": 90,
        "hazards": [{"kind": "pedestrian", "pos": [41.0, 6.0], "confidence": 0.92}],
    }


def main() -> None:
    mesh = RadioMesh(profile_id="ref-bpsk-1k")
    mesh.add_car(make_car_identity("car-A", "Ambulance"))
    mesh.add_car(make_car_identity("car-B", "Sedan"))

    print("=" * 74)
    print("FleetGraph radio feasibility — car-B beacons its pedestrian sighting to car-A")
    print("=" * 74)

    cases = [
        ("clear day, 30 m apart",        LinkConditions(distance_m=30, fog_density=0.0)),
        ("light fog, 45 m apart",        LinkConditions(distance_m=45, fog_density=0.3)),
        ("heavy fog, 70 m apart",        LinkConditions(distance_m=70, fog_density=0.8)),
        ("heavy fog + jammer, 70 m",     LinkConditions(distance_m=70, fog_density=0.8, interference=0.6)),
    ]
    for label, cond in cases:
        r = mesh.send_beacon("car-B", "car-A", beacon_payload(), conditions=cond, seed=7)
        mark = "OK  DELIVERED" if r.delivered else "XX  LOST"
        detail = (f"sig={'verified' if r.signature_verified else '—'} "
                  f"frames={r.frames_sent} acked={r.frames_acked} retries={r.retries}"
                  if r.delivered else f"decode={r.decode_outcome}")
        print(f"\n{label:32s} SNR={r.snr_db:6.1f} dB  -> {mark}")
        print(f"    {detail}")
        if r.delivered and r.received_payload:
            haz = r.received_payload.get("hazards", [{}])[0]
            print(f"    car-A now knows: {haz.get('kind')} at {haz.get('pos')} "
                  f"(conf {haz.get('confidence')})")

    # The delivery "cliff" — deterministic given the seed, proving it is channel physics, not luck.
    print("\n" + "-" * 74)
    print("Delivery vs distance in heavy fog (fog=0.8), reference BPSK link — the fallback cliff:")
    for d in range(20, 121, 10):
        cond = LinkConditions(distance_m=d, fog_density=0.8)
        r = mesh.send_beacon("car-B", "car-A", beacon_payload(), conditions=cond, seed=7)
        bar = "#" * max(0, int(r.snr_db)) if r.snr_db > 0 else ""
        print(f"    {d:3d} m  SNR={r.snr_db:6.1f} dB  {'DELIVERED' if r.delivered else 'lost     '} {bar}")


if __name__ == "__main__":
    main()
