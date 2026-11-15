# Overheads, hi-hat, cymbals and room

Goal: clear, wide cymbals that never get harsh, and a room that adds size without mud. In metal, overheads are mostly cymbal mics; the close mics carry the drums.

### overheads.close-pan
- **Why:** a hi-hat or cymbal spot mic panned away from where the overheads hear it smears the stereo picture.
- **Start:** in 0-60 ms after the close mic's hits, the overheads' 4-12 kHz RMS left (L) and right (R); pan = 2 × (R - L) / (R + L), clipped to ±1.
- **Tool:** track `pan`.
- **Needed if:** a mono hat, ride or cymbal close mic, stereo overheads, and at least 0.2 s inside the hit windows.
- **Worked if:** always kept; it is a placement, not a trial.

### overheads.hpf
- **Why:** the low end in overheads is drum spill that blurs the close mics and fights the kick.
- **Start:** a 12 dB/oct high-pass at 150 Hz, then 220 Hz.
- **Tool:** `FreeEQ`.
- **Needed if:** the overheads' 20-250 Hz is within 10 dB of their 2-8 kHz.
- **Worked if:** 20-150 Hz falls by at least 3 dB and 2-8 kHz holds within 0.5 dB.

### overheads.close-hpf
- **Why:** a hat, ride or cymbal close mic is there for detail; its low end is snare and kick spill.
- **Start:** a 12 dB/oct high-pass at 400 Hz.
- **Tool:** `FreeEQ`.
- **Needed if:** always tried on hat, ride and cymbal close mics.
- **Worked if:** 20-300 Hz falls by at least 3 dB and 4-12 kHz holds within 0.5 dB.

### overheads.room-blend.lpf
- **Why:** room mics give the kit size, but their cymbal spill adds wash in fast music.
- **Start:** a 12 dB/oct low-pass at 7 kHz on the room mic.
- **Tool:** `FreeEQ`.
- **Needed if:** the room's 6-20 kHz is within 20 dB of its full band (20 Hz-20 kHz).
- **Worked if:** 6-20 kHz falls by more than 3 dB.

### overheads.room-blend.cut
- **Why:** a room mic's 200-400 Hz is mud that blurs the kit.
- **Start:** -4 dB at 300 Hz, Q 0.8, after the low-pass.
- **Tool:** `PeakFilter`.
- **Needed if:** the room's 200-400 Hz is within 3 dB of its 80-160 Hz.
- **Worked if:** 200-400 Hz falls by more than 0.5 dB.

### overheads.room-blend.compress
- **Why:** heavy compression with a fast release brings up the room's tail, which is the size it adds.
- **Start:** threshold the room's median hit level -12 dB, 8:1, attack 5 ms, release 40 ms, knee 6 dB, style Clean. Hits are found on the filtered room.
- **Tool:** `ZL Compressor`.
- **Needed if:** always tried on a room mic.
- **Worked if:** the median hit tail (`hits.py`) rises by more than 1 dB.

### overheads.close-blend
- **Why:** a spot mic adds detail, but too loud it harms the snare tone and the stereo picture.
- **Start:** in 0-300 ms after the close mic's hits, the close mic's fader puts it 8 LU under the overheads, clipped to -30 to +6 dB.
- **Tool:** track `fader_db`.
- **Needed if:** the song has overheads, and a close mic in the cymbals group (not an overhead) has at least 0.5 s inside its hit windows.
- **Worked if:** always kept; it is a level match, not a trial.

### overheads.brightness
- **Why:** air and top follow the cymbals as recorded: a bright source is left bright, a dull one gets lifted.
- **Start:** the cymbal bus input's 8-16 kHz minus its 1-4 kHz, over the full-band frames.
- **Tool:** none; a measure.
- **Needed if:** the song has a cymbals bus (else `CYMBAL_AIR_REF_DB` -4.3 dB is used and nothing is logged).
- **Worked if:** logged as measured; air and top lower their rule by `CYMBAL_AIR_SLOPE` 1.0 × the distance between this and `CYMBAL_AIR_REF_DB`, either way.

## Not built

- overheads.snare-control: no song needed it yet.
- overheads.harsh-cut: presence (context.md) sets the 2-4 kHz balance on its owner instead.
- overheads.air: air and top (context.md) lift the source that owns the band instead.
- overheads.compress: no song needed it yet.
- overheads.hihat-blend: split into overheads.close-pan, overheads.close-hpf and overheads.close-blend.
- overheads.room-reverb: no song needed it yet.
