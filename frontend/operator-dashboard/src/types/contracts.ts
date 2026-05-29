/**
 * Mirrors the Pydantic contracts in shared/veilwatch_contracts.
 *
 * We intentionally don't auto-generate these from the OpenAPI schema yet —
 * the contract surface is small and the code reviewer benefits from seeing
 * exactly what fields the dashboard depends on. When the contract grows
 * past ~20 types we'll switch to openapi-typescript.
 */

export type AlertDisposition =
  | 'auto_confirmed'
  | 'operator_review'
  | 'suppressed'
  | 'operator_dismissed';

export type ThreatClass =
  | 'person'
  | 'vehicle'
  | 'weapon'
  | 'package'
  | 'animal'
  | 'unknown';

export interface CameraRef {
  camera_id: string;
  latitude: number;
  longitude: number;
  environment?: string;
  label?: string;
}

export interface BoundingBox {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

export interface Detection {
  detection_id: string;
  class_name: string;
  threat_class: ThreatClass;
  confidence: number;
  bbox: BoundingBox;
  track_id?: string;
  embedding?: number[];
}

export interface WatchlistHit {
  detection_id: string;
  identity_id: string;
  identity_label: string;
  similarity: number;
  source: string;
}

export interface ConfirmedAlert {
  event_id: string;
  incident_id: string;
  camera: CameraRef;
  frame_id: string;
  frame_uri: string;
  frame_sha256: string;
  threat_score: number;
  disposition: AlertDisposition;
  primary_detection: Detection;
  watchlist_hits: WatchlistHit[];
  signal_breakdown: Record<string, number>;
  confirming_operator_id: string | null;
  emitted_at?: string;
}

export interface CrossCameraMatch {
  event_id: string;
  global_track_id: string;
  prior_camera: CameraRef;
  new_camera: CameraRef;
  prior_detection_id: string;
  new_detection_id: string;
  similarity: number;
  elapsed_seconds: number;
  emitted_at?: string;
}

/**
 * Flattened shape returned by GET /alerts/recent. The backend intentionally
 * trims the full ConfirmedAlert here — we only ship fields an operator needs
 * to render a queue row. The WebSocket feed carries the full ConfirmedAlert.
 */
export interface AlertSummary {
  incident_id: string;
  camera_id: string;
  disposition: AlertDisposition;
  threat_score: number;
  threat_score_display: string;
  signal_breakdown: Record<string, number>;
  emitted_at: string;
  frame_uri: string;
  primary_detection: {
    class_name: string;
    threat_class: ThreatClass;
    confidence: number;
  };
  watchlist_hits: Array<{
    identity_id: string;
    identity_label: string;
    similarity: number;
  }>;
}

/**
 * /ws/live-map envelope. Matches the backend's LiveMapBroadcaster.publish()
 * shape exactly.
 */
export type LiveMapEnvelope =
  | { type: 'alert'; data: ConfirmedAlert }
  | { type: 'handoff'; data: CrossCameraMatch };
