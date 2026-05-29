# VeilWatch Operator Dashboard

Phase 3 Slice 1 — Vue 3 + Vite + TypeScript + Tailwind operator console.

## What it does

- **Live Map** (`/live`) — Leaflet map of the camera network with cross-camera handoffs drawn as fading lines, plus a right-hand live alert feed.
- **Incident Queue** (`/incidents`) — Alerts awaiting operator confirmation. One-click confirm action that calls `POST /alerts/{id}/operator-confirm`.
- **Connection badge** — live/connecting/offline/error status with exponential-backoff auto-reconnect.

Data sources:

- **REST** `GET /alerts/recent` — bootstrap history on mount.
- **WebSocket** `/ws/live-map` — streams `ConfirmedAlert` and `CrossCameraMatch` events in real time.

If the confirmation service isn't running the dashboard still boots with an empty state and the connection badge shows `OFFLINE`. It retries automatically.

## Run it

```bash
# From the project root
cd frontend/operator-dashboard
npm install
npm run dev
```

Dev server: <http://localhost:5173>. Type-check: `npm run typecheck`. Production build: `npm run build`.

The Vite dev server proxies:

| URL path on :5173 | Forwards to |
|---|---|
| `/api/*` | `http://localhost:8080/*` (strips the `/api` prefix) |
| `/ws/*` | `ws://localhost:8080/ws/*` |

So you don't need CORS for local dev. Start the confirmation service separately (`docker compose up confirmation` or the relevant `python -m confirmation.main`).

## Architecture notes

- **Pinia stores** (`stores/incidents.ts`, `stores/cameras.ts`) are the single source of truth. Components are stateless presentational.
- **WebSocket client** (`api/live-map.ts`) self-heals with exponential backoff capped at 15s. Malformed frames are logged and dropped — a corrupt payload cannot take the socket down.
- **Optimistic confirm**: after calling `operator-confirm` the local store updates the alert's disposition immediately; the backend re-emits via WS to reconcile.
- **Leaflet + OSM tiles** for zero-API-key dev. Dark theming is a CSS filter on `.leaflet-tile` — production should ship a proper dark-themed tile source.
- **No Mapbox token required.** The plan calls for Mapbox GL JS; swapping is a single-component change in `components/CameraMap.vue` once a token is provisioned.

## Charter constraints pinned by this dashboard

- **Constraint #2** (no automated dispatch without a logged human confirmation): the Incident Queue requires an operator ID before any confirm action fires. The backend `operator-confirm` endpoint writes an `OPERATOR_CONFIRMED` audit record before publishing the confirmed alert — enforced at the API layer, not just the UI.
- **Never round up threat scores**: the dashboard displays whatever `threat_score_display` the backend sends (which goes through `format_score`, i.e. `math.floor`). The `ThreatScoreBar` also floors the percentage for the progress fill.

## Deployment

For a same-origin deployment (SPA and API served from one hostname), no CORS config needed.

For a split deployment, enable CORS on the confirmation service:

```bash
CONFIRMATION_CORS_ORIGINS=https://operator.veilwatch.example python -m confirmation.main
```

Multiple origins: comma-separated. Wildcards are deliberately refused — this API can trigger dispatch.

## Roadmap

**Slice 2** (next):
- Postgres-backed incidents history (survives restart)
- New `/alerts/search` endpoint with date range + camera + disposition filters
- Export incident as PDF/CAD payload

**Slice 3**:
- Mapbox GL JS swap (requires token)
- Camera thumbnails in alert cards (frame preview from capture service)
- Multi-operator presence indicators
