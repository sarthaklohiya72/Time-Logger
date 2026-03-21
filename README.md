# Time Tracker Pro

Time Tracker Pro is a personal productivity analytics dashboard built with Flask, SQLite, pandas, and Chart.js. It pulls raw time logs from a Google Sheet (via Sheety), normalizes them into structured sessions, and visualizes how your time is split across tags and the Eisenhower Matrix (urgent/important quadrants).

## Core Concepts

- **Logs**: Each row describes a task with start/end, duration, tag, and urgency/importance flags.
- **Eisenhower Matrix**:
  - Q1 – Critical: urgent and important.
  - Q2 – Growth: important, not urgent (discipline).
  - Q3 – Drudgery: urgent, not important (reactive obligations).
  - Q4 – Waste: neither urgent nor important.
- **Tags**: Free-form labels such as Work, Necessity, Waste.

## Backend Overview

- `/app.py`
  - Parses raw sheet rows into normalized time blocks (`TimeLogParser`).
  - Synchronizes cloud data into a local SQLite database (`sync_cloud_data`).
  - Exposes three main routes:
    - `/` – main dashboard.
    - `/api/tasks` – filtered tasks for a date/period.
    - `/api/tags` – tag stats for a date/period.
    - `/hard-reset` – full resync from Google Sheets (protected).
  - Computes matrix statistics via `get_matrix_stats`.
  - Normalizes date/period inputs and shared ranges via helpers:
    - `parse_date_param`
    - `parse_period_param`
    - `get_period_range`

### Security and Authorization

- Optional API token:
  - Set `TIME_TRACKER_API_TOKEN` in the environment.
  - All `/api/*` endpoints and `/hard-reset` require:
    - `X-API-Token` header **or**
    - `?token=YOUR_TOKEN` query parameter.
  - If unset, endpoints remain open (matches previous behavior).

### Cloud Sync Behavior

- `SHEETY_ENDPOINT` controls where to pull Google Sheet data from.
- Sync is automatically triggered from the dashboard view.
- To reduce load, sync is throttled:
  - `SYNC_INTERVAL_SECONDS` (default `300` seconds) defines the minimum time between syncs.
  - `/hard-reset` forces a full sync regardless of the interval.

## Frontend Overview

- `/templates/dashboard.html`
  - Uses Tailwind via CDN and Chart.js for visualizations.
  - Main sections:
    - Period and date controls.
    - Summary cards for Focus Time, Critical Load, and Aligned Time.
    - Priority matrix (Growth, Critical, Drudgery, Waste) with clickable quadrants.
    - Tag distribution doughnut chart (“Time by Category”).
    - Weekly bar chart (“This Week”).
    - Modal for deep-dive into tasks.

### Modal Insights

- When you open the modal:
  - Top stats show:
    - **Discipline** – important, not urgent time.
    - **Reactive** – urgent, not important time.
    - **Waste** – neither urgent nor important.
  - The pie chart adapts:
    - For tag filters, it shows Growth / Critical / Drudgery / Waste within that tag.
    - For other filters, it groups by normalized tag.
  - Hiding a slice in the chart hides those tasks in the list.

### Tag Color Semantics

Tag colors are consistent across charts and chips and tuned for psychological meaning:

- Work – healthy green.
- Important – bright positive green.
- Necessity – heavy amber (obligation).
- Urgent – strong red (fire).
- Waste – dark rose (regret).

Unknown tags fall back to a neutral luxury palette.

### Accessibility and UX

- `<main>` and `<header>` use semantic roles.
- Charts are given `role="img"` with descriptive `aria-label`s and hide purely decorative canvases from assistive tech.
- Modal is marked as `role="dialog"` with `aria-modal="true"` and `aria-labelledby`.
- A small global loading indicator appears during task fetches and hides when complete.
- Errors during task loading are surfaced via a subtle toast instead of only using alerts.

## Running the App

```bash
python app.py
```

The app starts on port `5001` by default.

## Backend Tests

Backend unit tests live in `tests/test_app.py` and cover:

- `get_matrix_stats` – distribution of durations into quadrants.
- `parse_period_param`, `parse_date_param` – safe handling of query parameters.
- `get_period_range` – correct date ranges for day/week/month.

Run tests with:

```bash
python -m unittest
```

## Configuration

Environment variables:

- `SHEETY_ENDPOINT` – URL to the Sheety API backing your Google Sheet.
- `SYNC_INTERVAL_SECONDS` – minimum seconds between automatic syncs (default 300).
- `TIME_TRACKER_API_TOKEN` – optional shared token for API/hard-reset protection.

