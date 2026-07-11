// lib/providers/settings_provider.dart  [agro_client]
//
// Mirrors agro_operator/lib/providers/settings_provider.dart.
// Manages:
//   • ttsEnabled  — whether the client plays tts_audio confirmations
//   • serverUrl   — exposes the AppConfig URL (read-only display)
import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';
import '../config/app_config.dart';

class SettingsProvider extends ChangeNotifier {
  static const _keyTtsEnabled = 'client_tts_enabled';

  bool _ttsEnabled = true;
  bool get ttsEnabled => _ttsEnabled;

  String get serverUrl => AppConfig.serverUrl;

  Future<void> load() async {
    final prefs = await SharedPreferences.getInstance();
    _ttsEnabled = prefs.getBool(_keyTtsEnabled) ?? true;
  }

  Future<void> setTtsEnabled(bool enabled) async {
    _ttsEnabled = enabled;
    notifyListeners();
    final prefs = await SharedPreferences.getInstance();
    await prefs.setBool(_keyTtsEnabled, enabled);
  }

  Future<void> updateServerUrl(String url) async {
    await AppConfig.save(url);
    notifyListeners();
  }
}
