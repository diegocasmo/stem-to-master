# Bass

Goal: a solid, even low end that locks with the kick, and a growl in the mids that is heard on small speakers.

### bass.hpf
- **Why:** rumble below the lowest note eats headroom.
- **Start:** the lowest note is the 3rd percentile of the notes' fundamentals (`notes.py`; 41 Hz if none found). A 24 dB/oct high-pass at 0.7 × that.
- **Tool:** `ZL Equalizer 2`.
- **Needed if:** energy in 15 Hz to 0.85 × the lowest note is within 10 dB of the lowest note's band (0.9 to 1.12 × it).
- **Worked if:** 15 Hz to 0.7 × the lowest note falls by at least 1 dB and 0.95 to 1.6 × the lowest note holds within 1 dB.

### bass.rides
- **Why:** levelling single notes by hand first means less compression later and more life kept.
- **Start:** measures the note level spread (p90 minus p10) over the whole song. No ride is built.
- **Tool:** none.
- **Needed if:** never tried; logged with the spread for the record.
- **Worked if:** never kept. Dropped as "compression evens the notes" when the spread is over 3 dB, else "not needed".

### bass.compress
- **Why:** a heavy-music bass must be almost flat in level so the low end is solid.
- **Start:** one compressor after the high-pass: threshold the median note level -8 dB, 4:1, attack 3 ms, release 60 ms, knee 6 dB, style Clean.
- **Tool:** `ZL Compressor`.
- **Needed if:** note levels spread more than 3 dB (p90 minus p10, more than 4 notes) over the whole song.
- **Worked if:** the spread of the energy in the 100 ms after each note onset falls by more than 0.5 dB.

## Not built

- bass.align: not built yet; two bass tracks are summed as recorded.
- bass.split: not built yet; each bass track gets the same chain.
- bass.mud-cut: lowmid.cut (context.md) cuts a build-up on the bass when it owns it.
- bass.growl: no song needed it yet.
- bass.multiband: no song needed it yet.
- bass.saturate: no song needed it yet.
- bass.chorus: no song needed it yet.
- bass.limit: no song needed it yet.

The kick-keyed duck on the bass is kick.duck-bass. A sub cut on the bass bus is sub.cut (context.md).
