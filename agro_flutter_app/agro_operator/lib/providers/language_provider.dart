// lib/providers/language_provider.dart
import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';

class LanguageProvider extends ChangeNotifier {
  bool _isNepali = true;
  static const _key = 'language_is_nepali';

  bool get isNepali => _isNepali;
  Locale get locale => _isNepali ? const Locale('ne') : const Locale('en');
  String get sttLanguage => _isNepali ? 'ne-NP' : 'en-US';
  String get sttPipeline => _isNepali ? 'faster_whisper_ne' : 'groq_whisper_en';

  /// Inline translation helper — use where ARB keys are overkill.
  String t(String en, String ne) => _isNepali ? ne : en;

  Future<void> load() async {
    final prefs = await SharedPreferences.getInstance();
    _isNepali = prefs.getBool(_key) ?? true;
    notifyListeners();
  }

  Future<void> toggle() async {
    _isNepali = !_isNepali;
    await _persist();
  }

  Future<void> setNepali() async {
    if (_isNepali) return;
    _isNepali = true;
    await _persist();
  }

  Future<void> setEnglish() async {
    if (!_isNepali) return;
    _isNepali = false;
    await _persist();
  }

  Future<void> _persist() async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setBool(_key, _isNepali);
    notifyListeners();
  }
}