// lib/services/voice_service_native.dart
//
// Compiled only on non-web targets (Android, iOS, Windows).
// Uses dart:io and path_provider freely.
//
// Mic permission is NOT handled here anymore — see voice_service.dart's
// requestPermission(), which uses AudioRecorder.hasPermission() from the
// `record` package instead of permission_handler. permission_handler's
// Windows microphone support never reliably worked (there's no real OS
// permission prompt for Win32 desktop apps), which is why the mic button
// silently failed on Windows before this fix.

import 'dart:io';
import 'package:flutter/foundation.dart';
import 'package:path_provider/path_provider.dart';

/// Returns a unique temp file path for the AAC recording.
Future<String> getTempAudioPath() async {
  final dir = await getTemporaryDirectory();
  return '${dir.path}/agro_stt_${DateTime.now().millisecondsSinceEpoch}.m4a';
}

/// Reads the file at [path] into bytes, deletes it, and returns the bytes.
/// Returns null and logs on error.
Future<List<int>?> readAndDeleteFile(String path) async {
  try {
    final file = File(path);
    if (!await file.exists()) {
      debugPrint('VoiceService [native]: audio file not found at $path');
      return null;
    }
    final bytes = await file.readAsBytes();
    try {
      await file.delete();
    } catch (_) {
      // Non-fatal: file is in a temp dir and will be cleaned up by the OS.
    }
    return bytes;
  } catch (e) {
    debugPrint('VoiceService [native]: readAndDeleteFile error: $e');
    return null;
  }
}
