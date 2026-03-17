#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Align camera frame timestamps to Open Ephys time using SYNC_START/STOP messages.

Inputs:
 - Open Ephys events CSV (exported; must contain message/text column and a timestamp column)
 - Camera CSV produced by sync_ephys_basler.py

Output:
 - CSV with per-frame aligned Open Ephys time (samples + seconds)
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path


START_TAG = "SYNC_START_UNIX_NS"
STOP_TAG = "SYNC_STOP_UNIX_NS"


def read_events_csv(path: Path) -> tuple[int, int, float, float]:
    """
    Returns:
        start_unix_ns, stop_unix_ns, start_oe_time, stop_oe_time
    """
    with open(path, "r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise RuntimeError("Events CSV has no header.")

        headers = [h.strip().lower() for h in reader.fieldnames]

        def find_col(candidates):
            for cand in candidates:
                if cand in headers:
                    return reader.fieldnames[headers.index(cand)]
            return None

        msg_col = find_col(["message", "text", "event", "description"])
        ts_col = find_col(
            [
                "timestamp",
                "timestamp_samples",
                "sample_index",
                "sample",
                "time",
                "time_seconds",
            ]
        )

        if msg_col is None or ts_col is None:
            raise RuntimeError(
                "Could not find message/text and timestamp columns. "
                "Use an exported events CSV that includes message/text and timestamp."
            )

        start_unix_ns = None
        stop_unix_ns = None
        start_oe_time = None
        stop_oe_time = None

        for row in reader:
            msg = str(row.get(msg_col, "")).strip()
            ts_raw = row.get(ts_col, None)
            if ts_raw is None or ts_raw == "":
                continue
            try:
                ts_val = float(ts_raw)
            except ValueError:
                continue

            if msg.startswith(START_TAG):
                parts = msg.split()
                if len(parts) >= 2:
                    start_unix_ns = int(parts[1])
                    start_oe_time = ts_val
            elif msg.startswith(STOP_TAG):
                parts = msg.split()
                if len(parts) >= 2:
                    stop_unix_ns = int(parts[1])
                    stop_oe_time = ts_val

        if start_unix_ns is None or start_oe_time is None:
            raise RuntimeError("SYNC_START_UNIX_NS not found in events CSV.")

        return start_unix_ns, stop_unix_ns or 0, start_oe_time, stop_oe_time or 0.0


def read_camera_csv(path: Path):
    with open(path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise RuntimeError("Camera CSV has no header.")
        for row in reader:
            yield row


def main() -> int:
    p = argparse.ArgumentParser(description="Align camera frames to Open Ephys time.")
    p.add_argument("--oe-events", required=True, help="Open Ephys events CSV path")
    p.add_argument("--camera-csv", required=True, help="Camera CSV path")
    p.add_argument("--out", required=True, help="Output aligned CSV path")
    p.add_argument(
        "--sample-rate",
        type=float,
        required=False,
        help="Open Ephys sample rate (Hz). Required unless OE timestamps are already in seconds.",
    )
    p.add_argument(
        "--oe-time-in-seconds",
        action="store_true",
        help="Set if OE event timestamps are already in seconds (not samples).",
    )
    args = p.parse_args()

    oe_events = Path(args.oe_events).resolve()
    camera_csv = Path(args.camera_csv).resolve()
    out_path = Path(args.out).resolve()

    start_unix_ns, stop_unix_ns, start_oe_time, stop_oe_time = read_events_csv(oe_events)

    # Convert OE time to seconds if in samples
    if args.oe_time_in_seconds:
        start_oe_sec = float(start_oe_time)
        stop_oe_sec = float(stop_oe_time) if stop_oe_time else None
    else:
        if not args.sample_rate:
            raise RuntimeError("Provide --sample-rate if OE timestamps are in samples.")
        start_oe_sec = float(start_oe_time) / float(args.sample_rate)
        stop_oe_sec = float(stop_oe_time) / float(args.sample_rate) if stop_oe_time else None

    # Build mapping from unix_ns -> oe_seconds
    if stop_unix_ns and stop_oe_sec is not None and stop_oe_sec > start_oe_sec:
        # Linear map to account for drift
        unix_span = (stop_unix_ns - start_unix_ns) / 1e9
        oe_span = (stop_oe_sec - start_oe_sec)
        slope = oe_span / unix_span if unix_span > 0 else 1.0
    else:
        # Fallback: no drift correction, only offset
        slope = 1.0

    def unix_to_oe_sec(unix_ns: int) -> float:
        dt_sec = (unix_ns - start_unix_ns) / 1e9
        return start_oe_sec + slope * dt_sec

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "frame_index",
                "unix_ns",
                "camera_timestamp",
                "grab_succeeded",
                "oe_time_sec",
                "oe_time_samples",
            ]
        )

        for row in read_camera_csv(camera_csv):
            try:
                unix_ns = int(row["unix_ns"])
            except Exception:
                continue

            oe_sec = unix_to_oe_sec(unix_ns)
            oe_samples = oe_sec * float(args.sample_rate) if not args.oe_time_in_seconds else ""

            writer.writerow(
                [
                    row.get("frame_index", ""),
                    unix_ns,
                    row.get("camera_timestamp", ""),
                    row.get("grab_succeeded", ""),
                    f"{oe_sec:.9f}",
                    f"{oe_samples:.3f}" if oe_samples != "" else "",
                ]
            )

    print(f"Wrote aligned CSV: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
