// lib/main.dart
// ─────────────────────────────────────────────────────────────────────────────
// Providers wired (Phase 12 timer upgrade adds JobTimerService):
//   WsService            — WebSocket connection to JARVIS AGRO server
//   VoiceService         — mic → STT
//   SyncService          — drains offline queue on reconnect
//   AuthService          — PIN lock
//   LanguageProvider     — Nepali/English toggle
//   SettingsProvider     — server URL + TTS toggle
//   TtsPlaybackService   — bilingual voice confirmations
//   JobProvider          — job state + WS message handler
//   JobTimerService      — per-minute live timer (agriculture only)
//                          Restored from SharedPreferences on startup so
//                          timers survive app restarts and lock screens.
// ─────────────────────────────────────────────────────────────────────────────

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import 'config/app_config.dart';
import 'providers/job_provider.dart';
import 'providers/language_provider.dart';
import 'providers/settings_provider.dart';
import 'services/ws_service.dart';
import 'services/voice_service.dart';
import 'services/sync_service.dart';
import 'services/auth_service.dart';
import 'services/tts_playback_service.dart';
import 'services/job_timer_service.dart';
import 'app.dart';

void main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await AppConfig.load();

  final languageProvider = LanguageProvider();
  await languageProvider.load();

  final authService = AuthService();
  await authService.load();

  final settingsProvider = SettingsProvider();
  await settingsProvider.load();

  final wsService = WsService();
  wsService.connect();

  // ── Global error surfacing ──────────────────────────────────────────────
  // Several server actions can reply with {"type": "error", "message": ...}
  // (e.g. the mic button's STT pipeline failing, or being unavailable
  // because the server has no STT engine configured). Nothing in the app
  // was listening for that message type at all — MicButton only listens
  // for "stt_result" — so on failure the mic would just go quiet with zero
  // feedback: no crash, no snackbar, nothing, making it impossible to tell
  // "broken" from "used wrong". This is a catch-all fallback; screens that
  // already handle their own action-specific errors are unaffected.
  wsService.stream.listen((msg) {
    if (msg['type'] == 'error') {
      final message = (msg['message'] as String?) ?? 'Something went wrong.';
      AgroApp.scaffoldMessengerKey.currentState?.showSnackBar(
        SnackBar(content: Text(message), backgroundColor: Colors.red),
      );
    }
  });

  final voiceService = VoiceService(wsService);
  final syncService  = SyncService(wsService);

  final ttsPlaybackService = TtsPlaybackService(
    wsService,
    settingsProvider,
    languageProvider,
  );

  // Restore any timers that were running when the app was killed.
  final jobTimerService = JobTimerService();
  await jobTimerService.restore();

  runApp(
    MultiProvider(
      providers: [
        Provider<WsService>.value(value: wsService),
        Provider<VoiceService>.value(value: voiceService),
        Provider<SyncService>.value(value: syncService),

        ChangeNotifierProvider<AuthService>.value(value: authService),
        ChangeNotifierProvider<LanguageProvider>.value(value: languageProvider),
        ChangeNotifierProvider<SettingsProvider>.value(value: settingsProvider),
        ChangeNotifierProvider<TtsPlaybackService>.value(value: ttsPlaybackService),
        ChangeNotifierProvider<JobTimerService>.value(value: jobTimerService),
        ChangeNotifierProvider(create: (_) => JobProvider(wsService)),
      ],
      child: const AgroApp(),
    ),
  );
}
