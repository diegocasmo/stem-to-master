# Mixing chains

The moves the engineer (`tools/engineer.py`) makes for each source in heavy instrumental music, with the reason for every move and how it is measured. This is the factory's only mixing reference. The code is the source of truth; these files describe it.

Each move is a heading `### <id>`, where `<id>` is exactly the id the engineer logs in the recipe's `decisions` and puts on the inserts it keeps. Recipes and checks name moves by it. Under it: **Why**, **Start**, **Tool**, **Needed if**, **Worked if**. Moves on one source live in the file of their chain. [context.md](context.md) holds the moves on the whole song or the full mix (classification, balance, low-mid, sub, presence, air, top), with the ids the engineer logs. Each file ends with `## Not built`: moves engineers use that the engineer does not make yet.

| File | Covers |
|---|---|
| [context.md](context.md) | Stem classification, full-band frames, balance and punch faders, full-mix tone moves |
| [kick.md](kick.md) | Kick mics and samples |
| [snare.md](snare.md) | Snare top, bottom and samples |
| [toms.md](toms.md) | Toms |
| [overheads.md](overheads.md) | Overheads, hi-hat, cymbal spots, room mics |
| [drum-bus.md](drum-bus.md) | The drums bus |
| [bass.md](bass.md) | Bass |
| [guitars.md](guitars.md) | Rhythm and lead guitars |
| [synths.md](synths.md) | Synths, pads, piano |
| [mix-bus.md](mix-bus.md) | Whole mix while mixing |
| [master.md](master.md) | The master and the export gates |

## Order

`Engineer.stages()` runs these stages in order; `tools/run.py` then exports. Within a file, moves are listed in this order.

1. **setup:** classify the stems (in the constructor), pan guitars, toms and close cymbals, align kick and snare mics, find the full-band frames.
2. **tracks:** per-track inserts, in parallel workers: the toms gate or edit, snare, overhead and close-cymbal high-passes, the room chain, bass, guitar and synth filters.
3. **drums:** kick and snare mic and sample blends, close-cymbal and guitar-pair faders; then on the kick bus the tail, weight and mud cut, and the snare ring cut.
4. **balance:** static bus faders against the reference group, then kick and snare faders for punch.
5. **context:** on the full mix: kick-keyed ducks, low-mid cut, sub cut, presence, air, top, lead space, kick click, snare crack.
6. **glue:** drum-bus compression, kept only if punch holds.
7. **rebalance:** a trial master measures the punch the master costs; the kick and snare faders are set again to targets that add it back.
8. **master:** drive, clip and limiter, at the profile loudness or a step below it.
9. **export:** render the recipe, write the 24-bit master, check the gates.

## How to try a move

Each move has **Needed if** (a measured condition that justifies a trial) and **Worked if** (the measured change that keeps it).

- An insert is rendered on and off on the same node (track or bus), from cached audio. Candidates are tried in a fixed order; the move keeps the first that passes, unless its section says otherwise.
- Hits are found once, on the input, and every one is measured again after processing. Finding them again on the output would hide the soft hits a step lost.
- Source moves are judged on the source. Context moves are judged on the full mix with the candidate in place (`mix_of`: the drums bus and mix bus run through their inserts). Tone and balance moves (`balance.fader`, `lowmid.cut`, `presence`, `air`, `top`, `guitars.lead-space`) read the full-band frames; prominence (punch faders, ducks, `kick.click`, `snare.crack`, `drum-bus.glue`) is read over the whole song; `sub.cut` is judged on its own bus.
- A move applies to each source it names: a song with five toms decides `toms.clean` for each tom.
- Full-mix limits are rules in `Engineer` (for example `KICK_LOW_PROMINENCE`), the same for every song. The profile's references are only a printed comparison at export; nothing reads them back.
- A rule changes only with measurements from a song, and the reason goes in the code next to the constant.

No published metric measures punch on a finished master, and a master can sit inside the references' range on every proxy and still sound amateur. So the engineer judges punch by its own hit measures, and the master by how much punch it costs.

## Measures

- **Hit times:** `punch.drum_hits` finds kick hits in 30-150 Hz and snare hits in 1-8 kHz (`hits.py` detection on the band's energy), so bleed of the other drum is not counted. Other sources use `hits.py` on the full band.
- **Prominence:** `punch.prominence`, per hit: band power in 5 ms frames (4th-order zero-phase band-pass), the smoothed peak from 30 ms before to 40 ms after the hit, over the median of 130-50 ms before and 70-150 ms after, in dB. The median over hits is used. Bands: kick low 40-120 Hz, kick click 2-6 kHz, snare body 150-350 Hz, snare crack 2-8 kHz.
- **Kick tail:** `punch.tail`, per hit: the 40-120 Hz level 80-180 ms after the hit over its peak from 10 ms before to 40 ms after, in dB. Higher is a longer tail.
- **Hit stats:** `hits.py` per hit: level (envelope peak in the first 25 ms), attack (energy 0-25 ms over 30-100 ms), tail (80-200 ms over 0-30 ms), and off-hit bleed (mean power outside 20 ms before to 200 ms after hits, over mean power in the first 100 ms of hits, per band).
- **Band level:** power in a band of the long-term spectrum (Welch, 8192 points); third-octave levels for the low-mid build-up.
- **Masked band level:** band power only inside a sample mask, such as the samples between kick hits (`meters.masked_band_db`).
- **Sum gain:** energy of two summed mics over the sum of their energies, per band, in the first 100 ms of hits. Below 0 dB the mics cancel.
- **Note spread:** p90 minus p10 of note levels (`notes.py` finds the notes and their levels).
- **Side/mid:** side energy over mid energy in a band, in dB.
- **Loudness and peak:** integrated LUFS (pyloudnorm) and true peak (4x oversampled).
- **Crest loss:** each prominence in the mix minus the same in the master, in dB.
- **Export gates:** loudness, true peak, crest loss, kick and snare prominence in the master, and side/mid width. See [master.md](master.md).
- **Holds:** falls by at most 0.5 dB (`HOLD_DB`), unless the move gives another number.

Standalone tools: `analyze.py` measures whole-file loudness, peaks, octave bands and width; `hits.py` the hit stats; `measure.py` valley, overlap, limiter dip, section loudness and profile range; `notes.py` finds notes; `grade.py` compares a master with the official one. The engineer uses `hits.py` (through `meters` and `punch`), `notes.py` for the bass notes, and `measure.py`'s profile range for the printed comparison.

## Done rule

Every implemented move that applies to the song's stems is logged in the recipe's `decisions`, by id, with its target, an outcome and its numbers. The outcome is `kept` (the insert or setting is in the recipe; `kept: deepest, short of target` for kick.tail's fallback), `dropped: <reason>` (not needed, did not work, or not tried), or `measured` for a measure that later moves read. A move whose **Needed if** finds no fault is dropped with the numbers it checked, except these, which skip without a log entry when their precondition fails: `balance.punch` (under 4 hits), `kick.sample-blend` and `snare.sample-blend` (under 2 hits), `overheads.close-pan` (under 0.2 s), `overheads.close-blend` (under 0.5 s), and `balance.fader` (a silent bus, or toms with under 0.3 s of fills). Every kept insert carries the move's id.

## Limits

- Instrumental only: vocal stems (`vox`, `vocal`, `voice`) and DI stems are left out by name, and silent stems by level; the `stems.classify` decision lists them under `left_out`. DIs are never mixed in, re-amped or used as a timing reference. Sound effects follow synths.
- No new audio: no sample triggering beyond sample stems that already exist.
- pedalboard's `HighpassFilter` and `LowpassFilter` are 6 dB/oct. For steeper slopes use `FreeEQ` (`filter_eq`).

## Where this comes from

Interviews and walkthroughs with metal mix engineers and mixing research, checked by adversarial review. Each move states its own reason; these links are only for auditing that a move wasn't invented.

- Nolly Getgood: [Audiofanzine](https://en.audiofanzine.com/sound-technique/editorial/articles/tracking-and-mixing-with-nolly.html); [*Heaviness in Metal Music Production II*](https://pure.hud.ac.uk/en/publications/heaviness-in-metal-music-production-volume-ii-learn-from-the-mast/) (CC BY 4.0; also Buster Odeholm, Mike Exeter, Andrew Scheps).
- [SOS, "The Masters' Art Of Mixing"](https://soundonsound.com/techniques/masters-art-mixing) (Odeholm, Nolly, Bogren, Otero, Scheps, Exeter).
- Andy Sneap: [SOS](https://www.soundonsound.com/techniques/inside-track-judas-priest). Joey Sturgis: [URM drums](https://urm.academy/tt4-drum-mixing-tips-tricks/), [URM mastering](https://urm.academy/tt3-joey-sturgis-mastering-tips-tricks/). Jens Bogren: [URM 29](https://urm.academy/ep29-jens-bogren/), [URM 195](https://urm.academy/ep195-jens-bogren/). Ermin Hamidovic and Nolly: [URM 151](https://urm.academy/ep151-nolly-getgood-ermin-hamidovic).
- Mark Mynett: [SOS 2009](https://www.soundonsound.com/techniques/mixing-metal), [SOS part 3](https://www.soundonsound.com/techniques/making-modern-metal-part-3). Mike Senior: [phase](https://www.soundonsound.com/techniques/mix-rescue-phase-relationships), [metal](https://www.soundonsound.com/techniques/mix-rescue-mixing-metal), [No Logo](https://www.soundonsound.com/techniques/mix-rescue-no-logo), [bass](https://www.soundonsound.com/techniques/mixing-bass), [guitars](https://www.soundonsound.com/sound-advice/q-how-do-make-guitars-sound-more-epic).
- Bass: Dave Lockwood ([SOS](https://www.soundonsound.com/techniques/mixing-di-bass-guitar)), Paul White ([SOS](https://www.soundonsound.com/techniques/bass-guitars-mix)).
- Others: Dan Lancaster ([SOS](https://www.soundonsound.com/techniques/inside-track-bring-me-horizon)), Ghost's mixers ([SOS](https://www.soundonsound.com/techniques/inside-track-producing-mixing-ghost)), Tom Lord-Alge ([SOS](https://www.soundonsound.com/techniques/inside-track-tom-lord-alge)), Chris Lord-Alge ([SOS](https://www.soundonsound.com/techniques/secrets-mix-engineers-chris-lord-alge)), Taylor Larson ([URM](https://urm.academy/ep26-taylor-larson/)), Will Putney ([Mix](https://www.mixonline.com/recording/getting-heavy-with-will-putney)), Ian Shepherd ([iZotope](https://izotope.com/en/learn/how-to-master-metal.html)).
- Research: [Fenton et al. 2015, punch](https://eprints.hud.ac.uk/id/eprint/26288/); [De Man et al. 2017, reverb preference](https://aes.org/publications/elibrary/elibrary-page/?id=18548).
