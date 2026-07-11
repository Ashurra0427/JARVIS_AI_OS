// lib/services/auth_service.dart
//
// Simple PIN-based local authentication for the JARVIS AGRO operator app.
// No external server needed — the PIN is stored hashed in SharedPreferences.
// Designed for a 1–5 operator family business where a single 4-digit PIN
// protects the app on a shared device.
//
// Usage:
//   final auth = AuthService();
//   await auth.load();                 // call once at startup (after AppConfig.load)
//   auth.isSetup                       // false if no PIN has been configured yet
//   await auth.setupPin('1234');       // first-time setup
//   await auth.verify('1234');         // returns true/false
//   await auth.changePin('1234','5678');
//   await auth.clearPin();             // factory reset (admin only)
//
// Integration with main.dart:
//   AuthService is a ChangeNotifier so you can wrap it in a Provider and
//   listen to authentication state changes (e.g. isAuthenticated).

import 'dart:convert';
import 'package:crypto/crypto.dart';
import 'package:flutter/foundation.dart';
import 'package:shared_preferences/shared_preferences.dart';

class AuthService extends ChangeNotifier {
  // ── Prefs keys ──────────────────────────────────────────────────────
  static const _keyPinHash     = 'agro_pin_hash';
  static const _keyPinEnabled  = 'agro_pin_enabled';
  static const _keyFailCount   = 'agro_pin_fail_count';
  static const _keyLockUntil   = 'agro_pin_lock_until';

  // Lock out after this many consecutive wrong PINs
  static const int _maxFailures   = 5;
  // Lock duration in minutes
  static const int _lockMinutes   = 5;

  // ── State ────────────────────────────────────────────────────────────
  bool _pinEnabled      = false;
  bool _isAuthenticated = false;
  int  _failCount       = 0;
  DateTime? _lockUntil;

  bool get isSetup         => _pinEnabled;
  bool get isAuthenticated => _isAuthenticated;
  bool get isLocked        => _lockUntil != null && DateTime.now().isBefore(_lockUntil!);
  int  get failCount       => _failCount;
  DateTime? get lockUntil => _lockUntil;

  /// Remaining lock seconds (0 if not locked)
  int get lockSecondsRemaining {
    if (!isLocked) return 0;
    return _lockUntil!.difference(DateTime.now()).inSeconds.clamp(0, 9999);
  }

  // ── Initialisation ───────────────────────────────────────────────────

  /// Load persisted state. Call once at app startup.
  Future<void> load() async {
    final prefs = await SharedPreferences.getInstance();
    _pinEnabled      = prefs.getBool(_keyPinEnabled)  ?? false;
    _failCount       = prefs.getInt(_keyFailCount)    ?? 0;
    final lockMs     = prefs.getInt(_keyLockUntil);
    _lockUntil       = lockMs != null
        ? DateTime.fromMillisecondsSinceEpoch(lockMs)
        : null;

    // If lock period has expired, clear it
    if (_lockUntil != null && DateTime.now().isAfter(_lockUntil!)) {
      _lockUntil  = null;
      _failCount  = 0;
      await _persistCounters(prefs);
    }

    // If PIN is not enabled, consider the session authenticated automatically
    if (!_pinEnabled) _isAuthenticated = true;

    notifyListeners();
  }

  // ── PIN setup ────────────────────────────────────────────────────────

  /// First-time PIN setup. Validates the PIN is exactly 4 digits.
  Future<AuthResult> setupPin(String pin) async {
    final check = _validate(pin);
    if (check != null) return AuthResult.error(check);

    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(_keyPinHash,    _hash(pin));
    await prefs.setBool(_keyPinEnabled,   true);
    await _persistCounters(prefs, failCount: 0, lockUntil: null);

    _pinEnabled      = true;
    _failCount       = 0;
    _lockUntil       = null;
    _isAuthenticated = true;
    notifyListeners();
    return AuthResult.success();
  }

  // ── Verification ─────────────────────────────────────────────────────

  /// Verify the entered PIN. Tracks failures and enforces lock-out.
  Future<AuthResult> verify(String pin) async {
    if (!_pinEnabled) {
      _isAuthenticated = true;
      notifyListeners();
      return AuthResult.success();
    }

    // Check lock
    if (isLocked) {
      return AuthResult.error(
        'Too many wrong PINs. Try again in $lockSecondsRemaining seconds.',
      );
    }

    final prefs = await SharedPreferences.getInstance();
    final stored = prefs.getString(_keyPinHash);
    if (stored == null) {
      // PIN was wiped — auto-authenticate
      _isAuthenticated = true;
      _pinEnabled      = false;
      await prefs.setBool(_keyPinEnabled, false);
      notifyListeners();
      return AuthResult.success();
    }

    if (_hash(pin) == stored) {
      // ✅ Correct
      _failCount       = 0;
      _lockUntil       = null;
      _isAuthenticated = true;
      await _persistCounters(prefs, failCount: 0, lockUntil: null);
      notifyListeners();
      return AuthResult.success();
    }

    // ❌ Wrong PIN
    _failCount++;
    DateTime? newLock;
    if (_failCount >= _maxFailures) {
      newLock    = DateTime.now().add(Duration(minutes: _lockMinutes));
      _lockUntil = newLock;
      debugPrint('[AuthService] locked until $_lockUntil');
    }
    await _persistCounters(prefs, failCount: _failCount, lockUntil: newLock);
    notifyListeners();

    if (isLocked) {
      return AuthResult.error(
        'Too many wrong PINs. Locked for $_lockMinutes minutes.',
      );
    }
    final remaining = _maxFailures - _failCount;
    return AuthResult.error('Wrong PIN. $remaining attempt(s) remaining.');
  }

  // ── PIN change ───────────────────────────────────────────────────────

  /// Change PIN — requires the old PIN to be correct first.
  Future<AuthResult> changePin(String oldPin, String newPin) async {
    final verifyResult = await verify(oldPin);
    if (!verifyResult.ok) return verifyResult;

    return setupPin(newPin);
  }

  // ── Logout / lock ────────────────────────────────────────────────────

  /// Lock the session (requires PIN on next open).
  void lock() {
    if (!_pinEnabled) return;
    _isAuthenticated = false;
    notifyListeners();
  }

  /// Remove PIN entirely — returns to unauthenticated-optional mode.
  Future<void> clearPin() async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.remove(_keyPinHash);
    await prefs.setBool(_keyPinEnabled, false);
    await _persistCounters(prefs, failCount: 0, lockUntil: null);

    _pinEnabled      = false;
    _failCount       = 0;
    _lockUntil       = null;
    _isAuthenticated = true;
    notifyListeners();
  }

  // ── Helpers ──────────────────────────────────────────────────────────

  String _hash(String pin) {
    final bytes = utf8.encode('agro_jarvis_$pin');
    return sha256.convert(bytes).toString();
  }

  String? _validate(String pin) {
    if (pin.length != 4) return 'PIN must be exactly 4 digits.';
    if (!RegExp(r'^\d{4}$').hasMatch(pin)) return 'PIN must contain only digits.';
    return null;
  }

  Future<void> _persistCounters(
    SharedPreferences prefs, {
    int? failCount,
    DateTime? lockUntil,
  }) async {
    final fc = failCount ?? _failCount;
    final lu = lockUntil ?? _lockUntil;
    await prefs.setInt(_keyFailCount, fc);
    if (lu != null) {
      await prefs.setInt(_keyLockUntil, lu.millisecondsSinceEpoch);
    } else {
      await prefs.remove(_keyLockUntil);
    }
  }
}

// ── Result type ──────────────────────────────────────────────────────────────

class AuthResult {
  final bool ok;
  final String? message;

  const AuthResult._({required this.ok, this.message});

  factory AuthResult.success() => const AuthResult._(ok: true);
  factory AuthResult.error(String msg) => AuthResult._(ok: false, message: msg);

  @override
  String toString() => ok ? 'AuthResult.success' : 'AuthResult.error($message)';
}