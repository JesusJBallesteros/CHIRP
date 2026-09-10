#!/usr/bin/env python3
"""
Cluster statistics for a whole recording, with nothing rendered.

Sizing a session up is CHIRP's main use: run every channel, read the report,
then choose the sorter's settings. That survey is what this module does, and
it is deliberately separate from dat_to_video.py, which exists to draw a single
excerpt. Nothing here opens a window, writes a WAV or needs ffmpeg.

The detection, clustering and per-cluster measurements all live in
dat_to_video.py; this module only owns the run: one cross-channel pass, then
the best few windows of every channel, then the CSV and the report.

Usage:
    # every channel in a recording folder
    python dat_to_stats.py --folder /path/to/recording --all

    # two channels, 20 s windows, written somewhere specific
    python dat_to_stats.py amp-A-010.dat amp-A-011.dat -d 20 -o ./survey

Importable too, which is how the GUI and external wrappers drive it:

    import dat_to_stats as eng_stats
    result = eng_stats.run_stats(paths, eng_stats.StatsParams(fs=30000))
    print(eng_stats.build_report(result, []))
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Same nudge dat_to_video.py and intan_gui.py use: the modules import each
# other by bare name, so their own directory has to be importable even when the
# script is launched from a recording folder somewhere else entirely.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import dat_to_audio as eng_audio
import dat_to_video as eng_video
from dat_to_audio import DEFAULT_BAND, SAMPLE_RATE, __version__
from dat_to_video import AUTO_LABEL, Cancelled

# Statistics are averaged over this many of the best-scoring windows per
# channel rather than a single one - one excerpt is a thin basis for a firing
# rate. When a video is rendered as well, it uses the first of them.
STAT_SEGMENTS = 3

DEFAULT_CSV_NAME = "chirp_cluster_stats.csv"
DEFAULT_REPORT_NAME = "chirp_report.txt"


# ------------------------------------------------------------------- params --
@dataclass
class StatsParams:
    """
    Everything that changes what the numbers mean, in the engine's own
    vocabulary. Callers build one of these rather than passing a dozen loose
    arguments, so the report can state the settings the rows were measured
    under without being handed a second copy of them.
    """
    duration: float = 10.0              # excerpt length, s
    start: float | None = None          # fixed start, s; None = scan for windows
    band: tuple[float, float] = DEFAULT_BAND
    fs: int = SAMPLE_RATE               # acquisition rate, Hz
    neg_k: float = 5.0                  # detection threshold, sigma below zero
    pos_k: float = 8.0                  # artifact rejection, sigma above zero
    artifact_k: float = 18.0            # window is "clean" below this x sigma
    max_k: int = 3                      # most amplitude clusters to consider
    segments: int = STAT_SEGMENTS       # windows per channel
    scan_step: float = 60.0             # spacing of candidate windows, s
    scan_range: tuple[float, float] | None = None
    pre_ms: float = 1.0                 # waveform window before the trough
    post_ms: float = 2.0                # waveform window after the trough
    refractory_ms: float = 1.0          # minimum spacing between detections

    def validate(self) -> "StatsParams":
        """Reject the combinations that would otherwise fail deep in a scan."""
        if self.duration <= 0:
            raise ValueError("duration must be positive")
        if not 1 <= self.max_k <= 3:
            raise ValueError("max_k must be 1, 2 or 3")
        if self.segments < 1:
            raise ValueError("segments must be at least 1")
        if self.artifact_k <= 0:
            raise ValueError("artifact_k must be positive")
        lo, hi = self.band
        if not 0 < lo < hi < self.fs / 2:
            raise ValueError(f"band must satisfy 0 < low < high < {self.fs / 2:.0f} Hz "
                             f"(Nyquist), got {lo:g}-{hi:g} Hz")
        return self


@dataclass
class StatsResult:
    """A finished run: the rows, plus the context needed to describe them."""
    rows: list = field(default_factory=list)
    segments: dict = field(default_factory=dict)   # channel stem -> [start_s, ...]
    shares: dict = field(default_factory=dict)     # channel stem -> co-active count
    n_channels: int = 0                            # channels in the sharing pass
    params: StatsParams = field(default_factory=StatsParams)


# ---------------------------------------------------------------- the run --
def run_stats(paths, params, cancel=None, progress=None, status=None,
              on_rows=None, on_channel=None, verbose=True) -> StatsResult:
    """
    Survey every channel in `paths` and return the rows.

    paths       list of amp-*.dat Paths, all from one session.
    params      StatsParams.
    cancel      callable() -> bool, polled between channels; True raises Cancelled.
    progress    callable(fraction 0..1) as the run advances.
    status      callable(text) naming the phase or the channel in flight.
    on_rows     callable(rows) once per channel x segment, for a live table.
    on_channel  callable(path, segments, rows) once per finished channel. The
                chosen windows are handed over so a caller that also renders
                does not have to scan for them a second time.

    The cross-channel pass runs first and over one window common to every
    channel: sharing is a property of a channel within its session, so each
    channel's own best window would not be comparable. It needs two or more
    channels to mean anything and is skipped below that, which leaves
    auto_quality resting on waveform and rate alone.
    """
    params = params.validate()
    paths = [Path(p) for p in paths]
    if not paths:
        raise ValueError("no channels given")

    def check():
        if cancel is not None and cancel():
            raise Cancelled("cancelled")

    def say(text):
        if status is not None:
            status(text)

    def note(text):
        if verbose:
            print(text, flush=True)

    result = StatsResult(params=params)

    # Sharing costs one read per channel, the per-channel work costs a scan
    # plus one read per segment. Weighting the bar by that keeps it honest on
    # long recordings, where the scan dominates.
    share_weight = 0.05 if len(paths) >= 2 else 0.0

    def advance(frac):
        if progress is not None:
            progress(min(1.0, max(0.0, frac)))

    # --- cross-channel pass ------------------------------------------------
    if len(paths) >= 2:
        shortest = min(eng_audio.file_duration_s(p, params.fs) for p in paths)
        share_start = (params.start if params.start is not None
                       else max(0.0, (shortest - params.duration) / 2))
        say("cross-channel scan")
        note(f"cross-channel scan: {len(paths)} channels, "
             f"{params.duration:.0f} s at {share_start:.0f} s")
        result.shares, result.n_channels = eng_video.session_sharing(
            paths, share_start, params.duration, params.band, params.fs,
            params.neg_k, params.pos_k,
            pre_ms=params.pre_ms, post_ms=params.post_ms,
            refractory_ms=params.refractory_ms,
            cancel=cancel, progress=lambda f: advance(share_weight * f),
            verbose=verbose)
        busy = [s for s in result.shares.values()
                if s > max(eng_video.SHARE_MIN_CHANNELS,
                           eng_video.SHARE_FRAC * result.n_channels)]
        note(f"  {len(busy)}/{result.n_channels} channel(s) carry a signal "
             f"shared across a large batch")
    else:
        note("cross-channel scan skipped: needs 2+ channels")

    # --- per-channel pass --------------------------------------------------
    for i, path in enumerate(paths):
        check()
        say(path.name)
        done_frac = share_weight + (1.0 - share_weight) * (i / len(paths))
        span = (1.0 - share_weight) / len(paths)

        if params.start is not None:
            segments = [params.start]
        else:
            segments = eng_video.find_best_windows(
                path, params.duration, params.band, params.fs,
                params.scan_step, params.artifact_k, params.neg_k, params.pos_k,
                params.pre_ms, params.post_ms, params.refractory_ms,
                params.scan_range, True, verbose,
                cancel=cancel,
                progress=lambda f, d=done_frac, s=span: advance(d + 0.5 * s * f),
                max_k=params.max_k, top_n=params.segments)
        if not segments:
            segments = [(eng_audio.file_duration_s(path, params.fs)
                         - params.duration) / 2]
        result.segments[path.stem] = segments

        rows_here = []
        for rank, seg_start in enumerate(segments, start=1):
            check()
            rows, _ = eng_video.channel_stats(
                path, seg_start, params.duration, params.band, params.fs,
                params.neg_k, params.pos_k,
                pre_ms=params.pre_ms, post_ms=params.post_ms,
                refractory_ms=params.refractory_ms, max_k=params.max_k,
                segment=rank,
                share_count=result.shares.get(path.stem),
                n_channels=result.n_channels)
            result.rows += rows
            rows_here += rows
            if on_rows is not None:
                on_rows(rows)
            note(f"{path.name} segment {rank} ({seg_start:.0f} s): "
                 f"{len(rows)} cluster(s) - " + "; ".join(
                     f"#{r['cluster']} {r['mean_amplitude_uV']:.0f} uV, "
                     f"{r['firing_rate_sp_s']:.1f} sp/s, SNR {r['snr']:.1f}, "
                     f"auto {AUTO_LABEL.get(r['auto_quality'], '?')}"
                     for r in rows))
            advance(done_frac + span * (0.5 + 0.5 * rank / len(segments)))

        if on_channel is not None:
            on_channel(path, segments, rows_here)

    advance(1.0)
    return result


# ------------------------------------------------------------------ report --
def build_report(result: StatsResult, files=()) -> str:
    """Plain-text summary of a whole run. `files` are the paths it wrote."""
    import statistics as st
    from collections import Counter

    rows, p = result.rows, result.params
    if not rows:
        return "CHIRP - cluster statistics report\nno clusters found"

    # Cluster count is a property of one excerpt, so tally it per
    # channel x segment rather than collapsing it onto the channel.
    per_seg = {}
    for r in rows:
        per_seg[(r["channel"], r["segment"])] = r["n_clusters"]
    kdist = Counter(per_seg.values())
    channels = {r["channel"] for r in rows}

    def pm(key, fmt):
        vals = [r[key] for r in rows]
        m = st.fmean(vals)
        sd = st.pstdev(vals) if len(vals) > 1 else 0.0
        return f"{fmt.format(m)} +/- {fmt.format(sd)}"

    L = []
    L.append("CHIRP - cluster statistics report")
    L.append("=" * 60)
    L.append(f"channels analysed   : {len(channels)}")
    L.append(f"segments analysed   : {len(per_seg)} "
             f"({len(per_seg) / max(1, len(channels)):.1f} per channel)")
    L.append(f"excerpt             : {p.duration:.0f} s" + (
        f" from {p.start:.0f} s" if p.start is not None
        else f", best {p.segments} non-overlapping windows per channel"))
    L.append(f"band-pass           : {p.band[0]:.0f}-{p.band[1]:.0f} Hz")
    L.append(f"detect / reject     : -{p.neg_k:g} sigma / +{p.pos_k:g} sigma")
    L.append(f"artifact scan       : {p.artifact_k:g} sigma")
    L.append(f"max clusters        : {p.max_k}")
    L.append("")
    L.append("clusters per segment : " + ", ".join(
        f"{k} cluster(s) x {v} segment(s)" for k, v in sorted(kdist.items())))
    L.append(f"cluster rows in total: {len(rows)}")
    L.append(f"spikes in total      : {sum(r['n_spikes'] for r in rows)}")
    L.append("")
    L.append("across all cluster rows (mean +/- sd):")
    L.append(f"  mean amplitude     : {pm('mean_amplitude_uV', '{:.1f}')} uV")
    L.append(f"  firing rate        : {pm('firing_rate_sp_s', '{:.1f}')} sp/s")
    L.append(f"  SNR                : {pm('snr', '{:.1f}')}")
    L.append(f"  half-width         : {pm('half_width_ms', '{:.3f}')} ms")
    L.append(f"  trough-to-peak     : {pm('trough_to_peak_ms', '{:.2f}')} ms")
    L.append(f"  noise sigma        : {pm('sigma_uV', '{:.2f}')} uV")
    L.append("")
    tagged = [r for r in rows if r.get("auto_quality")]
    if tagged:
        ac = Counter(r["auto_quality"] for r in tagged)
        L.append("first-pass tag, per cluster:")
        for v in (1, 2, 3):
            if ac.get(v):
                L.append(f"  {AUTO_LABEL[v]:<14s} {ac[v]:4d} / {len(tagged)}")
        if len(tagged) < len(rows):
            L.append(f"  {'not assessed':<14s} {len(rows) - len(tagged):4d}"
                     f" / {len(rows)}  (too few spikes to judge)")
        # A channel is worth opening if any of its clusters looks isolated.
        best = {}
        for r in tagged:
            b = best.get(r["channel"], 9)
            best[r["channel"]] = min(b, r["auto_quality"])
        bc = Counter(best.values())
        L.append("")
        L.append("first-pass tag, per channel (best cluster on it):")
        for v in (1, 2, 3):
            if bc.get(v):
                L.append(f"  {AUTO_LABEL[v]:<14s} {bc[v]:4d} / {len(best)}")
        worth = sorted(c for c, v in best.items() if v == 1)
        if worth:
            L.append("")
            L.append("channels with a cluster that looks isolated:")
            for i in range(0, len(worth), 6):
                L.append("  " + ", ".join(worth[i:i + 6]))
        L.append("")
        L.append("This tag is a first pass meant to be reviewed, not a")
        L.append("verdict. Against one hand-tagged session it agreed on")
        L.append("95% of channels and 91% of clusters.")
    if files:
        L.append("")
        L.append("written:")
        for f in files:
            L.append(f"  {f}")
    L.append("")
    L.append("note: half-width and trough-to-peak shift with the")
    L.append("      band-pass, so compare them only between recordings")
    L.append("      filtered the same way.")
    return "\n".join(L)


def write_report(text, path):
    """Save the report next to the CSV, so a run leaves both behind."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------- main --
def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Survey Intan .dat channels and write cluster statistics. "
                    "Renders nothing; see dat_to_video.py for that.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("files", nargs="*", type=Path,
                   help="specific .dat files (default: --all)")
    p.add_argument("--all", action="store_true",
                   help="every amp-*.dat in the folder")
    p.add_argument("--folder", type=Path, default=None,
                   help="folder holding the amp-*.dat files "
                        "(default: current directory)")
    p.add_argument("-o", "--out-dir", type=Path, default=None,
                   help="output directory (default: <folder>/stats)")
    p.add_argument("-d", "--duration", type=float, default=10.0,
                   help="seconds of signal per window")
    p.add_argument("-s", "--start", type=float, default=None,
                   help="fixed start time in s (default: scan for windows)")
    p.add_argument("-b", "--band", type=float, nargs=2,
                   metavar=("LOW", "HIGH"), default=list(DEFAULT_BAND),
                   help="band-pass limits in Hz")
    p.add_argument("--fs", type=int, default=SAMPLE_RATE,
                   help="acquisition sample rate of the .dat files")
    p.add_argument("--neg-k", type=float, default=5.0,
                   help="detection threshold, in sigma below zero")
    p.add_argument("--pos-k", type=float, default=8.0,
                   help="artifact rejection threshold, in sigma above zero")
    p.add_argument("--artifact-k", type=float, default=18.0,
                   help="a window is 'clean' if no sample exceeds this x sigma")
    p.add_argument("--max-clusters", type=int, default=3, choices=(1, 2, 3),
                   help="most amplitude clusters to consider")
    p.add_argument("--segments", type=int, default=STAT_SEGMENTS,
                   help="best non-overlapping windows measured per channel")
    p.add_argument("--scan-step", type=float, default=60.0,
                   help="spacing of candidate windows when scanning, in s")
    p.add_argument("--scan-range", type=float, nargs=2, default=None,
                   metavar=("FROM", "TO"), help="restrict the scan, in s")
    p.add_argument("--csv-name", default=DEFAULT_CSV_NAME,
                   help="name of the statistics table")
    p.add_argument("--report-name", default=DEFAULT_REPORT_NAME,
                   help="name of the text report")
    p.add_argument("-q", "--quiet", action="store_true",
                   help="only print the final report")
    p.add_argument("--version", action="version", version=f"CHIRP {__version__}")
    args = p.parse_args(argv)

    folder = (args.folder or Path.cwd()).resolve()
    out_dir = args.out_dir or folder / "stats"
    available = sorted(folder.glob("amp-*.dat"))

    if args.files:
        targets = [f if f.is_absolute() else folder / f for f in args.files]
        missing = [t for t in targets if not t.is_file()]
        if missing:
            print("Not found: " + ", ".join(map(str, missing)), file=sys.stderr)
            return 1
    elif args.all:
        targets = available
    else:
        print("Give one or more .dat files, or --all", file=sys.stderr)
        return 1
    if not targets:
        print(f"No amp-*.dat files found in {folder}", file=sys.stderr)
        return 1

    try:
        params = StatsParams(
            duration=args.duration, start=args.start,
            band=(args.band[0], args.band[1]), fs=args.fs,
            neg_k=args.neg_k, pos_k=args.pos_k, artifact_k=args.artifact_k,
            max_k=args.max_clusters, segments=args.segments,
            scan_step=args.scan_step,
            scan_range=tuple(args.scan_range) if args.scan_range else None,
        ).validate()
    except ValueError as exc:
        print(f"bad parameters: {exc}", file=sys.stderr)
        return 1

    try:
        result = run_stats(targets, params, verbose=not args.quiet)
    except Cancelled:
        print("cancelled", file=sys.stderr)
        return 1
    if not result.rows:
        print("no clusters found in any channel", file=sys.stderr)
        return 1

    csv_path = eng_video.write_stats_csv(result.rows, out_dir / args.csv_name)
    report = build_report(result, [csv_path])
    report_path = write_report(report, out_dir / args.report_name)
    if not args.quiet:
        print()
    print(report)
    print(f"\n  -> {csv_path}  ({len(result.rows)} rows)")
    print(f"  -> {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
