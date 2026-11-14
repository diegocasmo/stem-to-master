# Kick

Goal: a tight low-end thump and a clear click that both survive the master, even in fast double-kick runs.

### kick.align
- **Why:** kick mics out of time or polarity cancel each other and thin the kick.
- **Start:** the mic with the most 40-150 Hz is the reference. Each other mic, in that order, is checked against the sum of the ones before it, inside 0-100 ms of the reference's kick hits: polarity as recorded or flipped, offset 0 or the cross-correlation lag within ±6 ms.
- **Tool:** track `invert`, `offset_samples`.
- **Needed if:** the kick has two or more mics (sample stems excluded).
- **Worked if:** the option with the highest sum gain in 40-150 Hz (rounded to 0.1 dB), then in 2-6 kHz, wins. Kept if it flips or moves the mic; else dropped as recorded.

### kick.blend
- **Why:** each kick mic holds a different part (inside mic click, outside mic body). Their balance is most of the kick sound.
- **Start:** the mic with the most 40-120 Hz at 0 dB; every other mic at 0, -4, -8, -12 dB or muted. A full grid for up to three mics, else one mic at a time in order.
- **Tool:** track `fader_db` (a muted mic gets -60 dB).
- **Needed if:** the kick has two or more mics (sample stems excluded).
- **Worked if:** among blends whose kick low peak stays within 3 dB of the first mic alone, the highest score wins: low decay (minus the kick tail) plus 0.25 × click prominence. Logged kept only if another mic sits at -4, -8 or -12 dB; a blend that only mutes mics is applied but logged dropped.

### kick.sample-blend
- **Why:** a sample reinforces the hits it doubles only at the close mics' level.
- **Start:** the sample's fader is the mics' median hit level minus the sample's, each on its own kick hits.
- **Tool:** track `fader_db` on the sample stem.
- **Needed if:** the kick has a sample stem, a mic, and at least 2 hits in each.
- **Worked if:** always kept; it is a level match, not a trial.

### kick.tail
- **Why:** a long low tail after each hit piles up in fast double-kick runs and blurs the low end.
- **Start:** a hit-gated decay below 150 Hz on the kick bus: full level for 30 ms after a hit, then down over 40 ms. Depths 4, 8, 12, 18 dB; the smallest that reaches the target wins.
- **Tool:** native `Shape` (`band_hz` [null, 150], `hold_ms` 30, `fade_ms` 40, `floor_db` 30).
- **Needed if:** the kick bus's tail is above `KICK_TAIL_DB` -14 dB.
- **Worked if:** the tail falls to `KICK_TAIL_DB` or below. If no depth reaches it, the 18 dB depth is kept when it shortens the tail by at least 2 dB ("kept: deepest, short of target").

### kick.weight
- **Why:** sets the weight felt on every speaker: the kick's own low peak against its boxy band.
- **Start:** a bell at the kick bus's spectral peak in 50-100 Hz, +2 then +3 dB, Q 1.2.
- **Tool:** `PeakFilter`.
- **Needed if:** always tried.
- **Worked if:** 45-110 Hz minus 200-400 Hz rises by at least 1 dB.

### kick.mud-cut
- **Why:** 200-400 Hz is boxy. It hides the click and fights the guitars.
- **Start:** -3 dB at 300 Hz, Q 1.0, on the kick bus.
- **Tool:** `PeakFilter`.
- **Needed if:** the kick bus's 200-400 Hz is within 10 dB of its 50-100 Hz.
- **Worked if:** 200-400 Hz falls by more than 0.5 dB and 50-100 Hz holds within 0.5 dB.

### kick.duck-bass
- **Why:** dips the bass's low end only while the kick hits, so each owns the low end in turn without a static cut.
- **Start:** below 150 Hz on the bass bus, keyed from the kick bus: threshold the kick's median hit level -15 dB, ratio 4, attack 1 ms, release 80 ms, range 3 then 6 dB.
- **Tool:** native `Duck` (`key` kick, `band_hz` [null, 150]).
- **Needed if:** the kick has at least 4 hits (else "dropped: under 4 kick hits") and its low prominence in the full mix is under `KICK_LOW_PROMINENCE` 12.5 dB.
- **Worked if:** kick low prominence rises by at least 0.5 dB and the bass's 40-150 Hz between kick hits (outside 5 ms before to 150 ms after) falls by at most 1 dB. The deepest range that passes is kept.

### kick.duck-gtr
- **Why:** the guitars' lows can mask the kick too, once the bass makes room.
- **Start:** as kick.duck-bass, on the guitar bus, judged with any kept bass duck in place.
- **Tool:** native `Duck` (`key` kick, `band_hz` [null, 150]).
- **Needed if:** the kick has at least 4 hits (else "dropped: under 4 kick hits") and its low prominence in the full mix (after the bass duck) is under `KICK_LOW_PROMINENCE` 12.5 dB.
- **Worked if:** as kick.duck-bass, with the guitars' 40-150 Hz between kick hits.

### kick.click
- **Why:** the click makes each hit audible in fast runs under dense guitars.
- **Start:** after the tone moves, a bell at 4 kHz, Q 0.8, on the kick bus: +2, +4, +6 dB, stopping at the first that reaches the rule.
- **Tool:** `PeakFilter`.
- **Needed if:** kick click prominence (2-6 kHz) in the full mix is under `KICK_CLICK_PROMINENCE` 5.0 dB.
- **Worked if:** click prominence rises by at least 0.5 dB. The last gain that passed is kept.

## Not built

- kick.gate: kick.tail shortens the low tail instead, and bleed has not needed a gate.
- kick.hpf: kick sub is handled by sub.cut (context.md) on the kick bus.
- kick.bass-room: the low-mid cut on its owner (lowmid.cut) and the ducks cover it.
- kick.thump: kick.weight is the low boost the engineer makes.
- kick.compress: no song needed it yet.
- kick.saturate: no song needed it yet.
- kick.transient: no song needed it yet.
- kick.limit: no song needed it yet.
- kick.rides: no song needed it yet.
