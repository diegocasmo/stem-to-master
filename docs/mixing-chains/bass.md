# Bass

Goal: a solid, even low end that locks with the kick, and a growl in the mids that is heard on small speakers.

### bass.hpf
- **Why:** rumble below the lowest note eats headroom. A part that mostly plays under drop D puts its fundamentals in the sub, where few speakers play them and they cost the limiter headroom, so the bass gives those up and the second harmonic carries the notes. A part that only dips there keeps them.
- **Start:** the lowest note is the 3rd percentile of the notes' fundamentals (`notes.py`; 41 Hz if none found), the typical note their median. If the typical note is under `SUB_NOTE_HZ` 35 Hz, a 24 dB/oct high-pass at `SUB_HPF_HZ` 41 Hz (the standard low E); else at 0.7 × the lowest note (20 Hz at least, the filter's floor).
- **Tool:** `FreeEQ`.
- **Needed if:** the typical note is under `SUB_NOTE_HZ`; else energy in 15 Hz to 0.85 × the lowest note is within 10 dB of the lowest note's band (0.9 to 1.12 × it).
- **Worked if:** the band under the cutoff (15 Hz to `SUB_NOTE_HZ`, or to 0.7 × the lowest note) falls by at least 1 dB, and the band that carries the note holds within 1 dB (1.9 to 3.2 × the typical note when it is under `SUB_NOTE_HZ`, else 0.95 to 1.6 × the lowest note).

### bass.rides
- **Why:** levelling single notes by hand first means less compression later and more life kept.
- **Start:** measures the note level spread (p90 minus p10) over the whole song. No ride is built.
- **Tool:** none.
- **Needed if:** never tried; logged with the spread for the record.
- **Worked if:** never kept. Dropped as "compression evens the notes" when the spread is over 3 dB, else "not needed".

### bass.compress
- **Why:** a heavy-music bass must be almost flat in level so the low end is solid.
- **Start:** one compressor after the high-pass: threshold the median note level -8 dB, 4:1, attack 3 ms, release 60 ms, hard knee (ZamCompX2 4.5's soft knee lifts the level under the threshold), no makeup.
- **Tool:** `ZamCompX2`.
- **Needed if:** note levels spread more than 3 dB (p90 minus p10, more than 4 notes) over the whole song.
- **Worked if:** the spread of the energy in the 100 ms after each note onset falls by more than 0.5 dB.

### bass.saturate
- **Why:** small speakers play a bass through its harmonics. A bass with little between 700 Hz and 2 kHz disappears on them, so a parallel crush adds that band.
- **Start:** after the compressor, the bass is staged to -1 dBFS peak, then a bit crush at 8, 6, then 4 bits (intensity 12, mix 30 %), then the level comes back. The first depth that passes is kept.
- **Tool:** `Gain`, `FreeCrush`, `Gain`.
- **Needed if:** the bass's 700 Hz-2 kHz is more than `GROWL_DB` 20 dB under its 40-200 Hz.
- **Worked if:** that difference rises by at least 2 dB and the note spread (as bass.compress measures it) rises by at most 0.5 dB.

## Not built

- bass.align: not built yet; two bass tracks are summed as recorded.
- bass.split: not built yet; each bass track gets the same chain.
- bass.mud-cut: lowmid.cut (context.md) cuts a build-up on the bass when it owns it.
- bass.growl: no song needed it yet.
- bass.multiband: no song needed it yet.
- bass.chorus: no song needed it yet.
- bass.limit: no song needed it yet.

The kick-keyed duck on the bass is kick.duck-bass. A sub cut on the bass bus is sub.cut (context.md).
