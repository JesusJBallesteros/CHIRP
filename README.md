# CHIRP

**C**lustered **H**igh-frequency **I**ntan **R**ecording **P**layer

Turn raw Intan RHD `.dat` recordings into clips you can actually watch and
listen to: band-passed extracts from raw data, into audio and videos with the spike waveforms
accumulating as they appear, pre-clustered and coloured by amplitude.

Spikes played through a speaker chirp.

## Demo

[![CHIRP output preview](docs/demo.gif)](demo/SingleCh_10s_BP_450-8000Hz_2cluster.mp4)

**[▶ Watch the clip with sound](demo/SingleCh_10s_BP_450-8000Hz_2cluster.mp4)**
— one channel, 450–8000 Hz, 10 s in real time, two amplitude clusters. The
preview above is silent and runs at 2×; the clip is the real thing, spikes
appearing exactly as you hear them.

*Top: the whole window at a fixed voltage scale, with detected troughs marked as the
playhead passes them. Bottom: narrow panes per amplitude cluster, waveforms
overlaid and accumulating, aligned on the trough, plus the running mean in bold.*

## The GUI

![The GUI](docs/gui.png)

---

## What it does

- Reads Intan's **"one file per channel"** recordings (`amp-<port>-<chan>.dat`):
  headerless `int16` little-endian, 0.195 µV/bit. It seeks straight to the
  requested byte offset.
- **Zero-phase band-pass** (Butterworth, `sosfiltfilt`), padded on both
  sides and trimmed afterwards, no filter edge transients.
- **WAV export** at the acquisition rate or resampled, 16-bit PCM or 32-bit
  float, volume normalised against a high percentile, not the raw.
- **Spike detection**: negative crossings of user defined units of sigma (−kσ), where σ = MAD/0.6745
  (Quiroga et al. 2004), taken at the trough with an adjustable refractory period.
- **Artifact rejection**: events whose waveform reaches +qσ are discarded.
- **Amplitude clustering**: 1-D k-means over trough amplitudes, trying 1 to 3
  clusters and keeping the largest count whose split is real — every cluster
  populated, and *every adjacent pair* of centres separated by at least a
  configurable multiple of their summed spread. Otherwise it falls back to a
  single group. Very simple but sufficient clustering.
- **Clean-window search**: scans the whole recording for the excerpt with the
  fewest artifacts and the clearest cluster split. Two-hour recordings are scanned in about 6 seconds.
- **MP4 rendering** with audio muxed in. Frames and audio are generated
  from the same filtered array in one pass. No drift.
- **Cluster statistics** (optional): spike count, mean trough amplitude,
  firing rate in sp/s, SNR (|mean trough| / σ), spike half-width and
  trough-to-peak latency. Computed over the **best three non-overlapping
  windows per channel**, not just the one that gets rendered, so a firing rate
  rests on more than a single excerpt. Written as two CSVs — one row per
  channel × segment × cluster, plus a file averaged across channels and
  segments — and shown in the GUI table as the run progresses, with a summary
  report at the end.

> Half-width and trough-to-peak shift with the band-pass, so compare them only
> between recordings filtered the same way.

## Requirements

- Python 3.10+
- `numpy`, `scipy`, `matplotlib` (`pip install -r requirements.txt`)
- `tkinter` for the GUI — bundled with python.org and most distributions
  (on Debian/Ubuntu: `sudo apt install python3-tk`)
- **ffmpeg** on `PATH`, for MP4 output only.

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

Browse to a recording folder, select the channel/s, set the parameters,
press **Run**. Work happens on a background thread, so the window
stays responsive and **Cancel** interrupts a long render.

### Without the GUI

Scripts look for `amp-*.dat` in the **current directory** unless you pass
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

#### Some options

| Option | Meaning |
|---|---|
| `--folder` | where the `.dat` files live (default: current directory) |
| `--band LOW HIGH` | band-pass corners in Hz |
| `--duration` / `--start` | excerpt length and offset; blank start = auto |
| `--neg-k` / `--pos-k` | detection and artifact-rejection thresholds, in σ |
| `--artifact-k` | σ above which the clean-window scan calls a window dirty (default 18) |
| `--max-clusters` | most amplitude clusters to consider, 1–3 (default 3) |
| `--window` / `--slow` | detail pane width in ms; slow-motion factor |
| `--wave-width` | width of each cluster pane, as a fraction of the frame |
| `--resample` / `--bit-depth` | WAV output rate and depth |
| `--fs` | acquisition rate, if not 30 kHz (check `settings.xml`) |

> **On `--pos-k`.** Rejecting anything that crosses +kσ removes real artifacts,
> but on a healthy channel it also discards ordinary large spikes whose
> positive rebound happens to be big. If your artifacts are hundreds of
> µV while your spikes are tens, a higher `--pos-k` rejects the former
> without touching the latter. Check the accepted/rejected counts it prints.

## Building the standalone Windows executable

The GUI can be packaged into a single `.exe`.

```bash
pip install pyinstaller
python chirp/build_exe.py
```

The result appears in `chirp/dist/`. Options:

| Command | Result |
|---|---|
| `python chirp/build_exe.py` | one `.exe`, ffmpeg bundled if found on `PATH` |
| `... --onedir` | unpack on a permanent folder instead of a temporal one, faster start-up |
| `... --no-ffmpeg` | lighter executable; needs ffmpeg on `PATH` |
| `... --ffmpeg C:\path\ffmpeg.exe` | bundle a specific ffmpeg build |
| `... --console` | keep a console window, for debugging |

An `.exe` file is **not** committed. Either build it yourself with the above,
or download it from the [Releases](https://github.com/JesusJBallesteros/CHIRP/releases) 
page. The release build ships **without** ffmpeg, because of licencing reasons.

The downloaded `.exe` exports WAV out of the box, but needs ffmpeg installed for MP4.
Building locally bundles whichever ffmpeg you already have.

Notes on the executable:

- **Size.** ~217 MB: ffmpeg alone is 211 MB plus numpy, scipy
  and matplotlib. The project's code is a few tens of kB.
- **Startup.** A one-file build unpacks on each launch, so the
  application takes ~14 s to start up. `--onedir` avoids this.
- **Antivirus.** Unsigned PyInstaller executables sometimes trip heuristic
  scanners. Building locally avoids the issue.
- **Platform.** Windows x64. Run `build_exe.py` on macOS or Linux to get a
  binary for those.
- **ffmpeg licensing.** ffmpeg is not redistributed in this repository. If you
  distribute a build with ffmpeg bundled, mind the licence of the ffmpeg build
  you used.

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

Verified against INTAN's `settings.xml` and `info.rhd` metadata of the 
recordings this was written for. If your acquisition rate differs, 
pass `--fs`. Tested on "one file per channel" layout.

## Licence

MIT — see [LICENSE](LICENSE).
