# Snare

Goal: a crack that cuts through dense guitars, a body with weight, and hits that survive the master limiter.

### snare.align
- **Why:** top and bottom mics face opposite ways and sit apart. Out of polarity or time, they cancel the body.
- **Start:** the mic with the most 150-400 Hz is the reference. Each other mic, in that order, is checked against the sum of the ones before it, inside 0-100 ms of the reference's snare hits: polarity as recorded or flipped, offset 0 or the cross-correlation lag within ±6 ms.
- **Tool:** track `invert`, `offset_samples`.
- **Needed if:** the snare has two or more mics (sample stems excluded).
- **Worked if:** the option with the highest sum gain in 150-400 Hz (rounded to 0.1 dB), then in 2-6 kHz, wins. Kept if it flips or moves the mic; else dropped as recorded.

### snare.hpf
- **Why:** removes kick spill below the snare body.
- **Start:** a 12 dB/oct high-pass at 80 Hz on each snare mic (not samples).
- **Tool:** `FreeEQ`.
- **Needed if:** the mic's 20-80 Hz is within 20 dB of its 150-250 Hz.
- **Worked if:** 20-80 Hz falls by at least 1 dB and 150-250 Hz holds within 0.5 dB.

### snare.blend
- **Why:** the top mic gives body and attack, the bottom mic crack and wire sizzle. Their balance sets the snare sound.
- **Start:** the mic with the most 150-350 Hz at 0 dB; every other mic at 0, -4, -8, -12 dB or muted. A full grid for up to three mics, else one mic at a time in order.
- **Tool:** track `fader_db` (a muted mic gets -60 dB).
- **Needed if:** the snare has two or more mics (sample stems excluded).
- **Worked if:** among blends whose crack prominence stays within 1 dB of the first mic alone, the highest body plus crack prominence wins. Logged kept only if another mic sits at -4, -8 or -12 dB; a blend that only mutes mics is applied but logged dropped.

### snare.sample-blend
- **Why:** a sample reinforces the hits it doubles only at the close mics' level.
- **Start:** the sample's fader is the mics' median hit level minus the sample's, each on its own snare hits.
- **Tool:** track `fader_db` on the sample stem.
- **Needed if:** the snare has a sample stem, a mic, and at least 2 hits in each.
- **Worked if:** always kept; it is a level match, not a trial.

### snare.ring-cut
- **Why:** a ringing note makes the snare sound small and tubby.
- **Start:** on the snare bus input, a cut at the peak in 300-900 Hz that rises most: -4 then -6 dB, Q 4.
- **Tool:** `PeakFilter`.
- **Needed if:** a peak in 300-900 Hz, the highest bin within ±6%, rises more than 6 dB over the median of its 1/3-octave neighbourhood.
- **Worked if:** the band within ±8% of the peak falls by more than 0.5 dB and 150-250 Hz holds within 0.5 dB.

### snare.crack
- **Why:** the crack must cut through the guitars' presence, which masks it.
- **Start:** after the tone moves, a bell at 5 kHz, Q 0.8, on the snare bus: +2, +4, +6 dB, stopping at the first that reaches the rule.
- **Tool:** `PeakFilter`.
- **Needed if:** snare crack prominence (2-8 kHz) in the full mix is under `SNARE_CRACK_PROMINENCE` 7.0 dB.
- **Worked if:** crack prominence rises by at least 0.5 dB. The last gain that passed is kept.

## Not built

- snare.bleed: no song needed it yet.
- snare.body-crack: snare.crack is the crack boost; the body is set by snare.blend and balance.punch.
- snare.compress: no song needed it yet.
- snare.saturate: no song needed it yet.
- snare.transient: no song needed it yet.
- snare.clip: the master's clipper (master.clip) takes the hits' peaks instead.
- snare.reverb: no song needed it yet.
