# CHIRP

**C**lustered **H**igh-frequency **I**ntan **R**ecording **P**layer

Turn raw Intan RHD `.dat` recordings into things you can actually watch and
listen to: band-passed audio excerpts, and videos of the spike waveforms
accumulating in sync with that audio, coloured by amplitude cluster.

Spikes played through a speaker chirp. Hence the name.

## Demo

[![CHIRP output preview](docs/demo.gif)](demo/SingleCh_20s_BP_450-8000Hz_2cluster.mp4)

**[▶ Watch the clip with sound](demo/SingleCh_20s_BP_450-8000Hz_2cluster.mp4)**
— one channel, 450–8000 Hz, 10 s in real time, two amplitude clusters. The
preview above is silent and runs at 2×; the clip is the real thing, spikes
appearing exactly as you hear them.

<!--
  The GIF above is a fallback, because GitHub does not render a player for a
  video committed to the repository - a relative link to an .mp4 becomes a
  download link, not a player.

  To get a real inline player with sound: open this file on github.com, click
  edit, and drag demo/SingleCh_20s_BP_450-8000Hz_2cluster.mp4 into the text
  area. GitHub uploads it and pastes a URL of the form

      https://github.com/user-attachments/assets/<uuid>

  Put that URL on a line of its own, right here, and it renders as a video
  player. Then the GIF and its link above can be deleted if you like.
-->

![An output frame](docs/example_frame.png)

*Top: the whole excerpt at a fixed µV scale, with detected troughs marked as the
playhead passes them. Bottom: one narrow pane per amplitude cluster, waveforms
overlaid and accumulating, aligned on the trough, running mean drawn bold.*

## The GUI

![The GUI](docs/gui.png)

---

## What it does

- Reads Intan **"one file per channel"** recordings (`amp-<port>-<chan>.dat`):
  headerless `int16` little-endian, 0.195 µV/bit. It seeks straight to the
  requested byte offset, so a 20 s excerpt from a 500 MB channel costs a 1 MB
  read, not 500 MB.
- **Zero-phase band-pass** (Butterworth SOS, `sosfiltfilt`), with padding read
  either side and trimmed afterwards so filter edge transients never reach the
  output.
- **WAV export** at the acquisition rate or resampled, 16-bit PCM or 32-bit
  float, normalised against a high percentile rather than the raw peak so one
  stimulation artifact cannot push the whole track into silence.
- **Spike detection**: negative crossings of −kσ, where σ = MAD/0.6745
  (Quiroga et al. 2004), taken at the trough with a refractory period.
- **Artifact rejection**: events whose waveform also reaches +kσ are discarded.
- **Amplitude clustering**: 1-D k-means over trough amplitudes, accepted only
  when the two groups are genuinely separated — otherwise a single colour.
- **Clean-window search**: scans the whole recording for the excerpt with the
  fewest artifacts and the clearest cluster split. A 136-minute recording
  sweeps in about 6 seconds.
- **MP4 rendering** with the audio muxed in. Frames and audio are generated
  from the same filtered array in one pass, so they cannot drift apart.

## Requirements

- Python 3.10+
- `numpy`, `scipy`, `matplotlib` (`pip install -r requirements.txt`)
- `tkinter` for the GUI — bundled with python.org and most distributions
  (on Debian/Ubuntu: `sudo apt install python3-tk`)
- **ffmpeg** on `PATH`, for MP4 output only. WAV export needs nothing extra.

## Quick start

```bash
git clone https://github.com/JesusJBallesteros/CHIRP.git
cd CHIRP
pip install -r requirements.txt
```

### With the GUI

```bash
python chirp/intan_gui.py
```

Browse to a recording folder, tick the channels, set the parameters across the
four tabs, press **Run**. Work happens on a background thread, so the window
stays responsive and **Cancel** interrupts a long render.

### Without the GUI

Both scripts look for `amp-*.dat` in the **current directory** unless you pass
`--folder`.

```bash
# 20 s from the middle of one channel, band-passed, as a WAV
python chirp/dat_to_audio.py --folder /path/to/recording amp-A-010.dat

# every channel in the current folder, 10 s each
cd /path/to/recording
python /path/to/CHIRP/chirp/dat_to_audio.py --all --duration 10

# spike video; omit --start and it finds a clean window for you
python chirp/dat_to_video.py --folder /path/to/recording amp-A-010.dat --duration 10

# keep spikes with a large positive rebound, and slow everything down 4x
python chirp/dat_to_video.py amp-A-010.dat --pos-k 12 --slow 4
```

`--help` on either script lists every option.

#### Options worth knowing

| Option | Meaning |
|---|---|
| `--folder` | where the `.dat` files live (default: current directory) |
| `--band LOW HIGH` | band-pass corners in Hz |
| `--duration` / `--start` | excerpt length and offset; blank start = auto |
| `--neg-k` / `--pos-k` | detection and artifact-rejection thresholds, in σ |
| `--window` / `--slow` | detail pane width in ms; slow-motion factor |
| `--resample` / `--bit-depth` | WAV output rate and depth |
| `--fs` | acquisition rate, if not 30 kHz (check `settings.xml`) |

> **On `--pos-k`.** Rejecting anything that crosses +7σ removes real artifacts,
> but on a healthy channel it also discards ordinary large spikes whose
> positive rebound happens to be big — in our test data about 13% of events,
> including the deepest spike in the window. If your artifacts are hundreds of
> µV while your spikes are tens, a much higher `--pos-k` rejects the former
> without touching the latter. Check the accepted/rejected counts it prints.

## Building the standalone Windows executable

The GUI can be packaged into a single double-clickable `.exe` that needs no
Python and no installation.

```bash
pip install pyinstaller
python chirp/build_exe.py
```

The result appears in `chirp/dist/`. Options:

| Command | Result |
|---|---|
| `python chirp/build_exe.py` | one `.exe`, ffmpeg bundled if found on `PATH` |
| `... --onedir` | a folder instead — starts in ~1 s rather than ~14 s |
| `... --no-ffmpeg` | ~25 MB instead of ~217 MB; needs ffmpeg on `PATH` |
| `... --ffmpeg C:\path\ffmpeg.exe` | bundle a specific ffmpeg build |
| `... --console` | keep a console window, for debugging |

The `.exe` is **not** committed here — it is far too large for git. Either
build it yourself with the above, or download it from the
[Releases](https://github.com/JesusJBallesteros/CHIRP/releases) page: pushing a
tag (`git tag v1.0.0 && git push origin v1.0.0`) builds it on GitHub and
attaches it automatically.

The release build ships **without** ffmpeg, because the usual Windows ffmpeg
builds are GPL and bundling one into an MIT release would pull the whole
download under the GPL. So the downloaded `.exe` exports WAV out of the box,
and needs ffmpeg installed for MP4. Building locally bundles whichever ffmpeg
you already have.

Notes on the executable:

- **Size.** ~217 MB, almost entirely bundled ffmpeg (211 MB) plus numpy, scipy
  and matplotlib. The project's own code is a few tens of kB.
- **Startup.** A one-file build unpacks to a temp folder on each launch, so the
  window takes ~14 s to appear. `--onedir` avoids this.
- **Antivirus.** Unsigned PyInstaller executables sometimes trip heuristic
  scanners. Building locally avoids the question entirely.
- **Platform.** Windows x64. Run `build_exe.py` on macOS or Linux to get a
  binary for those.
- **ffmpeg licensing.** ffmpeg is not redistributed in this repository. If you
  distribute a build with ffmpeg bundled, mind the licence of the ffmpeg build
  you used — the common Windows builds are GPL.

## Repository layout

```
CHIRP/
├── chirp/
│   ├── dat_to_audio.py    reading, filtering, WAV export  (importable engine)
│   ├── dat_to_video.py    detection, clustering, MP4 rendering
│   ├── intan_gui.py       tkinter front end
│   └── build_exe.py       PyInstaller packaging
├── demo/                  drop your demo video here
├── docs/                  screenshots used by this README
└── .github/workflows/     release build for the Windows .exe
```

`dat_to_video.py` imports its DSP from `dat_to_audio.py`, so the filtering and
scaling are defined once and both paths stay consistent.

## Data format

Verified against `settings.xml` (`SampleRateHertz="30000"`) and the `info.rhd`
header (magic `0xC6912702`) of the recordings this was written for. If your
acquisition rate differs, pass `--fs`. Only the "one file per channel" layout
is supported — not the single-file `.rhd` format.

## Licence

MIT — see [LICENSE](LICENSE).
