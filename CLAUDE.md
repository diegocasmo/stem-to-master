# stem-to-master

Mix and master a song from its aligned stems like an engineer in a DAW, to sound as professional as the profile's reference tracks. Signal processing only: never create or generate audio.

## Rules

- Performance comes first: stems of a ~1:30 song to a mix and master that pass their measured checks in about 5 minutes, 10 at most, on this Mac (M1, 8 cores, 16 GB). Any design that can't meet that is wrong.
- Solve only the problems the current song has. Don't build for instruments, routings or edge cases it doesn't have (no DI handling if there is no DI); add them when a song needs them.
- Instrumental only: exclude every vocal stem. Ignore DI stems. Every song has drums.
- The recipe is the source of truth: per track an ordered list of inserts (any order), sends, buses, faders, pan, automation, then the master chain. Renders are a cache rebuilt from the stems.
- Correctness is never traded for speed: a cached result must equal a fresh one.
- Python with pedalboard and the plugins pinned in plugins.lock (each must render identically in fresh runs). ./setup.sh installs them into plugins/; run with uv run.
- Master: 24-bit WAV at the stems' rate, true peak at or below -1 dBTP, integrated loudness from the profile.

## Workflow

- One change per PR: a branch from latest origin/main, assigned to diegocasmo, squash merged. Never force push.
- Merge gate: `uv run pytest` passes. A change to render, engineer or grade also needs: all 4 benchmark songs graded (before and after per song in the PR), no song clearly worse, total confident excess holds or improves, and a cold 90 s run of 000 under 5 minutes.
- Then one review by Claude and Codex: only P1/P2 block; P3 and style become issues. At most 2 rounds.
- Audio stays on this Mac: never commit, upload or attach it.
