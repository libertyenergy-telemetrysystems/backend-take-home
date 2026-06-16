"""
Telemetry Replay Simulator
--------------------------
Reads engine telemetry data from a CSV file and writes it to InfluxDB 3 Core
in accelerated time, simulating a live stream of incoming data.

Each "tick" the simulator writes the next CHUNK_MINUTES worth of CSV rows
to InfluxDB (using their original timestamps), then sleeps for
TICK_INTERVAL_SECONDS before writing the next chunk.

This means a 24-hour CSV with CHUNK_MINUTES=30 and TICK_INTERVAL_SECONDS=60
replays in roughly 48 minutes, giving candidates fresh data to process
approximately every minute.

Configuration (environment variables):
  INFLUX_HOST              InfluxDB base URL          (default: http://localhost:8181)
  INFLUX_DATABASE          InfluxDB database name     (default: engine_telemetry)
  INFLUX_TOKEN             InfluxDB auth token        (required — see setup.sh)
  CSV_FILE                 Path to the telemetry CSV  (default: ./data/telemetry.csv)
  CHUNK_MINUTES            Minutes of data per tick   (default: 30)
  TICK_INTERVAL_SECONDS    Seconds between ticks      (default: 60)

Expected CSV columns:
  timestamp   — ISO 8601 datetime string, e.g. 2024-01-15T08:00:00Z
  engine_id   — string identifier, e.g. ENG-001
  location    — site name, e.g. Site-Alpha
  rpm         — float, engine RPM
  oil_temp    — float, oil temperature in Celsius
  fuel_level  — float, fuel percentage 0-100
"""

import os
import sys
import time
import logging
from datetime import timedelta

import pandas as pd
from influxdb_client_3 import InfluxDBClient3, write_client_options, WriteOptions

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

INFLUX_HOST = os.environ.get("INFLUX_HOST", "http://localhost:8181")
INFLUX_DATABASE = os.environ.get("INFLUX_DATABASE", "engine_telemetry")
INFLUX_TOKEN = os.environ.get("INFLUX_TOKEN", "")
CSV_FILE = os.environ.get("CSV_FILE", "./data/telemetry.csv")
CHUNK_MINUTES = int(os.environ.get("CHUNK_MINUTES", "30"))
TICK_INTERVAL_SECONDS = int(os.environ.get("TICK_INTERVAL_SECONDS", "60"))

MEASUREMENT = "engine_telemetry"
TAG_COLUMNS = ["engine_id", "location"]
FIELD_COLUMNS = ["rpm", "oil_temp", "fuel_level"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_csv(path: str) -> pd.DataFrame:
    """Load and validate the telemetry CSV."""
    log.info(f"Loading CSV from {path}")
    df = pd.read_csv(path)

    required = {"timestamp", "engine_id", "location", "rpm", "oil_temp", "fuel_level"}
    missing = required - set(df.columns)
    if missing:
        log.error(f"CSV is missing required columns: {missing}")
        log.error(f"Found columns: {list(df.columns)}")
        sys.exit(1)

    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.sort_values("timestamp").reset_index(drop=True)

    log.info(
        f"Loaded {len(df):,} rows | "
        f"time range: {df['timestamp'].min()} → {df['timestamp'].max()} | "
        f"engines: {df['engine_id'].nunique()} | "
        f"locations: {df['location'].nunique()}"
    )
    return df


def rows_to_line_protocol(chunk: pd.DataFrame) -> list[str]:
    """Convert a DataFrame chunk to InfluxDB line protocol strings."""
    lines = []
    for row in chunk.itertuples(index=False):
        ts_ns = int(row.timestamp.timestamp() * 1_000_000_000)

        # Escape tag values (spaces and commas need escaping in line protocol)
        engine_id = str(row.engine_id).replace(" ", "\\ ").replace(",", "\\,")
        location = str(row.location).replace(" ", "\\ ").replace(",", "\\,")

        tags = f"engine_id={engine_id},location={location}"
        fields = f"rpm={float(row.rpm)},oil_temp={float(row.oil_temp)},fuel_level={float(row.fuel_level)}"

        lines.append(f"{MEASUREMENT},{tags} {fields} {ts_ns}")
    return lines


def write_chunk(client: InfluxDBClient3, lines: list[str], chunk_label: str) -> None:
    """Write a batch of line protocol strings to InfluxDB."""
    try:
        client.write(record="\n".join(lines), write_precision="ns")
        log.info(f"  ✓ Wrote {len(lines):,} points ({chunk_label})")
    except Exception as exc:
        log.error(f"  ✗ Write failed for {chunk_label}: {exc}")
        raise


# ---------------------------------------------------------------------------
# Main replay loop
# ---------------------------------------------------------------------------

def main() -> None:
    if not INFLUX_TOKEN:
        log.error(
            "INFLUX_TOKEN is not set. "
            "Run setup.sh first to create a token, then set it in .env"
        )
        sys.exit(1)

    df = load_csv(CSV_FILE)
    total_rows = len(df)
    total_duration = df["timestamp"].max() - df["timestamp"].min()
    chunk_delta = timedelta(minutes=CHUNK_MINUTES)

    log.info(
        f"Replay config: CHUNK_MINUTES={CHUNK_MINUTES}, "
        f"TICK_INTERVAL_SECONDS={TICK_INTERVAL_SECONDS}"
    )

    estimated_ticks = int(total_duration.total_seconds() / 60 / CHUNK_MINUTES) + 1
    estimated_wall_minutes = estimated_ticks * TICK_INTERVAL_SECONDS / 60
    log.info(
        f"Estimated replay: ~{estimated_ticks} ticks over ~{estimated_wall_minutes:.1f} wall-clock minutes"
    )

    client = InfluxDBClient3(
        host=INFLUX_HOST,
        database=INFLUX_DATABASE,
        token=INFLUX_TOKEN,
    )

    window_start = df["timestamp"].min()
    window_end = window_start + chunk_delta
    df_end = df["timestamp"].max()

    tick = 0
    rows_written = 0

    while window_start <= df_end:
        tick += 1
        chunk = df[(df["timestamp"] >= window_start) & (df["timestamp"] < window_end)]

        if chunk.empty:
            log.info(f"Tick {tick}: no rows in window {window_start} → {window_end}, skipping")
        else:
            chunk_label = f"{window_start.strftime('%H:%M')}–{window_end.strftime('%H:%M')} UTC"
            log.info(f"Tick {tick}/{estimated_ticks}: replaying {chunk_label} ({len(chunk):,} rows)")
            lines = rows_to_line_protocol(chunk)
            write_chunk(client, lines, chunk_label)
            rows_written += len(chunk)
            pct = rows_written / total_rows * 100
            log.info(f"  Progress: {rows_written:,}/{total_rows:,} rows ({pct:.1f}%)")

        window_start = window_end
        window_end = window_start + chunk_delta

        if window_start <= df_end:
            log.info(f"  Sleeping {TICK_INTERVAL_SECONDS}s until next tick...")
            time.sleep(TICK_INTERVAL_SECONDS)

    log.info(f"Replay complete. {rows_written:,} total rows written across {tick} ticks.")
    client.close()


if __name__ == "__main__":
    main()
