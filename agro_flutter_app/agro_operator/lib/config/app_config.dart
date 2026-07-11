import 'package:shared_preferences/shared_preferences.dart';

// ─────────────────────────────────────────────────────────────────────────────
// AppConfig — tunnel-aware server configuration
//
// Supports three deployment modes, all free:
//
//   MODE 1 · Local LAN  (no tunnel)
//     _defaultServer = 'http://192.168.100.9:7788'
//
//   MODE 2 · ngrok free tunnel
//     Run:  ngrok http 7788
//     URL looks like: https://catchy-wow-extras.ngrok-free.app
//     Free plan rotates the URL on every restart — update _defaultServer
//     or use the Settings screen inside the app.
//
//   MODE 3 · Cloudflare Tunnel (cloudflared) ← NEW
//     Run:  cloudflared tunnel --url http://localhost:7788
//     URL looks like: https://random-words.trycloudflare.com
//     Also free, also rotates on restart, same drill.
//     Advantage over ngrok free: no rate limits, no browser warning page.
//
// HOW TO SWITCH:
//   • Change _defaultServer below and rebuild, OR
//   • Open the app → Settings → Server URL and paste the new tunnel URL.
//     The app picks it up on the next reconnect (≤ 5 s) — no rebuild needed.
//   • Call AppConfig.resetToDefault() from Settings to revert to the
//     value hardcoded below (useful after a tunnel URL rotation).
// ─────────────────────────────────────────────────────────────────────────────

class AppConfig {
  static const String _keyServer = 'server_url';

  // ✅ SET THIS to whichever tunnel URL you are currently running.
  //    ngrok example   → 'https://catchy-wow-extras.ngrok-free.app'
  //    cloudflared     → 'https://random-words.trycloudflare.com'
  //    local LAN       → 'http://192.168.100.9:7788'
  static const String _defaultServer =
      'https://catchy-wow-extras.ngrok-free.dev';

  // Must match AGRO_SECRET in your .env (leave empty string if no auth).
  static const String _jarvisSecret = '8f3c2a7d91b44e1ab6c5f9d7e8a12345';

  static String _serverUrl = _defaultServer;

  /// Current HTTP base URL (e.g. https://xxx.ngrok-free.app)
  static String get serverUrl => _serverUrl;

  /// Derived WebSocket URL — handles http/https → ws/wss automatically.
  /// Works identically for ngrok, Cloudflare Tunnel, and local LAN.
  static String get wsUrl {
    final String baseWs;
    if (_serverUrl.startsWith('https://')) {
      baseWs = _serverUrl.replaceFirst('https://', 'wss://');
    } else {
      baseWs = _serverUrl.replaceFirst('http://', 'ws://');
    }
    // Append token only when one is configured.
    final query = _jarvisSecret.isNotEmpty ? '?token=$_jarvisSecret' : '';
    return '$baseWs/ws$query';
  }

  /// Detect which tunnel provider is in use (for display in Settings UI).
  static TunnelProvider get tunnelProvider {
    if (_serverUrl.contains('ngrok')) return TunnelProvider.ngrok;
    if (_serverUrl.contains('trycloudflare.com') ||
        _serverUrl.contains('cfargotunnel.com') ||
        _serverUrl.contains('cloudflare')) return TunnelProvider.cloudflare;
    if (_serverUrl.startsWith('http://192.') ||
        _serverUrl.startsWith('http://10.') ||
        _serverUrl.startsWith('http://172.') ||
        _serverUrl.startsWith('http://localhost')) return TunnelProvider.local;
    return TunnelProvider.other;
  }

  /// Load the saved URL from SharedPreferences.
  /// Falls back to [_defaultServer] if nothing has been saved yet.
  static Future<void> load() async {
    final prefs = await SharedPreferences.getInstance();
    _serverUrl = prefs.getString(_keyServer) ?? _defaultServer;
  }

  /// Persist a new server URL and update the in-memory value immediately.
  /// Strips trailing slashes so URL concatenation never breaks.
  static Future<void> save(String url) async {
    _serverUrl = url.trimRight().replaceAll(RegExp(r'/$'), '');
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(_keyServer, _serverUrl);
  }

  /// Clears the persisted URL so the next [load()] picks up [_defaultServer].
  /// Call this from the Settings screen after updating the tunnel URL above.
  static Future<void> resetToDefault() async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.remove(_keyServer);
    _serverUrl = _defaultServer;
  }
}

/// Which tunnel (or network mode) the current server URL belongs to.
enum TunnelProvider { ngrok, cloudflare, local, other }
