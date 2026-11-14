# Drum bus

Goal: one kit, not separate mics: shells and cymbals glued, without blurring fast playing or losing punch.

The bus layout is fixed, not a move: kick, snare, toms, cymbals and room buses (those the song has) sum into a drums bus; kick and bass never share a bus.

### drum-bus.glue
- **Why:** light bus compression glues the kit and evens it against the guitars, but only if the kick and snare keep their punch.
- **Start:** threshold the drums bus's median kick and snare hit level -4 dB, then -7 dB; 4:1, attack 10 ms, release 100 ms, knee 6 dB, style Clean.
- **Tool:** `ZL Compressor` on the drums bus.
- **Needed if:** always tried.
- **Worked if:** at the mix's loudness before the move, kick low, snare body and snare crack prominence each hold within 0.5 dB, and the drums bus's loudness falls by 0.5 to 4 dB. The deepest that passes is kept. Dropped as "punch did not hold", or "loudness drop outside 0.5-4 dB" when punch held at some depth.

## Not built

- drum-bus.groups: the bus layout is fixed by the recipe skeleton, not decided.
- drum-bus.compress: drum-bus.glue is the bus compression.
- drum-bus.parallel: no song needed it yet.
- drum-bus.distort-parallel: no song needed it yet.
- drum-bus.saturate: no song needed it yet.
- drum-bus.reverb: no song needed it yet.
- drum-bus.rides: songs have no marked sections.
