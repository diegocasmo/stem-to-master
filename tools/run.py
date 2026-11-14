"""Stems to mix and master: uv run tools/run.py songs/<song> [--workers N]"""
import json
import multiprocessing
import sys
import time
from pathlib import Path

import engineer

if __name__ == "__main__":
    song = Path(sys.argv[1])
    workers = int(sys.argv[sys.argv.index("--workers") + 1]) if "--workers" in sys.argv else 2
    t0 = time.perf_counter()
    times = {}
    eng = engineer.Engineer(song, workers)
    with multiprocessing.get_context("spawn").Pool(workers) as pool:
        for stage in eng.stages():
            start = time.perf_counter()
            getattr(eng, stage)(pool)
            times[stage] = round(time.perf_counter() - start, 1)
            print(f"{stage}: {times[stage]} s", flush=True)
        start = time.perf_counter()
        checks = eng.export(pool)
        times["export"] = round(time.perf_counter() - start, 1)
        print(json.dumps({k: v for k, v in checks.items() if k != "profile"}), flush=True)
    (song / "recipe.json").write_text(json.dumps(eng.recipe, indent=1, default=lambda o: o.item() if hasattr(o, "item") else str(o)) + "\n")
    print(json.dumps({"total_s": round(time.perf_counter() - t0, 1), "stages": times, "cache_hits": eng.cache.hits}))
