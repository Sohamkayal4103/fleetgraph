"""Record a narrated ~4-min FleetGraph demo: OpenAI TTS narration + Playwright driving the LIVE app
in a headless browser, muxed into one MP4.

Requires the app running (backend :8099 + web :5180) and OPENAI_API_KEY in env.
    OPENAI_API_KEY=sk-... .venv/bin/python make_narrated_demo.py   ->  fleetgraph_narrated_demo.mp4
"""

import os
import re
import subprocess
import time

import httpx
import imageio_ffmpeg
from playwright.sync_api import sync_playwright

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
KEY = os.environ["OPENAI_API_KEY"]
APP = "http://localhost:5180"
WORK = "/tmp/fgdemo"
os.makedirs(WORK, exist_ok=True)
VOICE = "onyx"
VW, VH = 1440, 810
NARRATOR = ("Speak as an upbeat, clear, confident product-demo narrator — friendly and engaging, "
            "with natural pacing and light enthusiasm. Not robotic.")


def tts(text, path):
    r = httpx.post("https://api.openai.com/v1/audio/speech",
                   headers={"Authorization": f"Bearer {KEY}"},
                   json={"model": "gpt-4o-mini-tts", "voice": VOICE, "input": text,
                         "response_format": "mp3", "instructions": NARRATOR}, timeout=180)
    r.raise_for_status()
    with open(path, "wb") as f:
        f.write(r.content)


def dur(path):
    out = subprocess.run([FFMPEG, "-i", path], capture_output=True, text=True).stderr
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", out)
    h, mn, s = m.groups()
    return int(h) * 3600 + int(mn) * 60 + float(s)


# ---- scenes: (narration, action_fn(page)). action runs fast; we then hold for the narration length.
def sel_scenario(page, sid):
    page.locator("select").first.select_option(sid)
    page.wait_for_timeout(900)


def click_run(page):
    page.get_by_role("button", name="Run scenario").click()


def click_ask(page, label):
    page.get_by_role("button", name=label).click()


SCENES = [
    (  # 1. intro
        "Hi! This is FleetGraph, our project for HackwithBay 3.0. It tackles a real problem in "
        "self-driving and connected cars. A car senses the world with lidar and cameras — but those "
        "are line-of-sight. In fog, glare, or behind a truck, they go blind, and the car can miss a "
        "pedestrian right in front of it. Our idea is simple: when a car's own sensors fail, it "
        "falls back to radio and shares what it knows with nearby cars. When sensors go blind, the "
        "mesh sees.",
        lambda p: sel_scenario(p, "fog-occluded-pedestrian"),
    ),
    (  # 2. fog run
        "Let me show you. Here's a foggy road. The red car is an ambulance racing east — but the fog "
        "has crushed its lidar to almost nothing. Watch what happens when I press Run.",
        click_run,
    ),
    (  # 3. the story (plays during this)
        "A parked car up ahead can see a pedestrian stepping into the road, so it broadcasts that "
        "over radio. The ambulance is too far to hear it through the fog — so a middle car relays "
        "the message. And there — the ambulance brakes about twenty-eight metres early, warned "
        "entirely by radio, before its own lidar ever sees the pedestrian. On the right, every one "
        "of those events just became a node and an edge in a live knowledge graph.",
        lambda p: p.wait_for_timeout(200),
    ),
    (  # 4. ask the graph
        "Now the powerful part. I'll ask the graph, in plain English: why did the ambulance brake? "
        "The answer isn't a guess. It's a real multi-hop traversal through Neo4j — parked car, to "
        "middle car, to ambulance — the exact relay path that warned it. That's a question a plain "
        "SQL join could never answer cleanly.",
        lambda p: (click_ask(p, "Why did the ambulance brake?"), p.wait_for_timeout(400)),
    ),
    (  # 5. jammer contrast
        "But what if someone jams the radio? Here's the same scene with an attacker's jammer. Watch: "
        "the mesh is silenced, the ambulance gets no warning at all, and only its own lidar catches "
        "the pedestrian at the very last second — a dangerous near miss. The graph shows exactly why.",
        lambda p: (sel_scenario(p, "jammer-attack"), click_run(p)),
    ),
    (  # 6. grid + authoring
        "You can build any scene. Drag cars, trucks, pedestrians, traffic lights, even a jammer onto "
        "the road — or just describe a scenario in words and our AI generator builds it. Here's a "
        "city grid: cars follow turn routes through intersections, obey three-phase traffic lights, "
        "and accelerate and brake like real cars.",
        lambda p: (sel_scenario(p, "grid-2x2"), click_run(p)),
    ),
    (  # 7. sponsors + how built
        "Under the hood, three sponsors do the heavy lifting. Neo4j is the graph brain — the "
        "relationships between cars, beacons, and hazards. Butterbase is the entire backend: user "
        "login, the database of every run, the AI gateway that powers scenario generation and the "
        "agent's answers, and even payments, where signed-in users spend sim credits per run. And "
        "RocketRide hosts the question-answering pipeline as a deployed cloud endpoint. Even the "
        "radio is real — signed messages pushed through an actual software modem, so a beacon fails "
        "a real error-check when the signal's too weak, never a coin flip.",
        lambda p: (p.get_by_role("button", name="Sign in").click(), p.wait_for_timeout(400)),
    ),
    (  # 8. outro
        "That's FleetGraph — modeling connected-vehicle safety as a live graph an agent can reason "
        "over. When sensors go blind, the mesh sees. Thanks for watching!",
        lambda p: p.keyboard.press("Escape"),
    ),
]


def main():
    # 1) narration audio + durations
    print("generating narration...")
    segs, durs = [], []
    for i, (text, _) in enumerate(SCENES):
        path = f"{WORK}/seg{i}.mp3"
        tts(text, path)
        d = dur(path)
        segs.append(path); durs.append(d)
        print(f"  scene {i}: {d:.1f}s")
    total = sum(durs)
    print(f"narration total: {total:.0f}s")

    # 2) record the live app, holding each scene for its narration length
    print("recording browser...")
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--force-color-profile=srgb"])
        t0 = time.monotonic()
        ctx = browser.new_context(viewport={"width": VW, "height": VH},
                                  record_video_dir=WORK, record_video_size={"width": VW, "height": VH})
        page = ctx.new_page()
        page.goto(APP, wait_until="networkidle")
        page.get_by_role("button", name="Run scenario").wait_for()
        page.wait_for_timeout(1500)
        preroll = time.monotonic() - t0
        for i, (_, action) in enumerate(SCENES):
            try:
                action(page)
            except Exception as e:  # noqa: BLE001 — keep the recording going
                print(f"  (scene {i} action warn: {e})")
            page.wait_for_timeout(int(durs[i] * 1000))
        video = page.video.path()
        ctx.close(); browser.close()
    print(f"video: {video} (preroll {preroll:.1f}s)")

    # 3) build narration track: preroll silence + segments, concat-filter (handles differing params)
    inputs, filt = [], ""
    sil = f"{WORK}/sil.mp3"
    subprocess.run([FFMPEG, "-y", "-f", "lavfi", "-t", f"{preroll:.2f}",
                    "-i", "anullsrc=r=24000:cl=mono", "-q:a", "9", sil],
                   capture_output=True)
    files = [sil] + segs
    for f in files:
        inputs += ["-i", f]
    filt = "".join(f"[{k}:a]" for k in range(len(files))) + f"concat=n={len(files)}:v=0:a=1[a]"
    narration = f"{WORK}/narration.m4a"
    subprocess.run([FFMPEG, "-y", *inputs, "-filter_complex", filt, "-map", "[a]",
                    "-c:a", "aac", "-b:a", "160k", narration], capture_output=True)

    # 4) mux audio over the recorded video
    out = "fleetgraph_narrated_demo.mp4"
    r = subprocess.run([FFMPEG, "-y", "-i", video, "-i", narration,
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "medium", "-crf", "23",
                        "-c:a", "aac", "-b:a", "160k", "-shortest", out], capture_output=True, text=True)
    if r.returncode != 0:
        print("MUX ERROR:\n", r.stderr[-1500:])
        return
    print(f"\nDONE -> {out}")
    print(subprocess.run([FFMPEG, "-i", out], capture_output=True, text=True).stderr
          .split("Duration:")[1].split(",")[0].strip() if os.path.exists(out) else "")


if __name__ == "__main__":
    main()
