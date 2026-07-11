// lib/providers/settings_provider.dart
import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';
import '../config/app_config.dart';

class SettingsProvider extends ChangeNotifier {
  static const _keyTtsEnabled = 'tts_enabled';

  String _serverUrl = AppConfig.serverUrl;
  String get serverUrl => _serverUrl;

  // Voice-confirmation toggle (job registered / job completed playback).
  // Defaults on; persisted locally only -- this is independent of the
  // server's own tts_enabled flag, which controls the web HUD separately.
  bool _ttsEnabled = true;
  bool get ttsEnabled => _ttsEnabled;

  /// Load persisted settings. Call once at startup before this provider is
  /// handed to the widget tree (mirrors AppConfig.load() / LanguageProvider.load()).
  Future<void> load() async {
    final prefs = await SharedPreferences.getInstance();
    _ttsEnabled = prefs.getBool(_keyTtsEnabled) ?? true;
  }

  Future<void> updateServerUrl(String url) async {
    await AppConfig.save(url);
    _serverUrl = AppConfig.serverUrl;
    notifyListeners();
  }

  Future<void> setTtsEnabled(bool enabled) async {
    _ttsEnabled = enabled;
    notifyListeners();
    final prefs = await SharedPreferences.getInstance();
    await prefs.setBool(_keyTtsEnabled, enabled);
  }
}
