# Walk and talk (dead air removed)

## Re-cut

Changes from v1:

- Pacing: tight → loose
- Length: 0:08.1 → 0:08.5 (+0:00.4)

_All spoken material in recording order with dead air removed._

- **Rough cut:** 0:08, target 1:30 (91% under) (1920x1080, 24.000 fps)
- **Raw media:** 0:18 across 3 clips; 0:08 of transcribed speech
- **Stringout** (all dialogue, dead air removed): 0:08
- **Dead air removed:** 0:03 of 0:12 talking footage (29%)
- **Planned by:** heuristic
- **Importing:** If Final Cut asks about items with the same names, choose Keep Both: each version is its own project.

## Cut check

- Length: 0:08, target 1:30 (91% under)
- Jump cuts on camera with no B-roll over them: 1: 0:04 V01 (V01.S001, V01.S002 → V01.S003)

## Paper edit

### talk  `0:00`

- `0:00` **V01.S001** Welcome to Orange County.  
  <sub>talk.mp4 @ 0:00.0</sub>
- `0:01` **V01.S002** This weekend the food festival takes over the park.  
  <sub>talk.mp4 @ 0:01.5</sub>
- `0:04` **V01.S003** Over forty chefs are cooking, and tickets start at twenty dollars.  
  <sub>talk.mp4 @ 0:08.4</sub>

## Levels

Speech is matched to about -16 LUFS, B-roll and music sit under it. Volume is each clip's Volume in Final Cut's audio inspector; change it there, or set `--dialogue-level` and run `roughcut render`.

| Clip | Used as | Measured | Volume | Note |
|---|---|---|---|---|
| talk.mp4 | dialogue | -14.0 LUFS (speech) | -2 dB |  |

## Media

| ID | File | Role | Length | Notes |
|---|---|---|---|---|
| V01 | talk.mp4 | A-Roll | 0:12 | Welcome to Orange County. |
| V02 | broll_park.mp4 | B-Roll | 0:06 |  |
| I01 | still_festival.jpg | Stills | still |  |
