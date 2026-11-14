"""Render a recipe as written: uv run tools/mixdown.py songs/<song> [--workers N]. Reuses every cached insert."""
import json
import multiprocessing
import sys
import time
from pathlib import Path

import engineer

if __name__ == "__main__":
    song = Path(sys.argv[1])
    workers = int(sys.argv[sys.argv.index("--workers") + 1]) if "--workers" in sys.argv else 3
    start = time.perf_counter()
    eng = engineer.Engineer(song, workers)
    eng.recipe = json.loads((song / "recipe.json").read_text())
    # export measures over the full-band frames, which a run sets in setup_stage.
    eng.length = engineer.daw.length_of(song, eng.recipe)
    eng.dense = eng.full_band_frames()
    with multiprocessing.get_context("spawn").Pool(workers) as pool:
        checks = eng.export(pool)
    print(json.dumps({"seconds": round(time.perf_counter() - start, 1), "cache_hits": eng.cache.hits, "cache_misses": eng.cache.misses,
                      **{k: v for k, v in checks.items() if k != "profile"}}))
