// lib/services/voice_service.dart
//
// Platform-adaptive STT recording service.
//
// ┌─────────────┬──────────────────────────────────────────────────────────────┐
// │ Platform    │ Strategy                                                     │
// ├─────────────┼──────────────────────────────────────────────────────────────┤
// │ Android     │ record pkg → AAC file → base64 → WS stt_audio              │
// │ iOS         │ record pkg → AAC file → base64 → WS stt_audio              │
// │ Windows     │ record pkg → AAC file → base64 → WS stt_audio              │
// │             │ permission via record's own hasPermission() (see below)    │
// │ Web / PWA   │ Web MediaRecorder API via dart:html → base64 → WS          │
// │ (iOS PWA)   │ Same Web path — no native plugins needed                    │
// └─────────────┴──────────────────────────────────────────────────────────────┘
//
// Key design decisions:
//   • kIsWeb guard separates the two code paths at runtime.
//   • Mic permission is requested via AudioRecorder.hasPermission() (from the
//     `record` package itself) instead of permission_handler. permission_handler's
//     Windows microphone support is long-standing broken/unreliable (Win32 desktop
//     apps don't have an OS permission-prompt API the way mobile does), which is
//     why the mic button previously failed silently on Windows. `record` handles
//     the correct per-platform behaviour internally: it triggers the native
//     prompt on Android/iOS, and on Windows it just confirms a capture device
//     is available (no prompt needed/possible for a plain Win32 app).
//   • Web uses window.navigator.mediaDevices.getUserMedia via dart:html, then
//     MediaRecorder, collecting Blob chunks and converting to base64. This
//     avoids any native plugin on web and works in Chrome, Edge, Safari
//     (iOS 15.4+), and Android Chrome.
//   • dart:io is only imported inside a conditional import so the web build
//     never sees it.

import 'dart:async';
import 'dart:convert';

import 'package:flutter/foundation.dart'; // kIsWeb, debugPrint
import 'package:record/record.dart';

import 'ws_service.dart';

// Web-only imports — conditional so dart2js never sees dart:io.
import 'voice_service_stub.dart'
    if (dart.library.html) 'voice_service_web.dart' as web_impl;

// Non-web imports — conditional so web build never sees dart:io / path_provider.
import 'voice_service_native.dart'
    if (dart.library.html) 'voice_service_stub.dart' as native_impl;

class VoiceService {
  final WsService wsService;
  VoiceService(this.wsService);

  // ── Native (Android / iOS / Windows) ─────────────────────────────────────
  final AudioRecorder _recorder = AudioRecorder();
  bool _isRecording = false;

  bool get isRecording => _isRecording;

  // ── Permission ────────────────────────────────────────────────────────────

  Future<bool> requestPermission() async {
    if (kIsWeb) {
      // On web, the browser's getUserMedia call itself triggers the
      // native browser permission prompt. Nothing to do here.
      return true;
    }
    // Use the `record` package's own permission check rather than
    // permission_handler. permission_handler's Windows microphone support
    // is unreliable (no real OS prompt exists for Win32 desktop apps), which
    // is why the mic button previously did nothing on Windows. `record`
    // does the right thing per-platform: triggers the OS prompt on
    // Android/iOS, and on Windows/macOS/Linux just verifies a capture
    // device is available.
    try {
      return await _recorder.hasPermission();
    } catch (e) {
      debugPrint('VoiceService: hasPermission error: $e');
      return false;
    }
  }

  // ── Start recording ───────────────────────────────────────────────────────

  Future<bool> startRecording() async {
    if (_isRecording) return false;

    if (kIsWeb) {
      final started = await web_impl.startWebRecording();
      if (started) _isRecording = true;
      return started;
    }

    // Native path ─────────────────────────────────────────────────────────
    final granted = await requestPermission();
    if (!granted) {
      debugPrint('VoiceService: mic permission denied');
      return false;
    }

    try {
      final path = await native_impl.getTempAudioPath();
      await _recorder.start(
        const RecordConfig(
          encoder: AudioEncoder.aacLc,
          sampleRate: 16000,
          numChannels: 1,
          bitRate: 64000,
        ),
        path: path,
      );
      _isRecording = true;
      debugPrint('VoiceService: recording started → $path');
      return true;
    } catch (e) {
      debugPrint('VoiceService: startRecording error: $e');
      return false;
    }
  }

  // ── Stop recording + send ─────────────────────────────────────────────────

  Future<void> stopAndSend({required String sttPipeline}) async {
    if (!_isRecording) return;
    _isRecording = false;

    if (kIsWeb) {
      // Web: stop MediaRecorder, collect chunks, encode, send.
      final base64Audio = await web_impl.stopWebRecordingAndEncode();
      if (base64Audio == null) {
        debugPrint('VoiceService [web]: no audio data returned');
        return;
      }
      _sendToServer(base64Audio, 'audio/webm', sttPipeline);
      return;
    }

    // Native path ─────────────────────────────────────────────────────────
    try {
      final path = await _recorder.stop();
      if (path == null) {
        debugPrint('VoiceService: recording stopped but no file path');
        return;
      }
      final bytes = await native_impl.readAndDeleteFile(path);
      if (bytes == null) return;
      final base64Audio = base64Encode(bytes);
      _sendToServer(base64Audio, 'audio/mp4', sttPipeline);
    } catch (e) {
      debugPrint('VoiceService: stopAndSend error: $e');
    }
  }

  void _sendToServer(String base64Audio, String mime, String sttPipeline) {
    wsService.send({
      'type': 'stt_audio',
      'audio': base64Audio,
      'mime': mime,
      'stt_pipeline': sttPipeline,
    });
    debugPrint('VoiceService: sent audio, pipeline=$sttPipeline');
  }

  /// Cancel recording without sending.
  Future<void> cancel() async {
    if (!_isRecording) return;
    _isRecording = false;

    if (kIsWeb) {
      web_impl.cancelWebRecording();
      return;
    }
    try {
      final path = await _recorder.stop();
      if (path != null) {
        await native_impl.readAndDeleteFile(path); // clean up, ignore bytes
      }
    } catch (e) {
      debugPrint('VoiceService: cancel error: $e');
    }
    debugPrint('VoiceService: recording cancelled');
  }

  void dispose() {
    if (!kIsWeb) _recorder.dispose();
  }
}