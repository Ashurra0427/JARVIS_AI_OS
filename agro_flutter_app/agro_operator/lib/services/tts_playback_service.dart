// lib/services/tts_playback_service.dart
//
// Plays the spoken job-registration / job-completion confirmations the
// server sends as:
//   {"type": "tts_audio", "b64": "...", "mime": "audio/mpeg",
//    "duration_s": 4.2, "text": "...", "source": "agro"}
//
// BILINGUAL EXTENSION (Phase 11.1):
//   • Reads LanguageProvider to know the operator's current language (ne/en).
//   • When the language toggle changes, sends a WS message to agro_server.py:
//       {"type": "set_language", "language": "ne"}  or  "en"
//   • The server updates its in-memory language preference so the NEXT TTS
//     clip it synthesises uses the right voice.
//   • Preference is persisted in LanguageProvider (SharedPreferences), so
//     the correct language is pushed again on every reconnect/app restart.
//
// Everything else unchanged from the original:
//   • audioplayers only — no temp files, no native platform setup.
//   • Most-recent clip always wins (stop → play).
//   • ttsEnabled toggle in SettingsProvider silences playback client-side.

import 'dart:async';
import 'dart:convert';

import 'package:audioplayers/audioplayers.dart';
import 'package:flutter/foundation.dart';

import 'ws_service.dart';
import '../providers/settings_provider.dart';
import '../providers/language_provider.dart';

class TtsPlaybackService extends ChangeNotifier {
  final WsService        _ws;
  final SettingsProvider _settings;
  final LanguageProvider _language;
  final AudioPlayer      _player = AudioPlayer();

  StreamSubscription? _wsSub;
  StreamSubscription? _completeSub;

  bool    _isSpeaking = false;
  String? _lastText;

  /// True while a job-confirmation clip is actively playing.
  bool    get isSpeaking => _isSpeaking;

  /// The text bundled by the server alongside the audio — always in sync
  /// with what is coming out of the speaker.
  String? get lastText   => _lastText;

  TtsPlaybackService(this._ws, this._settings, this._language) {
    _wsSub       = _ws.stream.listen(_onMessage);
    _completeSub = _player.onPlayerComplete.listen((_) {
      _isSpeaking = false;
      notifyListeners();
    });

    // When the operator flips the language toggle, tell the server immediately
    // so the *next* TTS clip is synthesised in the new language.
    _language.addListener(_onLanguageChanged);

    // Push the current language on startup so the server is in sync even
    // after an app restart (server defaults to English on cold boot).
    _pushLanguageToServer();
  }

  // ── Inbound audio ────────────────────────────────────────────────────────

  void _onMessage(Map<String, dynamic> msg) {
    if (msg['type'] != 'tts_audio') return;
    if (!_settings.ttsEnabled)       return; // client-side mute — drop silently

    final b64 = msg['b64'] as String?;
    if (b64 == null || b64.isEmpty)  return;

    _lastText = msg['text'] as String?;
    notifyListeners();
    _play(b64, msg['mime'] as String? ?? 'audio/mpeg');
  }

  Future<void> _play(String b64, String mime) async {
    try {
      final bytes = base64Decode(b64);
      // Pre-empt whatever is still playing — the most recently received
      // (text, audio) pair always wins.
      await _player.stop();
      _isSpeaking = true;
      notifyListeners();
      // audioplayers 6.x: BytesSource no longer accepts a mimeType parameter.
      await _player.play(BytesSource(bytes));
    } catch (e) {
      debugPrint('TtsPlaybackService: playback failed: $e');
      _isSpeaking = false;
      notifyListeners();
    }
  }

  // ── Language sync to server ──────────────────────────────────────────────

  void _onLanguageChanged() => _pushLanguageToServer();

  /// Sends {"type": "set_language", "language": "ne"|"en"} to agro_server.py.
  /// The server handler updates its in-memory language and persists it so
  /// subsequent synthesize_speech() calls use the correct voice.
  void _pushLanguageToServer() {
    final lang = _language.isNepali ? 'ne' : 'en';
    try {
      _ws.send({'type': 'set_language', 'language': lang});
      debugPrint('TtsPlaybackService: pushed language=$lang to server');
    } catch (e) {
      // WS may not be connected yet on startup — the server will receive it
      // once the connection is established (WsService queues until connected).
      debugPrint('TtsPlaybackService: could not push language yet: $e');
    }
  }

  // ── Public API ───────────────────────────────────────────────────────────

  /// Stop any currently playing clip (e.g. when the operator presses mic).
  Future<void> stopPlayback() async {
    await _player.stop();
    _isSpeaking = false;
    notifyListeners();
  }

  @override
  void dispose() {
    _language.removeListener(_onLanguageChanged);
    _wsSub?.cancel();
    _completeSub?.cancel();
    _player.dispose();
    super.dispose();
  }
}
