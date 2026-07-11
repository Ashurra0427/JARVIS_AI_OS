// lib/screens/auth/pin_screen.dart
//
// PIN entry / first-time setup screen for AuthService.
// Shown by app.dart whenever AuthService.isAuthenticated is false.
//
// Two modes, chosen automatically from AuthService.isSetup:
//   • Not set up yet  -> "Create a PIN" (enter once, confirm once)
//   • Already set up  -> "Enter PIN" (verify against stored hash)
//
// Lock-out (5 wrong PINs -> 5 minute lock) is enforced entirely by
// AuthService; this screen just reflects isLocked / lockSecondsRemaining.

import 'dart:async';

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../providers/language_provider.dart';
import '../../services/auth_service.dart';

class PinScreen extends StatefulWidget {
  const PinScreen({super.key});

  @override
  State<PinScreen> createState() => _PinScreenState();
}

class _PinScreenState extends State<PinScreen> {
  String _pin = '';
  String? _firstPin; // held during create-PIN confirmation step
  String? _error;
  bool _confirming = false;
  Timer? _lockTicker;

  @override
  void initState() {
    super.initState();
    // Re-render once a second while locked so the countdown updates.
    _lockTicker = Timer.periodic(const Duration(seconds: 1), (_) {
      final auth = context.read<AuthService>();
      if (auth.isLocked) setState(() {});
    });
  }

  @override
  void dispose() {
    _lockTicker?.cancel();
    super.dispose();
  }

  void _onDigit(String d) {
    final auth = context.read<AuthService>();
    if (auth.isLocked) return;
    if (_pin.length >= 4) return;
    setState(() {
      _pin += d;
      _error = null;
    });
    if (_pin.length == 4) _submit();
  }

  void _onBackspace() {
    if (_pin.isEmpty) return;
    setState(() => _pin = _pin.substring(0, _pin.length - 1));
  }

  Future<void> _submit() async {
    final auth = context.read<AuthService>();

    if (!auth.isSetup) {
      // First-time setup: two-step (enter, then confirm).
      if (!_confirming) {
        setState(() {
          _firstPin = _pin;
          _pin = '';
          _confirming = true;
        });
        return;
      }
      if (_pin != _firstPin) {
        setState(() {
          _error = 'PINs did not match. Try again.';
          _pin = '';
          _firstPin = null;
          _confirming = false;
        });
        return;
      }
      final result = await auth.setupPin(_pin);
      if (!result.ok && mounted) {
        setState(() {
          _error = result.message;
          _pin = '';
          _firstPin = null;
          _confirming = false;
        });
      }
      return;
    }

    // Verifying an existing PIN.
    final result = await auth.verify(_pin);
    if (!mounted) return;
    if (!result.ok) {
      setState(() {
        _error = result.message;
        _pin = '';
      });
    }
    // On success AuthService.isAuthenticated flips true and app.dart's
    // watch<AuthService>() rebuilds straight past this screen.
  }

  @override
  Widget build(BuildContext context) {
    final auth = context.watch<AuthService>();
    final lang = context.watch<LanguageProvider>();
    final isNe = lang.isNepali;
    final locked = auth.isLocked;

    final String title;
    if (locked) {
      title = isNe ? 'बन्द गरिएको' : 'Locked';
    } else if (!auth.isSetup) {
      title = _confirming
          ? (isNe ? 'PIN पुष्टि गर्नुहोस्' : 'Confirm your PIN')
          : (isNe ? 'PIN बनाउनुहोस्' : 'Create a PIN');
    } else {
      title = isNe ? 'PIN प्रविष्ट गर्नुहोस्' : 'Enter PIN';
    }

    return Scaffold(
      backgroundColor: const Color(0xFF003893),
      body: SafeArea(
        child: Center(
          child: Padding(
            padding: const EdgeInsets.all(24),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                const Icon(Icons.agriculture, color: Colors.white, size: 56),
                const SizedBox(height: 12),
                const Text(
                  'JARVIS AGRO',
                  style: TextStyle(
                    color: Colors.white,
                    fontWeight: FontWeight.bold,
                    fontSize: 22,
                  ),
                ),
                const SizedBox(height: 32),
                Text(
                  title,
                  style: const TextStyle(
                    color: Colors.white,
                    fontSize: 17,
                    fontWeight: FontWeight.w600,
                  ),
                ),
                const SizedBox(height: 20),

                if (locked)
                  Text(
                    isNe
                        ? '${auth.lockSecondsRemaining} सेकेन्डमा पुन: प्रयास गर्नुहोस्'
                        : 'Try again in ${auth.lockSecondsRemaining}s',
                    style: const TextStyle(color: Colors.white70, fontSize: 14),
                  )
                else ...[
                  _PinDots(length: _pin.length),
                  const SizedBox(height: 16),
                  if (_error != null)
                    Padding(
                      padding: const EdgeInsets.only(bottom: 8),
                      child: Text(
                        _error!,
                        textAlign: TextAlign.center,
                        style: const TextStyle(color: Colors.amberAccent, fontSize: 13),
                      ),
                    ),
                  _Keypad(onDigit: _onDigit, onBackspace: _onBackspace),
                ],
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _PinDots extends StatelessWidget {
  final int length;
  const _PinDots({required this.length});

  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisAlignment: MainAxisAlignment.center,
      children: List.generate(4, (i) {
        final filled = i < length;
        return Container(
          margin: const EdgeInsets.symmetric(horizontal: 8),
          width: 16,
          height: 16,
          decoration: BoxDecoration(
            shape: BoxShape.circle,
            color: filled ? Colors.white : Colors.transparent,
            border: Border.all(color: Colors.white, width: 2),
          ),
        );
      }),
    );
  }
}

class _Keypad extends StatelessWidget {
  final ValueChanged<String> onDigit;
  final VoidCallback onBackspace;
  const _Keypad({required this.onDigit, required this.onBackspace});

  @override
  Widget build(BuildContext context) {
    const rows = [
      ['1', '2', '3'],
      ['4', '5', '6'],
      ['7', '8', '9'],
    ];

    Widget keyBtn(String label, {VoidCallback? onTap, Widget? child}) {
      return Padding(
        padding: const EdgeInsets.all(6),
        child: SizedBox(
          width: 64,
          height: 64,
          child: Material(
            color: Colors.white.withOpacity(0.08),
            shape: const CircleBorder(),
            child: InkWell(
              customBorder: const CircleBorder(),
              onTap: onTap,
              child: Center(
                child: child ??
                    Text(
                      label,
                      style: const TextStyle(
                        color: Colors.white,
                        fontSize: 22,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
              ),
            ),
          ),
        ),
      );
    }

    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        for (final row in rows)
          Row(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [for (final d in row) keyBtn(d, onTap: () => onDigit(d))],
          ),
        Row(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            const SizedBox(width: 76), // empty slot under '7'
            keyBtn('0', onTap: () => onDigit('0')),
            keyBtn(
              '',
              onTap: onBackspace,
              child: const Icon(Icons.backspace_outlined, color: Colors.white70, size: 20),
            ),
          ],
        ),
      ],
    );
  }
}
