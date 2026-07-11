// lib/screens/settings/settings_screen.dart
// FIXED: Save URL now calls wsService.reconnect() immediately — no restart needed.
// Added: live connection status indicator in the header card.
// Added: "Test Connection" — a direct HTTP GET to /health, independent of the
//        WebSocket logic, so you can tell whether the *tunnel itself* is
//        reachable before worrying about WS/auth. Sends
//        ngrok-skip-browser-warning so a free ngrok tunnel's interstitial
//        page (if it ever applies) can't be mistaken for a real response.
import 'dart:async';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:provider/provider.dart';
import '../../providers/language_provider.dart';
import '../../providers/settings_provider.dart';
import '../../services/auth_service.dart';
import '../../services/ws_service.dart';
import '../../config/app_config.dart';

class SettingsScreen extends StatefulWidget {
  const SettingsScreen({super.key});
  @override
  State<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends State<SettingsScreen> {
  late TextEditingController _urlController;
  bool _saving = false;
  bool _testing = false;

  @override
  void initState() {
    super.initState();
    _urlController = TextEditingController(text: AppConfig.serverUrl);
  }

  @override
  void dispose() {
    _urlController.dispose();
    super.dispose();
  }

  Future<void> _testConnection() async {
    final url = _urlController.text.trim();
    final isNe = context.read<LanguageProvider>().isNepali;
    if (url.isEmpty || (!url.startsWith('http://') && !url.startsWith('https://'))) {
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
        content: Text('Enter a valid http:// or https:// URL first'),
        backgroundColor: Colors.red,
      ));
      return;
    }
    setState(() => _testing = true);
    String message;
    Color color;
    try {
      final res = await http
          .get(Uri.parse('$url/health'), headers: {'ngrok-skip-browser-warning': 'true'})
          .timeout(const Duration(seconds: 8));
      if (res.statusCode == 200) {
        message = isNe ? '✓ सर्भर पुगियो — ठीक छ' : '✓ Server reachable — looking good';
        color = Colors.green;
      } else {
        message = isNe
            ? 'सर्भरले HTTP ${res.statusCode} फिर्ता गर्यो'
            : 'Server responded with HTTP ${res.statusCode} — check the URL/port';
        color = Colors.red;
      }
    } on TimeoutException {
      message = isNe
          ? 'समय सकियो — सर्भर पुगिएन (नेटवर्क/टनेल जाँच गर्नुहोस्)'
          : 'Timed out — server unreachable (check the tunnel is running and the URL is current)';
      color = Colors.red;
    } catch (e) {
      message = isNe ? 'जडान असफल भयो' : 'Connection failed — ${e.toString().split('\n').first}';
      color = Colors.red;
    }
    if (mounted) {
      setState(() => _testing = false);
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(message), backgroundColor: color));
    }
  }

  Future<void> _save() async {
    final url = _urlController.text.trim();
    if (url.isEmpty) return;
    if (!url.startsWith('http://') && !url.startsWith('https://')) {
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
        content: Text('URL must start with http:// or https://'),
        backgroundColor: Colors.red,
      ));
      return;
    }

    setState(() => _saving = true);
    final ws = context.read<WsService>();
    await context.read<SettingsProvider>().updateServerUrl(url);

    // Immediately reconnect with the new URL — no restart needed.
    await ws.reconnect();

    if (mounted) {
      setState(() => _saving = false);
      final isNe = context.read<LanguageProvider>().isNepali;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(isNe
            ? 'URL सुरक्षित भयो — पुन:जोडिँदै...'
            : 'URL saved — reconnecting…'),
        backgroundColor: Colors.green,
      ));
    }
  }

  @override
  Widget build(BuildContext context) {
    final lang     = context.watch<LanguageProvider>();
    final isNe     = lang.isNepali;
    final settings = context.watch<SettingsProvider>();
    final ws       = context.read<WsService>();

    return Scaffold(
      appBar: AppBar(title: Text(isNe ? 'सेटिङ' : 'Settings')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [

          // ── Live connection status ────────────────────────────────
          StreamBuilder<bool>(
            stream: ws.connectionStream,
            initialData: ws.connected,
            builder: (_, snap) {
              final connected = snap.data ?? ws.connected;
              return Container(
                margin: const EdgeInsets.only(bottom: 12),
                padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
                decoration: BoxDecoration(
                  color: connected ? Colors.green.shade50 : Colors.orange.shade50,
                  borderRadius: BorderRadius.circular(10),
                  border: Border.all(
                    color: connected ? Colors.green.shade300 : Colors.orange.shade300),
                ),
                child: Row(children: [
                  Icon(connected ? Icons.cloud_done_outlined : Icons.cloud_off_outlined,
                      color: connected ? Colors.green.shade700 : Colors.orange.shade700),
                  const SizedBox(width: 10),
                  Expanded(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                    Text(
                      connected
                          ? (isNe ? '✓ सर्भरसँग जोडिएको' : '✓ Connected to server')
                          : (isNe ? 'सर्भरसँग जोडिएको छैन' : 'Not connected — check URL below'),
                      style: TextStyle(
                        fontWeight: FontWeight.w600, fontSize: 13,
                        color: connected ? Colors.green.shade800 : Colors.orange.shade800,
                      ),
                    ),
                    Text(
                      AppConfig.serverUrl,
                      style: TextStyle(fontSize: 11,
                        color: connected ? Colors.green.shade600 : Colors.orange.shade600),
                      overflow: TextOverflow.ellipsis,
                    ),
                    if (!connected && ws.lastError != null)
                      Padding(
                        padding: const EdgeInsets.only(top: 2),
                        child: Text(
                          ws.lastError!,
                          style: TextStyle(fontSize: 11, color: Colors.orange.shade900,
                              fontStyle: FontStyle.italic),
                          overflow: TextOverflow.ellipsis,
                        ),
                      ),
                  ])),
                  if (!connected)
                    TextButton(
                      onPressed: ws.reconnect,
                      child: Text(isNe ? 'पुन:जोड्नुस्' : 'Retry',
                          style: TextStyle(color: Colors.orange.shade800,
                              fontWeight: FontWeight.bold)),
                    ),
                ]),
              );
            },
          ),

          // ── Server URL ────────────────────────────────────────────
          Card(
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text(isNe ? 'सर्भर ठेगाना' : 'JARVIS Server URL',
                    style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 15)),
                const SizedBox(height: 4),
                Text(
                  isNe
                      ? 'ngrok वा LAN URL (https:// वा http://)'
                      : 'Your ngrok tunnel or local LAN URL (https:// or http://)',
                  style: const TextStyle(color: Colors.black54, fontSize: 12.5),
                ),
                const SizedBox(height: 12),
                TextFormField(
                  controller: _urlController,
                  keyboardType: TextInputType.url,
                  autocorrect: false,
                  decoration: const InputDecoration(
                    hintText: 'https://xxxx.ngrok-free.app',
                    prefixIcon: Icon(Icons.link),
                  ),
                ),
                const SizedBox(height: 12),
                SizedBox(
                  width: double.infinity,
                  height: 46,
                  child: ElevatedButton.icon(
                    style: ElevatedButton.styleFrom(
                      backgroundColor: const Color(0xFF003893),
                      foregroundColor: Colors.white,
                    ),
                    icon: const Icon(Icons.save_outlined, size: 18),
                    onPressed: _saving ? null : _save,
                    label: _saving
                        ? const SizedBox(width: 18, height: 18,
                            child: CircularProgressIndicator(
                                color: Colors.white, strokeWidth: 2))
                        : Text(isNe ? 'सुरक्षित गरी जोड्नुस्' : 'Save & Reconnect'),
                  ),
                ),
                const SizedBox(height: 8),
                SizedBox(
                  width: double.infinity,
                  height: 42,
                  child: OutlinedButton.icon(
                    onPressed: _testing ? null : _testConnection,
                    icon: _testing
                        ? const SizedBox(width: 16, height: 16,
                            child: CircularProgressIndicator(strokeWidth: 2))
                        : const Icon(Icons.network_check, size: 18),
                    label: Text(isNe ? 'जडान जाँच गर्नुहोस्' : 'Test Connection'),
                  ),
                ),
              ]),
            ),
          ),

          const SizedBox(height: 12),

          // ── Language ──────────────────────────────────────────────
          Card(
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text(isNe ? 'भाषा' : 'Language',
                    style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 15)),
                const SizedBox(height: 12),
                Row(children: [
                  Expanded(child: _LangBtn(
                      label: 'English', selected: !isNe, onTap: lang.setEnglish)),
                  const SizedBox(width: 10),
                  Expanded(child: _LangBtn(
                      label: 'नेपाली', selected: isNe, onTap: lang.setNepali)),
                ]),
              ]),
            ),
          ),

          const SizedBox(height: 12),

          // ── Voice confirmations ───────────────────────────────────
          Card(
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: Row(children: [
                Expanded(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Text(isNe ? 'आवाज पुष्टि' : 'Voice Confirmations',
                      style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 15)),
                  const SizedBox(height: 4),
                  Text(
                    isNe
                        ? 'काम दर्ता वा पूरा हुन्दा बोलिने पुष्टि'
                        : 'Spoken summary when a job is registered or completed',
                    style: const TextStyle(color: Colors.black54, fontSize: 13),
                  ),
                ])),
                Switch(
                  value: settings.ttsEnabled,
                  activeColor: const Color(0xFF003893),
                  onChanged: (v) => context.read<SettingsProvider>().setTtsEnabled(v),
                ),
              ]),
            ),
          ),

          const SizedBox(height: 12),

          // ── PIN security ──────────────────────────────────────────
          Card(
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text(isNe ? 'सुरक्षा' : 'Security',
                    style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 15)),
                const SizedBox(height: 4),
                Text(
                  isNe
                      ? 'साझा डिभाइसमा अनुप्रयोग खोल्न ४-अङ्कको PIN'
                      : 'Require a 4-digit PIN to open the app on this device',
                  style: const TextStyle(color: Colors.black54, fontSize: 13),
                ),
                const SizedBox(height: 12),
                Consumer<AuthService>(builder: (context, auth, _) {
                  if (!auth.isSetup) {
                    return SizedBox(width: double.infinity,
                        child: OutlinedButton.icon(
                          icon: const Icon(Icons.lock_outline),
                          label: Text(isNe ? 'PIN सेट गर्नुहोस्' : 'Set up PIN'),
                          onPressed: () => _showSetPinDialog(context, isNe),
                        ));
                  }
                  return Column(children: [
                    SizedBox(width: double.infinity,
                        child: OutlinedButton.icon(
                          icon: const Icon(Icons.password),
                          label: Text(isNe ? 'PIN परिवर्तन' : 'Change PIN'),
                          onPressed: () => _showChangePinDialog(context, isNe),
                        )),
                    const SizedBox(height: 8),
                    SizedBox(width: double.infinity,
                        child: OutlinedButton.icon(
                          style: OutlinedButton.styleFrom(foregroundColor: Colors.red),
                          icon: const Icon(Icons.lock_open),
                          label: Text(isNe ? 'PIN हटाउनुहोस्' : 'Remove PIN'),
                          onPressed: () => _showRemovePinDialog(context, auth, isNe),
                        )),
                    const SizedBox(height: 8),
                    SizedBox(width: double.infinity,
                        child: TextButton.icon(
                          icon: const Icon(Icons.lock),
                          label: Text(isNe ? 'अहिले बन्द गर्नुहोस्' : 'Lock now'),
                          onPressed: auth.lock,
                        )),
                  ]);
                }),
              ]),
            ),
          ),

          const SizedBox(height: 12),

          // ── About ─────────────────────────────────────────────────
          Card(
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text(isNe ? 'बारे' : 'About',
                    style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 15)),
                const SizedBox(height: 10),
                _InfoRow('App', 'JARVIS AGRO Operator'),
                _InfoRow('Version', '1.0.1'),
                _InfoRow(isNe ? 'व्यवसाय' : 'Business', 'Nawal Parasi, Nepal'),
                _InfoRow('Agent', 'AGRO — Agent 07'),
              ]),
            ),
          ),
        ],
      ),
    );
  }

  Future<void> _showSetPinDialog(BuildContext context, bool isNe) async {
    final auth = context.read<AuthService>();
    final pin1 = TextEditingController();
    final pin2 = TextEditingController();
    String? error;
    await showDialog(
      context: context,
      builder: (ctx) => StatefulBuilder(
        builder: (ctx, ss) => AlertDialog(
          title: Text(isNe ? 'PIN सेट' : 'Set up PIN'),
          content: Column(mainAxisSize: MainAxisSize.min, children: [
            TextField(controller: pin1, keyboardType: TextInputType.number, maxLength: 4,
                obscureText: true,
                decoration: InputDecoration(labelText: isNe ? 'नयाँ PIN' : 'New PIN')),
            TextField(controller: pin2, keyboardType: TextInputType.number, maxLength: 4,
                obscureText: true,
                decoration: InputDecoration(labelText: isNe ? 'PIN पुष्टि' : 'Confirm PIN')),
            if (error != null) Padding(padding: const EdgeInsets.only(top: 8),
                child: Text(error!, style: const TextStyle(color: Colors.red, fontSize: 12))),
          ]),
          actions: [
            TextButton(onPressed: () => Navigator.pop(ctx),
                child: Text(isNe ? 'रद्द' : 'Cancel')),
            ElevatedButton(
              onPressed: () async {
                if (pin1.text != pin2.text) {
                  ss(() => error = isNe ? 'PIN मिलेन' : 'PINs do not match'); return;
                }
                final r = await auth.setupPin(pin1.text);
                if (r.ok) { if (ctx.mounted) Navigator.pop(ctx); }
                else { ss(() => error = r.message); }
              },
              child: Text(isNe ? 'सुरक्षित' : 'Save'),
            ),
          ],
        ),
      ),
    );
  }

  Future<void> _showChangePinDialog(BuildContext context, bool isNe) async {
    final auth = context.read<AuthService>();
    final oldPin = TextEditingController();
    final newPin = TextEditingController();
    String? error;
    await showDialog(
      context: context,
      builder: (ctx) => StatefulBuilder(
        builder: (ctx, ss) => AlertDialog(
          title: Text(isNe ? 'PIN परिवर्तन' : 'Change PIN'),
          content: Column(mainAxisSize: MainAxisSize.min, children: [
            TextField(controller: oldPin, keyboardType: TextInputType.number, maxLength: 4,
                obscureText: true,
                decoration: InputDecoration(labelText: isNe ? 'पुरानो PIN' : 'Current PIN')),
            TextField(controller: newPin, keyboardType: TextInputType.number, maxLength: 4,
                obscureText: true,
                decoration: InputDecoration(labelText: isNe ? 'नयाँ PIN' : 'New PIN')),
            if (error != null) Padding(padding: const EdgeInsets.only(top: 8),
                child: Text(error!, style: const TextStyle(color: Colors.red, fontSize: 12))),
          ]),
          actions: [
            TextButton(onPressed: () => Navigator.pop(ctx),
                child: Text(isNe ? 'रद्द' : 'Cancel')),
            ElevatedButton(
              onPressed: () async {
                final r = await auth.changePin(oldPin.text, newPin.text);
                if (r.ok) { if (ctx.mounted) Navigator.pop(ctx); }
                else { ss(() => error = r.message); }
              },
              child: Text(isNe ? 'सुरक्षित' : 'Save'),
            ),
          ],
        ),
      ),
    );
  }

  Future<void> _showRemovePinDialog(BuildContext context, AuthService auth, bool isNe) async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text(isNe ? 'PIN हटाउनुहोस्?' : 'Remove PIN?'),
        content: Text(isNe
            ? 'अनुप्रयोगले अब PIN नचाहिकन खुल्नेछ।'
            : 'The app will open without a PIN from now on.'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx, false),
              child: Text(isNe ? 'रद्द' : 'Cancel')),
          ElevatedButton(
            style: ElevatedButton.styleFrom(
                backgroundColor: Colors.red, foregroundColor: Colors.white),
            onPressed: () => Navigator.pop(ctx, true),
            child: Text(isNe ? 'हटाउनुहोस्' : 'Remove'),
          ),
        ],
      ),
    );
    if (confirmed == true) await auth.clearPin();
  }
}

class _LangBtn extends StatelessWidget {
  final String label;
  final bool selected;
  final VoidCallback onTap;
  const _LangBtn({required this.label, required this.selected, required this.onTap});
  @override
  Widget build(BuildContext context) => InkWell(
    onTap: onTap,
    borderRadius: BorderRadius.circular(8),
    child: AnimatedContainer(
      duration: const Duration(milliseconds: 200),
      padding: const EdgeInsets.symmetric(vertical: 12),
      decoration: BoxDecoration(
        color: selected ? const Color(0xFF003893) : Colors.grey.shade100,
        borderRadius: BorderRadius.circular(8),
        border: Border.all(color: selected ? const Color(0xFF003893) : Colors.black12),
      ),
      alignment: Alignment.center,
      child: Text(label, style: TextStyle(
        color: selected ? Colors.white : Colors.black87, fontWeight: FontWeight.bold)),
    ),
  );
}

class _InfoRow extends StatelessWidget {
  final String label, value;
  const _InfoRow(this.label, this.value);
  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.symmetric(vertical: 3),
    child: Row(children: [
      Text('$label: ', style: const TextStyle(color: Colors.black54, fontSize: 13)),
      Text(value, style: const TextStyle(fontWeight: FontWeight.w600, fontSize: 13)),
    ]),
  );
}
