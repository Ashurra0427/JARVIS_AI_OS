// lib/screens/customers/customer_requests_screen.dart
//
// IMPROVEMENTS (Phase 12 robustness):
//   ✓ Confirmation dialog before accept/decline
//   ✓ Accepted/Declined requests shown in history section
//   ✓ All requests shown (pending + history) not just pending
//   ✓ Status localized in Nepali
//   ✓ Better empty states
import 'dart:async';
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import '../../providers/language_provider.dart';
import '../../services/ws_service.dart';

class CustomerRequestsScreen extends StatefulWidget {
  const CustomerRequestsScreen({super.key});
  @override
  State<CustomerRequestsScreen> createState() => _CustomerRequestsScreenState();
}

class _CustomerRequestsScreenState extends State<CustomerRequestsScreen>
    with SingleTickerProviderStateMixin {
  final _phone = TextEditingController();
  final _pin   = TextEditingController();
  bool _issuing = false;
  bool _loadingRequests = true;
  String? _issueResultMessage;
  bool? _issueSuccess;
  List<dynamic> _allRequests  = [];
  StreamSubscription? _sub;
  final Set<int> _actingOnRequest = {};
  late TabController _tabs;

  @override
  void initState() {
    super.initState();
    _tabs = TabController(length: 2, vsync: this);
    _loadRequests();
  }

  @override
  void dispose() {
    _phone.dispose();
    _pin.dispose();
    _sub?.cancel();
    _tabs.dispose();
    super.dispose();
  }

  List<dynamic> get _pendingRequests =>
      _allRequests.where((r) => (r as Map)['status'] == 'pending').toList();
  List<dynamic> get _historyRequests =>
      _allRequests.where((r) => (r as Map)['status'] != 'pending').toList();

  Future<void> _loadRequests() async {
    final ws = context.read<WsService>();
    setState(() => _loadingRequests = true);

    final completer = Completer<List<dynamic>>();
    _sub?.cancel();
    _sub = ws.stream.listen((msg) {
      if (msg['type'] == 'agro_result' && msg['action'] == 'get_all_job_requests') {
        final data = msg['data'] as Map<String, dynamic>?;
        if (!completer.isCompleted) {
          completer.complete((data?['requests'] as List?) ?? []);
        }
      } else if (msg['type'] == 'agro_result' && msg['action'] == 'get_pending_job_requests') {
        // Fallback for servers that only support pending
        final data = msg['data'] as Map<String, dynamic>?;
        if (!completer.isCompleted) {
          completer.complete((data?['requests'] as List?) ?? []);
        }
      }
    });

    // Try all requests first, fall back to pending
    ws.sendAgroAction('get_all_job_requests', {});
    // Also send pending as fallback (server may only support one)
    Future.delayed(const Duration(milliseconds: 300), () {
      if (!completer.isCompleted) {
        ws.sendAgroAction('get_pending_job_requests', {});
      }
    });

    final result = await Future.any([
      completer.future,
      Future.delayed(const Duration(seconds: 5), () => <dynamic>[]),
    ]);
    _sub?.cancel();

    if (mounted) setState(() {
      _allRequests = result;
      _loadingRequests = false;
    });
  }

  Future<void> _actOnRequest(int requestId, String action) async {
    if (_actingOnRequest.contains(requestId)) return;
    final isNe = context.read<LanguageProvider>().isNepali;
    final isAccept = action == 'accept_job_request';

    // Confirmation dialog
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Row(children: [
          Icon(isAccept ? Icons.check_circle_outline : Icons.cancel_outlined,
              color: isAccept ? Colors.green : Colors.red),
          const SizedBox(width: 8),
          Text(isAccept
              ? (isNe ? 'अनुरोध स्वीकार गर्नुस्?' : 'Accept Request?')
              : (isNe ? 'अनुरोध अस्वीकार गर्नुस्?' : 'Decline Request?')),
        ]),
        content: Text(isAccept
            ? (isNe
                ? 'यो अनुरोध स्वीकार गरेपछि ग्राहकलाई सूचना पठाइनेछ। काम थप्न \"काम थप्नुस्\" प्रयोग गर्नुस्।'
                : 'Customer will be notified. Use "Add Job" to create the actual job.')
            : (isNe
                ? 'यो अनुरोध अस्वीकार गरेपछि ग्राहकलाई सूचना पठाइनेछ।'
                : 'Customer will be notified that their request was declined.')),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx, false),
            child: Text(isNe ? 'वापस' : 'Go Back'),
          ),
          ElevatedButton(
            style: ElevatedButton.styleFrom(
              backgroundColor: isAccept ? Colors.green.shade700 : Colors.red,
              foregroundColor: Colors.white,
            ),
            onPressed: () => Navigator.pop(ctx, true),
            child: Text(isAccept
                ? (isNe ? 'स्वीकार' : 'Accept')
                : (isNe ? 'अस्वीकार' : 'Decline')),
          ),
        ],
      ),
    );

    if (confirmed != true || !mounted) return;

    setState(() => _actingOnRequest.add(requestId));

    final ws = context.read<WsService>();
    final completer = Completer<Map<String, dynamic>>();
    _sub?.cancel();
    _sub = ws.stream.listen((msg) {
      if (msg['type'] == 'agro_result' && msg['action'] == action) {
        final data = msg['data'] as Map<String, dynamic>? ?? {};
        if (!completer.isCompleted) completer.complete(data);
      }
    });

    ws.sendAgroAction(action, {'request_id': requestId});
    final result = await Future.any([
      completer.future,
      Future.delayed(const Duration(seconds: 6),
          () => <String, dynamic>{'success': false, 'error': 'Timed out.'}),
    ]);
    _sub?.cancel();

    if (mounted) {
      setState(() => _actingOnRequest.remove(requestId));
      final ok = result['success'] == true;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(ok
            ? (isAccept
                ? (isNe ? 'अनुरोध स्वीकार गरियो ✓' : 'Request accepted ✓')
                : (isNe ? 'अनुरोध अस्वीकार गरियो' : 'Request declined'))
            : (result['error'] ?? result['message'] ?? 'Failed').toString()),
        backgroundColor: ok
            ? (isAccept ? Colors.green.shade700 : Colors.orange.shade700)
            : Colors.red,
      ));
      if (ok) _loadRequests();
    }
  }

  Future<void> _issuePin() async {
    final isNe  = context.read<LanguageProvider>().isNepali;
    final phone = _phone.text.trim();
    final pin   = _pin.text.trim();
    if (phone.isEmpty || pin.length != 4) {
      setState(() {
        _issueSuccess = false;
        _issueResultMessage = isNe
            ? 'फोन नम्बर र ४-अङ्कको PIN आवश्यक छ।'
            : 'Enter a phone number and a 4-digit PIN.';
      });
      return;
    }

    final ws = context.read<WsService>();
    setState(() { _issuing = true; _issueResultMessage = null; });

    final completer = Completer<Map<String, dynamic>>();
    _sub?.cancel();
    _sub = ws.stream.listen((msg) {
      if (msg['type'] == 'agro_result' && msg['action'] == 'issue_customer_pin') {
        final data = msg['data'] as Map<String, dynamic>? ?? {};
        if (!completer.isCompleted) completer.complete(data);
      }
    });

    ws.sendAgroAction('issue_customer_pin', {'phone': phone, 'pin': pin});
    final result = await Future.any([
      completer.future,
      Future.delayed(const Duration(seconds: 5),
          () => <String, dynamic>{'success': false, 'error': 'Timed out — server not responding'}),
    ]);
    _sub?.cancel();

    if (mounted) {
      setState(() {
        _issuing = false;
        _issueSuccess = result['success'] == true;
        _issueResultMessage = result['success'] == true
            ? (isNe
                ? '${result['customer_name'] ?? phone} को लागि PIN सेट भयो।'
                : 'PIN set for ${result['customer_name'] ?? phone}.')
            : (result['error'] ?? 'Failed to set PIN.').toString();
      });
      if (_issueSuccess == true) _pin.clear();
    }
  }

  @override
  Widget build(BuildContext context) {
    final lang = context.watch<LanguageProvider>();
    final isNe = lang.isNepali;
    final pendingCount = _pendingRequests.length;

    return Scaffold(
      appBar: AppBar(
        backgroundColor: Colors.teal.shade700,
        foregroundColor: Colors.white,
        title: Text(isNe ? 'ग्राहक पोर्टल' : 'Customer Portal'),
        bottom: TabBar(
          controller: _tabs,
          indicatorColor: Colors.white,
          labelColor: Colors.white,
          unselectedLabelColor: Colors.white70,
          tabs: [
            Tab(child: Row(mainAxisSize: MainAxisSize.min, children: [
              Text(isNe ? 'अनुरोधहरू' : 'Requests'),
              if (pendingCount > 0) ...[
                const SizedBox(width: 6),
                Container(
                  padding: const EdgeInsets.symmetric(horizontal: 5, vertical: 1),
                  decoration: BoxDecoration(
                    color: Colors.orange, borderRadius: BorderRadius.circular(8)),
                  child: Text('$pendingCount',
                      style: const TextStyle(fontSize: 10, color: Colors.white,
                          fontWeight: FontWeight.bold)),
                ),
              ],
            ])),
            Tab(text: isNe ? 'PIN व्यवस्थापन' : 'PIN Management'),
          ],
        ),
      ),
      body: TabBarView(
        controller: _tabs,
        children: [
          // ── Tab 1: Requests ────────────────────────────────────────
          RefreshIndicator(
            onRefresh: _loadRequests,
            child: _loadingRequests
                ? const Center(child: CircularProgressIndicator())
                : _allRequests.isEmpty
                    ? Center(child: Column(
                        mainAxisAlignment: MainAxisAlignment.center,
                        children: [
                          const Icon(Icons.inbox_outlined, size: 52, color: Colors.black12),
                          const SizedBox(height: 12),
                          Text(isNe ? 'कुनै अनुरोध छैन' : 'No requests yet',
                              style: const TextStyle(color: Colors.black38, fontSize: 15)),
                        ],
                      ))
                    : ListView(
                        padding: const EdgeInsets.all(16),
                        children: [
                          // Pending
                          if (_pendingRequests.isNotEmpty) ...[
                            _SectionHeader(
                              isNe ? 'पेन्डिंग अनुरोधहरू (${_pendingRequests.length})' 
                                   : 'Pending Requests (${_pendingRequests.length})',
                              Colors.orange.shade700,
                            ),
                            const SizedBox(height: 8),
                            ..._pendingRequests.map((r) => _RequestCard(
                              request: r as Map<String, dynamic>,
                              isNe: isNe,
                              acting: _actingOnRequest.contains(r['id'] as int? ?? 0),
                              onAct: (action) => _actOnRequest(r['id'] as int, action),
                            )),
                          ],
                          // History
                          if (_historyRequests.isNotEmpty) ...[
                            const SizedBox(height: 16),
                            _SectionHeader(
                              isNe ? 'इतिहास (${_historyRequests.length})' 
                                   : 'History (${_historyRequests.length})',
                              Colors.black38,
                            ),
                            const SizedBox(height: 8),
                            ..._historyRequests.map((r) => _RequestCard(
                              request: r as Map<String, dynamic>,
                              isNe: isNe,
                              acting: false,
                              onAct: null, // no actions for history
                            )),
                          ],
                        ],
                      ),
          ),

          // ── Tab 2: PIN Management ──────────────────────────────────
          SingleChildScrollView(
            padding: const EdgeInsets.all(16),
            child: Card(
              child: Padding(
                padding: const EdgeInsets.all(16),
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Row(children: [
                    Icon(Icons.pin, color: Colors.teal.shade700),
                    const SizedBox(width: 8),
                    Text(isNe ? 'ग्राहकलाई PIN दिनुहोस्' : 'Issue Customer PIN',
                        style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 15)),
                  ]),
                  const SizedBox(height: 8),
                  Text(
                    isNe
                        ? 'ग्राहकको पहिलो काम दर्ता भइसकेपछि मात्र PIN दिन सकिन्छ।'
                        : 'Customer must have at least one job logged before a PIN can be issued.',
                    style: const TextStyle(color: Colors.black54, fontSize: 12.5),
                  ),
                  const SizedBox(height: 16),
                  TextField(
                    controller: _phone,
                    keyboardType: TextInputType.phone,
                    decoration: InputDecoration(
                      labelText: isNe ? 'फोन नम्बर' : 'Phone number',
                      border: const OutlineInputBorder(),
                      prefixIcon: const Icon(Icons.phone),
                    ),
                  ),
                  const SizedBox(height: 10),
                  TextField(
                    controller: _pin,
                    keyboardType: TextInputType.number,
                    maxLength: 4,
                    obscureText: true,
                    decoration: InputDecoration(
                      labelText: isNe ? '४-अङ्कको PIN' : '4-digit PIN',
                      border: const OutlineInputBorder(),
                      counterText: '',
                      prefixIcon: const Icon(Icons.lock_outline),
                    ),
                  ),
                  const SizedBox(height: 12),
                  if (_issueResultMessage != null)
                    Container(
                      padding: const EdgeInsets.all(10),
                      margin: const EdgeInsets.only(bottom: 12),
                      decoration: BoxDecoration(
                        color: _issueSuccess == true
                            ? Colors.green.shade50 : Colors.red.shade50,
                        borderRadius: BorderRadius.circular(8),
                        border: Border.all(
                          color: _issueSuccess == true
                              ? Colors.green.shade200 : Colors.red.shade200),
                      ),
                      child: Row(children: [
                        Icon(
                          _issueSuccess == true ? Icons.check_circle_outline : Icons.error_outline,
                          color: _issueSuccess == true ? Colors.green.shade700 : Colors.red,
                          size: 18,
                        ),
                        const SizedBox(width: 8),
                        Expanded(child: Text(_issueResultMessage!,
                            style: TextStyle(
                              color: _issueSuccess == true ? Colors.green.shade700 : Colors.red,
                              fontSize: 13,
                            ))),
                      ]),
                    ),
                  SizedBox(
                    width: double.infinity, height: 46,
                    child: ElevatedButton.icon(
                      style: ElevatedButton.styleFrom(
                          backgroundColor: Colors.teal.shade700, foregroundColor: Colors.white),
                      icon: const Icon(Icons.key),
                      onPressed: _issuing ? null : _issuePin,
                      label: Text(_issuing
                          ? '...'
                          : (isNe ? 'PIN सेट गर्नुहोस्' : 'Set PIN')),
                    ),
                  ),
                ]),
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _SectionHeader extends StatelessWidget {
  final String title;
  final Color color;
  const _SectionHeader(this.title, this.color);
  @override
  Widget build(BuildContext context) => Text(title,
      style: TextStyle(fontWeight: FontWeight.bold, fontSize: 14, color: color));
}

class _RequestCard extends StatelessWidget {
  final Map<String, dynamic> request;
  final bool isNe;
  final bool acting;
  final void Function(String)? onAct;
  const _RequestCard({required this.request, required this.isNe,
      required this.acting, required this.onAct});

  @override
  Widget build(BuildContext context) {
    final map     = request;
    final reqId   = map['id'] as int? ?? 0;
    final status  = (map['status'] ?? 'pending').toString();
    final isPending = status == 'pending';
    final rColor  = switch (status) {
      'accepted' => Colors.green.shade700,
      'declined' => Colors.red.shade600,
      _          => Colors.orange.shade700,
    };
    final statusLabel = switch (status) {
      'accepted' => isNe ? 'स्वीकृत' : 'ACCEPTED',
      'declined' => isNe ? 'अस्वीकृत' : 'DECLINED',
      _          => isNe ? 'पेन्डिंग' : 'PENDING',
    };

    return Card(
      margin: const EdgeInsets.only(bottom: 10),
      elevation: isPending ? 2 : 0,
      color: status == 'declined' ? Colors.grey.shade100 : null,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(12),
        side: BorderSide(color: rColor.withOpacity(0.3), width: 1),
      ),
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Row(children: [
            Icon(
              map['job_type'] == 'transport' ? Icons.local_shipping : Icons.agriculture,
              color: status == 'declined' ? Colors.grey : rColor, size: 20,
            ),
            const SizedBox(width: 8),
            Expanded(child: Text(
              '${map['customer_name'] ?? 'Unknown'} — ${map['service'] ?? map['job_type'] ?? ''}',
              style: TextStyle(fontWeight: FontWeight.bold,
                  color: status == 'declined' ? Colors.grey : null),
            )),
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 7, vertical: 3),
              decoration: BoxDecoration(
                color: rColor.withOpacity(0.1),
                borderRadius: BorderRadius.circular(8),
                border: Border.all(color: rColor.withOpacity(0.4)),
              ),
              child: Text(statusLabel,
                  style: TextStyle(color: rColor, fontSize: 10, fontWeight: FontWeight.bold)),
            ),
          ]),
          if (map['customer_phone'] != null) ...[
            const SizedBox(height: 6),
            Text('📞 ${map['customer_phone']}',
                style: const TextStyle(fontSize: 12.5, color: Colors.black54)),
          ],
          if (map['preferred_date'] != null) ...[
            const SizedBox(height: 2),
            Text('📅 ${map['preferred_date']}',
                style: const TextStyle(fontSize: 12.5, color: Colors.black54)),
          ],
          if ((map['notes'] ?? '').toString().isNotEmpty) ...[
            const SizedBox(height: 2),
            Text('📝 ${map['notes']}',
                style: const TextStyle(fontSize: 12.5, color: Colors.black54)),
          ],
          if (isPending && onAct != null) ...[
            const SizedBox(height: 10),
            Row(mainAxisAlignment: MainAxisAlignment.end, children: [
              if (acting)
                const SizedBox(width: 20, height: 20,
                    child: CircularProgressIndicator(strokeWidth: 2))
              else ...[
                OutlinedButton.icon(
                  icon: const Icon(Icons.close, size: 16),
                  label: Text(isNe ? 'अस्वीकार' : 'Decline'),
                  style: OutlinedButton.styleFrom(
                    foregroundColor: Colors.red,
                    side: const BorderSide(color: Colors.red),
                    padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
                    textStyle: const TextStyle(fontSize: 12.5),
                  ),
                  onPressed: () => onAct!('decline_job_request'),
                ),
                const SizedBox(width: 8),
                ElevatedButton.icon(
                  icon: const Icon(Icons.check, size: 16),
                  label: Text(isNe ? 'स्वीकार' : 'Accept'),
                  style: ElevatedButton.styleFrom(
                    backgroundColor: Colors.teal.shade700, foregroundColor: Colors.white,
                    padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
                    textStyle: const TextStyle(fontSize: 12.5),
                  ),
                  onPressed: () => onAct!('accept_job_request'),
                ),
              ],
            ]),
          ],
        ]),
      ),
    );
  }
}
