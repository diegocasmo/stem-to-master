# Mix bus

Goal: finish the full-mix tone moves that one source cannot do alone. These steps are part of the mix; the master comes later.

The mix bus takes the rest of a tone move from context.md (presence, air, top) after its owners: when the two sources that own the band leave it more than 1 dB off the rule. Its gain is not judged by a render.

### mix-bus.presence
- **Why:** dull or harsh guitars against their own low-mids are a fault; what the owning sources cannot fix, the whole mix gets.
- **Start:** a bell at 3 kHz, Q 0.7, with gain the remaining distance to `PRESENCE_TILT_DB` -2.0 dB, capped at 3 dB, in the direction of the move (boost or cut).
- **Tool:** `PeakFilter` on `mix`.
- **Needed if:** after presence on its owners, the mix's 2-4 kHz minus 125-250 Hz is still more than 1 dB off the rule in that direction.
- **Worked if:** always kept when needed; not judged.

### mix-bus.air
- **Why:** cymbals and guitars that leave the mix dull need a lift across the whole mix.
- **Start:** a high shelf at 7 kHz, Q 0.707, gain the remaining distance to the air rule, capped at 3 dB.
- **Tool:** `HighShelfFilter` on `mix`.
- **Needed if:** after air on its owners, the mix's 8-16 kHz minus 1-4 kHz is still more than 1 dB under `AIR_TILT_DB` -10.5 dB plus the cymbal shift (overheads.brightness).
- **Worked if:** always kept when needed; not judged.

### mix-bus.top
- **Why:** the top octave sets how open the mix sounds.
- **Start:** a high shelf at 12 kHz, Q 0.707, gain the remaining distance to the top rule, capped at 3 dB.
- **Tool:** `HighShelfFilter` on `mix`.
- **Needed if:** after top on its owners, the mix's 11.3-16 kHz minus 5.6-11.3 kHz is still more than 1 dB under `TOP_TILT_DB` -10.3 dB plus the cymbal shift.
- **Worked if:** always kept when needed; not judged.

## Not built

- mix-bus.trial-master: the trial master is a measure in rebalance (balance.punch-targets), not a step in the mix.
- mix-bus.console: no song needed it yet.
- mix-bus.compress: no song needed it yet.
- mix-bus.tone: presence, air, top and their mix-bus steps set the tone instead.
- mix-bus.rides: songs have no marked sections.
