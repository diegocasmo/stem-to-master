# Context

Goal: moves on the whole song or the full mix: what each stem is, where the full band plays, the static balance, the punch faders, and the broad tone balance. Each is logged with the id below. Tone moves land on the source that owns the band; what is left goes to the mix bus ([mix-bus.md](mix-bus.md)).

### stems.classify
- **Why:** every later move depends on what each stem is; vocals and DIs are out of scope.
- **Start:** the file name is the hint (first match wins, so "snaresample" is a snare). A stem with no hint is classed by content: more than 0.5 hits per second with a crest over 12 dB is a kick (over half its energy under 150 Hz), toms (centroid under 1.5 kHz) or snare; else bass (over 60% under 150 Hz) or synth. "sample" in the name marks a sample; a `DT` suffix a double track.
- **Tool:** track `class`, `group` (which bus it feeds).
- **Needed if:** always.
- **Worked if:** always kept; logs each stem's class and how it was found, and the stems left out (vocal, DI, silent under -60 dBFS).

### song.full-band
- **Why:** balance decisions must be read where drums, bass and guitars all play, not in intros or breaks.
- **Start:** 100 ms frames where the kick, bass and guitar groups are each within 20 dB of their own p95 level (each widened by 5 frames either side).
- **Tool:** none; a measure every later move reads.
- **Needed if:** always.
- **Worked if:** logged as measured. If under 20% of the song qualifies, every frame is used.

### balance.fader
- **Why:** a static balance between groups is the base every later move is judged on.
- **Start:** the reference group is the first of gtr, bass, synths, kick the song has. Over the full-band frames, each other bus fader is set so the bus sits at its `TARGETS_LU` loudness against the reference: kick -4, snare -4, bass -3, cymbals -9, room -13, synths -7 LU. Toms: 3 LU under the reference inside fills (50 ms before to 600 ms after tom hits), clipped to -12 to +6 dB.
- **Tool:** bus `fader_db`.
- **Needed if:** the bus exists and is not silent (over -100 LUFS); toms need at least 0.3 s of fills.
- **Worked if:** always kept; it is a level match, not a trial.

### balance.punch
- **Why:** kick and snare must stand out of the full mix by a set amount; a fader is the cleanest way there.
- **Start:** kick fader for kick low prominence, snare fader for snare body prominence, in the full mix. Steps 0 to +9 dB, then -1 to -6 dB, limited to 9 dB over and 6 dB under the static balance. Targets: `KICK_LOW_PROMINENCE` 12.5 dB and `SNARE_BODY_PROMINENCE` 14.0 dB here; in rebalance, the targets from balance.punch-targets.
- **Tool:** bus `fader_db`.
- **Needed if:** the bus exists and has at least 4 hits.
- **Worked if:** the step closest to the target wins (the smaller move on a tie). If the highest step lifts the measure by less than 1 dB and it is under the target, the fader stays. Kept if the fader moved; else "dropped: on target".

### lowmid.cut
- **Why:** a 125-200 Hz build-up makes the mix woolly. It is cut on the source that owns it, not on the whole mix.
- **Start:** the build-up is the loudest third-octave of 125, 160, 200 Hz minus the mean of 63 and 500 Hz, over the full-band frames. The owner is the bus with the most at that peak. A bell at the peak, Q 1.4: -2, -3, -4.5, -6 dB, stopping when the build-up is at or under the trigger.
- **Tool:** `PeakFilter` on the owner bus.
- **Needed if:** the build-up is over `LOWMID_BUILDUP_DB` 2.5 dB.
- **Worked if:** the build-up falls by more than 0.5 dB and the owner's own band (kick 63 Hz, bass 80 Hz, others 100 Hz third-octave) falls by at most 1.5 dB. The last gain that passed is kept.

### sub.cut
- **Why:** boom under the note or the hit eats headroom. Nothing is boosted: a source with no sub stays without it.
- **Start:** on the kick bus, then the bass bus: a low shelf at 45 Hz, Q 0.707, -2, -4, -6, -8, -10 dB, until the sub sits `SUB_CUT_DEPTH_DB` 4.0 dB under the trigger (-8 dB).
- **Tool:** `LowShelfFilter`.
- **Needed if:** the bus's 25-45 Hz minus its 60-100 Hz, over the full-band frames, is over `SUB_SOURCE_DB` -4.0 dB; or, on the bass, bass.hpf found notes under `SUB_NOTE_HZ` 35 Hz (the notes above its high-pass still fill the sub).
- **Worked if:** each deeper step lowers that tilt by at least 0.5 dB and 60-100 Hz holds within 1 dB. The last step that passed is kept; the first failure stops the search.

### presence
- **Why:** dull or harsh guitars against their own low-mids are a fault. Releases in this genre sit within 1.5 dB of each other here.
- **Start:** a bell at 3 kHz, Q 0.7, on the owner: up +1.5, +3, +4.5, +6 dB, or down -1.5, -3, -4.5 dB (each direction checked in turn). Owners are the two steady buses (playing through at least half the full-band frames) with the most 2-4 kHz, the second only if the first falls short.
- **Tool:** `PeakFilter`.
- **Needed if:** the mix's 2-4 kHz minus 125-250 Hz, over the full-band frames, is more than 1 dB off `PRESENCE_TILT_DB` -2.0 dB in that direction.
- **Worked if:** the tilt moves at least 0.5 dB toward the rule and overshoots it by at most 1 dB. Gains stop once within 0.5 dB; the last that passed is kept. A rest over 1 dB goes to mix-bus.presence.

### air
- **Why:** air follows the cymbals as recorded: a bright cymbal source is left bright, a dull one gets lifted.
- **Start:** a high shelf at 7 kHz, Q 0.707, on the owner: +2, +4, +6 dB. Owners as for presence, by 8-16 kHz level.
- **Tool:** `HighShelfFilter`.
- **Needed if:** the mix's 8-16 kHz minus 1-4 kHz is more than 1 dB under `AIR_TILT_DB` -10.5 dB plus the cymbal shift: `CYMBAL_AIR_SLOPE` 1.0 × (`CYMBAL_AIR_REF_DB` -4.3 dB minus overheads.brightness).
- **Worked if:** as presence, upward only. A rest over 1 dB goes to mix-bus.air.

### top
- **Why:** the top octave sets how open the mix sounds, and follows the cymbals the same way as air.
- **Start:** a high shelf at 12 kHz, Q 0.707, on the owner: +2, +4, +6 dB. Owners as for presence, by 11.3-16 kHz level.
- **Tool:** `HighShelfFilter`.
- **Needed if:** the mix's 11.3-16 kHz minus 5.6-11.3 kHz is more than 1 dB under `TOP_TILT_DB` -10.3 dB plus the same cymbal shift.
- **Worked if:** as presence, upward only. A rest over 1 dB goes to mix-bus.top.

### balance.punch-targets
- **Why:** the punch that counts is the master's. The master costs some punch, so the mix must carry that much more.
- **Start:** after glue, a trial master (as master.limit, at the profile loudness) on the current mix measures the crest loss of kick low and snare body.
- **Tool:** none; a measure. balance.punch then runs again with these targets.
- **Needed if:** always.
- **Worked if:** logged as measured. Targets: `MASTER_KICK_PROMINENCE` 10.0 dB and `MASTER_SNARE_PROMINENCE` 12.5 dB, each plus its loss when the loss is positive.

## Not built

None: these moves are the engineer's own and have no research-plan counterpart left out.
