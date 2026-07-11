// lib/screens/reports/daily_report_screen.dart
//
// Shows a daily summary: job breakdown, revenue, expenses, profit.
// Lets operator request an Excel export from AGRO_AGENT.
//
import 'dart:async';
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import '../../providers/job_provider.dart';
import '../../providers/language_provider.dart';
import '../../services/ws_service.dart';
import '../../services/report_download_service.dart';
import '../../utils/bs_date_utils.dart';
import '../../widgets/bs_date_picker.dart';
import '../../models/daily_stats.dart';

class DailyReportScreen extends StatefulWidget {
  const DailyReportScreen({super.key});
  @override
  State<DailyReportScreen> createState() => _DailyReportScreenState();
}

class _DailyReportScreenState extends State<DailyReportScreen> {
  String _selectedDate = DateTime.now().toIso8601String().substring(0, 10);
  DailyStats? _stats;
  List<Map<String, dynamic>> _jobSummary = [];
  bool _loading = false;
  bool _exporting = false;
  String? _exportMessage;
  StreamSubscription? _sub;

  @override
  void initState() {
    super.initState();
    final ws = context.read<WsService>();
    _sub = ws.stream.listen(_onMessage);
    _fetchReport();
  }

  @override
  void dispose() {
    _sub?.cancel();
    super.dispose();
  }

  void _onMessage(Map<String, dynamic> msg) {
    if (msg['type'] != 'agro_result') return;
    final action = msg['action'] as String?;
    final data   = msg['data'] as Map<String, dynamic>? ?? {};

    if (action == 'get_stats') {
      final s = data['stats'] as Map<String, dynamic>?;
      if (s != null && mounted) {
        setState(() {
          _stats   = DailyStats.fromMap(s);
          _loading = false;
        });
      }
    } else if (action == 'get_jobs') {
      final jobs = (data['jobs'] as List? ?? [])
          .cast<Map<String, dynamic>>();
      if (mounted) setState(() => _jobSummary = jobs);
    } else if (action == 'daily_report') {
      if (mounted) {
        setState(() => _exporting = false);
        final lang = context.read<LanguageProvider>();
        final isNe = lang.isNepali;
        final filePath = data['file_path'] as String?;
        setState(() {
          _exportMessage = filePath != null
              ? (isNe ? 'फोनमा डाउनलोड हुँदैछ…' : 'Downloading to phone…')
              : (isNe ? 'Excel तयार भयो!' : 'Excel generated!');
        });
        if (filePath != null) {
          _downloadToPhone(isNe);
        } else {
          ScaffoldMessenger.of(context).showSnackBar(SnackBar(
            content: Text(_exportMessage!),
            backgroundColor: Colors.green,
            duration: const Duration(seconds: 4),
          ));
        }
      }
    }
  }

  Future<void> _downloadToPhone(bool isNe) async {
    try {
      await ReportDownloadService.downloadAndOpenDaily(_selectedDate);
      if (!mounted) return;
      setState(() => _exportMessage = isNe ? '✅ फोनमा सुरक्षित भयो' : '✅ Saved to phone');
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(_exportMessage!),
        backgroundColor: Colors.green,
        duration: const Duration(seconds: 4),
      ));
    } catch (e) {
      if (!mounted) return;
      setState(() => _exportMessage = isNe ? 'डाउनलोड असफल भयो' : 'Download failed');
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text('${_exportMessage!}: $e'),
        backgroundColor: Colors.red,
        duration: const Duration(seconds: 4),
      ));
    }
  }

  void _fetchReport() {
    setState(() { _loading = true; _stats = null; _jobSummary = []; });
    final ws = context.read<WsService>();
    ws.sendAgroAction('get_stats', {'date': _selectedDate});
    ws.sendAgroAction('get_jobs',  {'date': _selectedDate});
  }

  void _requestExcel() {
    setState(() { _exporting = true; _exportMessage = null; });
    context.read<WsService>().sendAgroAction('daily_report', {'date': _selectedDate});
  }

  Future<void> _pickDate() async {
    final isNe = context.read<LanguageProvider>().isNepali;
    final picked = await showBsDatePicker(
      context: context,
      initialDate: DateTime.parse(_selectedDate),
      firstDate: DateTime(2024),
      lastDate: DateTime.now(),
      isNe: isNe,
    );
    if (picked != null) {
      setState(() => _selectedDate = picked.toIso8601String().substring(0, 10));
      _fetchReport();
    }
  }

  @override
  Widget build(BuildContext context) {
    final lang = context.watch<LanguageProvider>();
    final isNe = lang.isNepali;

    return Scaffold(
      appBar: AppBar(
        backgroundColor: const Color(0xFF1B5E20),
        foregroundColor: Colors.white,
        title: Text(isNe ? 'दैनिक रिपोर्ट' : 'Daily Report'),
        actions: [
          IconButton(
            icon: const Icon(Icons.calendar_today),
            onPressed: _pickDate,
            tooltip: isNe ? 'मिति छान्नुस्' : 'Pick date',
          ),
        ],
      ),
      body: RefreshIndicator(
        onRefresh: () async => _fetchReport(),
        child: SingleChildScrollView(
          physics: const AlwaysScrollableScrollPhysics(),
          padding: const EdgeInsets.all(16),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              // Date header
              _DateHeader(date: _selectedDate, onTap: _pickDate, isNe: isNe),
              const SizedBox(height: 16),

              if (_loading)
                const Center(child: Padding(
                  padding: EdgeInsets.all(40),
                  child: CircularProgressIndicator(),
                ))
              else if (_stats == null)
                Center(child: Padding(
                  padding: const EdgeInsets.all(40),
                  child: Text(
                    isNe ? 'यस मितिको डाटा उपलब्ध छैन' : 'No data for this date',
                    style: const TextStyle(color: Colors.black45),
                  ),
                ))
              else ...[
                // Summary cards
                _SummaryGrid(stats: _stats!, isNe: isNe),
                const SizedBox(height: 20),

                // P&L card
                _PnLCard(stats: _stats!, isNe: isNe),
                const SizedBox(height: 20),

                // Jobs breakdown
                if (_jobSummary.isNotEmpty) ...[
                  Text(
                    isNe ? 'कामको विवरण' : 'Jobs Breakdown',
                    style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 15),
                  ),
                  const SizedBox(height: 8),
                  ..._jobSummary.map((j) => _JobSummaryTile(job: j, isNe: isNe)),
                  const SizedBox(height: 20),
                ],
              ],

              // Excel export button
              SizedBox(
                width: double.infinity,
                height: 50,
                child: ElevatedButton.icon(
                  style: ElevatedButton.styleFrom(
                    backgroundColor: const Color(0xFF1B5E20),
                    foregroundColor: Colors.white,
                    shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
                  ),
                  icon: _exporting
                      ? const SizedBox(
                          width: 18, height: 18,
                          child: CircularProgressIndicator(color: Colors.white, strokeWidth: 2))
                      : const Icon(Icons.download),
                  label: Text(
                    _exporting
                        ? (isNe ? 'बनाउँदैछ...' : 'Generating…')
                        : (isNe ? 'Excel निकाल्नुस्' : 'Export Excel'),
                    style: const TextStyle(fontSize: 15, fontWeight: FontWeight.bold),
                  ),
                  onPressed: _exporting ? null : _requestExcel,
                ),
              ),

              if (_exportMessage != null) ...[
                const SizedBox(height: 10),
                Container(
                  padding: const EdgeInsets.all(12),
                  decoration: BoxDecoration(
                    color: Colors.green.shade50,
                    borderRadius: BorderRadius.circular(8),
                    border: Border.all(color: Colors.green.shade200),
                  ),
                  child: Row(
                    children: [
                      const Icon(Icons.check_circle, color: Colors.green, size: 18),
                      const SizedBox(width: 8),
                      Expanded(child: Text(_exportMessage!,
                          style: const TextStyle(color: Colors.green, fontSize: 13))),
                    ],
                  ),
                ),
              ],

              const SizedBox(height: 20),
            ],
          ),
        ),
      ),
    );
  }
}

// ── Sub-widgets ─────────────────────────────────────────────────────────────

class _DateHeader extends StatelessWidget {
  final String date;
  final VoidCallback onTap;
  final bool isNe;
  const _DateHeader({required this.date, required this.onTap, required this.isNe});

  @override
  Widget build(BuildContext context) {
    final adDate = DateTime.parse(date);
    return InkWell(
    onTap: onTap,
    borderRadius: BorderRadius.circular(10),
    child: Container(
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
      decoration: BoxDecoration(
        color: const Color(0xFF1B5E20).withOpacity(0.08),
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: const Color(0xFF1B5E20).withOpacity(0.3)),
      ),
      child: Row(
        children: [
          const Icon(Icons.calendar_today, size: 18, color: Color(0xFF1B5E20)),
          const SizedBox(width: 8),
          Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                formatBsLong(adDate, isNe: isNe),
                style: const TextStyle(
                  fontWeight: FontWeight.bold,
                  fontSize: 15,
                  color: Color(0xFF1B5E20),
                ),
              ),
              Text(
                isNe ? '$date (ई.)' : '$date (AD)',
                style: const TextStyle(fontSize: 11, color: Colors.black45),
              ),
            ],
          ),
          const Spacer(),
          const Icon(Icons.edit, size: 16, color: Colors.black38),
        ],
      ),
    ),
  );
  }
}

class _SummaryGrid extends StatelessWidget {
  final DailyStats stats;
  final bool isNe;
  const _SummaryGrid({required this.stats, required this.isNe});

  @override
  Widget build(BuildContext context) {
    return GridView.count(
      crossAxisCount: 2,
      shrinkWrap: true,
      physics: const NeverScrollableScrollPhysics(),
      crossAxisSpacing: 10,
      mainAxisSpacing: 10,
      childAspectRatio: 1.6,
      children: [
        _StatTile(
          label: isNe ? 'जम्मा काम' : 'Total Jobs',
          value: '${stats.totalJobs}',
          icon: Icons.work_outline,
          color: Colors.blue.shade700,
        ),
        _StatTile(
          label: isNe ? 'सकिएका' : 'Completed',
          value: '${stats.completedJobs}',
          icon: Icons.check_circle_outline,
          color: Colors.green.shade700,
        ),
        _StatTile(
          label: isNe ? 'बाँकी' : 'Pending',
          value: '${stats.pendingJobs}',
          icon: Icons.pending_outlined,
          color: Colors.orange.shade700,
        ),
        _StatTile(
          label: isNe ? 'आम्दानी' : 'Revenue',
          value: 'Rs ${stats.revenue.toStringAsFixed(0)}',
          icon: Icons.currency_rupee,
          color: Colors.teal.shade700,
        ),
      ],
    );
  }
}

class _StatTile extends StatelessWidget {
  final String label;
  final String value;
  final IconData icon;
  final Color color;
  const _StatTile({
    required this.label, required this.value,
    required this.icon, required this.color,
  });

  @override
  Widget build(BuildContext context) => Card(
    elevation: 2,
    shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
    child: Padding(
      padding: const EdgeInsets.all(14),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(children: [
            Icon(icon, size: 18, color: color),
            const SizedBox(width: 6),
            Expanded(child: Text(label,
                style: TextStyle(color: color, fontSize: 12, fontWeight: FontWeight.w600),
                maxLines: 1, overflow: TextOverflow.ellipsis)),
          ]),
          const Spacer(),
          Text(value,
              style: const TextStyle(fontSize: 20, fontWeight: FontWeight.bold)),
        ],
      ),
    ),
  );
}

class _PnLCard extends StatelessWidget {
  final DailyStats stats;
  final bool isNe;
  const _PnLCard({required this.stats, required this.isNe});

  @override
  Widget build(BuildContext context) => Card(
    elevation: 2,
    shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
    child: Padding(
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            isNe ? 'आर्थिक सारांश' : 'Financial Summary',
            style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 14),
          ),
          const Divider(),
          _PnLRow(isNe ? 'आम्दानी' : 'Revenue',
              'Rs ${stats.revenue.toStringAsFixed(0)}', Colors.green),
          _PnLRow(isNe ? 'इन्धन खर्च' : 'Fuel Cost',
              '- Rs ${stats.fuelCost.toStringAsFixed(0)}', Colors.orange),
          _PnLRow(isNe ? 'अन्य खर्च' : 'Other Expenses',
              '- Rs ${stats.otherExpenses.toStringAsFixed(0)}', Colors.red.shade400),
          const Divider(),
          _PnLRow(
            isNe ? 'नाफा' : 'Net Profit',
            'Rs ${stats.profit.toStringAsFixed(0)}',
            stats.profit >= 0 ? Colors.green.shade800 : Colors.red,
            bold: true,
          ),
        ],
      ),
    ),
  );
}

class _PnLRow extends StatelessWidget {
  final String label;
  final String value;
  final Color color;
  final bool bold;
  const _PnLRow(this.label, this.value, this.color, {this.bold = false});

  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.symmetric(vertical: 5),
    child: Row(
      children: [
        Text(label, style: TextStyle(
            fontSize: 13,
            color: bold ? Colors.black87 : Colors.black54,
            fontWeight: bold ? FontWeight.bold : FontWeight.normal)),
        const Spacer(),
        Text(value, style: TextStyle(
            fontSize: 13,
            color: color,
            fontWeight: bold ? FontWeight.bold : FontWeight.w600)),
      ],
    ),
  );
}

class _JobSummaryTile extends StatelessWidget {
  final Map<String, dynamic> job;
  final bool isNe;
  const _JobSummaryTile({required this.job, required this.isNe});

  @override
  Widget build(BuildContext context) {
    final service  = job['service'] as String? ?? '';
    final customer = job['customer_name'] as String? ?? '';
    final status   = job['status'] as String? ?? '';
    final amount   = (job['total_amount'] as num?)?.toDouble();
    final isAgri   = (job['job_type'] as String?) == 'agriculture';

    return Card(
      margin: const EdgeInsets.only(bottom: 6),
      child: ListTile(
        leading: Icon(
          isAgri ? Icons.agriculture : Icons.local_shipping,
          color: const Color(0xFF1565C0),
        ),
        title: Text(service, style: const TextStyle(fontWeight: FontWeight.w600)),
        subtitle: customer.isNotEmpty ? Text(customer) : null,
        trailing: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          crossAxisAlignment: CrossAxisAlignment.end,
          children: [
            if (amount != null)
              Text('Rs ${amount.toStringAsFixed(0)}',
                  style: const TextStyle(fontWeight: FontWeight.bold,
                      color: Color(0xFF1B5E20), fontSize: 13)),
            _StatusDot(status),
          ],
        ),
        dense: true,
      ),
    );
  }
}

class _StatusDot extends StatelessWidget {
  final String status;
  const _StatusDot(this.status);

  static const _colors = {
    'pending':     Color(0xFFE67E22),
    'confirmed':   Color(0xFF2980B9),
    'in_progress': Color(0xFF8E44AD),
    'completed':   Color(0xFF27AE60),
    'cancelled':   Color(0xFFE74C3C),
  };

  @override
  Widget build(BuildContext context) => Row(
    mainAxisSize: MainAxisSize.min,
    children: [
      Container(
        width: 8, height: 8,
        decoration: BoxDecoration(
          shape: BoxShape.circle,
          color: _colors[status] ?? Colors.grey,
        ),
      ),
      const SizedBox(width: 4),
      Text(status.replaceAll('_', ' '),
          style: TextStyle(fontSize: 10, color: _colors[status] ?? Colors.grey)),
    ],
  );
}