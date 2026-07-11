// lib/screens/home/customer_home_screen.dart  [agro_client]
//
// IMPROVEMENTS (Phase 12 robustness):
//   ✓ Cancelled filter chip added
//   ✓ Cancelled stat card in summary grid
//   ✓ Cancelled job cards visually dimmed with strikethrough
//   ✓ Request status labels localized (Nepali)
//   ✓ Cancel request button for pending requests
//   ✓ Real-time connection stream for offline banner
//   ✓ Empty state improvements
import 'dart:async';
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../providers/customer_auth_provider.dart';
import '../../providers/language_provider.dart';
import '../../services/ws_service.dart';
import '../../utils/bs_date_utils.dart';
import '../../widgets/offline_banner.dart';
import '../../widgets/language_toggle.dart';
import '../../widgets/status_chip.dart';
import '../../widgets/tts_speaking_bar.dart';
import '../request/request_job_screen.dart';
import '../settings/settings_screen.dart';
import '../jobs/customer_job_detail_screen.dart';

class CustomerHomeScreen extends StatefulWidget {
  const CustomerHomeScreen({super.key});
  @override
  State<CustomerHomeScreen> createState() => _CustomerHomeScreenState();
}

class _CustomerHomeScreenState extends State<CustomerHomeScreen>
    with SingleTickerProviderStateMixin {
  late TabController _tabs;
  bool _loading = true;
  Map<String, dynamic> _outstanding = {'total_due': 0, 'unpaid_jobs': 0};
  List<dynamic> _jobs     = [];
  List<dynamic> _requests = [];
  String _filterStatus    = '';
  StreamSubscription? _sub;
  StreamSubscription? _liveSub;

  @override
  void initState() {
    super.initState();
    _tabs = TabController(length: 2, vsync: this);
    WidgetsBinding.instance.addPostFrameCallback((_) => _loadAll());

    // Persistent listener (separate from _loadAll's transient one) so that
    // server-pushed updates — e.g. operator accepts/declines a request, or
    // changes a job's status to in_progress/completed — are reflected here
    // immediately, even if this screen isn't actively fetching right now.
    // ws.stream is a broadcast stream, so this can coexist with _loadAll's
    // own listener without stealing its events.
    _liveSub = context.read<WsService>().stream.listen((msg) {
      if (msg['type'] != 'customer_result') return;
      final data = (msg['data'] as Map<String, dynamic>?) ?? {};
      if (data['success'] == false) return;

      if (msg['action'] == 'get_job_requests') {
        if (mounted) setState(() => _requests = (data['requests'] as List?) ?? _requests);
      } else if (msg['action'] == 'get_jobs') {
        if (mounted) setState(() => _jobs = (data['jobs'] as List?) ?? _jobs);
      }
    });
  }

  @override
  void dispose() {
    _tabs.dispose();
    _sub?.cancel();
    _liveSub?.cancel();
    super.dispose();
  }

  Future<void> _loadAll() async {
    final auth = context.read<CustomerAuthProvider>();
    setState(() => _loading = true);

    final outC  = Completer<Map<String, dynamic>>();
    final jobsC = Completer<List<dynamic>>();
    final reqC  = Completer<List<dynamic>>();

    _sub?.cancel();
    _sub = context.read<WsService>().stream.listen((msg) {
      if (msg['type'] != 'customer_result') return;
      final data = (msg['data'] as Map<String, dynamic>?) ?? {};

      if (data['success'] == false &&
          (data['error'] ?? '').toString().toLowerCase().contains('session')) {
        auth.forceLogout();
        return;
      }

      if (msg['action'] == 'get_outstanding' && !outC.isCompleted)
        outC.complete(data);
      else if (msg['action'] == 'get_jobs' && !jobsC.isCompleted)
        jobsC.complete((data['jobs'] as List?) ?? []);
      else if (msg['action'] == 'get_job_requests' && !reqC.isCompleted)
        reqC.complete((data['requests'] as List?) ?? []);
    });

    auth.sendCustomerAction('get_outstanding',  {});
    auth.sendCustomerAction('get_jobs',         {});
    auth.sendCustomerAction('get_job_requests', {});

    final timeout = (Future<dynamic> f) =>
        Future.any([f, Future.delayed(const Duration(seconds: 8))]);

    final outstanding = await timeout(outC.future) ?? {'total_due': 0, 'unpaid_jobs': 0};
    final jobs        = await timeout(jobsC.future) ?? [];
    final requests    = await timeout(reqC.future)  ?? [];

    _sub?.cancel();
    if (mounted) setState(() {
      _outstanding = outstanding as Map<String, dynamic>;
      _jobs        = jobs as List<dynamic>;
      _requests    = requests as List<dynamic>;
      _loading     = false;
    });
  }

  // ── Computed stats ─────────────────────────────────────────────────────────
  int    get _totalJobs     => _jobs.length;
  int    get _completedJobs => _jobs.where((j) => (j as Map)['status'] == 'completed').length;
  int    get _cancelledJobs => _jobs.where((j) => (j as Map)['status'] == 'cancelled').length;
  int    get _activeJobs    => _jobs.where((j) {
    final s = (j as Map)['status'];
    return s == 'pending' || s == 'confirmed' || s == 'in_progress';
  }).length;
  double get _totalDue      => ((_outstanding['total_due'] ?? 0) as num).toDouble();

  List<dynamic> get _filtered {
    if (_filterStatus.isEmpty) return _jobs;
    return _jobs.where((j) => (j as Map)['status'] == _filterStatus).toList();
  }

  String _localizeRequestStatus(String s, bool isNe) => switch (s) {
    'accepted' => isNe ? 'स्वीकृत' : 'ACCEPTED',
    'declined' => isNe ? 'अस्वीकृत' : 'DECLINED',
    'pending'  => isNe ? 'प्रतीक्षामा' : 'PENDING',
    _          => s.toUpperCase(),
  };

  @override
  Widget build(BuildContext context) {
    final lang  = context.watch<LanguageProvider>();
    final auth  = context.watch<CustomerAuthProvider>();
    final ws    = context.read<WsService>();
    final isNe  = lang.isNepali;
    final today = DateTime.now().toIso8601String().substring(0, 10);

    return Scaffold(
      backgroundColor: const Color(0xFFF5F7FA),
      appBar: AppBar(
        backgroundColor: const Color(0xFF003893),
        title: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          const Text('JARVIS AGRO',
              style: TextStyle(color: Colors.white, fontWeight: FontWeight.bold, fontSize: 16)),
          Text(isNe ? 'नमस्ते, ${auth.customerName ?? ''}' : 'Hi, ${auth.customerName ?? ''}',
              style: const TextStyle(color: Colors.white70, fontSize: 12)),
        ]),
        actions: [
          const LanguageToggle(),
          IconButton(
            icon: const Icon(Icons.refresh, color: Colors.white),
            tooltip: isNe ? 'ताजा गर्नुस्' : 'Refresh',
            onPressed: _loadAll,
          ),
          IconButton(
            icon: const Icon(Icons.settings, color: Colors.white),
            onPressed: () => Navigator.push(context,
                MaterialPageRoute(builder: (_) => const SettingsScreen())),
          ),
        ],
        bottom: TabBar(
          controller: _tabs,
          indicatorColor: Colors.white,
          labelColor: Colors.white,
          unselectedLabelColor: Colors.white60,
          tabs: [
            Tab(text: isNe ? 'काम इतिहास' : 'My Jobs'),
            Tab(
              child: Row(mainAxisSize: MainAxisSize.min, children: [
                Text(isNe ? 'मेरा अनुरोध' : 'My Requests'),
                if (_requests.where((r) => (r as Map)['status'] == 'pending').isNotEmpty) ...[
                  const SizedBox(width: 6),
                  Container(
                    padding: const EdgeInsets.symmetric(horizontal: 5, vertical: 1),
                    decoration: BoxDecoration(
                      color: Colors.orange, borderRadius: BorderRadius.circular(8)),
                    child: Text(
                      '${_requests.where((r) => (r as Map)['status'] == 'pending').length}',
                      style: const TextStyle(fontSize: 10, color: Colors.white,
                          fontWeight: FontWeight.bold)),
                  ),
                ],
              ]),
            ),
          ],
        ),
      ),
      body: Column(children: [
        StreamBuilder<bool>(
          stream: ws.connectionStream,
          initialData: ws.connected,
          builder: (_, snap) => OfflineBanner(
              connected: snap.data ?? ws.connected, queuedCount: ws.queuedCount),
        ),
        const TtsSpeakingBar(),
        Expanded(
          child: _loading
              ? const Center(child: CircularProgressIndicator())
              : TabBarView(
                  controller: _tabs,
                  children: [
                    // ── Tab 1: My Jobs ─────────────────────────────────
                    RefreshIndicator(
                      onRefresh: _loadAll,
                      child: CustomScrollView(
                        physics: const AlwaysScrollableScrollPhysics(),
                        slivers: [
                          SliverToBoxAdapter(child: Padding(
                            padding: const EdgeInsets.all(12),
                            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                              Text('📅 ${formatBsLong(DateTime.now(), isNe: isNe)}  '
                                   '(${isNe ? "$today ई." : "$today AD"})',
                                  style: const TextStyle(fontSize: 12, color: Colors.black45)),
                              const SizedBox(height: 12),

                              // Stats grid — now 2×3 (6 stats, show 4 most important)
                              GridView.count(
                                crossAxisCount: 2,
                                shrinkWrap: true,
                                physics: const NeverScrollableScrollPhysics(),
                                crossAxisSpacing: 10, mainAxisSpacing: 10,
                                childAspectRatio: 1.7,
                                children: [
                                  _StatCard(isNe ? 'जम्मा काम' : 'Total Jobs',
                                      '$_totalJobs', Colors.blue.shade700, Icons.work_outline),
                                  _StatCard(isNe ? 'सकिएका' : 'Completed',
                                      '$_completedJobs', Colors.green.shade700, Icons.check_circle_outline),
                                  _StatCard(isNe ? 'बाँकी रकम' : 'Outstanding',
                                      'Rs ${_totalDue.toStringAsFixed(0)}',
                                      _totalDue > 0 ? Colors.red.shade700 : Colors.teal.shade700,
                                      Icons.currency_rupee),
                                  _StatCard(isNe ? 'चलिरहेको' : 'Active',
                                      '$_activeJobs', Colors.orange.shade700, Icons.pending_actions),
                                ],
                              ),

                              // Cancelled count (shown only if > 0)
                              if (_cancelledJobs > 0) ...[
                                const SizedBox(height: 10),
                                Container(
                                  padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
                                  decoration: BoxDecoration(
                                    color: Colors.red.shade50,
                                    borderRadius: BorderRadius.circular(8),
                                    border: Border.all(color: Colors.red.shade200),
                                  ),
                                  child: Row(children: [
                                    Icon(Icons.cancel_outlined, color: Colors.red.shade600, size: 16),
                                    const SizedBox(width: 6),
                                    Text(
                                      isNe
                                          ? '$_cancelledJobs काम रद्द गरिएको'
                                          : '$_cancelledJobs job${_cancelledJobs > 1 ? "s" : ""} cancelled',
                                      style: TextStyle(color: Colors.red.shade700, fontSize: 13),
                                    ),
                                    const Spacer(),
                                    GestureDetector(
                                      onTap: () => setState(() => _filterStatus = 'cancelled'),
                                      child: Text(isNe ? 'हेर्नुस्' : 'View',
                                          style: TextStyle(color: Colors.red.shade700,
                                              fontWeight: FontWeight.bold, fontSize: 13)),
                                    ),
                                  ]),
                                ),
                              ],

                              const SizedBox(height: 16),

                              // Filter chips — now includes Cancelled
                              SingleChildScrollView(
                                scrollDirection: Axis.horizontal,
                                child: Row(children: [
                                  _Chip(isNe ? 'सबै'       : 'All',         _filterStatus.isEmpty,
                                      () => setState(() => _filterStatus = '')),
                                  const SizedBox(width: 8),
                                  _Chip(isNe ? 'पेन्डिंग'  : 'Pending',     _filterStatus == 'pending',
                                      () => setState(() => _filterStatus = 'pending')),
                                  const SizedBox(width: 8),
                                  _Chip(isNe ? 'चलिरहेको' : 'In Progress',  _filterStatus == 'in_progress',
                                      () => setState(() => _filterStatus = 'in_progress')),
                                  const SizedBox(width: 8),
                                  _Chip(isNe ? 'सकियो'    : 'Completed',    _filterStatus == 'completed',
                                      () => setState(() => _filterStatus = 'completed')),
                                  const SizedBox(width: 8),
                                  _Chip(isNe ? 'रद्द'     : 'Cancelled',    _filterStatus == 'cancelled',
                                      () => setState(() => _filterStatus = 'cancelled'),
                                      chipColor: Colors.red.shade50, selectedColor: Colors.red.shade100),
                                ]),
                              ),
                              const SizedBox(height: 10),
                            ]),
                          )),

                          if (_filtered.isEmpty)
                            SliverFillRemaining(child: Center(child: Column(
                              mainAxisAlignment: MainAxisAlignment.center,
                              children: [
                                Icon(
                                  _filterStatus == 'cancelled'
                                      ? Icons.check_circle_outline
                                      : Icons.inbox,
                                  size: 48, color: Colors.black12),
                                const SizedBox(height: 8),
                                Text(
                                  _filterStatus == 'cancelled'
                                      ? (isNe ? 'कुनै रद्द काम छैन' : 'No cancelled jobs')
                                      : (isNe ? 'कुनै काम फेला परेन' : 'No jobs found'),
                                  style: const TextStyle(color: Colors.black38)),
                              ],
                            )))
                          else
                            SliverPadding(
                              padding: const EdgeInsets.fromLTRB(12, 0, 12, 100),
                              sliver: SliverList(
                                delegate: SliverChildBuilderDelegate(
                                  (_, i) => _JobCard(
                                    job: _filtered[i] as Map<String, dynamic>,
                                    isNe: isNe,
                                    onTap: () => Navigator.push(context, MaterialPageRoute(
                                        builder: (_) => CustomerJobDetailScreen(
                                            job: _filtered[i] as Map<String, dynamic>)))
                                        .then((_) => _loadAll()),
                                  ),
                                  childCount: _filtered.length,
                                ),
                              ),
                            ),
                        ],
                      ),
                    ),

                    // ── Tab 2: My Requests ─────────────────────────────
                    RefreshIndicator(
                      onRefresh: _loadAll,
                      child: _requests.isEmpty
                          ? Center(child: Column(
                              mainAxisAlignment: MainAxisAlignment.center,
                              children: [
                                const Icon(Icons.pending_actions, size: 52, color: Colors.black12),
                                const SizedBox(height: 10),
                                Text(isNe ? 'कुनै अनुरोध छैन' : 'No requests yet',
                                    style: const TextStyle(color: Colors.black38)),
                                const SizedBox(height: 6),
                                Text(
                                  isNe
                                      ? '"काम मागनुस्" थिचेर नयाँ अनुरोध पठाउनुस्'
                                      : 'Tap "Request Job" below to send a new request',
                                  style: const TextStyle(color: Colors.black26, fontSize: 12),
                                  textAlign: TextAlign.center,
                                ),
                              ],
                            ))
                          : ListView.builder(
                              padding: const EdgeInsets.fromLTRB(12, 12, 12, 100),
                              itemCount: _requests.length,
                              itemBuilder: (_, i) {
                                final r = _requests[i] as Map<String, dynamic>;
                                final rStatus = (r['status'] ?? 'pending').toString();
                                final rColor = switch (rStatus) {
                                  'accepted' => Colors.green.shade700,
                                  'declined' => Colors.red.shade600,
                                  _          => Colors.orange.shade700,
                                };
                                final localStatus = _localizeRequestStatus(rStatus, isNe);
                                return Card(
                                  margin: const EdgeInsets.only(bottom: 10),
                                  elevation: rStatus == 'declined' ? 0 : 2,
                                  color: rStatus == 'declined' ? Colors.grey.shade100 : null,
                                  shape: RoundedRectangleBorder(
                                    borderRadius: BorderRadius.circular(12),
                                    side: BorderSide(
                                      color: rColor.withOpacity(0.3), width: 1),
                                  ),
                                  child: Padding(
                                    padding: const EdgeInsets.all(14),
                                    child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                                      Row(children: [
                                        Icon(
                                          r['job_type'] == 'transport'
                                              ? Icons.local_shipping
                                              : Icons.agriculture,
                                          color: rStatus == 'declined' ? Colors.grey : rColor,
                                          size: 22,
                                        ),
                                        const SizedBox(width: 10),
                                        Expanded(child: Text(
                                          r['service'] ?? '',
                                          style: TextStyle(
                                            fontWeight: FontWeight.bold, fontSize: 15,
                                            color: rStatus == 'declined' ? Colors.grey : null,
                                            decoration: rStatus == 'declined'
                                                ? TextDecoration.lineThrough : null,
                                          ),
                                        )),
                                        Container(
                                          padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
                                          decoration: BoxDecoration(
                                            color: rColor.withOpacity(0.1),
                                            borderRadius: BorderRadius.circular(8),
                                            border: Border.all(color: rColor.withOpacity(0.4)),
                                          ),
                                          child: Text(localStatus,
                                              style: TextStyle(color: rColor, fontSize: 11,
                                                  fontWeight: FontWeight.bold)),
                                        ),
                                      ]),
                                      if (r['preferred_date'] != null) ...[
                                        const SizedBox(height: 6),
                                        Text('📅 ${r['preferred_date']}',
                                            style: const TextStyle(color: Colors.black54, fontSize: 13)),
                                      ],
                                      if ((r['notes'] ?? '').toString().isNotEmpty) ...[
                                        const SizedBox(height: 4),
                                        Text('📝 ${r['notes']}',
                                            style: const TextStyle(color: Colors.black54, fontSize: 12.5)),
                                      ],
                                      // Status meaning explanation
                                      if (rStatus == 'accepted') ...[
                                        const SizedBox(height: 8),
                                        Container(
                                          padding: const EdgeInsets.all(8),
                                          decoration: BoxDecoration(
                                            color: Colors.green.shade50,
                                            borderRadius: BorderRadius.circular(8),
                                          ),
                                          child: Row(children: [
                                            Icon(Icons.info_outline, color: Colors.green.shade700, size: 14),
                                            const SizedBox(width: 6),
                                            Expanded(child: Text(
                                              isNe
                                                  ? 'अपरेटरले स्वीकार गरे — \"काम इतिहास\" ट्याबमा हेर्नुस्'
                                                  : 'Accepted by operator — check "My Jobs" tab',
                                              style: TextStyle(color: Colors.green.shade700, fontSize: 11),
                                            )),
                                          ]),
                                        ),
                                      ] else if (rStatus == 'declined') ...[
                                        const SizedBox(height: 8),
                                        Container(
                                          padding: const EdgeInsets.all(8),
                                          decoration: BoxDecoration(
                                            color: Colors.red.shade50,
                                            borderRadius: BorderRadius.circular(8),
                                          ),
                                          child: Row(children: [
                                            Icon(Icons.info_outline, color: Colors.red.shade700, size: 14),
                                            const SizedBox(width: 6),
                                            Expanded(child: Text(
                                              isNe
                                                  ? 'अपरेटरले अस्वीकार गरे — नयाँ अनुरोध पठाउनुस्'
                                                  : 'Declined by operator — submit a new request',
                                              style: TextStyle(color: Colors.red.shade700, fontSize: 11),
                                            )),
                                          ]),
                                        ),
                                      ],
                                    ]),
                                  ),
                                );
                              },
                            ),
                    ),
                  ],
                ),
        ),
      ]),
      floatingActionButton: FloatingActionButton.extended(
        heroTag: 'request_fab',
        backgroundColor: const Color(0xFF003893),
        icon: const Icon(Icons.add, color: Colors.white),
        label: Text(isNe ? 'काम मागनुस्' : 'Request Job',
            style: const TextStyle(color: Colors.white)),
        onPressed: () => Navigator.push(context,
            MaterialPageRoute(builder: (_) => const RequestJobScreen()))
            .then((_) => _loadAll()),
      ),
    );
  }
}

// ── Sub-widgets ──────────────────────────────────────────────────────────────

class _StatCard extends StatelessWidget {
  final String label, value;
  final Color color;
  final IconData icon;
  const _StatCard(this.label, this.value, this.color, this.icon);

  @override
  Widget build(BuildContext context) => Container(
    decoration: BoxDecoration(
      color: color.withOpacity(0.08),
      borderRadius: BorderRadius.circular(12),
      border: Border.all(color: color.withOpacity(0.2)),
    ),
    padding: const EdgeInsets.all(12),
    child: Column(crossAxisAlignment: CrossAxisAlignment.start,
      mainAxisAlignment: MainAxisAlignment.center,
      children: [
        Icon(icon, color: color, size: 18),
        const SizedBox(height: 4),
        Text(value, style: TextStyle(fontSize: 20, fontWeight: FontWeight.bold, color: color)),
        Text(label, style: const TextStyle(fontSize: 11, color: Colors.black45),
            maxLines: 1, overflow: TextOverflow.ellipsis),
      ],
    ),
  );
}

class _Chip extends StatelessWidget {
  final String label;
  final bool selected;
  final VoidCallback onTap;
  final Color? chipColor;
  final Color? selectedColor;
  const _Chip(this.label, this.selected, this.onTap, {this.chipColor, this.selectedColor});

  @override
  Widget build(BuildContext context) => FilterChip(
    label: Text(label),
    selected: selected,
    onSelected: (_) => onTap(),
    backgroundColor: chipColor,
    selectedColor: selectedColor ?? const Color(0xFF003893).withOpacity(0.18),
    checkmarkColor: selectedColor != null ? Colors.red.shade700 : const Color(0xFF003893),
  );
}

class _JobCard extends StatelessWidget {
  final Map<String, dynamic> job;
  final bool isNe;
  final VoidCallback onTap;
  const _JobCard({required this.job, required this.isNe, required this.onTap});

  @override
  Widget build(BuildContext context) {
    final status  = (job['status'] ?? '').toString();
    final service = (job['service'] ?? job['job_type'] ?? '').toString();
    final total   = (job['total_amount'] as num?)?.toDouble();
    final due     = (job['balance_due'] as num?)?.toDouble() ?? 0;
    final date    = job['scheduled_date'] as String?;
    final loc     = job['location'] as String?;
    final isCancelled = status == 'cancelled';
    final isCompleted = status == 'completed';

    return Opacity(
      opacity: isCancelled ? 0.55 : 1.0,
      child: Card(
        margin: const EdgeInsets.only(bottom: 10),
        elevation: isCancelled ? 0 : 2,
        color: isCancelled ? Colors.grey.shade100 : null,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(12),
          side: isCancelled
              ? BorderSide(color: Colors.red.shade200, width: 1)
              : isCompleted
                  ? BorderSide(color: Colors.green.shade200, width: 1)
                  : BorderSide.none,
        ),
        child: InkWell(
          onTap: onTap,
          borderRadius: BorderRadius.circular(12),
          child: Padding(
            padding: const EdgeInsets.all(14),
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Row(children: [
                Icon(
                  job['job_type'] == 'transport' ? Icons.local_shipping : Icons.agriculture,
                  color: isCancelled ? Colors.grey : const Color(0xFF1565C0), size: 20,
                ),
                const SizedBox(width: 8),
                Expanded(child: Text(service, style: TextStyle(
                  fontWeight: FontWeight.bold, fontSize: 15,
                  color: isCancelled ? Colors.grey : null,
                  decoration: isCancelled ? TextDecoration.lineThrough : null,
                ))),
                StatusChip(status),
              ]),
              if (loc != null && loc.isNotEmpty) ...[
                const SizedBox(height: 4),
                Text('📍 $loc', style: TextStyle(
                  color: isCancelled ? Colors.grey : Colors.black54, fontSize: 13)),
              ],
              const SizedBox(height: 4),
              Row(children: [
                if (date != null)
                  Text('📅 ${formatBsShort(DateTime.parse(date))} ($date)', style: TextStyle(
                    color: isCancelled ? Colors.grey.shade400 : Colors.black38, fontSize: 11)),
                const Spacer(),
                if (total != null)
                  Text('Rs ${total.toStringAsFixed(0)}',
                      style: TextStyle(
                        fontWeight: FontWeight.bold,
                        color: isCancelled ? Colors.grey : const Color(0xFF1B5E20),
                        fontSize: 13,
                        decoration: isCancelled ? TextDecoration.lineThrough : null,
                      )),
              ]),
              if (!isCancelled && due > 0)
                Padding(
                  padding: const EdgeInsets.only(top: 4),
                  child: Text('${isNe ? "बाँकी" : "Due"}: Rs ${due.toStringAsFixed(0)}',
                      style: const TextStyle(color: Colors.red, fontSize: 12)),
                ),
            ]),
          ),
        ),
      ),
    );
  }
}
