// lib/services/ws_service.dart  [agro_operator]
//
// FIXES (reconnect-loop patch):
//
//   FIX 1 — _isConnecting guard
//     connect() now sets _isConnecting=true immediately and clears it when the
//     connection is confirmed OR fails. A second concurrent call to connect()
//     is a no-op while _isConnecting is true. This prevents the race where
//     _scheduleReconnect fires a new connect() while a slow TLS/ngrok handshake
//     is still in progress, stacking up parallel connection attempts.
//
//   FIX 2 — Handshake confirmation before _connected=true
//     _connected is only set true after the stream emits at least one message
//     OR after the channel.ready Future resolves (available in web_socket_channel
//     ≥2.4). For older SDK compat we use a first-message OR a 10 s timeout
//     approach: we wait for the stream's first event before declaring connected.
//     Actually: we use channel.ready (Future that completes when the WS
//     handshake succeeds). This is the correct fix — it's what .ready is for.
//
//   FIX 3 — Exponential back-off on repeated failures
//     Reconnect delay starts at 2 s, doubles on each failure up to 30 s.
//     Resets to 2 s on first successful connection. This prevents flooding
//     ngrok with connection attempts (ngrok rate-limits repeated bad requests).
//
//   FIX 4 — Stale channel teardown before reconnect
//     Old channel is explicitly closed before a new one is opened, preventing
//     ghost listeners from accumulating and double-firing _onDisconnect.
//
//   FIX 5 — reconnect() public method
//     SettingsProvider can call wsService.reconnect() after saving a new URL,
//     eliminating the "Restart app to reconnect" UX problem.

import 'dart:async';
import 'dart:convert';
import 'package:web_socket_channel/web_socket_channel.dart';
import '../config/app_config.dart';
import 'local_db_service.dart';

// ── OFFLINE SYNC AUDIT (fixed) ─────────────────────────────────────────────
// What was here before: every write (log_job, log_expense, log_fuel,
// update_job, record_payment, …) went through sendAgroAction(), which — when
// offline — pushed the message onto `_offlineQueue`, a plain in-memory List.
// Meanwhile a SEPARATE, persisted queue existed in LocalDbService
// (offline_queue table) and SyncService was already wired up to drain it on
// reconnect. Nothing ever wrote to that persisted queue, though — enqueue()
// was never called from anywhere in the app. So SyncService's drain loop was
// dead code (always draining an empty table), and the queue that actually
// held a job typed offline was the in-memory List, which:
//   1. vanished completely if the app was killed/crashed/swiped away before
//      reconnecting — a job logged offline with no further action from the
//      operator was silently lost, no error shown.
//   2. flushed with no delivery confirmation and no re-queue on failure — if
//      `.sink.add()` threw partway through the flush loop, everything after
//      that point was dropped too.
// Fix: sendAgroAction() now persists to LocalDbService.enqueue() (real
// SQLite row, survives app kill) instead of the in-memory list whenever it
// can't send immediately, and SyncService (unchanged file, see its header)
// is now the only thing that drains it.

class WsService {
  WebSocketChannel? _channel;
  StreamSubscription? _channelSub;

  final _controller           = StreamController<Map<String, dynamic>>.broadcast();
  final _connectionController = StreamController<bool>.broadcast();

  bool   _connected    = false;
  bool   _isConnecting = false;
  bool   _disposed     = false;

  // Back-off state
  int    _failCount        = 0;
  static const _minDelay   = 2;   // seconds
  static const _maxDelay   = 30;  // seconds

  Timer? _pingTimer;
  Timer? _reconnectTimer;

  // Most recent connection failure, in plain language. Null once connected.
  String? _lastError;

  // Only for ephemeral, non-critical messages (ping, set_language, voice
  // clips) — losing one of these on disconnect is harmless, unlike job data,
  // so it's fine for this to stay in-memory and not survive an app kill.
  final List<Map<String, dynamic>> _offlineQueue = [];

  // Mirrors LocalDbService's persisted offline_queue row count, so the
  // "N action(s) queued" banner is accurate even right after an app restart
  // (see _refreshPersistedQueueCount, called from the constructor).
  int _persistedQueueCount = 0;

  WsService() {
    _refreshPersistedQueueCount();
  }

  Stream<Map<String, dynamic>> get stream           => _controller.stream;
  Stream<bool>                 get connectionStream  => _connectionController.stream;
  bool                         get connected         => _connected;
  int                          get queuedCount       => _offlineQueue.length + _persistedQueueCount;
  String?                      get lastError         => _lastError;

  Future<void> _refreshPersistedQueueCount() async {
    try {
      _persistedQueueCount = await LocalDbService.queueCount();
    } catch (_) {
      // LocalDbService not ready yet / platform channel not up — leave at
      // last known value, this is only for the banner's number, not correctness.
    }
  }

  /// Public so SyncService can ask us to re-check the count after a drain
  /// pass (queue count only actually matters while offline / banner visible,
  /// but keeping it accurate costs nothing).
  Future<void> refreshPersistedQueueCount() => _refreshPersistedQueueCount();

  // ── connect ────────────────────────────────────────────────────────────────
  Future<void> connect() async {
    if (_disposed)      return;
    if (_isConnecting)  return;   // FIX 1: guard against concurrent calls

    _reconnectTimer?.cancel();
    _isConnecting = true;

    // FIX 4: tear down stale channel before opening a new one
    await _teardown();

    try {
      await AppConfig.load();
      final uri = Uri.parse(AppConfig.wsUrl);

      final channel = WebSocketChannel.connect(uri);
      _channel = channel;

      // FIX 2: wait for the WS handshake to actually succeed.
      // channel.ready is a Future<void> that completes when the HTTP upgrade
      // is confirmed. It throws a WebSocketChannelException if the server
      // rejects the connection (e.g. 401, 403, ngrok 404, etc.).
      await channel.ready;

      // Handshake confirmed — we are now truly connected.
      _connected    = true;
      _lastError    = null;
      _failCount    = 0;           // FIX 3: reset back-off on success
      _isConnecting = false;
      _connectionController.add(true);

      _channelSub = channel.stream.listen(
        (data) {
          try {
            final msg = jsonDecode(data as String) as Map<String, dynamic>;
            _controller.add(msg);
          } catch (_) {}
        },
        onDone:  _onDisconnect,
        onError: (_) => _onDisconnect(),
        cancelOnError: false,
      );

      // Flush offline queue
      if (_offlineQueue.isNotEmpty) {
        for (final msg in List<Map<String, dynamic>>.from(_offlineQueue)) {
          _sendRaw(msg);
        }
        _offlineQueue.clear();
      }

      // Keepalive ping every 25 s (ngrok idles out at 30 s)
      _pingTimer?.cancel();
      _pingTimer = Timer.periodic(
        const Duration(seconds: 25),
        (_) => _sendRaw({'type': 'ping'}),
      );

    } catch (e) {
      // channel.ready threw — handshake failed (bad token, ngrok 40x, DNS, etc.)
      _isConnecting = false;
      _connected    = false;
      _lastError    = _describeError(e);
      _failCount++;
      _connectionController.add(false);  // refresh UI with the new lastError
      _scheduleReconnect();
    }
  }

  // ── Public: force a fresh reconnect (e.g. after URL change in Settings) ───
  Future<void> reconnect() async {
    _failCount = 0;  // reset back-off so reconnect is immediate
    await _teardown();
    await connect();
  }

  // ── internals ──────────────────────────────────────────────────────────────

  void _onDisconnect() {
    if (_disposed) return;
    _connected    = false;
    _isConnecting = false;
    _lastError  ??= 'Connection dropped';
    _connectionController.add(false);
    _pingTimer?.cancel();
    _failCount++;
    _scheduleReconnect();
  }

  void _scheduleReconnect() {
    if (_disposed) return;
    _reconnectTimer?.cancel();
    // FIX 3: exponential back-off — 2 s, 4 s, 8 s … capped at 30 s
    final delay = (_minDelay * (1 << _failCount.clamp(0, 4))).clamp(_minDelay, _maxDelay);
    _reconnectTimer = Timer(Duration(seconds: delay), () => connect());
  }

  /// Cleanly close the current channel and cancel its subscription.
  Future<void> _teardown() async {
    _pingTimer?.cancel();
    await _channelSub?.cancel();
    _channelSub = null;
    try {
      await _channel?.sink.close();
    } catch (_) {}
    _channel = null;
    _connected = false;
  }

  /// Turn a raw exception into a short, human-readable reason. Helps the
  /// Settings screen explain *why* a connection failed instead of just
  /// showing "Offline" — important for diagnosing tunnel/network issues.
  String _describeError(Object e) {
    final s = e.toString();
    if (s.contains('SocketException')) {
      if (s.contains('Failed host lookup')) return 'Could not resolve host — check the URL / your internet connection';
      if (s.contains('Connection refused')) return 'Connection refused — is the server running?';
      return 'Network error — check the URL and your connection';
    }
    if (s.contains('TimeoutException') || s.contains('timed out')) return 'Connection timed out — server unreachable';
    if (s.contains('403') || s.contains('401') || s.contains('Unauthorized')) return 'Unauthorized — token mismatch with server';
    if (s.contains('404')) return 'Server reachable but /ws not found — check the URL/port';
    if (s.contains('HandshakeException') || s.contains('CERTIFICATE')) return 'TLS/certificate error — tunnel URL may be invalid or expired';
    if (s.contains('FormatException')) return 'Invalid server URL';
    return 'Connection failed — ${s.length > 80 ? s.substring(0, 80) : s}';
  }

  void _sendRaw(Map<String, dynamic> msg) {
    if (_connected && _channel != null) {
      try {
        _channel!.sink.add(jsonEncode(msg));
      } catch (_) {
        _connected = false;
      }
    }
  }

  void send(Map<String, dynamic> msg) {
    if (_connected) {
      _sendRaw(msg);
    } else {
      _offlineQueue.add(msg);
    }
  }

  /// Used by every job-data write (log_job, log_expense, log_fuel,
  /// update_job, record_payment, …). Sends immediately if connected;
  /// otherwise persists to LocalDbService's offline_queue table so the
  /// action survives an app restart and gets drained by SyncService on
  /// reconnect. Also falls back to persisting if the socket write itself
  /// throws (e.g. connection dropped in the instant between the `connected`
  /// check and the actual write) — previously that case just silently lost
  /// the action instead of queueing it.
  Future<void> sendAgroAction(String action, Map<String, dynamic> data) async {
    if (_connected && _channel != null) {
      try {
        _channel!.sink.add(jsonEncode({'type': 'agro', 'action': action, 'data': data}));
        return;
      } catch (_) {
        _connected = false;
        // fall through — persist below instead of dropping it
      }
    }
    await LocalDbService.enqueue(action, data);
    _persistedQueueCount++;
  }

  /// Low-level, synchronous send used only by SyncService while draining the
  /// persisted queue. Unlike sendAgroAction(), this does NOT re-queue on
  /// failure — the caller already owns that queue row and decides whether to
  /// remove it or leave it for the next drain pass. Returns true only if the
  /// socket accepted the write; note this is NOT a delivery guarantee (no
  /// per-action server ack exists), just the same "as good as it gets
  /// without an ack protocol" guarantee the old code silently assumed.
  bool trySendAgroAction(String action, Map<String, dynamic> data) {
    if (!_connected || _channel == null) return false;
    try {
      _channel!.sink.add(jsonEncode({'type': 'agro', 'action': action, 'data': data}));
      return true;
    } catch (_) {
      _connected = false;
      return false;
    }
  }

  void dispose() {
    _disposed = true;
    _pingTimer?.cancel();
    _reconnectTimer?.cancel();
    _channelSub?.cancel();
    _channel?.sink.close();
    _controller.close();
    _connectionController.close();
  }
}
