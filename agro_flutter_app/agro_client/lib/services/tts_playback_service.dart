// lib/services/tts_playback_service.dart  [agro_client]
//
// Listens for  {"type":"tts_audio", "b64":"...", "mime":"audio/mpeg", "text":"..."}
// messages from the JARVIS server and plays them through the device speaker.
//
// The server sends tts_audio to the CUSTOMER connection when:
//   • Their job request is accepted by the operator
//   • Their job is marked completed
//   • Balance reminder (if operator enables it)
//
// The client does NOT push language preference — the operator controls
// the TTS language server-side via the agro_operator app.
//
// Phase 12: accepts [initialEnabled] from main.dart so the persisted
// preference (stored in SettingsProvider / SharedPreferences) survives
// app restarts.  setEnabled() now notifies SettingsProvider via the
// callback so the preference is written back to disk immediately.
import 'dart:async';
import 'dart:convert';
import 'package:audioplayers/audioplayers.dart';
import 'package:flutter/foundation.dart';
import 'ws_service.dart';

class TtsPlaybackService extends ChangeNotifier {
  final WsService _ws;
  /// Optional callback invoked whenever enabled state changes,
  /// so SettingsProvider can persist the new value to SharedPreferences.
  final void Function(bool)? onEnabledChanged;

  final AudioPlayer _player = AudioPlayer();
  StreamSubscription? _wsSub;
  StreamSubscription? _completeSub;

  bool    _isSpeaking = false;
  String? _lastText;
  bool    _enabled;

  bool    get isSpeaking => _isSpeaking;
  String? get lastText   => _lastText;
  bool    get enabled    => _enabled;

  TtsPlaybackService(
    this._ws, {
    bool initialEnabled = true,
    this.onEnabledChanged,
  }) : _enabled = initialEnabled {
    _wsSub = _ws.stream.listen(_onMessage);
    _completeSub = _player.onPlayerComplete.listen((_) {
      _isSpeaking = false;
      notifyListeners();
    });
  }

  void setEnabled(bool v) {
    _enabled = v;
    if (!v) stopPlayback();
    onEnabledChanged?.call(v); // persist via SettingsProvider
    notifyListeners();
  }

  void _onMessage(Map<String, dynamic> msg) {
    if (msg['type'] != 'tts_audio') return;
    if (!_enabled) return;
    final b64 = msg['b64'] as String?;
    if (b64 == null || b64.isEmpty) return;
    _lastText = msg['text'] as String?;
    notifyListeners();
    _play(b64, msg['mime'] as String? ?? 'audio/mpeg');
  }

  Future<void> _play(String b64, String mime) async {
    try {
      final bytes = base64Decode(b64);
      await _player.stop();
      _isSpeaking = true;
      notifyListeners();
      // audioplayers 6.x: BytesSource no longer accepts a mimeType parameter.
      await _player.play(BytesSource(bytes));
    } catch (e) {
      debugPrint('TtsPlaybackService[client]: $e');
      _isSpeaking = false;
      notifyListeners();
    }
  }

  Future<void> stopPlayback() async {
    await _player.stop();
    _isSpeaking = false;
    notifyListeners();
  }

  @override
  void dispose() {
    _wsSub?.cancel();
    _completeSub?.cancel();
    _player.dispose();
    super.dispose();
  }
}