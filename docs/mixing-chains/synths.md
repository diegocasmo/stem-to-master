# Synths and keys

Goal: synths, pads, piano and sound effects add colour and width without muddying the low end or masking the guitars.

### synths.hpf
- **Why:** full-range synths, pads and piano stack up low end that the kick and bass need.
- **Start:** the part's main band is the loudest octave from 125-250 Hz up to 4-8 kHz. A 12 dB/oct high-pass at 150, 120, 80, then 50 Hz: the highest that passes wins, and even a low one takes the sub rumble out.
- **Tool:** `ZL Equalizer 2`.
- **Needed if:** the part's 20-150 Hz is within 20 dB of its full band (20 Hz-20 kHz).
- **Worked if:** 20-45 Hz falls by at least 3 dB and the main band holds within 1 dB.

## Not built

- synths.role: every synth, keys or effects stem goes to the synths bus by name or content.
- synths.cut-in-context: no song needed it yet.
- synths.colour: no song needed it yet.
- synths.space: no song needed it yet.
