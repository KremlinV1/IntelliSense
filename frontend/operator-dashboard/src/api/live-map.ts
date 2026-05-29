import type { LiveMapEnvelope } from '../types/contracts';

/**
 * Self-healing WebSocket client for /ws/live-map.
 *
 * Policy:
 *   - Reconnect with exponential backoff capped at 15s.
 *   - Surface connection state to callers so the UI can show a status chip.
 *   - Drop malformed payloads silently but log them — a corrupt frame must
 *     not take the socket down.
 *
 * Non-goals:
 *   - Replaying history. If the socket drops, the caller should hit
 *     fetchRecentAlerts() to catch up. That's what REST is for.
 */

export type ConnectionState = 'connecting' | 'open' | 'closed' | 'error';

export interface LiveMapClient {
  close: () => void;
  state: () => ConnectionState;
}

export interface LiveMapHandlers {
  onMessage: (envelope: LiveMapEnvelope) => void;
  onStateChange?: (state: ConnectionState) => void;
}

const URL_PATH = '/ws/live-map';

export function connectLiveMap(handlers: LiveMapHandlers): LiveMapClient {
  let socket: WebSocket | null = null;
  let state: ConnectionState = 'connecting';
  let backoffMs = 500;
  let closed = false;
  let reconnectTimer: number | null = null;

  const setState = (next: ConnectionState) => {
    state = next;
    handlers.onStateChange?.(next);
  };

  const open = () => {
    if (closed) return;
    // Vite proxies /ws → ws://localhost:8080/ws, so the scheme upgrade
    // happens transparently in dev. In prod the same-origin URL just works.
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = `${proto}//${window.location.host}${URL_PATH}`;
    setState('connecting');
    socket = new WebSocket(url);

    socket.onopen = () => {
      backoffMs = 500; // reset on successful connection
      setState('open');
    };

    socket.onmessage = (ev) => {
      try {
        const parsed = JSON.parse(ev.data) as LiveMapEnvelope;
        if (parsed.type !== 'alert' && parsed.type !== 'handoff') {
          console.warn('[live-map] unknown envelope type', parsed);
          return;
        }
        handlers.onMessage(parsed);
      } catch (err) {
        console.warn('[live-map] failed to parse frame', err);
      }
    };

    socket.onerror = () => setState('error');

    socket.onclose = () => {
      if (closed) return;
      setState('closed');
      reconnectTimer = window.setTimeout(open, backoffMs);
      backoffMs = Math.min(backoffMs * 2, 15_000);
    };
  };

  open();

  return {
    close() {
      closed = true;
      if (reconnectTimer != null) window.clearTimeout(reconnectTimer);
      socket?.close();
    },
    state: () => state,
  };
}
