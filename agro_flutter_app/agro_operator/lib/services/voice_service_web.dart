// lib/services/voice_service_web.dart
//
// Compiled only on the web target (dart.library.html is true).
// Uses the browser MediaRecorder API via dart:html.
//
// Note: dart:html is deprecated in favor of package:web + dart:js_interop,
// and isn't supported when compiling to WebAssembly. It's still fully
// functional for the default JS (dart2js) compile target used by a plain
// `flutter run -d chrome` / `-d edge`, which is what this app uses. If you
// later need a Wasm build, migrate this file to package:web.
//
// Flow:
//   startWebRecording()  → getUserMedia() → MediaRecorder.start()
//   stopWebRecordingAndEncode() → MediaRecorder.stop() → collect Blob chunks
//                              → FileReader.readAsDataURL → extract base64
//   cancelWebRecording() → MediaRecorder.stop() without encoding

// ignore: avoid_web_libraries_in_flutter
import 'dart:html' as html;
import 'dart:async';
import 'dart:convert';
import 'package:flutter/foundation.dart';

html.MediaRecorder? _mediaRecorder;
html.MediaStream? _mediaStream;
final List<html.Blob> _chunks = [];
Completer<String?>? _encodeCompleter;

/// Requests mic access and starts MediaRecorder.
/// Returns true on success, false if permission denied or API unavailable.
Future<bool> startWebRecording() async {
  try {
    _chunks.clear();
    _encodeCompleter = null;

    // Request microphone — browser shows its own permission prompt.
    _mediaStream = await html.window.navigator.getUserMedia(audio: true);

    // Pick the best supported MIME type.
    // Safari (iOS/macOS) supports audio/mp4; Chrome prefers audio/webm;opus.
    final mime = _bestMime();
    final options = mime != null ? {'mimeType': mime} : <String, dynamic>{};

    _mediaRecorder = html.MediaRecorder(_mediaStream!, options);
    _mediaRecorder!.addEventListener('dataavailable', (event) {
      final e = event as html.BlobEvent;
      if (e.data != null && e.data!.size > 0) {
        _chunks.add(e.data!);
      }
    });

    _mediaRecorder!.start();
    debugPrint('VoiceService [web]: recording started (mime=$mime)');
    return true;
  } catch (e) {
    debugPrint('VoiceService [web]: startWebRecording error: $e');
    return false;
  }
}

/// Stops MediaRecorder, waits for all chunks, and returns base64-encoded audio.
/// Returns null on error.
Future<String?> stopWebRecordingAndEncode() async {
  if (_mediaRecorder == null) return null;

  _encodeCompleter = Completer<String?>();

  _mediaRecorder!.addEventListener('stop', (_) async {
    try {
      if (_chunks.isEmpty) {
        _encodeCompleter?.complete(null);
        return;
      }
      final blob = html.Blob(_chunks);
      final base64 = await _blobToBase64(blob);
      _encodeCompleter?.complete(base64);
    } catch (e) {
      debugPrint('VoiceService [web]: encode error: $e');
      _encodeCompleter?.complete(null);
    } finally {
      _stopTracks();
    }
  });

  _mediaRecorder!.stop();
  return _encodeCompleter!.future;
}

/// Stops MediaRecorder without encoding (user cancelled).
void cancelWebRecording() {
  _mediaRecorder?.stop();
  _stopTracks();
  _chunks.clear();
  _encodeCompleter?.complete(null);
  _encodeCompleter = null;
  debugPrint('VoiceService [web]: recording cancelled');
}

// ── Helpers ──────────────────────────────────────────────────────────────────

void _stopTracks() {
  _mediaStream?.getTracks().forEach((t) => t.stop());
  _mediaStream = null;
  _mediaRecorder = null;
}

/// Converts a Blob to a raw base64 string (without the data-URL prefix).
Future<String> _blobToBase64(html.Blob blob) {
  final completer = Completer<String>();
  final reader = html.FileReader();
  reader.onLoadEnd.listen((_) {
    final dataUrl = reader.result as String;
    // dataUrl looks like: "data:audio/webm;base64,AAAA..."
    final comma = dataUrl.indexOf(',');
    final b64 = comma >= 0 ? dataUrl.substring(comma + 1) : dataUrl;
    completer.complete(b64);
  });
  reader.onError.listen((e) => completer.completeError(e));
  reader.readAsDataUrl(blob);
  return completer.future;
}

/// Returns the best supported MIME type for this browser/device, or null
/// to let the browser pick its default.
String? _bestMime() {
  // Prefer opus in WebM (best quality, Chrome/Firefox/Edge)
  const candidates = [
    'audio/webm;codecs=opus',
    'audio/webm',
    'audio/mp4',   // Safari iOS 15.4+
    'audio/ogg;codecs=opus',
  ];
  for (final m in candidates) {
    if (html.MediaRecorder.isTypeSupported(m)) return m;
  }
  return null;
}
