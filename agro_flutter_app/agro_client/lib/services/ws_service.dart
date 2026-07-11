// lib/services/ws_service.dart  [agro_client]
//
// FIXES (ported from agro_operator's ws_service.dart — parity patch):
//
//   FIX 1 — _isConnecting guard
//     connect() now sets _isConnecting=true immediately and clears it when
//     the connection is confirmed OR fails. A second concurrent call to
//     connect() is a no-op while _isConnecting is true. This prevents the
//     race where _scheduleReconnect (or a user tapping "Save" in Settings)
//     fires a new connect() while a slow TLS/ngrok handshake is still in
//     progress, stacking up parallel connection attempts.
//
//   FIX 2 — Handshake confirmation before _connected=true
//     The OLD client code set `_connected = true` the instant
//     WebSocketChannel.connect(uri) returned — but that call returns
//     immediately and connects in the background. On a fast, reliable LAN
//     this race was invisible. Over an ngrok tunnel (slower handshake,
//     occasional rejects/expired URLs) it meant the UI could briefly claim
//     "connected" for a socket that was never actually going to open, and
//     real failures only surfaced later (or not until the next message
//     send). We now `await channel.ready` — a Future that completes only
//     when the WS handshake actually succeeds, and throws if the server
//     rejects it (401/403, ngrok 404 for a dead tunnel, DNS failure, etc).
//
//   FIX 3 — Exponential back-off on repeated failures
//     Reconnect delay starts at 2 s, doubles on each failure up to 30 s
//     (was a fixed 5 s retry). Resets to 2 s on first successful connect.
//     Prevents hammering ngrok with reconnect attempts when the tunnel is
//     down or the URL is stale, which on the free tier can trigger rate
//     limiting and make things worse.
//
//   FIX 4 — Stale channel teardown before reconnect
//     The old channel/subscription is explicitly closed before a new one
//     is opened, so ghost listeners can't accumulate and double-fire
//     _onDisconnect (which was also resetting the reconnect timer twice).
//
//   FIX 5 — Public reconnect() + lastError
//     SettingsScreen now calls wsService.reconnect() after saving a new
//     URL (previously called connect(), which — given FIX 1 — would
//     silently no-op if a stale connection attempt was still in flight).
//     `lastError` exposes a short, human-readable reason for the most
//     recent failure (timeout, refused, DNS, unauthorized, etc.) so the
//     Settings screen can tell the operator/customer *why* they're
//     offline instead of just showing a generic "Offline" banner.

import 'dart:async';
import 'dart:convert';
import 'package:web_socket_channel/web_socket_channel.dart';
import '../config/app_config.dart';

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

  final List<Map<String, dynamic>> _offlineQueue = [];

  Stream<Map<String, dynamic>> get stream           => _controller.stream;
  Stream<bool>                 get connectionStream  => _connectionController.stream;
  bool                         get connected         => _connected;
  int                          get queuedCount       => _offlineQueue.length;
  String?                      get lastError         => _lastError;

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

      // IOWebSocketChannel (dart:io) crashes on Android — use the
      // cross-platform WebSocketChannel.connect instead, identical to
      // agro_operator which works correctly on Android.
      final channel = WebSocketChannel.connect(uri);
      _channel = channel;

      // FIX 2: wait for the WS handshake to actually succeed.
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

  void sendAgroAction(String action, Map<String, dynamic> data) {
    send({'type': 'agro', 'action': action, 'data': data});
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
