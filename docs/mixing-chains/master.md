# Master

Goal: reach the profile's loudness at or under -1 dBTP while keeping the kick and snare punch. The chain is drive, clipper, limiter, chosen as a whole in `master_stage`; only master.limit logs a decision, and its numbers cover all three.

### master.drive
- **Why:** sets how hard the mix hits the clipper and limiter, which sets the loudness.
- **Start:** gain to the target loudness, by a guarded secant search (at most 12 renders, steps of at most +6 dB, until within 0.003 LU).
- **Tool:** `Gain`.
- **Needed if:** always.
- **Worked if:** the chain lands within 0.003 LU of the target; else that clip level fails for that target.

### master.clip
- **Why:** a clipper shaves the shortest peaks, so the limiter does less to the drum hits. Some engineers never clip a master, so it is judged by the punch it costs.
- **Start:** a clipper at -2 dBFS, then at -3 dBFS, after the drive.
- **Tool:** pedalboard `Clipping`.
- **Needed if:** always tried.
- **Worked if:** the clip level whose master has the smaller worst crest loss (kick low, kick click, snare body, snare crack) wins.

### master.limit
- **Why:** a transparent true-peak limiter at the end brings the master to loudness safely.
- **Start:** ceiling `CEILING` -1.3 dB, true peak on, 4x oversampling, lookahead 2 ms, attack 10 ms, release 50 ms, recovery 10 ms. Targets from the profile's `master_integrated_lufs` (-7) down to `master_min_integrated_lufs` (-9) in 0.5 LU steps.
- **Tool:** `ZL Limiter`.
- **Needed if:** always.
- **Worked if:** the first target whose best chain has a worst crest loss at or under `CREST_LOSS_DB` 3.0 dB is kept. If none passes, the quietest target that reached loudness is kept, and its export gate fails.

## Export gates

`tools/run.py` renders the recipe, writes `renders/mix.wav` (float) and the 24-bit master at the stems' rate, and measures the file written. The master is `master.wav` if every gate passes, else `master-failed-gates.wav`; `checks.json` holds the numbers.

- **loudness:** within 0.05 LU of the chosen target, and between the profile floor and target (±0.05).
- **true_peak:** at or under `TARGET_TP` -1.0 dBTP.
- **crest_loss:** worst crest loss at or under `CREST_LOSS_DB` 3.0 dB.
- **kick_punch:** kick low prominence in the master at or over `KICK_GATE_DB` 8.5 dB.
- **snare_punch:** snare body prominence in the master at or over `SNARE_GATE_DB` 9.0 dB.
- **width:** side/mid in 500 Hz-8 kHz over the full-band frames at or over `MIN_SIDE_MID_DB` -12.0 dB.

## Not built

- master.compress: no song needed it yet.
- master.tone: the profile's references are a printed comparison only; nothing reads them back to EQ the master.
