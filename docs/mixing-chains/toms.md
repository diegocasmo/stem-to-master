# Toms

Goal: full, round fills that cut through, with no wash from open tom mics between fills.

### toms.pan
- **Why:** toms spread across the image from high to low, as a drummer hears them, make fills move.
- **Start:** each tom's pitch is its spectral peak in 50-400 Hz. Highest to lowest pitch, mono toms pan evenly from -0.4 to +0.4; stereo toms stay at 0.
- **Tool:** track `pan`.
- **Needed if:** the song has two or more toms.
- **Worked if:** always kept; it is a placement, not a trial.

### toms.clean
- **Why:** tom mics stay open all song and add cymbal and snare spill. That washes out the kit and costs tightness.
- **Start:** a gate first: threshold 6 dB under the softest own hit's per-channel level (hits within 20 dB of its loudest), ratio 99.9, attack 0.4 ms, lookahead 2 ms, hold 300 ms, release 99.8 ms, hysteresis 3 dB (values on the plugin's own grid; release reads back 0.01 ms off). If the gate fails, an edit: keep each tom only around its own hits, from 10 ms before the hit until its 40 Hz-and-up power falls 40 dB under the hit's peak (at least 300 ms, at most 2 s). Gaps longer than 30 ms go to -80 dB with 10 ms ramps.
- **Tool:** `FreeGate`, else native `Ride` (`segments`, `ramp_ms` 10).
- **Needed if:** the tom has hits (else `dropped: no hits`), its off-hit bleed is above -25 dB, and there is at least one gap longer than 30 ms between its hits' decays (nothing to gate or mute otherwise).
- **Worked if:** bleed falls by at least 1 dB, no own hit loses more than 1 dB of level, and p10 of the attack change is no worse than -1 dB.

## Not built

- toms.hpf: no song needed it yet.
- toms.eq: no song needed it yet.
- toms.compress: no song needed it yet.
- toms.multiband: no song needed it yet.
- toms.saturate-clip: no song needed it yet.
- toms.transient: no song needed it yet.
- toms.limit: no song needed it yet.
- toms.reverb: there is no drum reverb to send to.
