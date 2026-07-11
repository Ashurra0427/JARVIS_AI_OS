// lib/main.dart  [agro_client]
import 'dart:async' show unawaited;
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import 'app.dart';
import 'config/app_config.dart';
import 'providers/language_provider.dart';
import 'providers/customer_auth_provider.dart';
import 'providers/settings_provider.dart';
import 'services/ws_service.dart';
import 'services/tts_playback_service.dart';

void main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await AppConfig.load();

  final languageProvider = LanguageProvider();
  await languageProvider.load();

  // Load persisted settings (ttsEnabled, serverUrl) before anything else.
  final settingsProvider = SettingsProvider();
  await settingsProvider.load();

  final wsService = WsService();
  unawaited(wsService.connect()); // fire-and-forget — WsService has auto-reconnect

  final authProvider = CustomerAuthProvider(wsService);
  await authProvider.restoreSession();

  // TTS — plays server tts_audio messages (job accepted / completed confirmations).
  // • initialEnabled: restores the user's persisted preference across app restarts.
  // • onEnabledChanged: writes the new value back to SharedPreferences via
  //   SettingsProvider whenever the toggle changes in Settings screen.
  final ttsService = TtsPlaybackService(
    wsService,
    initialEnabled: settingsProvider.ttsEnabled,
    onEnabledChanged: settingsProvider.setTtsEnabled,
  );

  runApp(
    MultiProvider(
      providers: [
        ChangeNotifierProvider<LanguageProvider>.value(value: languageProvider),
        ChangeNotifierProvider<SettingsProvider>.value(value: settingsProvider),
        Provider<WsService>.value(value: wsService),
        ChangeNotifierProvider<CustomerAuthProvider>.value(value: authProvider),
        ChangeNotifierProvider<TtsPlaybackService>.value(value: ttsService),
      ],
      child: const AgroClientApp(),
    ),
  );
}