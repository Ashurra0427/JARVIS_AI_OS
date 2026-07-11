// lib/screens/settings/settings_screen.dart  [agro_client]
// Added: "Test Connection" — a direct HTTP GET to /health, independent of
//        the WebSocket logic, so you can tell whether the tunnel itself is
//        reachable before worrying about WS/auth.
import 'dart:async';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:provider/provider.dart';
import '../../config/app_config.dart';
import '../../providers/language_provider.dart';
import '../../providers/customer_auth_provider.dart';
import '../../services/ws_service.dart';
import '../../services/tts_playback_service.dart';

class SettingsScreen extends StatefulWidget {
  const SettingsScreen({super.key});
  @override
  State<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends State<SettingsScreen> {
  late TextEditingController _urlCtrl;
  bool _saving = false;
  bool _testing = false;

  @override
  void initState() {
    super.initState();
    _urlCtrl = TextEditingController(text: AppConfig.serverUrl);
  }

  @override
  void dispose() {
    _urlCtrl.dispose();
    super.dispose();
  }

  Future<void> _testConnection() async {
    final url = _urlCtrl.text.trim();
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

  Future<void> _saveUrl() async {
    final url = _urlCtrl.text.trim();
    if (url.isEmpty) return;
    if (!url.startsWith('http://') && !url.startsWith('https://')) {
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
        content: Text('URL must start with http:// or https://'),
        backgroundColor: Colors.red,
      ));
      return;
    }
    setState(() => _saving = true);
    await AppConfig.save(url);
    // BUGFIX: was ws.connect() — if a previous connection attempt was still
    // in flight (e.g. mid ngrok handshake), connect() would silently no-op
    // (see FIX 1 in ws_service.dart) and the new URL would never actually
    // be tried. reconnect() always tears down and retries immediately.
    await context.read<WsService>().reconnect();
    setState(() => _saving = false);
    if (mounted) ScaffoldMessenger.of(context).showSnackBar(
      const SnackBar(content: Text('Server URL saved — reconnecting…'),
          backgroundColor: Colors.green));
  }

  @override
  Widget build(BuildContext context) {
    final lang = context.watch<LanguageProvider>();
    final tts  = context.watch<TtsPlaybackService>();
    final auth = context.read<CustomerAuthProvider>();
    final ws   = context.read<WsService>();
    final isNe = lang.isNepali;

    return Scaffold(
      appBar: AppBar(
        backgroundColor: const Color(0xFF003893),
        foregroundColor: Colors.white,
        title: Text(isNe ? 'सेटिङ' : 'Settings'),
      ),
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

          // Server URL
          Card(
            shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text(isNe ? 'सर्भर ठेगाना' : 'Server URL',
                    style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 15)),
                const SizedBox(height: 4),
                Text(
                  isNe
                      ? 'ngrok/Cloudflare tunnel वा LAN IP ठेगाना'
                      : 'ngrok / Cloudflare tunnel URL or LAN IP',
                  style: const TextStyle(color: Colors.black54, fontSize: 12.5),
                ),
                const SizedBox(height: 12),
                TextFormField(
                  controller: _urlCtrl,
                  keyboardType: TextInputType.url,
                  decoration: const InputDecoration(
                    hintText: 'https://xxxx.ngrok-free.app  or  http://192.168.x.x:7788',
                    prefixIcon: Icon(Icons.link),
                  ),
                ),
                const SizedBox(height: 10),
                SizedBox(
                  width: double.infinity,
                  child: ElevatedButton.icon(
                    style: ElevatedButton.styleFrom(
                        backgroundColor: const Color(0xFF003893),
                        foregroundColor: Colors.white),
                    onPressed: _saving ? null : _saveUrl,
                    icon: _saving
                        ? const SizedBox(width: 16, height: 16,
                            child: CircularProgressIndicator(color: Colors.white, strokeWidth: 2))
                        : const Icon(Icons.save),
                    label: Text(isNe ? 'सेभ गर्नुस्' : 'Save & Reconnect'),
                  ),
                ),
                const SizedBox(height: 8),
                SizedBox(
                  width: double.infinity,
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

          // TTS toggle
          Card(
            shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
            child: SwitchListTile(
              value: tts.enabled,
              onChanged: tts.setEnabled,
              activeColor: const Color(0xFF003893),
              secondary: const Icon(Icons.volume_up_outlined, color: Color(0xFF003893)),
              title: Text(isNe ? 'आवाज सूचना' : 'Voice Notifications'),
              subtitle: Text(isNe
                  ? 'काम स्वीकार / सकिएमा आवाज आउनेछ'
                  : 'Play audio when your job is accepted or completed'),
            ),
          ),

          const SizedBox(height: 12),

          // Language
          Card(
            shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
            child: ListTile(
              leading: const Icon(Icons.language, color: Color(0xFF003893)),
              title: Text(isNe ? 'भाषा' : 'Language'),
              subtitle: Text(lang.isNepali ? 'नेपाली' : 'English'),
              trailing: TextButton(
                onPressed: lang.toggle,
                child: Text(lang.isNepali ? 'Switch to English' : 'नेपालीमा जानुस्',
                    style: const TextStyle(color: Color(0xFF003893))),
              ),
            ),
          ),

          const SizedBox(height: 12),

          // Account / logout
          Card(
            shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
            child: ListTile(
              leading: const Icon(Icons.person_outline, color: Color(0xFF003893)),
              title: Text(isNe ? 'खाता' : 'Account'),
              subtitle: Text(auth.customerName ?? auth.phone ?? ''),
              trailing: TextButton(
                onPressed: () => showDialog(
                  context: context,
                  builder: (_) => AlertDialog(
                    title: Text(isNe ? 'लग आउट' : 'Logout'),
                    content: Text(isNe
                        ? 'के तपाईं बाहिर निस्कन चाहनुहुन्छ?'
                        : 'Are you sure you want to logout?'),
                    actions: [
                      TextButton(onPressed: () => Navigator.pop(context),
                          child: Text(isNe ? 'रद्द' : 'Cancel')),
                      ElevatedButton(
                        style: ElevatedButton.styleFrom(backgroundColor: Colors.red),
                        onPressed: () { auth.logout(); Navigator.pop(context); },
                        child: Text(isNe ? 'बाहिर' : 'Logout'),
                      ),
                    ],
                  ),
                ),
                child: Text(isNe ? 'लग आउट' : 'Logout',
                    style: const TextStyle(color: Colors.red)),
              ),
            ),
          ),

          const SizedBox(height: 12),

          // About
          Card(
            shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
            child: Padding(
              padding: const EdgeInsets.all(14),
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text(isNe ? 'बारेमा' : 'About',
                    style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 14)),
                const SizedBox(height: 8),
                const Text('JARVIS AGRO — Customer App v1.1.0',
                    style: TextStyle(fontSize: 13)),
                Text(
                  isNe
                      ? 'Nawal Parasi, Lumbini Pradesh — परिवारिक कृषि तथा यातायात'
                      : 'Nawal Parasi, Lumbini Pradesh — family agri & transport',
                  style: const TextStyle(fontSize: 12, color: Colors.black45),
                ),
              ]),
            ),
          ),
        ],
      ),
    );
  }
}
