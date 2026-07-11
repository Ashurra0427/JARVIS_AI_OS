// lib/services/voice_service_stub.dart
//
// No-op fallback for the conditional imports in voice_service.dart.
//
// voice_service.dart imports BOTH voice_service_native.dart and
// voice_service_web.dart conditionally, using THIS file as the
// "opposite platform" placeholder for each:
//
//   import 'voice_service_stub.dart'
//       if (dart.library.html) 'voice_service_web.dart' as web_impl;
//   import 'voice_service_native.dart'
//       if (dart.library.html) 'voice_service_stub.dart' as native_impl;
//
// On native (Android/iOS/Windows): dart.library.html is false, so
//   - web_impl    → resolves to THIS file (needs the web_impl.* functions)
//   - native_impl → resolves to voice_service_native.dart (the real one)
//
// On Web: dart.library.html is true, so
//   - web_impl    → resolves to voice_service_web.dart (the real one)
//   - native_impl → resolves to THIS file (needs the native_impl.* functions)
//
// This file therefore has to provide BOTH sets of symbols. Whichever set
// is "active" for a given platform is never actually called at runtime
// (the kIsWeb branches in voice_service.dart prevent that) — they just
// need to exist so the unused branch type-checks.

// ── Stand-ins for web_impl (used when compiling for native platforms) ──────
Future<bool> startWebRecording() async => false;
Future<String?> stopWebRecordingAndEncode() async => null;
void cancelWebRecording() {}

// ── Stand-ins for native_impl (used when compiling for web) ────────────────
Future<String> getTempAudioPath() async => '';
Future<List<int>?> readAndDeleteFile(String path) async => null;
