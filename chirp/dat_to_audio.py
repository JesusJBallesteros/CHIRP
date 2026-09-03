#!/usr/bin/env python3
"""
Convert Intan RHD ("one file per channel") .dat recordings into listenable audio.

Each amp-<port>-<chan>.dat in this folder is a headerless stream of int16
samples (little-endian) at the rate declared in settings.xml / info.rhd
(30 kHz here), scaled 0.195 uV per bit.

Default behaviour: pick one file at random, take 20 s from the middle,
band-pass 400-8500 Hz, and write a 16-bit WAV into ./audio.

Examples
--------
    python dat_to_audio.py                          # random channel, 20 s, middle
    python dat_to_audio.py amp-A-017.dat            # a specific channel
    python dat_to_audio.py --duration 60 --start 300
    python dat_to_audio.py --all --duration 10      # every channel in the folder
    python dat_to_audio.py --band 300 6000 --resample 44100
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

import numpy as np
from scipy import signal
from scipy.io import wavfile

# ---------------------------------------------------------------- constants --
# Where the tool looks for amp-*.dat. The working directory, not the script
# directory: the scripts live in the repo, the recordings live elsewhere.
FOLDER = Path.cwd()
SAMPLE_RATE = 30000          # Hz, from settings.xml (SampleRateHertz)
UV_PER_BIT = 0.195           # Intan RHD2000 amplifier scale factor
BYTES_PER_SAMPLE = 2         # int16
DEFAULT_BAND = (450.0, 8000.0)
FILTER_ORDER = 4             # per direction; sosfiltfilt doubles it
EDGE_PAD_S = 1.0             # extra signal read each side, trimmed after filtering


# ------------------------------------------------------------------ helpers --
def file_duration_s(path: Path, fs: int = SAMPLE_RATE) -> float:
    return path.stat().st_size / BYTES_PER_SAMPLE / fs


def read_segment(path: Path, start_s: float, duration_s: float,
                 fs: int = SAMPLE_RATE) -> np.ndarray:
    """Read [start_s, start_s+duration_s) from a headerless int16 .dat as float64 uV."""
    n_total = path.stat().st_size // BYTES_PER_SAMPLE
    start = int(round(start_s * fs))
    count = int(round(duration_s * fs))
    if start < 0 or start >= n_total:
        raise ValueError(f"start {start_s:.3f} s outside file "
                         f"(0-{n_total / fs:.3f} s)")
    count = min(count, n_total - start)
    with open(path, "rb") as fh:
        fh.seek(start * BYTES_PER_SAMPLE)
        raw = np.fromfile(fh, dtype="<i2", count=count)
    return raw.astype(np.float64) * UV_PER_BIT


def bandpass(x: np.ndarray, low: float, high: float, fs: int,
             order: int = FILTER_ORDER) -> np.ndarray:
    """Zero-phase Butterworth band-pass, second-order sections for stability."""
    nyq = fs / 2.0
    if not 0 < low < high < nyq:
        raise ValueError(f"band {low}-{high} Hz invalid for fs={fs} Hz "
                         f"(Nyquist {nyq} Hz)")
    sos = signal.butter(order, [low / nyq, high / nyq], btype="bandpass",
                        output="sos")
    return signal.sosfiltfilt(sos, x)


def normalize(x: np.ndarray, headroom_db: float = 1.0,
              percentile: float = 99.98):
    """
    Scale towards full scale using a high percentile rather than the raw peak,
    so a single stimulation artifact does not push the whole track into silence.
    Anything above the resulting ceiling is hard-clipped.
    Returns (signal, reference_uV, n_clipped).
    """
    ref = float(np.percentile(np.abs(x), percentile))
    if ref <= 0:
        ref = float(np.max(np.abs(x)))
    if ref <= 0:
        return np.zeros_like(x), 0.0, 0
    target = 10.0 ** (-headroom_db / 20.0)
    y = x / ref * target
    n_clipped = int(np.count_nonzero(np.abs(y) > 1.0))
    return np.clip(y, -1.0, 1.0), ref, n_clipped


def to_pcm(y: np.ndarray, bit_depth: int) -> np.ndarray:
    if bit_depth == 16:
        return np.round(y * 32767.0).astype(np.int16)
    if bit_depth == 32:
        return y.astype(np.float32)          # WAV IEEE float, widely readable
    raise ValueError("bit_depth must be 16 or 32")


# --------------------------------------------------------------- conversion --
def convert(path: Path, out_dir: Path, duration: float, start,
            band, fs: int, resample, bit_depth: int,
            headroom_db: float, verbose: bool = True,
            out_name: str | None = None) -> Path:
    total_s = file_duration_s(path, fs)
    if duration > total_s:
        raise ValueError(f"{path.name} is only {total_s:.1f} s long")
    if start is None:                                   # centre of the file
        start = (total_s - duration) / 2.0
    if start < 0 or start + duration > total_s:
        raise ValueError(f"{start:.1f}+{duration:.1f} s exceeds "
                         f"{path.name} ({total_s:.1f} s)")

    # Read with padding so filter edge transients land outside the kept window.
    pad = min(EDGE_PAD_S, start, total_s - (start + duration))
    seg = read_segment(path, start - pad, duration + 2 * pad, fs)

    filt = bandpass(seg, band[0], band[1], fs)
    n_pad = int(round(pad * fs))
    if n_pad:
        filt = filt[n_pad:n_pad + int(round(duration * fs))]

    out_fs = fs
    if resample and int(resample) != fs:
        g = int(np.gcd(int(resample), int(fs)))
        filt = signal.resample_poly(filt, int(resample) // g, int(fs) // g)
        out_fs = int(resample)

    y, ref_uv, n_clipped = normalize(filt, headroom_db=headroom_db)
    pcm = to_pcm(y, bit_depth)

    out_name = out_name or (f"{path.stem}_{start:.0f}s+{duration:.0f}s_"
                            f"{band[0]:.0f}-{band[1]:.0f}Hz.wav")
    if not out_name.lower().endswith(".wav"):
        out_name += ".wav"
    out_path = out_dir / out_name
    wavfile.write(out_path, out_fs, pcm)

    if verbose:
        print(f"{path.name}: {total_s / 60:.1f} min total -> "
              f"{start:.1f}-{start + duration:.1f} s")
        print(f"  band-pass {band[0]:.0f}-{band[1]:.0f} Hz "
              f"(Butterworth order {FILTER_ORDER}, zero-phase)")
        print(f"  scale reference {ref_uv:.1f} uV"
              + (f", {n_clipped} samples clipped" if n_clipped else ""))
        print(f"  -> {out_path.name}  "
              f"({out_fs} Hz, {bit_depth}-bit, "
              f"{out_path.stat().st_size / 1e6:.2f} MB)")
    return out_path


# --------------------------------------------------------------------- main --
def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Intan .dat -> WAV excerpts (band-passed).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("files", nargs="*", type=Path,
                   help="specific .dat files (default: one picked at random)")
    p.add_argument("--all", action="store_true",
                   help="convert every amp-*.dat in the folder")
    p.add_argument("-d", "--duration", type=float, default=10.0,
                   help="seconds to extract")
    p.add_argument("-s", "--start", type=float, default=None,
                   help="start time in seconds (default: centred in the file)")
    p.add_argument("-b", "--band", type=float, nargs=2,
                   metavar=("LOW", "HIGH"), default=list(DEFAULT_BAND),
                   help="band-pass corners in Hz")
    p.add_argument("--fs", type=int, default=SAMPLE_RATE,
                   help="acquisition sample rate of the .dat files")
    p.add_argument("--resample", type=int, default=None,
                   help="output sample rate, e.g. 44100 (default: keep --fs)")
    p.add_argument("--bit-depth", type=int, choices=(16, 32), default=16,
                   help="16 = PCM, 32 = IEEE float")
    p.add_argument("--headroom", type=float, default=1.0,
                   help="dB below full scale for the normalisation ceiling")
    p.add_argument("--folder", type=Path, default=None,
                   help="folder holding the amp-*.dat files "
                        "(default: current directory)")
    p.add_argument("-o", "--out-dir", type=Path, default=None,
                   help="output directory (default: <folder>/audio)")
    p.add_argument("--seed", type=int, default=None,
                   help="seed for the random file pick (reproducibility)")
    args = p.parse_args(argv)

    folder = (args.folder or Path.cwd()).resolve()
    if args.out_dir is None:
        args.out_dir = folder / "audio"
    available = sorted(folder.glob("amp-*.dat"))
    if not available:
        print(f"No amp-*.dat files found in {folder}", file=sys.stderr)
        return 1

    if args.all:
        targets = available
    elif args.files:
        targets = [f if f.is_absolute() else folder / f for f in args.files]
        missing = [t for t in targets if not t.is_file()]
        if missing:
            print("Not found: " + ", ".join(str(m) for m in missing),
                  file=sys.stderr)
            return 1
    else:
        rng = random.Random(args.seed)
        targets = [rng.choice(available)]
        print(f"Randomly selected {targets[0].name} "
              f"of {len(available)} channels")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    failures = 0
    for t in targets:
        try:
            convert(t, args.out_dir, args.duration, args.start,
                    (args.band[0], args.band[1]), args.fs, args.resample,
                    args.bit_depth, args.headroom)
        except Exception as exc:                       # keep going on --all
            print(f"{t.name}: FAILED - {exc}", file=sys.stderr)
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
