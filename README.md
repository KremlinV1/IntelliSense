# VeilWatch — Phase 1 POC

Single-camera detection slice: **Capture → Detection → Confirmation → Alert event**.

This is the Phase 1 deliverable from `VeilWatch_Project_Plan.md` §6: face + plate match
against a test watchlist on a single RTSP stream, end-to-end runnable on a laptop.

MiroFish (Phase 4/5) is **not** wired in yet. Cross-camera re-ID (Phase 2) is not wired in.
The confirmation service emits `alerts.confirmed.v1` events — the dispatch/narrator/officer-app
consumers of that topic are Phase 6+.

## Layout

```
veilwatch/
├── docker-compose.yml        # kafka (KRaft), redis, postgres+postgis, minio
├── .env.example
├── services/
│   ├── capture/              # RTSP → camera.frames.v1
│   ├── detection/            # camera.frames.v1 → detection.events.v1
│   └── confirmation/         # detection.events.v1 → alerts.confirmed.v1 (+ FastAPI)
└── shared/
    └── veilwatch_contracts/  # pydantic event schemas + topic constants
```

## Kafka topic contract (v1)

| Topic | Producer | Consumer | Payload |
|---|---|---|---|
| `camera.frames.v1` | capture | detection | `FrameEvent` — frame_uri + camera metadata |
| `detection.events.v1` | detection | confirmation | `DetectionEvent` — bboxes, classes, watchlist hits |
| `alerts.confirmed.v1` | confirmation | (dispatch/narrator, later) | `ConfirmedAlert` — threat_score, evidence_ref |
| `audit.log.v1` | all | audit sink | `AuditRecord` — every state transition |

Frames are written as JPEG to a shared blob store (local FS volume in dev, MinIO in
staging, S3 WORM in prod). The Kafka event carries a URI, not the bytes.

## Quickstart (dev)

```bash
cp .env.example .env
docker compose up -d kafka redis postgres minio
docker compose up capture detection confirmation
# operator REST:
curl http://localhost:8080/healthz
curl http://localhost:8080/alerts/recent
```

To feed the pipeline without a real camera, point `RTSP_URL` at a file loop:

```bash
# in a separate terminal
docker run --rm -p 8554:8554 -v $PWD/sample.mp4:/sample.mp4 \
  bluenviron/mediamtx:latest
# then set RTSP_URL=rtsp://host.docker.internal:8554/stream
```

## Scoring rules (confirmation)

- Weighted fusion: `watchlist_hit (0.5) + behaviour (0.25) + persistence (0.15) + corroboration (0.10)`
- Auto-confirm threshold: `CONFIRMATION_AUTOCONFIRM_THRESHOLD` (default 0.85)
- Below threshold: queued to `operator_review` table for human gate (Phase 3 full build)
- **Scores are never rounded up on display.** See `veilwatch_contracts.scoring.format_score`.
- Audit record is written **before** any alert is produced. See `confirmation.audit`.

## What's deliberately stubbed

- YOLOv8 weights: loads `yolov8n.pt` by default (ultralytics auto-download). Replace with
  fine-tuned weights per deployment.
- FAISS watchlist: seeded with synthetic 512-d embeddings from `watchlist_seed.json`.
  Real pipeline will plug OSNet/ArcFace embeddings in Phase 2.
- **ALPR:** wired in Phase 1.5 as a parallel per-frame engine.
  - Runs on every vehicle detection produced by YOLO, concurrent with face matching.
  - Backends: `mock` (default, deterministic, no binary required) and `openalpr` (subprocess to the `alpr` CLI; image must vendor the binary).
  - Plate reads travel on `DetectionEvent.plate_reads`; plate-watchlist hits reuse `WatchlistHit(source="plate")` so confirmation scoring needed zero changes.
  - Canonicalisation (`normalise_plate`) is the single source of truth for "same plate?" comparisons — `ABC 123 == abc-123 == ABC123`.
  - Low-confidence reads are dropped at `ALPR_MIN_CONFIDENCE`; multi-frame plate consensus is a Phase 2 item (belongs in the confirmation track store).
- Evidence hashing: SHA-256 on JPEG bytes at capture time; WORM write is Phase 8.
