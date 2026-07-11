// lib/screens/auth/login_screen.dart
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import '../../providers/customer_auth_provider.dart';
import '../../providers/language_provider.dart';
import '../../widgets/language_toggle.dart';
import '../settings/settings_screen.dart';

class LoginScreen extends StatefulWidget {
  const LoginScreen({super.key});
  @override
  State<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends State<LoginScreen> {
  final _phone = TextEditingController();
  final _pin = TextEditingController();
  bool _submitting = false;

  @override
  void dispose() {
    _phone.dispose();
    _pin.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    final phone = _phone.text.trim();
    final pin = _pin.text.trim();
    if (phone.isEmpty || pin.length != 4) return;

    setState(() => _submitting = true);
    final auth = context.read<CustomerAuthProvider>();
    final ok = await auth.login(phone, pin);
    if (mounted) {
      setState(() => _submitting = false);
      if (!ok) {
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(
          content: Text(auth.lastError ?? 'Login failed'),
          backgroundColor: Colors.red,
        ));
      }
      // On success, app.dart's watch<CustomerAuthProvider>() rebuilds
      // straight to CustomerHomeScreen — no manual navigation needed here.
    }
  }

  @override
  Widget build(BuildContext context) {
    final lang = context.watch<LanguageProvider>();
    final auth = context.watch<CustomerAuthProvider>();
    final isNe = lang.isNepali;

    return Scaffold(
      backgroundColor: const Color(0xFF003893),
      body: SafeArea(
        child: Center(
          child: SingleChildScrollView(
            padding: const EdgeInsets.all(24),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                Row(
                  mainAxisAlignment: MainAxisAlignment.spaceBetween,
                  children: [
                    IconButton(
                      icon: const Icon(Icons.settings, color: Colors.white54),
                      tooltip: 'Server Settings',
                      onPressed: () => Navigator.push(
                        context,
                        MaterialPageRoute(
                            builder: (_) => const SettingsScreen()),
                      ),
                    ),
                    const LanguageToggle(),
                  ],
                ),
                const SizedBox(height: 8),
                const Icon(Icons.agriculture, color: Colors.white, size: 56),
                const SizedBox(height: 12),
                const Text(
                  'JARVIS AGRO',
                  style: TextStyle(color: Colors.white, fontWeight: FontWeight.bold, fontSize: 22),
                ),
                Text(
                  isNe ? 'ग्राहक' : 'Customer Portal',
                  style: const TextStyle(color: Colors.white70, fontSize: 14),
                ),
                const SizedBox(height: 32),
                _Field(
                  controller: _phone,
                  label: isNe ? 'फोन नम्बर' : 'Phone number',
                  keyboardType: TextInputType.phone,
                ),
                const SizedBox(height: 14),
                _Field(
                  controller: _pin,
                  label: isNe ? '४-अङ्कको PIN' : '4-digit PIN',
                  keyboardType: TextInputType.number,
                  obscure: true,
                  maxLength: 4,
                ),
                const SizedBox(height: 24),
                SizedBox(
                  width: double.infinity,
                  height: 48,
                  child: ElevatedButton(
                    style: ElevatedButton.styleFrom(
                      backgroundColor: Colors.white,
                      foregroundColor: const Color(0xFF003893),
                      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
                    ),
                    onPressed: (_submitting || auth.isRestoring) ? null : _submit,
                    child: Text(
                      _submitting ? '...' : (isNe ? 'लग इन' : 'Log In'),
                      style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 16),
                    ),
                  ),
                ),
                const SizedBox(height: 16),
                Text(
                  isNe
                      ? 'PIN छैन? अप्रेटरसँग सम्पर्क गर्नुहोस्।'
                      : "Don't have a PIN? Ask the operator to set one up for you.",
                  textAlign: TextAlign.center,
                  style: const TextStyle(color: Colors.white60, fontSize: 12.5),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _Field extends StatelessWidget {
  final TextEditingController controller;
  final String label;
  final TextInputType keyboardType;
  final bool obscure;
  final int? maxLength;
  const _Field({
    required this.controller,
    required this.label,
    required this.keyboardType,
    this.obscure = false,
    this.maxLength,
  });

  @override
  Widget build(BuildContext context) {
    return TextField(
      controller: controller,
      keyboardType: keyboardType,
      obscureText: obscure,
      maxLength: maxLength,
      style: const TextStyle(color: Colors.white),
      decoration: InputDecoration(
        labelText: label,
        labelStyle: const TextStyle(color: Colors.white70),
        counterText: '',
        enabledBorder: null,
        filled: true,
        fillColor: Colors.white.withOpacity(0.08),
        border: OutlineInputBorder(
          borderRadius: BorderRadius.circular(10),
          borderSide: BorderSide.none,
        ),
      ),
    );
  }
}
