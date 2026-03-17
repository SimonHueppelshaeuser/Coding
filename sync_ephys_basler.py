#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Synchronize Open Ephys GUI recording with a Basler camera (pypylon) and save MP4 + timestamp log.

Workflow:
1) Start Open Ephys acquisition + recording via HTTP API.
2) Send broadcast message with UNIX time for alignment.
3) Start Basler capture, write MP4, log per-frame timestamps.
4) Stop camera, send stop message, stop Open Ephys recording.

No TTL required. Alignment is done post hoc using the shared system clock and OE broadcast messages.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import sys
import time
from pathlib import Path

import requests

try:
    from pypylon import pylon
except Exception as exc:  # pragma: no cover
    print("ERROR: pypylon is required. Install with `pip install pypylon`.", file=sys.stderr)
    raise

try:
    import cv2
except Exception as exc:  # pragma: no cover
    print("ERROR: opencv-python is required. Install with `pip install opencv-python`.", file=sys.stderr)
    raise


def unix_ns() -> int:
    return time.time_ns()


def oe_get(url: str) -> dict:
    r = requests.get(url, timeout=5)
    r.raise_for_status()
    return r.json()


def oe_put(url: str, payload: dict) -> dict:
    r = requests.put(url, json=payload, timeout=5)
    r.raise_for_status()
    return r.json() if r.content else {}


def oe_set_mode(base_url: str, mode: str) -> None:
    oe_put(f"{base_url}/api/status", {"mode": mode})


def oe_get_mode(base_url: str) -> str:
    data = oe_get(f"{base_url}/api/status")
    return data.get("mode", "UNKNOWN")


def oe_message(base_url: str, text: str) -> None:
    oe_put(f"{base_url}/api/message", {"text": text})


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def make_output_paths(out_dir: Path, base: str) -> tuple[Path, Path]:
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = f"{base}_{ts}"
    video = out_dir / f"{stem}.mp4"
    log = out_dir / f"{stem}.csv"
    return video, log


def open_camera(serial: str | None, fps: float | None):
    tl_factory = pylon.TlFactory.GetInstance()
    devices = tl_factory.EnumerateDevices()
    if not devices:
        raise RuntimeError("No Basler cameras found.")

    device = None
    if serial:
        for d in devices:
            if d.GetSerialNumber() == serial:
                device = d
                break
        if device is None:
            raise RuntimeError(f"Camera with serial {serial} not found.")
    else:
        device = devices[0]

    cam = pylon.InstantCamera(tl_factory.CreateDevice(device))
    cam.Open()

    # Try to set a fixed frame rate if supported
    if fps is not None:
        if pylon.FeaturePersistence.IsWritable(cam.AcquisitionFrameRateEnable):
            cam.AcquisitionFrameRateEnable.SetValue(True)
            if pylon.FeaturePersistence.IsWritable(cam.AcquisitionFrameRate):
                cam.AcquisitionFrameRate.SetValue(float(fps))

    # Use continuous acquisition
    cam.StartGrabbing(pylon.GrabStrategy_OneByOne)

    # Converter to BGR for OpenCV writer
    converter = pylon.ImageFormatConverter()
    converter.OutputPixelFormat = pylon.PixelType_BGR8packed
    converter.OutputBitAlignment = pylon.OutputBitAlignment_MsbAligned

    return cam, converter


def create_writer(video_path: Path, width: int, height: int, fps: float) -> cv2.VideoWriter:
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(video_path), fourcc, fps, (width, height))
    if not writer.isOpened():
        raise RuntimeError("Failed to open VideoWriter. Check codec availability.")
    return writer


def run(args: argparse.Namespace) -> int:
    base_url = args.oe_url.rstrip("/")

    # Open Ephys preflight
    mode = oe_get_mode(base_url)
    print(f"Open Ephys status: {mode}")

    if args.recording_dir:
        oe_put(f"{base_url}/api/recording", {"parent_directory": args.recording_dir})

    if args.base_text:
        oe_put(f"{base_url}/api/recording", {"base_text": args.base_text})

    out_dir = Path(args.output_dir).resolve()
    ensure_dir(out_dir)
    video_path, log_path = make_output_paths(out_dir, args.file_base)

    # Start acquisition + recording
    if mode != "ACQUIRE":
        oe_set_mode(base_url, "ACQUIRE")
        time.sleep(0.2)
    oe_set_mode(base_url, "RECORD")
    time.sleep(0.2)

    start_unix_ns = unix_ns()
    oe_message(base_url, f"SYNC_START_UNIX_NS {start_unix_ns}")

    cam, converter = open_camera(args.serial, args.fps)

    try:
        w = int(cam.Width.Value)
        h = int(cam.Height.Value)
        effective_fps = float(args.fps) if args.fps else float(cam.ResultingFrameRate.Value)
        writer = create_writer(video_path, w, h, effective_fps)

        print(f"Recording video to: {video_path}")
        print(f"Logging timestamps to: {log_path}")
        print("Press Ctrl+C to stop.")

        with open(log_path, "w", newline="", encoding="utf-8") as f:
            csv_writer = csv.writer(f)
            csv_writer.writerow(
                [
                    "frame_index",
                    "unix_ns",
                    "camera_timestamp",
                    "grab_succeeded",
                ]
            )

            frame_idx = 0
            t_end = None
            if args.duration_sec:
                t_end = time.monotonic() + args.duration_sec

            while True:
                if t_end is not None and time.monotonic() >= t_end:
                    break

                grab = cam.RetrieveResult(5000, pylon.TimeoutHandling_ThrowException)
                try:
                    ok = bool(grab.GrabSucceeded())
                    ts_cam = int(grab.TimeStamp) if ok else -1
                    ts_unix = unix_ns()

                    if ok:
                        img = converter.Convert(grab).GetArray()
                        writer.write(img)

                    csv_writer.writerow([frame_idx, ts_unix, ts_cam, int(ok)])
                    frame_idx += 1
                finally:
                    grab.Release()

    finally:
        stop_unix_ns = unix_ns()
        try:
            oe_message(base_url, f"SYNC_STOP_UNIX_NS {stop_unix_ns}")
        except Exception:
            pass

        try:
            cam.StopGrabbing()
            cam.Close()
        except Exception:
            pass

        try:
            writer.release()
        except Exception:
            pass

        if args.stop_acquisition:
            oe_set_mode(base_url, "IDLE")
        else:
            oe_set_mode(base_url, "ACQUIRE")

    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Sync Open Ephys recording with Basler camera and save MP4 + timestamps."
    )
    p.add_argument("--oe-url", default="http://localhost:37497", help="Open Ephys HTTP server URL")
    p.add_argument("--serial", default=None, help="Basler camera serial (optional)")
    p.add_argument("--fps", type=float, default=None, help="Target camera FPS (optional)")
    p.add_argument("--duration-sec", type=float, default=None, help="Stop after N seconds")
    p.add_argument("--output-dir", default=".", help="Output directory for video + log")
    p.add_argument("--file-base", default="session", help="Base name for output files")
    p.add_argument("--recording-dir", default=None, help="Open Ephys parent directory")
    p.add_argument("--base-text", default=None, help="Open Ephys base_text for next recording")
    p.add_argument(
        "--stop-acquisition",
        action="store_true",
        help="Stop acquisition at the end (otherwise leaves ACQUIRE on)",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    raise SystemExit(run(args))
