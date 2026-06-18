"""
generate_csv.py
---------------
Generates a 24-hour engine telemetry CSV suitable for the backend interview
data environment.

Produces realistic RPM traces for 10 engines across 3 sites, with a
controlled number of early shutdown events (throttle drop from >1000 RPM
to ~700 RPM) baked in at known times so the answer key is verifiable.

Channel update rates:
  rpm, fuel_rate — fast channels, value on every row (every 5 seconds)
  oil_temp, fuel_level — slow channels, sparse: NaN on most rows, a reading
                         emitted at most every ~2 minutes (24-row heartbeat
                         with ±2-row jitter).

Output: ./data/telemetry.csv

Usage:
    python generate_csv.py [--seed 42] [--out ./data/telemetry.csv]
    python generate_csv.py --date 2026-06-16   # fix a specific date
"""

import argparse
import random
from datetime import date, datetime, timezone, timedelta

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ENGINES = [
    ("ENG-001", "Site-Alpha"),
    ("ENG-002", "Site-Alpha"),
    ("ENG-003", "Site-Alpha"),
    ("ENG-004", "Site-Bravo"),
    ("ENG-005", "Site-Bravo"),
    ("ENG-006", "Site-Bravo"),
    ("ENG-007", "Site-Bravo"),
    ("ENG-008", "Site-Charlie"),
    ("ENG-009", "Site-Charlie"),
    ("ENG-010", "Site-Charlie"),
]

# 24 hours of data, one reading every 5 seconds
# START_TIME is set at runtime (defaults to today)
DURATION_HOURS = 24
INTERVAL_SECONDS = 5

# Early shutdown events to inject — (engine_id, offset_hours_into_day)
# Spread across engines and times so there's variety for the dashboard
SHUTDOWN_EVENTS = [
    ("ENG-001", 1.5),
    ("ENG-001", 8.2),
    ("ENG-002", 3.7),
    ("ENG-003", 11.0),
    ("ENG-003", 19.5),
    ("ENG-004", 2.1),
    ("ENG-005", 6.8),
    ("ENG-005", 14.3),
    ("ENG-005", 22.1),
    ("ENG-006", 9.5),
    ("ENG-007", 17.0),
    ("ENG-008", 4.4),
    ("ENG-008", 13.6),
    ("ENG-009", 7.2),
    ("ENG-010", 20.8),
]

# How long a shutdown event lasts (seconds) before RPM recovers
SHUTDOWN_DURATION_SECONDS = 240  # 4 minutes

# Slow-channel heartbeat: emit oil_temp / fuel_level at most every N rows
# 24 rows × 5 s/row = 120 s = 2 minutes
SLOW_EMIT_INTERVAL = 24


# ---------------------------------------------------------------------------
# Engine state machine
# ---------------------------------------------------------------------------

def generate_engine_trace(
    engine_id: str,
    location: str,
    timestamps: list[datetime],
    shutdown_offsets_seconds: list[int],
    rng: np.random.Generator,
) -> pd.DataFrame:
    """
    Generate a realistic telemetry trace for one engine over the full time range.

    Fast channels (every row):
      rpm      — floats 1050–1400 in normal operation; drops to ~700 during shutdown
      fuel_rate — ~5 gph at 700 RPM, ~60 gph at operating speeds, with small noise

    Slow channels (sparse — NaN on most rows, value every ~2 minutes):
      oil_temp   — rises slightly when RPM is low
      fuel_level — drains slowly over the day

    Shutdown event: RPM ramps down to ~680–740 over ~30 s, holds, then ramps back up.
    """
    n = len(timestamps)
    rpm = np.zeros(n)
    fuel_rate = np.zeros(n)
    oil_temp = np.full(n, np.nan)
    fuel_level = np.full(n, np.nan)

    # Baseline operating RPM for this engine (slight variance per engine)
    base_rpm = rng.uniform(1100, 1300)
    base_oil = rng.uniform(78, 92)
    fuel_start = rng.uniform(60, 95)

    # Build a set of shutdown windows: (start_idx, end_idx)
    shutdown_windows = []
    for offset_s in shutdown_offsets_seconds:
        start_idx = offset_s // INTERVAL_SECONDS
        end_idx = start_idx + (SHUTDOWN_DURATION_SECONDS // INTERVAL_SECONDS)
        shutdown_windows.append((start_idx, min(end_idx, n - 1)))

    def in_shutdown(idx):
        for s, e in shutdown_windows:
            if s <= idx <= e:
                return (True, s, e)
        return (False, 0, 0)

    current_rpm = base_rpm
    current_oil = base_oil
    current_fuel = fuel_start

    # Next row index at which to emit slow-channel values
    next_slow_emit = 0

    for i in range(n):
        is_down, s_start, s_end = in_shutdown(i)

        if is_down:
            progress = (i - s_start) / max(s_end - s_start, 1)
            if progress < 0.15:
                # Rapid drop phase
                target = rng.uniform(680, 740)
                current_rpm = current_rpm + (target - current_rpm) * 0.35
            elif progress < 0.85:
                # Hold at low RPM with small noise
                current_rpm = rng.uniform(685, 730)
            else:
                # Ramp back up
                current_rpm = current_rpm + (base_rpm - current_rpm) * 0.25
        else:
            # Normal operation: smooth random walk around base
            drift = rng.normal(0, 8)
            current_rpm = np.clip(current_rpm + drift, base_rpm - 150, base_rpm + 150)
            # Slowly revert toward base
            current_rpm = current_rpm * 0.98 + base_rpm * 0.02

        # Oil temp tracks RPM loosely — rises when RPM is low (less cooling airflow)
        rpm_factor = (current_rpm - 700) / 600  # 0 at 700 RPM, 1 at 1300
        target_oil = base_oil + (1 - rpm_factor) * 8
        current_oil = current_oil * 0.995 + target_oil * 0.005
        current_oil = float(np.clip(current_oil + rng.normal(0, 0.3), 65, 110))

        # Fuel drains slowly, slightly faster at higher RPM
        fuel_drain = (0.0005 + 0.0002 * rpm_factor) * INTERVAL_SECONDS
        current_fuel = max(0.0, current_fuel - fuel_drain + rng.normal(0, 0.01))

        # --- Fast channels: value on every row ---
        rpm[i] = round(float(current_rpm), 2)

        # fuel_rate: ~5 gph at 700 RPM, ~60 gph at 1200 RPM (operating speed)
        fuel_rate_base = 5.0 + max(0.0, current_rpm - 700.0) * 0.11
        fuel_rate[i] = round(float(np.clip(fuel_rate_base * (1.0 + rng.normal(0, 0.05)), 0.0, 70.0)), 2)

        # --- Slow channels: heartbeat every SLOW_EMIT_INTERVAL rows ±2 jitter ---
        if i >= next_slow_emit:
            oil_temp[i] = round(current_oil, 2)
            fuel_level[i] = round(float(np.clip(current_fuel, 0, 100)), 2)
            jitter = int(rng.integers(-2, 3))
            next_slow_emit = i + SLOW_EMIT_INTERVAL + jitter

    return pd.DataFrame({
        "timestamp": [t.strftime("%Y-%m-%dT%H:%M:%SZ") for t in timestamps],
        "engine_id": engine_id,
        "location": location,
        "rpm": rpm,
        "fuel_rate": fuel_rate,
        "oil_temp": oil_temp,
        "fuel_level": fuel_level,
    })


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Generate interview telemetry CSV")
    parser.add_argument("--seed", type=int, default=42, help="Random seed (default: 42)")
    parser.add_argument("--out", default="./data/telemetry.csv", help="Output path")
    parser.add_argument("--date", default=None, help="Start date YYYY-MM-DD (default: today)")
    args = parser.parse_args()

    if args.date:
        start_date = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        start_date = datetime.combine(date.today(), datetime.min.time()).replace(tzinfo=timezone.utc)

    START_TIME = start_date

    rng = np.random.default_rng(args.seed)
    random.seed(args.seed)

    total_seconds = DURATION_HOURS * 3600
    timestamps = [
        START_TIME + timedelta(seconds=i)
        for i in range(0, total_seconds, INTERVAL_SECONDS)
    ]

    print(f"Generating telemetry for {len(ENGINES)} engines over {DURATION_HOURS}h "
          f"({len(timestamps):,} timestamps each)...")
    print(f"Total shutdown events to inject: {len(SHUTDOWN_EVENTS)}")

    # Build a lookup: engine_id -> list of offset_seconds
    shutdown_map: dict[str, list[int]] = {}
    for eng_id, offset_hours in SHUTDOWN_EVENTS:
        offset_s = int(offset_hours * 3600)
        shutdown_map.setdefault(eng_id, []).append(offset_s)

    frames = []
    for engine_id, location in ENGINES:
        offsets = shutdown_map.get(engine_id, [])
        print(f"  {engine_id} ({location}) — {len(offsets)} shutdown event(s)")
        df = generate_engine_trace(engine_id, location, timestamps, offsets, rng)
        frames.append(df)

    print("Combining and sorting...")
    combined = pd.concat(frames, ignore_index=True)
    combined = combined.sort_values(["timestamp", "engine_id"]).reset_index(drop=True)

    combined.to_csv(args.out, index=False)

    size_mb = combined.memory_usage(deep=True).sum() / 1024 / 1024
    print(f"\nDone.")
    print(f"  Rows:        {len(combined):,}")
    print(f"  Output:      {args.out}")
    print(f"  Approx size: {size_mb:.1f} MB in memory")

    # Print a summary of injected events for the answer key
    print(f"\nInjected shutdown events (answer key):")
    print(f"  {'Engine':<10} {'Location':<15} {'Event time (UTC)'}")
    print(f"  {'-'*10} {'-'*15} {'-'*25}")
    for eng_id, offset_hours in sorted(SHUTDOWN_EVENTS, key=lambda x: (x[0], x[1])):
        event_time = start_date + timedelta(hours=offset_hours)
        location = next(loc for eid, loc in ENGINES if eid == eng_id)
        print(f"  {eng_id:<10} {location:<15} {event_time.strftime('%Y-%m-%dT%H:%M:%SZ')}")


if __name__ == "__main__":
    main()
