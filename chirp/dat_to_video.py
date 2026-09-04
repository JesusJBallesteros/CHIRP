#!/usr/bin/env python3
"""
Render an Intan .dat excerpt as a spike-sorting video, in sync with its audio.

Two panes:

  * top    - the whole excerpt at a fixed +/-100 uV scale, so ordinary spikes
             fill the pane; anything larger is deliberately clipped. Detected
             events appear as dots at their trough as the playhead reaches them.
  * bottom - every accepted spike waveform, overlaid and accumulating in step
             with the audio, aligned on its trough, with a running mean per
             cluster drawn bold.

Detection: a spike is a negative crossing of -6 sigma (sigma = MAD/0.6745),
taken at the trough, with a 1 ms refractory period. An event is rejected as an
artifact if its waveform also reaches +7 sigma. Accepted troughs are clustered
into large- and small-amplitude groups by 1-D k-means, and coloured separately
when the two groups are genuinely separated.

Audio and video come from the same filtered array in one pass, so they cannot
drift apart. Filtering and file layout are imported from dat_to_audio.py.

Requires ffmpeg on PATH (frames are piped to it as rawvideo).

Examples
--------
    python dat_to_video.py                        # auto-picks a clean window
    python dat_to_video.py amp-A-010.dat --start 2000
    python dat_to_video.py amp-A-010.dat --pos-k 12   # keep big-rebound spikes
    python dat_to_video.py amp-A-010.dat --slow 4
"""

from __future__ import annotations

import argparse
import random
import os
import shutil
import subprocess
import tempfile
import sys
from pathlib import Path

import numpy as np

# Console messages carry sigma/micro signs; a cp1252 Windows console would
# otherwise raise UnicodeEncodeError mid-render.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):            # already wrapped, or a pipe
        pass

import matplotlib
matplotlib.use("Agg")
# The object API rather than pyplot: pyplot keeps global figure state that is
# not thread-safe, and the GUI drives rendering from a worker thread.
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg

from dat_to_audio import (
    FOLDER, SAMPLE_RATE, DEFAULT_BAND, FILTER_ORDER, EDGE_PAD_S,
    file_duration_s, read_segment, bandpass, normalize, to_pcm,
)
from scipy.io import wavfile

# ------------------------------------------------------------------- styling --
BG = "#0d1117"
FG = "#c9d1d9"
GRID = "#21262d"
ENVELOPE = "#3d7f8c"
PLAYHEAD = "#ff7043"
THRESH = "#8b949e"
CLUSTER_COLORS = ["#4dd0e1", "#ffb74d", "#b39ddb"]   # largest amplitude first
REJECT_COLOR = "#6e7681"

# Launching ffmpeg from a windowed (no-console) build would flash a black
# console window for every render; this suppresses it on Windows.
_POPEN_KW = ({"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
             if sys.platform == "win32" else {})


class Cancelled(RuntimeError):
    """Raised when the caller asks for an in-progress job to stop."""


# ------------------------------------------------------------------ helpers --
def minmax_envelope(x: np.ndarray, n_cols: int):
    """Decimate to (lo, hi) pairs per column - preserves spike extremes."""
    n_cols = max(1, min(n_cols, len(x)))
    per = len(x) // n_cols
    if per < 2:
        return x.copy(), x.copy()
    trimmed = x[:per * n_cols].reshape(n_cols, per)
    return trimmed.min(axis=1), trimmed.max(axis=1)


def robust_sigma(x: np.ndarray) -> float:
    """Noise SD from the median absolute deviation (Quiroga et al. 2004)."""
    return float(np.median(np.abs(x - np.median(x))) / 0.6745)


def detect_spikes(x, fs, neg_k=6.0, pos_k=7.0, pre_ms=1.0, post_ms=2.0,
                  refractory_ms=1.0):
    """
    Negative-threshold spike detection with artifact rejection.

    Returns (waveforms, trough_idx, sigma, n_rejected, rejected_idx).
    Waveforms are aligned so that the trough sits at sample `n_pre`.
    """
    sigma = robust_sigma(x)
    n_pre = int(round(pre_ms * fs / 1000))
    n_post = int(round(post_ms * fs / 1000))
    n_ref = max(1, int(round(refractory_ms * fs / 1000)))

    below = x < -neg_k * sigma
    starts = np.flatnonzero(np.diff(below.astype(np.int8)) == 1) + 1

    # Trough of each excursion. np.unique collapses the case where one deep
    # spike dips back below threshold within the search window and would
    # otherwise be reported twice at the same sample.
    troughs = [s + int(np.argmin(x[s:s + n_post]))
               for s in starts if s + 1 < len(x)]
    troughs = np.unique(troughs)

    kept = []
    for t in troughs:                              # enforce refractory period
        if not kept or t - kept[-1] >= n_ref:
            kept.append(int(t))

    waves, idx, rejected = [], [], []
    for t in kept:
        a, b = t - n_pre, t + n_post
        if a < 0 or b > len(x):                    # incomplete at the edges
            continue
        w = x[a:b]
        if w.max() > pos_k * sigma:
            rejected.append(t)
        else:
            waves.append(w)
            idx.append(t)

    waves = np.array(waves) if waves else np.zeros((0, n_pre + n_post))
    return (waves, np.array(idx, dtype=int), sigma,
            len(rejected), np.array(rejected, dtype=int))


def kmeans_1d(v, k=2, iters=100):
    """Tiny 1-D k-means - avoids a scikit-learn dependency for one clustering."""
    c = np.percentile(v, np.linspace(15, 85, k)).astype(float)
    lab = np.zeros(len(v), dtype=int)
    for _ in range(iters):
        lab = np.argmin(np.abs(v[:, None] - c[None, :]), axis=1)
        new = np.array([v[lab == j].mean() if np.any(lab == j) else c[j]
                        for j in range(k)])
        if np.allclose(new, c):
            break
        c = new
    return lab, c


def cluster_amplitudes(amp, max_k=3, min_sep=2.0, min_frac=0.08, min_n=8):
    """
    Group trough amplitudes into 1..max_k clusters.

    Takes the largest k whose split is real: every cluster populated, and every
    *adjacent* pair of centres separated by at least `min_sep` times their
    summed within-cluster spread. Adjacent pairs are what matters - with three
    clusters, checking only the extremes would happily accept a middle group
    smeared across the gap. Walks k down until one qualifies, ending at a
    single group.

    Returns (labels, centres, separation), label 0 = largest amplitude and
    `separation` = the weakest adjacent-pair separation at the chosen k.
    """
    n = len(amp)
    single = (np.zeros(n, dtype=int),
              np.array([float(amp.mean()) if n else 0.0]), 0.0)
    k_top = min(int(max_k), max(1, n // min_n))
    for k in range(k_top, 1, -1):
        lab, c = kmeans_1d(amp, k)
        order = np.argsort(c)                      # most negative first
        remap = np.empty(k, dtype=int)
        remap[order] = np.arange(k)
        lab, c = remap[lab], c[order]
        sizes = np.array([int(np.sum(lab == j)) for j in range(k)])
        if sizes.min() < min_n or sizes.min() < min_frac * n:
            continue
        sd = np.array([float(amp[lab == j].std()) for j in range(k)])
        seps = [abs(c[j + 1] - c[j]) / (sd[j] + sd[j + 1] + 1e-9)
                for j in range(k - 1)]
        if min(seps) >= min_sep:
            return lab, c, float(min(seps))
    return single


# --------------------------------------------------------- cluster metrics --
# ---------------------------------------------------------------------------
# DISABLED: putative excitatory/inhibitory classification.
#
# The shape thresholds below are the usual cortical convention, but they assume
# a high-pass near 250-300 Hz. At the corners this tool is typically used with,
# every waveform narrows and almost everything classifies as fast-spiking, so
# the labels would mislead more than they inform. Kept here, commented, until
# the thresholds are validated against this preparation.
#
# The measurements it was built on - half_width_ms and trough_to_peak_ms - are
# still computed and reported: they are observations, not claims about cell
# type. Note they do shift with the band-pass, so compare them only between
# recordings filtered the same way.
#
# FS_HALF_WIDTH_MS = 0.25        # trough width at half depth
# FS_TROUGH_PEAK_MS = 0.55       # trough to the following positive maximum
# RS_HALF_WIDTH_MS = 0.30
# RS_TROUGH_PEAK_MS = 0.60
# FS_RATE_HZ = 10.0              # tiebreaker in the ambiguous band
# ---------------------------------------------------------------------------


def waveform_metrics(w, fs, n_pre):
    """
    Shape descriptors of one (mean) waveform, trough at index `n_pre`.

    Returns (half_width_ms, trough_to_peak_ms, trough_uV, peak_uV).
    """
    trough = float(w[n_pre])
    half = trough / 2.0                            # trough is negative
    i = n_pre
    while i > 0 and w[i] <= half:
        i -= 1
    j = n_pre
    while j < len(w) - 1 and w[j] <= half:
        j += 1
    # One sample is 0.033 ms at 30 kHz, against a 0.25 ms decision threshold,
    # so the crossings are interpolated - otherwise the half-width is quantised
    # into ~8 possible values and the classification jitters on rounding alone.
    def _cross(a, b):
        ya, yb = w[a], w[b]
        return a + (half - ya) / (yb - ya) if yb != ya else float(a)
    left = _cross(i, i + 1) if i + 1 <= n_pre else float(i)
    right = _cross(j, j - 1) if j - 1 >= n_pre else float(j)
    half_width_ms = abs(right - left) / fs * 1000.0
    post = w[n_pre:]
    kpk = int(np.argmax(post)) if len(post) else 0
    return (half_width_ms, kpk / fs * 1000.0, trough,
            float(post[kpk]) if len(post) else 0.0)


# ---------------------------------------------------------------------------
# DISABLED alongside the classification above - the caveat existed only to
# qualify those labels.
#
# SHAPE_SAFE_HIGHPASS_HZ = 400.0     # above this, published thresholds drift
#
#
# def shape_caveat(band_low_hz):
#     """
#     Whether the band-pass makes the excitatory/inhibitory call unreliable.
#
#     The half-width and trough-to-peak thresholds in the literature assume a
#     high-pass around 250-300 Hz. A higher corner differentiates the waveform
#     and narrows every spike, which pushes broad units across the fast-spiking
#     boundary - so the labels stop meaning what they are named after.
#     """
#     if band_low_hz > SHAPE_SAFE_HIGHPASS_HZ:
#         return (f"high-pass at {band_low_hz:.0f} Hz narrows spike waveforms; "
#                 f"excitatory/inhibitory labels are unreliable above "
#                 f"{SHAPE_SAFE_HIGHPASS_HZ:.0f} Hz - prefer 250-300 Hz for "
#                 f"shape-based typing")
#     return ""
#
#
# def putative_type(half_width_ms, trough_to_peak_ms, rate_sp_s):
#     """Heuristic excitatory/inhibitory call. Always returns a '?' label."""
#     if (half_width_ms < FS_HALF_WIDTH_MS
#             and trough_to_peak_ms < FS_TROUGH_PEAK_MS):
#         return "inhibitory?"
#     if (half_width_ms >= RS_HALF_WIDTH_MS
#             and trough_to_peak_ms >= RS_TROUGH_PEAK_MS):
#         return "excitatory?"
#     return "inhibitory?" if rate_sp_s > FS_RATE_HZ else "unclear"
# ---------------------------------------------------------------------------


# ------------------------------------------------------- automatic tagging --
# A first-pass quality tag, on the same 1-3 scale a human would use:
#   1  a tight, repeatable waveform: likely one isolated unit
#   2  real activity, but not separable into a single unit
#   3  noise, or a signal shared across a large batch of channels
#   0  not assessed (too few spikes to judge)
# It is a screen, not a verdict. Validated against one hand-tagged session it
# agreed on 95% of channels and 91% of clusters, and never called noise signal.
SHARE_FRAC = 0.20          # of channels co-active before a signal is "common"
SHARE_MIN_CHANNELS = 8     # floor, so small probes never trip the test
RESID_ISOLATED = 0.15      # waveform residual at or below this reads as one unit
RATE_MULTIUNIT = 10.0      # sp/s above which unseparated activity reads as hash
MIN_SPIKES_FOR_RESIDUAL = 3


def waveform_residual(W):
    """
    How tightly a cluster's spikes superimpose on their own mean.

    RMS deviation about the mean waveform, divided by the mean trough depth,
    so it is dimensionless and comparable across channels. This is the overlay
    pane reduced to a number: a repeatable unit sits near 0.1, a smear of
    unrelated events near 0.3.
    """
    if len(W) < MIN_SPIKES_FOR_RESIDUAL:
        return None
    m = W.mean(axis=0)
    depth = abs(float(m.min()))
    if depth <= 0:
        return None
    return float(np.sqrt(((W - m) ** 2).mean()) / depth)


def auto_quality(share_count, n_channels, residual, rate_sp_s):
    """
    Apply the screen. `share_count` is the median number of channels co-active
    with this channel's spikes, or None when sharing was not measured.

    Sharing is tested first on purpose: a common-mode waveform is extremely
    repeatable and would otherwise score as a textbook single unit.
    """
    if share_count is not None and n_channels and n_channels >= 2:
        if share_count > max(SHARE_MIN_CHANNELS, SHARE_FRAC * n_channels):
            return 3
    if residual is None:
        return 0
    if residual <= RESID_ISOLATED:
        return 1
    if rate_sp_s >= RATE_MULTIUNIT:
        return 2
    return 3


def session_sharing(paths, start, duration, band, fs, neg_k, pos_k,
                    pre_ms=1.0, post_ms=2.0, refractory_ms=1.0, bin_ms=1.0,
                    cancel=None, progress=None, verbose=True):
    """
    For each channel, the median number of channels firing within one bin of
    its own spikes, measured over a window common to all of them.

    Counts how many sites see an event, never which ones, so no probe map is
    needed and channel numbering can be arbitrary. A unit picked up by a few
    neighbouring sites scores low; a waveform present across a large batch of
    channels scores high.

    Returns ({channel stem: median co-active count}, number of channels).
    """
    n_bins = max(1, int(round(duration * 1000.0 / bin_ms)))
    hits = np.zeros((len(paths), n_bins), dtype=bool)
    where = []
    for i, p in enumerate(paths):
        if cancel is not None and cancel():
            raise Cancelled("cancelled during sharing scan")
        x = load_excerpt(p, start, duration, band, fs)
        a = analyse(x, fs, neg_k, pos_k, pre_ms, post_ms, refractory_ms)
        b = np.clip((a["idx"] / fs * 1000.0 / bin_ms).astype(int), 0, n_bins - 1)
        hits[i, b] = True
        where.append(b)
        if progress is not None:
            progress((i + 1) / len(paths))
        if verbose and i % max(1, len(paths) // 10) == 0:
            print(f"\r  cross-channel scan {100 * i / len(paths):5.1f}%",
                  end="", flush=True)
    if verbose:
        print("\r  cross-channel scan 100.0%")
    breadth = hits.sum(axis=0)
    out = {p.stem: (float(np.median(breadth[b])) if len(b) else 0.0)
           for p, b in zip(paths, where)}
    return out, len(paths)


def cluster_stats(a, fs, duration, pre_ms):
    """
    Per-cluster descriptive statistics from an `analyse()` result.

    SNR is |mean trough| / sigma, sigma being the MAD-based noise estimate the
    detector itself used - so an SNR of 6 means the mean spike sits at the 6
    sigma the threshold was set from.
    """
    n_pre = int(round(pre_ms * fs / 1000))
    rows = []
    for j in range(len(a["centres"])):
        sel = a["labels"] == j
        n = int(np.sum(sel))
        if n == 0:
            continue
        mean_wave = a["waves"][sel].mean(axis=0)
        hw, t2p, trough, peak = waveform_metrics(mean_wave, fs, n_pre)
        amp_mean = float(a["amp"][sel].mean())
        rate = n / duration if duration > 0 else 0.0
        snr = abs(amp_mean) / a["sigma"] if a["sigma"] > 0 else 0.0
        rows.append(dict(
            cluster=j + 1, n_spikes=n,
            mean_amplitude_uV=amp_mean,
            amplitude_sd_uV=float(a["amp"][sel].std()),
            firing_rate_sp_s=rate, snr=snr,
            half_width_ms=hw, trough_to_peak_ms=t2p,
            peak_uV=peak, sigma_uV=a["sigma"],
            wf_residual=waveform_residual(a["waves"][sel])))
            # putative_type=putative_type(hw, t2p, rate)))   # DISABLED
    return rows


STAT_FIELDS = ["channel", "segment", "start_s", "duration_s", "cluster",
               "n_clusters", "n_spikes", "mean_amplitude_uV",
               "amplitude_sd_uV", "firing_rate_sp_s", "snr", "half_width_ms",
               "trough_to_peak_ms", "peak_uV", "sigma_uV", "n_rejected",
               "wf_residual", "share_count", "share_frac", "auto_quality"]
               # "putative_type" removed while the classification is disabled


def load_excerpt(path, start, duration, band, fs):
    """Read and band-pass one excerpt, padded then trimmed at the edges."""
    total_s = file_duration_s(path, fs)
    if start < 0 or start + duration > total_s:
        raise ValueError(f"{start:.1f}+{duration:.1f} s exceeds "
                         f"{path.name} ({total_s:.1f} s)")
    pad = min(EDGE_PAD_S, start, total_s - (start + duration))
    seg = read_segment(path, start - pad, duration + 2 * pad, fs)
    sig = bandpass(seg, band[0], band[1], fs)
    n_pad, n_keep = int(round(pad * fs)), int(round(duration * fs))
    if n_pad:
        sig = sig[n_pad:n_pad + n_keep]
    return sig[:n_keep]


def channel_stats(path, start, duration, band, fs, neg_k, pos_k,
                  pre_ms=1.0, post_ms=2.0, refractory_ms=1.0, max_k=3,
                  segment=1, share_count=None, n_channels=0):
    """
    Statistics for one channel over one excerpt, as rows ready for the CSV /
    GUI table. `segment` records which ranked window this was, so several
    excerpts from the same channel stay distinguishable in the output.

    `share_count` is this channel's median co-active channel count from
    session_sharing(). Pass it to have auto_quality filled in; without it the
    sharing test is skipped and the tag rests on waveform and rate alone.
    """
    sig = load_excerpt(path, start, duration, band, fs)
    a = analyse(sig, fs, neg_k, pos_k, pre_ms, post_ms, refractory_ms,
                max_k=max_k)
    rows = cluster_stats(a, fs, duration, pre_ms)
    for r in rows:
        r.update(channel=path.stem, segment=segment,
                 n_clusters=len(a["centres"]),
                 n_rejected=a["n_rej"], start_s=round(start, 3),
                 duration_s=duration,
                 share_count=share_count,
                 share_frac=(share_count / n_channels
                             if share_count is not None and n_channels else None),
                 auto_quality=auto_quality(share_count, n_channels,
                                           r["wf_residual"],
                                           r["firing_rate_sp_s"]))
    return rows, a


def analyse(x, fs, neg_k, pos_k, pre_ms, post_ms, refractory_ms, max_k=3):
    """Detect, reject artifacts and cluster - the one path used everywhere."""
    waves, idx, sigma, n_rej, rej_idx = detect_spikes(
        x, fs, neg_k, pos_k, pre_ms, post_ms, refractory_ms)
    n_pre = int(round(pre_ms * fs / 1000))
    if len(waves):
        amp = waves[:, n_pre]
        labels, centres, sep = cluster_amplitudes(amp, max_k=max_k)
    else:
        amp = np.zeros(0)
        labels, centres, sep = np.zeros(0, dtype=int), np.array([0.0]), 0.0
    n_ev = len(idx) + n_rej
    rej_amp = x[rej_idx] if len(rej_idx) else np.zeros(0)
    return dict(waves=waves, idx=idx, sigma=sigma, n_rej=n_rej,
                rej_idx=rej_idx, rej_amp=rej_amp,
                amp=amp, labels=labels, centres=centres,
                sep=sep, n=len(idx), peak=float(np.abs(x).max()) if len(x) else 0.0,
                score=len(idx) * (1 - n_rej / n_ev) if n_ev else 0.0,
                clustered=len(centres) > 1)


def find_best_windows(path, duration, band, fs, step, artifact_k,
                      neg_k, pos_k, pre_ms, post_ms, refractory_ms,
                      search_range=None, prefer_clusters=True, verbose=True,
                      cancel=None, progress=None, max_k=3, top_n=1):
    """
    Scan candidate start times and rank them. "Best" means free of large
    artifacts, then - since the point of the video is to show the amplitude
    classes - one where the split is genuinely separated, and only then the one
    with the most accepted spikes. Each candidate costs a single short read, so
    the whole recording sweeps in seconds.

    Returns up to `top_n` start times, best first. Chosen windows are kept at
    least `duration` apart: with a scan step finer than the excerpt they would
    otherwise overlap, and statistics computed over overlapping windows would
    count the same spikes more than once.
    """
    total = file_duration_s(path, fs)
    lo, hi = search_range or (0.0, total - duration)
    lo, hi = max(0.0, lo), min(total - duration, hi)
    starts = np.arange(lo, hi, step)
    if len(starts) == 0:
        return [lo]

    rows = []
    for i, s0 in enumerate(starts):
        x = bandpass(read_segment(path, float(s0), duration, fs),
                     band[0], band[1], fs)
        a = analyse(x, fs, neg_k, pos_k, pre_ms, post_ms, refractory_ms,
                    max_k=max_k)
        rows.append((float(s0), a["score"], a["n"], a["n_rej"], a["peak"],
                     a["sigma"], a["sep"],
                     float(a["peak"] < artifact_k * a["sigma"]),
                     float(a["clustered"])))
        if verbose and i % max(1, len(starts) // 10) == 0:
            print(f"\r  scanning {100 * i / len(starts):5.1f}%",
                  end="", flush=True)
    if verbose:
        print("\r  scanning 100.0%")

    v = np.array(rows)
    pool = v[v[:, 7] > 0]                          # artifact-free
    if len(pool) == 0:
        pool = v
    if prefer_clusters and np.any(pool[:, 8] > 0):
        pool = pool[pool[:, 8] > 0]                # separated amplitudes

    ranked = pool[np.argsort(-pool[:, 1])]
    chosen = []
    for r in ranked:                               # greedy, non-overlapping
        if all(abs(r[0] - c) >= duration for c in chosen):
            chosen.append(float(r[0]))
        if len(chosen) >= max(1, top_n):
            break

    if verbose:
        print(f"  {len(v)} candidate windows, {int(v[:, 7].sum())} artifact-free "
              f"(peak < {artifact_k:g} sigma), "
              f"{int(v[:, 8].sum())} with separated amplitude clusters")
        for r in ranked[:max(5, top_n)]:
            mark = ""
            if r[0] in chosen:
                rank = chosen.index(r[0]) + 1
                mark = "  <-- best" if rank == 1 else f"  <-- #{rank}"
            print(f"    start {r[0]:8.1f} s  spikes {int(r[2]):4d}  "
                  f"rejected {int(r[3]):3d}  peak {r[4]:5.0f} uV  "
                  f"cluster sep {r[6]:4.2f}" + mark)
    return chosen


def find_clean_window(*args, **kwargs):
    """Single best window - thin wrapper over find_best_windows()."""
    kwargs.pop("top_n", None)
    return find_best_windows(*args, top_n=1, **kwargs)[0]


# ------------------------------------------------------------- figure build --
def build_figure(sig_uv, fs, duration, band, chan_name, size, dpi, ylim,
                 waves, idx, labels, centres, sigma, neg_k, pos_k,
                 pre_ms, post_ms, slow, n_rejected, wave_frac):
    w, h = size
    fig = Figure(figsize=(w / dpi, h / dpi), dpi=dpi, facecolor=BG)
    FigureCanvasAgg(fig)                            # attaches fig.canvas
    gs = fig.add_gridspec(2, 1, height_ratios=[1, 2.1], hspace=0.30,
                          left=0.065, right=0.985, top=0.90, bottom=0.085)
    n_clusters = len(centres)

    # ---- pane 1: whole excerpt, fixed scale, artifacts clipped -------------
    ax_all = fig.add_subplot(gs[0], facecolor=BG)
    lo, hi = minmax_envelope(sig_uv, int(w * 0.9))
    ax_all.fill_between(np.linspace(0, duration, len(lo)), lo, hi,
                        color=ENVELOPE, linewidth=0)
    ax_all.set_xlim(0, duration)
    ax_all.set_ylim(*ylim)
    ax_all.axhline(-neg_k * sigma, color=THRESH, linewidth=0.7, alpha=0.55,
                   linestyle=(0, (5, 4)))
    ax_all.set_xlabel(f"Time window: {duration:g} s "
                      f"(fixed ±{ylim[1]:.0f} µV)",
                      color=FG, fontsize=9)
    ax_all.set_ylabel("µV", color=FG, fontsize=9)
    marks = [ax_all.scatter([], [], s=22, marker="v",
                            color=CLUSTER_COLORS[j], zorder=5,
                            linewidths=0)
             for j in range(n_clusters)]
    rej_mark = ax_all.scatter([], [], s=16, marker="x", color=REJECT_COLOR,
                              zorder=4, linewidths=0.9, alpha=0.8)
    playhead = ax_all.axvline(0, color=PLAYHEAD, linewidth=1.2)

    # ---- pane 2..n: one narrow pane per cluster ----------------------------
    # Each pane is deliberately much narrower than half the figure: squeezing
    # the same 3 ms into less width steepens the trough, which is what makes
    # the spike read as sharp rather than as a smooth dip.
    gap = 0.05
    row = fig.get_axes()[0].get_position()          # borrow the top pane's rows
    bottom, height = 0.085, row.y0 - 0.085 - 0.115
    # Pane width is held constant whatever the cluster count, so a spike looks
    # the same whether one cluster was found or three. It is only narrowed if
    # the requested width genuinely cannot fit the panes side by side.
    span_l, span_r = 0.07, 0.985
    span = span_r - span_l
    if n_clusters * wave_frac + (n_clusters - 1) * gap > span:
        wave_frac = (span - (n_clusters - 1) * gap) / n_clusters
    total = n_clusters * wave_frac + (n_clusters - 1) * gap
    left0 = span_l + (span - total) / 2.0

    t_ms = (np.arange(waves.shape[1]) - int(round(pre_ms * fs / 1000))) \
        / fs * 1000.0
    axes_w, traces, means, titles = [], [], [], []
    names = ([f"Cluster {j + 1}" for j in range(n_clusters)]
             if n_clusters > 1 else ["Spike events"])
    for j in range(n_clusters):
        ax = fig.add_axes([left0 + j * (wave_frac + gap), bottom,
                           wave_frac, height], facecolor=BG)
        ax.set_xlim(t_ms[0], t_ms[-1])
        ax.axvline(0, color=THRESH, linewidth=0.7, alpha=0.35)
        for lvl in (-neg_k * sigma, pos_k * sigma):
            ax.axhline(lvl, color=THRESH, linewidth=0.7, alpha=0.5,
                       linestyle=(0, (5, 4)))
        if j == n_clusters - 1:                     # label thresholds once
            for lvl, lab in ((-neg_k * sigma, f"−{neg_k:g}σ"),
                             (pos_k * sigma, f"+{pos_k:g}σ")):
                ax.text(t_ms[-1] * 0.97, lvl, lab, color=THRESH, fontsize=8,
                        ha="right", va="bottom" if lvl > 0 else "top",
                        alpha=0.85)
        ax.set_xticks([-1, 0, 1, 2])
        ax.set_xlabel("ms (from trough)", color=FG, fontsize=9)
        if j == 0:
            ax.set_ylabel("µV", color=FG, fontsize=10)
        else:
            ax.tick_params(labelleft=False)         # shared scale, one axis
        (ln,) = ax.plot([], [], color=CLUSTER_COLORS[j], linewidth=0.7,
                        alpha=0.30)
        (mn,) = ax.plot([], [], color=CLUSTER_COLORS[j], linewidth=2.4,
                        alpha=0.95, zorder=6)
        ttl = ax.set_title("", color=CLUSTER_COLORS[j], fontsize=10.5,
                           pad=8, family="monospace")
        axes_w.append(ax)
        traces.append(ln)
        means.append(mn)
        titles.append(ttl)
        names_j = names[j]
        ttl.set_text(names_j)

    for a in [ax_all] + axes_w:
        a.grid(True, color=GRID, linewidth=0.6)
        a.tick_params(colors=FG, labelsize=8)
        for spine in a.spines.values():
            spine.set_color(GRID)

    head = (f"{chan_name}   {band[0]:.0f}-{band[1]:.0f} Hz   "
            f"σ={sigma:.1f} µV   detect −{neg_k:g}σ = {-neg_k * sigma:.0f} µV"
            + (f"   {slow:g}× slow motion" if slow != 1 else "   real time"))
    fig.text(0.065, 0.955, head, color=FG, fontsize=11, ha="left",
             va="center", alpha=0.9)
    clock = fig.text(0.985, 0.955, "", color=FG, fontsize=11, ha="right",
                     va="center", family="monospace")
    rej_text = fig.text(0.985, bottom + height + 0.055, "", color=REJECT_COLOR,
                        fontsize=9.5, ha="right", va="center",
                        family="monospace")

    return fig, dict(marks=marks, rej_mark=rej_mark, playhead=playhead,
                     traces=traces, means=means, titles=titles, names=names,
                     clock=clock, rej_text=rej_text, axes_w=axes_w,
                     t_ms=t_ms, n_clusters=n_clusters)


# -------------------------------------------------------------------- render --
def render(path: Path, out_dir: Path, duration: float, start,
           band, fs: int, fps: int, size, dpi: int, slow: float, crf: int,
           bit_depth: int, headroom_db: float, ylim_uv: float,
           neg_k: float, pos_k: float, pre_ms: float, post_ms: float,
           refractory_ms: float, wave_frac: float, keep_wav: bool,
           out_stem: str | None = None, cancel=None, progress=None,
           max_k: int = 3) -> Path:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not found on PATH")

    total_s = file_duration_s(path, fs)
    sig = load_excerpt(path, start, duration, band, fs)

    a = analyse(sig, fs, neg_k, pos_k, pre_ms, post_ms, refractory_ms,
                max_k=max_k)
    waves, idx, sigma, n_rej = a["waves"], a["idx"], a["sigma"], a["n_rej"]
    amp, labels, centres, sep = a["amp"], a["labels"], a["centres"], a["sep"]
    n_clusters = len(centres)
    if len(waves) == 0:
        raise ValueError(f"no spikes crossed -{neg_k:g} sigma in this window")

    stem = out_stem or (f"{path.stem}_{start:.0f}s+{duration:.0f}s_"
                        f"{band[0]:.0f}-{band[1]:.0f}Hz_spikes"
                        + (f"_slow{slow:g}x" if slow != 1 else ""))
    out_dir.mkdir(parents=True, exist_ok=True)
    mp4_path = out_dir / f"{stem}.mp4"
    if keep_wav:
        wav_path = out_dir / f"{stem}.wav"
    else:
        # Not in out_dir: a discarded intermediate must never collide with
        # (and then delete) a WAV the caller exported under the same stem.
        _fd, _tmp = tempfile.mkstemp(suffix=".wav", prefix="intan_")
        os.close(_fd)
        wav_path = Path(_tmp)

    y, ref_uv, n_clipped = normalize(sig, headroom_db=headroom_db)
    wavfile.write(wav_path, max(1, int(round(fs / slow))), to_pcm(y, bit_depth))

    w, h = (int(v) // 2 * 2 for v in size)
    ylim = (-ylim_uv, ylim_uv)

    print(f"{path.name}: {total_s / 60:.1f} min total -> "
          f"{start:.1f}-{start + duration:.1f} s")
    print(f"  band-pass {band[0]:.0f}-{band[1]:.0f} Hz "
          f"(Butterworth order {FILTER_ORDER}, zero-phase), sigma {sigma:.2f} uV")
    print(f"  {len(idx)} spikes accepted, {n_rej} rejected at "
          f"+{pos_k:g} sigma ({100 * n_rej / (len(idx) + n_rej):.1f}% of events)")
    if n_clusters > 1:
        parts = ", ".join(f"{centres[k]:.0f} uV (n={int(np.sum(labels == k))})"
                          for k in range(n_clusters))
        print(f"  {n_clusters} amplitude clusters: {parts}, "
              f"weakest separation {sep:.2f}")
    else:
        print(f"  single amplitude population (separation {sep:.2f} "
              f"below threshold) - one colour")

    fig, art = build_figure(sig, fs, duration, band, path.stem, (w, h), dpi,
                            ylim, waves, idx, labels, centres, sigma,
                            neg_k, pos_k, pre_ms, post_ms, slow, n_rej,
                            wave_frac)
    fig.canvas.draw()
    fh, fw = np.asarray(fig.canvas.buffer_rgba()).shape[:2]
    w, h = fw // 2 * 2, fh // 2 * 2

    # Pre-build one polyline per cluster: all its waveforms end to end,
    # separated by NaN. Revealing k spikes is then a slice, not a redraw.
    t_ms, L = art["t_ms"], waves.shape[1]
    spike_t = idx / fs
    per_cluster = []
    for j in range(n_clusters):
        sel = np.flatnonzero(labels == j)
        sel = sel[np.argsort(spike_t[sel])]
        xs = np.tile(np.append(t_ms, np.nan), len(sel))
        ys = np.concatenate([np.append(waves[i], np.nan) for i in sel]) \
            if len(sel) else np.zeros(0)
        cum = np.cumsum(waves[sel], axis=0) if len(sel) else np.zeros((0, L))
        per_cluster.append(dict(t=spike_t[sel], xs=xs, ys=ys, cum=cum,
                                amp=amp[sel]))

    # One shared y-scale across the cluster panes, so the amplitude difference
    # between them stays readable rather than being normalised away. The range
    # is asymmetric because spikes are: giving the small positive lobe only the
    # room it needs leaves the trough filling the pane, which is what reads as
    # sharp. Both threshold lines are still guaranteed to be inside.
    lo_w = min(waves.min() * 1.10, -neg_k * sigma * 1.25)
    hi_w = max(waves.max() * 1.15, pos_k * sigma * 1.15)
    for ax in art["axes_w"]:
        ax.set_ylim(lo_w, hi_w)

    n_frames = int(np.ceil(duration * slow * fps))
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
        "-framerate", str(fps), "-i", "-", "-i", str(wav_path),
        "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-shortest", str(mp4_path),
    ]
    print(f"  {w}x{h} @ {fps} fps, {n_frames} frames"
          + (f", {slow:g}x slow motion" if slow != 1 else ""))

    rej_t = np.sort(a["rej_idx"] / fs) if n_rej else np.zeros(0)
    rej_a = a["rej_amp"][np.argsort(a["rej_idx"])] if n_rej else np.zeros(0)

    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, **_POPEN_KW)
    try:
        for i in range(n_frames):
            t_sig = i / fps / slow
            for j in range(n_clusters):
                pc = per_cluster[j]
                k = int(np.searchsorted(pc["t"], t_sig, side="right"))
                art["traces"][j].set_data(pc["xs"][:k * (L + 1)],
                                          pc["ys"][:k * (L + 1)])
                if k:
                    art["means"][j].set_data(t_ms, pc["cum"][k - 1] / k)
                    art["marks"][j].set_offsets(
                        np.c_[pc["t"][:k], pc["amp"][:k]])
                else:
                    art["means"][j].set_data([], [])
                    art["marks"][j].set_offsets(np.empty((0, 2)))
                art["titles"][j].set_text(f"{art['names'][j]}   n={k}")
            kr = int(np.searchsorted(rej_t, t_sig, side="right"))
            art["rej_mark"].set_offsets(np.c_[rej_t[:kr], rej_a[:kr]]
                                        if kr else np.empty((0, 2)))
            art["playhead"].set_xdata([t_sig, t_sig])
            art["clock"].set_text(f"{start + t_sig:9.3f} s")
            art["rej_text"].set_text(f"rejected as artifact  n={kr}")

            fig.canvas.draw()
            proc.stdin.write(
                np.asarray(fig.canvas.buffer_rgba())[:h, :w, :3].tobytes())
            if i % max(1, n_frames // 20) == 0:
                print(f"\r  rendering {100 * i / n_frames:5.1f}%",
                      end="", flush=True)
    finally:
        if proc.stdin:
            proc.stdin.close()
        fig.clear()
    rc = proc.wait()
    print("\r  rendering 100.0%")
    if rc != 0:
        raise RuntimeError(f"ffmpeg exited with code {rc}")

    if not keep_wav:
        wav_path.unlink(missing_ok=True)
    print(f"  scale reference {ref_uv:.1f} uV"
          + (f", {n_clipped} samples clipped" if n_clipped else ""))
    print(f"  -> {mp4_path.name}  ({duration * slow:.1f} s, "
          f"{mp4_path.stat().st_size / 1e6:.2f} MB)"
          + (f"   + {wav_path.name}" if keep_wav else ""))
    return mp4_path


def write_stats_csv(rows, path):
    """One row per channel x segment x cluster."""
    import csv
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        wr = csv.DictWriter(fh, fieldnames=STAT_FIELDS, extrasaction="ignore")
        wr.writeheader()
        for r in rows:
            wr.writerow({k: (round(v, 4) if isinstance(v, float) else v)
                         for k, v in r.items()})
    return path


# --------------------------------------------------------------------- main --
def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Raw .dat -> MP4 (Full window + spike clusters).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("files", nargs="*", type=Path,
                   help="specific .dat files (default: one picked at random)")
    p.add_argument("-d", "--duration", type=float, default=10.0,
                   help="seconds of signal to render")
    p.add_argument("-s", "--start", type=float, default=None,
                   help="start time in s (default: scan for a clean window)")
    p.add_argument("-b", "--band", type=float, nargs=2,
                   metavar=("LOW", "HIGH"), default=list(DEFAULT_BAND),
                   help="band-pass limits in Hz")
    p.add_argument("--ylim", type=float, default=100.0,
                   help="voltage +/- scale of the overview pane, in uV")
    p.add_argument("--neg-k", type=float, default=5.0,
                   help="detection threshold, in sigma below zero")
    p.add_argument("--pos-k", type=float, default=8.0,
                   help="artifact rejection threshold, in sigma above zero")
    p.add_argument("--pre", type=float, default=1.0,
                   help="waveform window before the trough, in ms")
    p.add_argument("--post", type=float, default=2.0,
                   help="waveform window after the trough, in ms")
    p.add_argument("--refractory", type=float, default=1.0,
                   help="minimum spacing between detections, in ms")
    p.add_argument("--max-clusters", type=int, default=3, choices=(1, 2, 3),
                   help="most amplitude clusters to consider")
    p.add_argument("--wave-width", type=float, default=0.26,
                   help="width of each cluster pane, as a fraction of the "
                        "frame; well under 0.5 keeps the trough steep")
    p.add_argument("--scan-step", type=float, default=60.0,
                   help="spacing of candidate windows when scanning, in s")
    p.add_argument("--scan-range", type=float, nargs=2, default=None,
                   metavar=("FROM", "TO"), help="restrict the scan, in s")
    p.add_argument("--artifact-k", type=float, default=18.0,
                   help="a window is 'clean' if no sample exceeds this x sigma")
    p.add_argument("--any-window", action="store_true",
                   help="when scanning, do not prefer windows whose spike "
                        "amplitudes split into two separated clusters")
    p.add_argument("--fps", type=int, default=60, help="video frame rate")
    p.add_argument("--size", type=int, nargs=2, metavar=("W", "H"),
                   default=[1600, 900], help="video resolution")
    p.add_argument("--dpi", type=int, default=220, help="figure dpi")
    p.add_argument("--slow", type=float, default=1.0,
                   help="slow-motion factor; audio pitch drops with it")
    p.add_argument("--crf", type=int, default=16,
                   help="x264 quality, lower is better")
    p.add_argument("--fs", type=int, default=SAMPLE_RATE,
                   help="acquisition sample rate of the .dat files")
    p.add_argument("--bit-depth", type=int, choices=(16, 32), default=16)
    p.add_argument("--headroom", type=float, default=1.0,
                   help="dB below full scale for the audio ceiling")
    p.add_argument("--no-wav", action="store_true",
                   help="delete the intermediate WAV after muxing")
    p.add_argument("--folder", type=Path, default=None,
                   help="folder holding the amp-*.dat files "
                        "(default: current directory)")
    p.add_argument("-o", "--out-dir", type=Path, default=None,
                   help="output directory (default: <folder>/video)")
    p.add_argument("--seed", type=int, default=None,
                   help="seed for the random file pick")
    args = p.parse_args(argv)

    folder = (args.folder or Path.cwd()).resolve()
    if args.out_dir is None:
        args.out_dir = folder / "video"
    available = sorted(folder.glob("amp-*.dat"))
    if not available:
        print(f"No amp-*.dat files found in {folder}", file=sys.stderr)
        return 1

    if args.files:
        targets = [f if f.is_absolute() else folder / f for f in args.files]
        missing = [t for t in targets if not t.is_file()]
        if missing:
            print("Not found: " + ", ".join(map(str, missing)), file=sys.stderr)
            return 1
    else:
        rng = random.Random(args.seed)
        targets = [rng.choice(available)]
        print(f"Randomly selected {targets[0].name} of {len(available)} channels")

    band = (args.band[0], args.band[1])
    failures = 0
    for t in targets:
        try:
            start = args.start
            if start is None:
                print(f"{t.name}: searching for a clean {args.duration:g} s "
                      f"window...")
                start = find_clean_window(
                    t, args.duration, band, args.fs, args.scan_step,
                    args.artifact_k, args.neg_k, args.pos_k, args.pre,
                    args.post, args.refractory, args.scan_range,
                    not args.any_window, max_k=args.max_clusters)
            render(t, args.out_dir, args.duration, start, band, args.fs,
                   args.fps, args.size, args.dpi, args.slow, args.crf,
                   args.bit_depth, args.headroom, args.ylim, args.neg_k,
                   args.pos_k, args.pre, args.post, args.refractory,
                   args.wave_width, not args.no_wav,
                   max_k=args.max_clusters)
        except Exception as exc:
            print(f"{t.name}: FAILED - {exc}", file=sys.stderr)
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
