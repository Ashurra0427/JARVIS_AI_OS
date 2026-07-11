// lib/providers/customer_auth_provider.dart
//
// Holds the logged-in customer's session for agro_client.
//
// Login flow:
//   1. UI calls login(phone, pin).
//   2. We send {"type":"customer","action":"login","data":{phone,pin}}
//      over the shared WsService.
//   3. Server (server.py OR agro_server.py — both implement this
//      identically, see agents/agro/customer_portal.py) replies with
//      {"type":"customer_result","action":"login","data":{success,...}}.
//   4. On success we store customer_id / customer_name / token, persist
//      the token+phone in SharedPreferences so the app can silently
//      resume the session on next launch (the token is just an opaque
//      in-memory key on the server — if the server restarted, the first
//      customer-scoped call will fail with "Not logged in or session
//      expired" and we fall back to the login screen automatically).
import 'dart:async';
import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';
import '../services/ws_service.dart';

class CustomerAuthProvider extends ChangeNotifier {
  static const _kToken = 'customer_token';
  static const _kPhone = 'customer_phone';
  static const _kName = 'customer_name';
  static const _kId = 'customer_id';

  final WsService ws;
  CustomerAuthProvider(this.ws);

  String? _token;
  String? _phone;
  String? _name;
  int? _customerId;
  bool _restoring = true;
  String? _lastError;

  bool get isLoggedIn => _token != null && _customerId != null;
  bool get isRestoring => _restoring;
  String? get token => _token;
  String? get phone => _phone;
  String? get customerName => _name;
  int? get customerId => _customerId;
  String? get lastError => _lastError;

  /// Call once at app startup (after WsService.connect()).
  Future<void> restoreSession() async {
    final prefs = await SharedPreferences.getInstance();
    _token = prefs.getString(_kToken);
    _phone = prefs.getString(_kPhone);
    _name = prefs.getString(_kName);
    final idStr = prefs.getInt(_kId);
    _customerId = idStr;
    _restoring = false;
    notifyListeners();
    // Note: we don't re-validate the token against the server here —
    // the first real request (e.g. get_outstanding on the home screen)
    // will surface "session expired" if the server has restarted, and
    // the UI should route back to login in that case (see ws_listener
    // wiring in app.dart / home_screen.dart).
  }

  Future<bool> login(String phone, String pin) async {
    _lastError = null;
    final completer = Completer<Map<String, dynamic>>();
    late StreamSubscription sub;
    sub = ws.stream.listen((msg) {
      if (msg['type'] == 'customer_result' && msg['action'] == 'login') {
        if (!completer.isCompleted) {
          completer.complete((msg['data'] as Map<String, dynamic>?) ?? {});
        }
      }
    });

    ws.send({
      'type': 'customer',
      'action': 'login',
      'data': {'phone': phone, 'pin': pin},
    });

    final result = await Future.any([
      completer.future,
      Future.delayed(
        const Duration(seconds: 8),
        () => <String, dynamic>{'success': false, 'error': 'Timed out — check your connection.'},
      ),
    ]);
    await sub.cancel();

    if (result['success'] == true) {
      _token = result['token'] as String?;
      _customerId = result['customer_id'] as int?;
      _name = result['customer_name'] as String?;
      _phone = phone;

      final prefs = await SharedPreferences.getInstance();
      await prefs.setString(_kToken, _token ?? '');
      await prefs.setString(_kPhone, _phone ?? '');
      await prefs.setString(_kName, _name ?? '');
      if (_customerId != null) await prefs.setInt(_kId, _customerId!);

      notifyListeners();
      return true;
    } else {
      _lastError = (result['error'] ?? 'Login failed').toString();
      notifyListeners();
      return false;
    }
  }

  /// Called by any screen that gets back {"success": false, "error":
  /// "Not logged in or session expired."} from a customer-scoped request —
  /// forces the user back to the login screen.
  Future<void> forceLogout() async {
    _token = null;
    _customerId = null;
    _name = null;
    final prefs = await SharedPreferences.getInstance();
    await prefs.remove(_kToken);
    await prefs.remove(_kId);
    await prefs.remove(_kName);
    notifyListeners();
  }

  Future<void> logout() => forceLogout();

  /// Convenience for screens: send a customer-scoped action with the
  /// auth_token automatically attached.
  void sendCustomerAction(String action, Map<String, dynamic> data) {
    ws.send({
      'type': 'customer',
      'action': action,
      'data': {...data, 'auth_token': _token ?? ''},
    });
  }
}
