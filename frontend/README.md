# Frontend

The dashboard is a **Next.js 15 (React + TypeScript)** application that gives domain experts a single, read-only place to review the platform's output. It presents alerts as decision-support signals, not disease diagnoses.

## Tabs

**Dashboard (Live Map)**

- **Live feed** — recent BirdWeather detections.
- **Live map** — where recent activity is occurring in space.
- **Species tracker** — effort-normalized activity patterns for the tracked species over time and location.

**Alerts**

- **Embedding view** — how a flagged clip compares with historical acoustic examples.
- **Hotspots view** — alerts grouped by 5×5 km grid cell so spatial clusters can be inspected.
- **Evaluation view** — whether alerts align with SOVON mortality reports.

## Stack

Next.js · React · TypeScript · Tailwind CSS · MapLibre GL / react-map-gl (maps) · Recharts (charts).

## Development

```bash
cd frontend
npm install
npm run dev      # local dev server
npm run build    # production build
npm run lint     # ESLint (flat config)
```

The dashboard reads from the same TimescaleDB and MinIO backend as the rest of the stack; see [`infrastructure/docker/README.md`](../infrastructure/docker/README.md) for running the full system. Additional notes live in [`tools.md`](tools.md).
