# Guitars

Goal: wide, heavy, tight rhythm guitars that leave room for the kick, bass, snare crack and leads; leads that sit on top.

### guitars.pan
- **Why:** double-tracked rhythms panned hard left and right make the width; a part with no double belongs in the middle.
- **Start:** among mono guitar tracks, a pair whose names mark a double track (`DT` suffix, same base name) pairs first; then pairs whose 50 ms level envelopes correlate at 0.75 or more, highest first. Each pair goes to -1 and +1; the rest stay at 0.
- **Tool:** track `pan`.
- **Needed if:** always run on the guitar group.
- **Worked if:** always kept; the log lists pairs, correlations and centred parts.

### guitars.filters
- **Why:** guitar lows fight the kick and bass. A hard low cut can kill the chug, so the cutoff is the highest that keeps it.
- **Start:** a 24 dB/oct high-pass at 100, 80 or 60 Hz: the highest that keeps 80-120 Hz within 1 dB. Only that one is tried.
- **Tool:** `FreeEQ`.
- **Needed if:** always tried on each guitar track.
- **Worked if:** 20-70 Hz falls by more than 0.5 dB and 80-120 Hz holds within 1 dB. No cutoff holding 80-120 Hz means dropped.

### guitars.resonance
- **Why:** cab and room resonances make guitars boomy or harsh, but a note's fundamental is not a resonance.
- **Start:** nothing is tried: a notch found on the long-term spectrum is not validated across notes.
- **Tool:** none.
- **Needed if:** never tried; logged for each guitar track.
- **Worked if:** never kept ("dropped: not tried").

### guitars.pair
- **Why:** unequal levels in a double-tracked pair pull the image to one side.
- **Start:** each pair from guitars.pan: left and right loudness over the full-band frames; each fader moves half the difference.
- **Tool:** track `fader_db`.
- **Needed if:** guitars.pan found a pair.
- **Worked if:** always kept; it is a level match, not a trial.

### guitars.lead-space
- **Why:** a lead or melody that plays alone in the middle needs its own space to sit above the rhythms.
- **Start:** a `gtr-space` bus with a stereo delay (250 ms left, 375 ms right, feedback 0.3, wet only; the plugin glides its dry level out over the render's first ~0.1 s). Sends from the centred guitars at -16, -12, -8, -4 dB.
- **Tool:** `FreeDelay` on a bus fed by sends.
- **Needed if:** a guitar panned at 0 (stereo ones only if their side/mid is -10 dB or less) plays within 30 dB of its peak in at least half the full-band frames.
- **Worked if:** the full mix's 500 Hz-2 kHz side/mid rises by at least 0.5 dB and that band's level by at most 1 dB. Sends rise until it widens by 1.5 dB or a send fails after one passed; the last passing send is kept, else the bus is removed.

## Not built

- guitars.align: not built yet; separate takes are never aligned to each other.
- guitars.side-tones: no song needed it yet.
- guitars.mud-cut: lowmid.cut (context.md) cuts a build-up on the guitars when they own it.
- guitars.multiband: no song needed it yet.
- guitars.mirror-eq: there is no bass growl move to mirror.
- guitars.lead-room: presence (context.md) and the click and crack boosts handle 2-4 kHz instead.
- guitars.compress: no song needed it yet.
- guitars.saturate: no song needed it yet.
- guitars.rides: songs have no marked sections.
- guitars.lead: guitars.lead-space is the only lead move.
