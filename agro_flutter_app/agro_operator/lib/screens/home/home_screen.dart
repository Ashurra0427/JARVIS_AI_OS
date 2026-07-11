// lib/screens/home/home_screen.dart
import 'dart:async';
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import '../../providers/job_provider.dart';
import '../../providers/language_provider.dart';
import '../../services/ws_service.dart';
import '../../services/voice_service.dart';
import '../../services/report_download_service.dart';
import '../../utils/bs_date_utils.dart';
import '../../widgets/stat_card.dart';
import '../../widgets/job_card.dart';
import '../../widgets/offline_banner.dart';
import '../../widgets/language_toggle.dart';
import '../../widgets/mic_button.dart';
import '../jobs/add_job_screen.dart';
import '../jobs/job_list_screen.dart';
import '../jobs/job_detail_screen.dart';   // ← FIX: import for tap navigation
import '../fuel/log_fuel_screen.dart';
import '../expense/log_expense_screen.dart';
import '../reports/daily_report_screen.dart';
import '../settings/settings_screen.dart';
import '../customers/customer_requests_screen.dart';
import '../billing/biller_screen.dart';
import '../operators/operator_performance_screen.dart';

class HomeScreen extends StatefulWidget {
  const HomeScreen({super.key});
  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> {
  StreamSubscription? _reportSub;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      context.read<JobProvider>().fetchTodayJobs();
      context.read<JobProvider>().fetchOutstanding();
    });
    // Listen for the auto-report scheduler's push (23:59 NPT daily /
    // month-end) so the operator gets a "your report is ready" banner
    // with a one-tap download, wherever they happen to be in the app.
    _reportSub = context.read<WsService>().stream.listen(_onWsMessage);
  }

  @override
  void dispose() {
    _reportSub?.cancel();
    super.dispose();
  }

  void _onWsMessage(Map<String, dynamic> msg) {
    if (!mounted) return;
    if (msg['type'] == 'new_job_request') {
      _onNewJobRequest(msg);
      return;
    }
    if (msg['type'] != 'report_ready') return;
    final reportType = msg['report_type'] as String? ?? 'daily';
    final period      = msg['period'] as String? ?? '';
    final isNe = context.read<LanguageProvider>().isNepali;
    final label = reportType == 'monthly'
        ? (isNe ? 'यस महिनाको रिपोर्ट तयार भयो' : 'This month\'s report is ready')
        : (isNe ? 'आजको रिपोर्ट तयार भयो' : 'Today\'s report is ready');

    ScaffoldMessenger.of(context).showSnackBar(SnackBar(
      content: Text(label),
      duration: const Duration(seconds: 10),
      action: SnackBarAction(
        label: isNe ? 'डाउनलोड' : 'Download',
        onPressed: () async {
          try {
            if (reportType == 'monthly') {
              await ReportDownloadService.downloadAndOpenMonthly(period);
            } else {
              await ReportDownloadService.downloadAndOpenDaily(period);
            }
          } catch (e) {
            if (!mounted) return;
            ScaffoldMessenger.of(context).showSnackBar(SnackBar(
              content: Text(isNe ? 'डाउनलोड असफल भयो: $e' : 'Download failed: $e'),
              backgroundColor: Colors.red,
            ));
          }
        },
      ),
    ));
  }

  int _newRequestBadge = 0;

  void _onNewJobRequest(Map<String, dynamic> msg) {
    final isNe = context.read<LanguageProvider>().isNepali;
    final customerName = msg['customer_name'] as String? ?? '';
    final service       = msg['service'] as String? ?? '';
    setState(() => _newRequestBadge++);

    final label = customerName.isNotEmpty
        ? (isNe ? '$customerName बाट नयाँ काम अनुरोध' : 'New job request from $customerName')
        : (isNe ? 'नयाँ काम अनुरोध आयो' : 'New job request received');

    ScaffoldMessenger.of(context).showSnackBar(SnackBar(
      content: Text(service.isNotEmpty ? '$label — $service' : label),
      duration: const Duration(seconds: 8),
      action: SnackBarAction(
        label: isNe ? 'हेर्नुस्' : 'View',
        onPressed: () {
          setState(() => _newRequestBadge = 0);
          Navigator.push(context,
              MaterialPageRoute(builder: (_) => const CustomerRequestsScreen()));
        },
      ),
    ));
  }

  @override
  Widget build(BuildContext context) {
    final lang = context.watch<LanguageProvider>();
    final jobs = context.watch<JobProvider>();
    final ws   = context.read<WsService>();
    final stats = jobs.todayStats;
    final isNe = lang.isNepali;

    final today = DateTime.now().toIso8601String().substring(0, 10);
    final todayJobs = jobs.jobs
        .where((j) => j.scheduledDate == today || j.scheduledDate == null)
        .toList();

    return Scaffold(
      backgroundColor: const Color(0xFFF5F7FA),
      appBar: AppBar(
        backgroundColor: const Color(0xFF003893),
        title: const Text(
          'JARVIS AGRO',
          style: TextStyle(
              color: Colors.white, fontWeight: FontWeight.bold, fontSize: 18),
        ),
        actions: [
          const LanguageToggle(),
          const SizedBox(width: 8),
          IconButton(
            icon: const Icon(Icons.settings, color: Colors.white),
            onPressed: () => Navigator.push(context,
                MaterialPageRoute(builder: (_) => const SettingsScreen())),
          ),
        ],
      ),
      body: Column(
        children: [
          // Offline banner
          StreamBuilder<bool>(
            stream: ws.connectionStream,
            initialData: ws.connected,
            builder: (_, snapshot) => OfflineBanner(
              connected: snapshot.data ?? ws.connected,
              queuedCount: ws.queuedCount,
            ),
          ),
          Expanded(
            child: RefreshIndicator(
              onRefresh: () async => jobs.fetchTodayJobs(),
              child: SingleChildScrollView(
                physics: const AlwaysScrollableScrollPhysics(),
                padding: const EdgeInsets.all(12),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    // Date header
                    Padding(
                      padding: const EdgeInsets.only(bottom: 12, left: 4),
                      child: Row(
                        crossAxisAlignment: CrossAxisAlignment.baseline,
                        textBaseline: TextBaseline.alphabetic,
                        children: [
                          Text(
                            '📅 ${formatBsLong(DateTime.now(), isNe: lang.isNepali)}',
                            style: const TextStyle(
                              fontSize: 14, color: Colors.black87,
                              fontWeight: FontWeight.w600),
                          ),
                          const SizedBox(width: 6),
                          Text(
                            lang.isNepali ? '($today ई.)' : '($today AD)',
                            style: const TextStyle(
                              fontSize: 12, color: Colors.black45,
                              fontWeight: FontWeight.w400),
                          ),
                        ],
                      ),
                    ),

                    // Outstanding dues banner — only shown when there's
                    // actually something owed, so it doesn't clutter the
                    // home screen on a clean day.
                    if (jobs.totalOutstanding > 0)
                      Padding(
                        padding: const EdgeInsets.only(bottom: 12),
                        child: InkWell(
                          borderRadius: BorderRadius.circular(10),
                          onTap: () => Navigator.push(context,
                              MaterialPageRoute(builder: (_) => const BillerScreen())),
                          child: Container(
                            width: double.infinity,
                            padding: const EdgeInsets.symmetric(
                                horizontal: 14, vertical: 12),
                            decoration: BoxDecoration(
                              color: Colors.red.shade50,
                              borderRadius: BorderRadius.circular(10),
                              border: Border.all(color: Colors.red.shade200),
                            ),
                            child: Row(
                              children: [
                                Icon(Icons.receipt_long,
                                    color: Colors.red.shade700, size: 20),
                                const SizedBox(width: 10),
                                Expanded(
                                  child: Text(
                                    isNe
                                        ? 'बाँकी रकम: Rs ${jobs.totalOutstanding.toStringAsFixed(0)}'
                                        : 'Outstanding dues: Rs ${jobs.totalOutstanding.toStringAsFixed(0)}',
                                    style: TextStyle(
                                      color: Colors.red.shade800,
                                      fontWeight: FontWeight.w600,
                                    ),
                                  ),
                                ),
                                Icon(Icons.chevron_right, color: Colors.red.shade700),
                              ],
                            ),
                          ),
                        ),
                      ),

                    // Stats row
                    GridView.count(
                      crossAxisCount: 2,
                      shrinkWrap: true,
                      physics: const NeverScrollableScrollPhysics(),
                      crossAxisSpacing: 10,
                      mainAxisSpacing: 10,
                      childAspectRatio: 1.7,
                      children: [
                        StatCard(
                          label: isNe ? 'आजका काम' : "Today's Jobs",
                          value: '${stats['total_jobs'] ?? 0}',
                          color: Colors.blue.shade700,
                          icon: Icons.work_outline,
                        ),
                        StatCard(
                          label: isNe ? 'सकिएका' : 'Completed',
                          value: '${stats['completed_jobs'] ?? 0}',
                          color: Colors.green.shade700,
                          icon: Icons.check_circle_outline,
                        ),
                        StatCard(
                          label: isNe ? 'आम्दानी' : 'Revenue',
                          value: 'Rs ${((stats['revenue'] ?? 0) as num).toStringAsFixed(0)}',
                          color: Colors.teal.shade700,
                          icon: Icons.currency_rupee,
                        ),
                        StatCard(
                          label: isNe ? 'नाफा' : 'Profit',
                          value: 'Rs ${((stats['profit'] ?? 0) as num).toStringAsFixed(0)}',
                          color: Colors.indigo.shade700,
                          icon: Icons.trending_up,
                        ),
                      ],
                    ),

                    const SizedBox(height: 16),

                    // Quick actions
                    Text(
                      isNe ? 'छिटो गर्नुहोस्' : 'Quick Actions',
                      style: const TextStyle(
                          fontSize: 15, fontWeight: FontWeight.bold),
                    ),
                    const SizedBox(height: 10),
                    SingleChildScrollView(
                      scrollDirection: Axis.horizontal,
                      child: Row(
                        children: [
                        _QuickAction(
                          icon: Icons.add_box_outlined,
                          label: isNe ? 'काम थप्नुस्' : 'Add Job',
                          color: const Color(0xFF003893),
                          onTap: () => Navigator.push(context,
                              MaterialPageRoute(builder: (_) => const AddJobScreen())),
                        ),
                        const SizedBox(width: 10),
                        _QuickAction(
                          icon: Icons.local_gas_station,
                          label: isNe ? 'इन्धन' : 'Fuel',
                          color: Colors.orange.shade700,
                          onTap: () => Navigator.push(context,
                              MaterialPageRoute(builder: (_) => const LogFuelScreen())),
                        ),
                        const SizedBox(width: 10),
                        _QuickAction(
                          icon: Icons.receipt_long,
                          label: isNe ? 'खर्च' : 'Expense',
                          color: Colors.red.shade700,
                          onTap: () => Navigator.push(context,
                              MaterialPageRoute(builder: (_) => const LogExpenseScreen())),
                        ),
                        const SizedBox(width: 10),
                        _QuickAction(
                          icon: Icons.bar_chart,
                          label: isNe ? 'रिपोर्ट' : 'Report',
                          color: Colors.green.shade700,
                          onTap: () => Navigator.push(context,
                              MaterialPageRoute(builder: (_) => const DailyReportScreen())),
                        ),
                        const SizedBox(width: 10),
                        _QuickAction(
                          icon: Icons.people_alt_outlined,
                          label: isNe ? 'ग्राहक' : 'Customers',
                          color: Colors.teal.shade700,
                          badgeCount: _newRequestBadge,
                          onTap: () {
                            setState(() => _newRequestBadge = 0);
                            Navigator.push(context,
                                MaterialPageRoute(builder: (_) => const CustomerRequestsScreen()));
                          },
                        ),
                        const SizedBox(width: 10),
                        _QuickAction(
                          icon: Icons.request_quote,
                          label: isNe ? 'बिलर' : 'Biller',
                          color: Colors.deepPurple.shade700,
                          onTap: () => Navigator.push(context,
                              MaterialPageRoute(builder: (_) => const BillerScreen())),
                        ),
                        const SizedBox(width: 10),
                        _QuickAction(
                          icon: Icons.payments_outlined,
                          label: isNe ? 'भुक्तानी' : 'Payouts',
                          color: Colors.indigo.shade700,
                          onTap: () => Navigator.push(context,
                              MaterialPageRoute(builder: (_) => const OperatorPerformanceScreen())),
                        ),
                        ],
                      ),
                    ),

                    const SizedBox(height: 20),

                    // Today's job list
                    Row(
                      children: [
                        Text(
                          isNe ? 'आजका काम' : "Today's Jobs",
                          style: const TextStyle(
                              fontSize: 15, fontWeight: FontWeight.bold),
                        ),
                        const Spacer(),
                        TextButton(
                          onPressed: () => Navigator.push(context,
                              MaterialPageRoute(builder: (_) => const JobListScreen())),
                          child: Text(isNe ? 'सबै हेर्नुस्' : 'View All'),
                        ),
                      ],
                    ),

                    if (jobs.loading)
                      const Center(child: Padding(
                        padding: EdgeInsets.all(20),
                        child: CircularProgressIndicator(),
                      ))
                    else if (todayJobs.isEmpty)
                      Center(
                        child: Padding(
                          padding: const EdgeInsets.all(30),
                          child: Column(
                            children: [
                              const Icon(Icons.inbox, size: 48, color: Colors.black26),
                              const SizedBox(height: 8),
                              Text(
                                isNe ? 'आज कुनै काम छैन' : 'No jobs today',
                                style: const TextStyle(color: Colors.black45),
                              ),
                            ],
                          ),
                        ),
                      )
                    else
                      // ── FIX: pass onTap so cards on the home screen are clickable ──
                      ...todayJobs.map((j) => JobCard(
                        job: j,
                        onTap: () => Navigator.push(
                          context,
                          MaterialPageRoute(
                            builder: (_) => JobDetailScreen(job: j),
                          ),
                        ),
                      )),
                  ],
                ),
              ),
            ),
          ),
        ],
      ),
      floatingActionButton: Row(
        mainAxisAlignment: MainAxisAlignment.end,
        children: [
          // Hold to speak → STT → prefills Add Job screen
          MicButton(
            voiceService: context.read<VoiceService>(),
            wsService: context.read<WsService>(),
            sttPipeline: lang.sttPipeline,
            isNepali: isNe,
            onResult: (text) {
              ScaffoldMessenger.of(context).showSnackBar(
                SnackBar(
                  content: Text('🎤 $text'),
                  duration: const Duration(seconds: 4),
                  action: SnackBarAction(
                    label: isNe ? 'थप्नुस्' : 'Add Job',
                    onPressed: () => Navigator.push(
                      context,
                      MaterialPageRoute(
                        builder: (_) => AddJobScreen(voiceNote: text),
                      ),
                    ),
                  ),
                ),
              );
            },
          ),
          const SizedBox(width: 12),
          FloatingActionButton.extended(
            heroTag: 'new_job_fab',
            backgroundColor: const Color(0xFF003893),
            icon: const Icon(Icons.add, color: Colors.white),
            label: Text(
              isNe ? 'नयाँ काम' : 'New Job',
              style: const TextStyle(color: Colors.white),
            ),
            onPressed: () => Navigator.push(
              context,
              MaterialPageRoute(builder: (_) => const AddJobScreen()),
            ),
          ),
        ],
      ),
    );
  }
}

class _QuickAction extends StatelessWidget {
  final IconData icon;
  final String label;
  final Color color;
  final VoidCallback onTap;
  final int badgeCount;
  const _QuickAction({
    required this.icon,
    required this.label,
    required this.color,
    required this.onTap,
    this.badgeCount = 0,
  });

  @override
  Widget build(BuildContext context) {
    return Expanded(
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(12),
        child: Container(
          padding: const EdgeInsets.symmetric(vertical: 12),
          decoration: BoxDecoration(
            color: color.withOpacity(0.1),
            borderRadius: BorderRadius.circular(12),
            border: Border.all(color: color.withOpacity(0.3)),
          ),
          child: Stack(
            clipBehavior: Clip.none,
            children: [
              Column(
                children: [
                  Icon(icon, color: color, size: 26),
                  const SizedBox(height: 4),
                  Text(
                    label,
                    style: TextStyle(
                        color: color, fontWeight: FontWeight.w600, fontSize: 11),
                    textAlign: TextAlign.center,
                  ),
                ],
              ),
              if (badgeCount > 0)
                Positioned(
                  top: -6,
                  right: -2,
                  child: Container(
                    padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                    decoration: BoxDecoration(
                      color: Colors.red.shade700,
                      borderRadius: BorderRadius.circular(10),
                    ),
                    constraints: const BoxConstraints(minWidth: 18),
                    child: Text(
                      badgeCount > 9 ? '9+' : '$badgeCount',
                      textAlign: TextAlign.center,
                      style: const TextStyle(
                        color: Colors.white,
                        fontSize: 10,
                        fontWeight: FontWeight.bold,
                      ),
                    ),
                  ),
                ),
            ],
          ),
        ),
      ),
    );
  }
}
