// lib/services/job_timer_service.dart
//
// Hybrid per-minute billing timer for agriculture jobs.
//
// DESIGN:
//   • One active timer per job_id (multiple jobs can run simultaneously,
//     though in practice only one tractor runs at a time).
//   • Start timestamp is persisted to SharedPreferences so the timer
//     survives app restarts and phone lock-screens.
//   • Elapsed time ticks every second via a Timer.periodic.
//   • Emits elapsed minutes (double, so 1.5 min = 90 s) to listeners.
//   • On stop: returns total elapsed minutes for the caller to record.
//
// TRANSPORT JOBS: this service is never called for transport — billing
// is per taali (trip), handled by the existing quantity field.
//
// USAGE:
//   final svc = context.read<JobTimerService>();
//   svc.start(jobId: 42);
//   svc.elapsedMinutes(42)   // → 3.75 (3 min 45 s)
//   final mins = svc.stop(jobId: 42); // returns total, clears state
import 'dart:async';
import 'package:flutter/foundation.dart';
import 'package:shared_preferences/shared_preferences.dart';

class JobTimerService extends ChangeNotifier {
  // jobId → start DateTime (UTC)
  final Map<int, DateTime> _starts = {};

  // jobId → current elapsed seconds (for UI ticker)
  final Map<int, int> _elapsed = {};

  Timer? _ticker;

  // ── Keys ────────────────────────────────────────────────────────────────────
  static String _key(int jobId) => 'job_timer_start_$jobId';

  // ── Lifecycle ────────────────────────────────────────────────────────────────

  /// Call once at startup to restore any timers that were running when
  /// the app was killed.
  Future<void> restore() async {
    final prefs = await SharedPreferences.getInstance();
    final keys  = prefs.getKeys().where((k) => k.startsWith('job_timer_start_'));
    for (final key in keys) {
      final ms = prefs.getInt(key);
      if (ms == null) continue;
      final jobId = int.tryParse(key.replaceFirst('job_timer_start_', ''));
      if (jobId == null) continue;
      final start = DateTime.fromMillisecondsSinceEpoch(ms, isUtc: true);
      _starts[jobId] = start;
      _elapsed[jobId] =
          DateTime.now().toUtc().difference(start).inSeconds;
    }
    if (_starts.isNotEmpty) _ensureTicker();
    notifyListeners();
  }

  // ── Public API ───────────────────────────────────────────────────────────────

  bool isRunning(int jobId) => _starts.containsKey(jobId);

  /// Elapsed time in whole seconds (for UI display).
  int elapsedSeconds(int jobId) => _elapsed[jobId] ?? 0;

  /// Elapsed time in fractional minutes (for billing calculation).
  double elapsedMinutes(int jobId) => (elapsedSeconds(jobId)) / 60.0;

  /// Formatted mm:ss string for display on the timer button.
  String elapsedFormatted(int jobId) {
    final s = elapsedSeconds(jobId);
    final m = s ~/ 60;
    final sec = s % 60;
    return '${m.toString().padLeft(2, '0')}:${sec.toString().padLeft(2, '0')}';
  }

  /// Start the timer for [jobId].
  /// If already running, this is a no-op (safe to call twice).
  Future<void> start(int jobId) async {
    if (_starts.containsKey(jobId)) return; // already running

    final now = DateTime.now().toUtc();
    _starts[jobId]  = now;
    _elapsed[jobId] = 0;

    final prefs = await SharedPreferences.getInstance();
    await prefs.setInt(_key(jobId), now.millisecondsSinceEpoch);

    _ensureTicker();
    notifyListeners();
    debugPrint('JobTimerService: started timer for job $jobId at $now');
  }

  /// Stop the timer for [jobId].
  /// Returns fractional elapsed minutes for billing.
  /// Clears persisted state.
  Future<double> stop(int jobId) async {
    final mins = elapsedMinutes(jobId);

    _starts.remove(jobId);
    _elapsed.remove(jobId);

    final prefs = await SharedPreferences.getInstance();
    await prefs.remove(_key(jobId));

    if (_starts.isEmpty) _ticker?.cancel();
    notifyListeners();

    debugPrint(
        'JobTimerService: stopped timer for job $jobId → ${mins.toStringAsFixed(2)} min');
    return mins;
  }

  /// Force-clear a timer without returning time (e.g. on job cancel).
  Future<void> clear(int jobId) async {
    _starts.remove(jobId);
    _elapsed.remove(jobId);
    final prefs = await SharedPreferences.getInstance();
    await prefs.remove(_key(jobId));
    if (_starts.isEmpty) _ticker?.cancel();
    notifyListeners();
  }

  // ── Internal ─────────────────────────────────────────────────────────────────

  void _ensureTicker() {
    if (_ticker?.isActive == true) return;
    _ticker = Timer.periodic(const Duration(seconds: 1), (_) {
      final now = DateTime.now().toUtc();
      bool changed = false;
      for (final entry in _starts.entries) {
        final newElapsed = now.difference(entry.value).inSeconds;
        if (_elapsed[entry.key] != newElapsed) {
          _elapsed[entry.key] = newElapsed;
          changed = true;
        }
      }
      if (changed) notifyListeners();
    });
  }

  @override
  void dispose() {
    _ticker?.cancel();
    super.dispose();
  }
}
