// lib/services/sync_service.dart
// Drains the local offline queue (LocalDbService.offline_queue, a real
// SQLite table) to the JARVIS server when the WebSocket reconnects.
// Runs automatically — no manual trigger needed.
//
// ── OFFLINE SYNC AUDIT ──────────────────────────────────────────────────
// This file itself was fine in isolation, but an audit of the full offline
// path (ws_service.dart → here → local_db_service.dart) found it was
// draining a table nothing ever wrote to: every actual job-data write
// (log_job, log_expense, log_fuel, update_job, record_payment, …) called
// WsService.sendAgroAction(), which queued to a plain in-memory List
// instead of LocalDbService.enqueue() — so this drain loop always found an
// empty queue and was effectively dead code. A job typed while offline
// lived only in memory and was silently lost if the app was killed before
// reconnecting. See ws_service.dart's header for the full writeup and fix
// (sendAgroAction now persists via LocalDbService.enqueue()).
//
// Two smaller fixes made here, now that this drain loop actually runs:
//   1. Trigger: was driven by spotting 'pong'/'agro_ack' messages arriving
//      on the WS stream — an indirect proxy for "we're connected" that
//      could lag behind the real reconnect. Now listens to
//      WsService.connectionStream directly.
//   2. Removal ordering: previously removed each queued row from the DB
//      immediately after firing the send, regardless of whether the send
//      actually succeeded. If the connection dropped mid-drain, whatever
//      item was "in flight" at that moment could be deleted from the queue
//      without ever having reached the server. Now uses
//      WsService.trySendAgroAction() (returns a success bool) and only
//      removes a row once the socket accepted the write, stopping the pass
//      entirely if a send fails or the connection drops — the remaining
//      rows stay queued for the next reconnect.
//   Caveat that's NOT fully solved (would need a per-action ack from the
//   server, a bigger change): "the socket accepted the write" is not the
//   same as "the server processed it" — a write can still be lost if the
//   connection dies in the split second after the local socket buffers it
//   but before the server actually reads it. This is a real, if narrow,
//   residual gap worth knowing about.

import 'dart:async';
import 'package:flutter/foundation.dart';
import 'local_db_service.dart';
import 'ws_service.dart';

class SyncService {
  final WsService _ws;
  StreamSubscription<bool>? _connSub;
  bool _syncing = false;

  SyncService(this._ws) {
    _connSub = _ws.connectionStream.listen((isConnected) {
      if (isConnected) _drain();
    });
    // Cover the case where we're already connected by the time this is
    // constructed (unlikely given main.dart's ordering, but cheap to guard).
    if (_ws.connected) _drain();
  }

  /// Call this manually if you ever need to force a drain attempt
  /// (e.g. a "retry sync" button in Settings).
  Future<void> drain() => _drain();

  Future<void> _drain() async {
    if (_syncing) return;
    _syncing = true;
    try {
      final pending = await LocalDbService.getPendingQueue();
      if (pending.isEmpty) {
        _syncing = false;
        return;
      }
      debugPrint('[SyncService] draining ${pending.length} queued action(s)');
      var sentCount = 0;
      for (final item in pending) {
        if (!_ws.connected) {
          debugPrint('[SyncService] connection dropped mid-drain — '
              '${pending.length - sentCount} item(s) remain queued for next attempt');
          break;
        }
        final id     = item['id'] as int;
        final action = item['action'] as String;
        final data   = item['data'] as Map<String, dynamic>;

        final sent = _ws.trySendAgroAction(action, data);
        if (!sent) {
          debugPrint('[SyncService] send failed for queued action #$id — stopping this pass, leaving it queued');
          break;
        }
        await LocalDbService.removeFromQueue(id);
        sentCount++;
        // Small delay to avoid flooding the server
        await Future.delayed(const Duration(milliseconds: 150));
      }
      await _ws.refreshPersistedQueueCount();
      debugPrint('[SyncService] drain pass complete — sent $sentCount/${pending.length}');
    } catch (e) {
      debugPrint('[SyncService] drain error: $e');
    } finally {
      _syncing = false;
    }
  }

  void dispose() {
    _connSub?.cancel();
  }
}
