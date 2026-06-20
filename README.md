# Backend Take-Home — Engine Telemetry Environment

This repo sets up the data infrastructure for your take-home project. You do
not need to modify anything here — just get it running, then build your
application against it.

---

## What's running

| Container | Purpose |
|---|---|
| **InfluxDB 3 Core** | Time-series database receiving telemetry from 10 industrial engines across 3 sites |
| **Simulator** | Replays a 24-hour telemetry dataset into InfluxDB at an accelerated rate so data arrives continuously while you work |
| **Grafana** | Pre-wired to InfluxDB at `http://localhost:3000` so you can explore the data visually |

---

## Prerequisites

- **Docker Desktop** (or Docker Engine + Compose plugin) — [download here](https://www.docker.com/products/docker-desktop/)
  OR **Podman** with the Compose plugin
- **Python 3** with `pandas` and `numpy` (for the one-time data generation step)

---

## Setup

**Step 1 — Generate the telemetry dataset:**

```bash
pip install pandas numpy
python3 generate_csv.py
```

This creates `./data/telemetry.csv` using today's date. You only need to run
this once. The script prints the exact timestamps of events injected into the
data — keep that output handy.

**Step 2 — Start the environment:**

```bash
docker compose up
```

All three containers start automatically. The simulator begins writing data to
InfluxDB immediately and logs its progress to the console.

To run in the background instead:

```bash
docker compose up -d
docker compose logs -f simulator   # tail the simulator progress
```

**To stop without losing data:**

```bash
docker compose down
```

**To wipe everything and start fresh:**

```bash
docker compose down -v
python3 generate_csv.py
docker compose up
```

---

## InfluxDB

Your application reads engine telemetry from here.

| Setting | Value |
|---|---|
| URL | `http://localhost:8181` |
| Database | `engine_telemetry` |
| Auth | None |
| Query language | SQL |

InfluxDB 3 Core uses **SQL only** — no Flux or InfluxQL.

**Verify it's receiving data:**

```bash
curl -G "http://localhost:8181/api/v3/query_sql" \
  --data-urlencode "db=engine_telemetry" \
  --data-urlencode "q=SELECT COUNT(*) FROM engine_telemetry" \
  --data-urlencode "format=pretty"
```

---

## Grafana

Open **[http://localhost:3000](http://localhost:3000)** — no login required.

The InfluxDB datasource is pre-configured. To explore the data:

1. Click **Explore** (compass icon in the left sidebar)
2. Select **InfluxDB** as the datasource
3. Make sure the query editor is in **Code** mode
4. Write SQL and hit **Run query**

Set the time range picker (top right) to cover today's date to see data.

---

## Telemetry schema

All data lives in a single InfluxDB measurement: `engine_telemetry`

| Column | Type | Description |
|---|---|---|
| `time` | timestamp | Reading timestamp (UTC) |
| `engine_id` | string | Engine identifier — `ENG-001` through `ENG-010` |
| `location` | string | Site name — `Site-Alpha`, `Site-Bravo`, or `Site-Charlie` |
| `rpm` | float | Engine RPM — value on every row |
| `fuel_rate` | float | Fuel consumption (gph) — value on every row; ~5 gph at idle, ~60 gph at operating speed |
| `oil_temp` | float | Oil temperature (°C) — sparse: emitted at most every ~2 minutes |
| `fuel_level` | float | Fuel level (0–100%) — sparse: emitted at most every ~2 minutes |

10 engines, 3 sites, one reading per engine every 5 seconds, covering 24 hours.
`rpm` and `fuel_rate` are present on every row. `oil_temp` and `fuel_level` are
sparse — most rows have no value for these columns; queries should account for
gaps (e.g. `WHERE oil_temp IS NOT NULL`).

**Starter queries:**

```sql
-- What engines are in the dataset?
SELECT DISTINCT engine_id, location FROM engine_telemetry ORDER BY engine_id;

-- Latest readings for a specific engine
SELECT time, rpm, fuel_rate, oil_temp, fuel_level
FROM engine_telemetry
WHERE engine_id = 'ENG-001'
ORDER BY time DESC
LIMIT 50;

-- RPM for all engines across a time window
-- Replace the timestamps with values from your generate_csv.py output
SELECT time, engine_id, rpm
FROM engine_telemetry
WHERE time >= '2026-06-16T08:00:00Z'
  AND time <  '2026-06-16T09:00:00Z'
ORDER BY time ASC;
```

---

## How the simulator works

The simulator replays the CSV into InfluxDB in chunks:

- Every tick it writes the next **30 minutes** of telemetry using the original timestamps
- The full 24-hour dataset finishes replaying in roughly 48 ticks
- Data is timestamped to **today** so queries against recent time ranges always return results

The simulator exits cleanly when replay is complete — this is expected, not a crash.

---

## Your task

Build a **separate application** (outside this repo) that:

1. Connects to the InfluxDB instance above
2. Queries the engine telemetry on a schedule
3. Detects engine events from the RPM data
4. Persists confirmed events to a database of your choice

The choice of database is entirely yours — pick whatever fits your design and
spin it up as part of your own application. This repo only provides the
telemetry infrastructure above.

Refer to the project brief you received for the full event definition, required
output schema, and deliverables.
