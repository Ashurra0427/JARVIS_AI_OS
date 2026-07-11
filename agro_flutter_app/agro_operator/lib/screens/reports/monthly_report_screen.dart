// lib/screens/reports/monthly_report_screen.dart
//
// Monthly summary screen for the JARVIS AGRO operator app.
//
// What it shows:
//   • Month/year picker (default: current month)
//   • Summary cards  — total jobs, completed, revenue, profit
//   • P&L breakdown  — revenue, fuel, other expenses, net profit
//   • Jobs-per-service breakdown chart (horizontal bars)
//   • Day-by-day revenue list (collapsible)
//   • Excel export button (fires monthly_report action → AGRO_AGENT)
//
// Wire-up:
//   • Routes:  routes.dart already imports and registers /reports/monthly
//   • Provider: JobProvider.requestMonthlyReport(year, month) already exists
//   • WS action: 'monthly_report' with {year, month} → server returns agro_result
//     with action:'monthly_report' and data:{stats:{...}, daily:[...], breakdown:[...]}
//
// Data contract from AGRO_AGENT (monthly_report response):
//   data = {
//     'stats': {
//       'year': int,  'month': int,
//       'total_jobs': int,  'completed_jobs': int, 'pending_jobs': int,
//       'revenue': double,  'fuel_cost': double,
//       'other_expenses': double,  'total_expenses': double,  'profit': double,
//     },
//     'daily': [            // one per calendar day that had activity
//       {'date': '2025-06-01', 'jobs': int, 'revenue': double, 'profit': double}
//     ],
//     'breakdown': [        // one per service type
//       {'service': 'Ploughing', 'count': int, 'revenue': double}
//     ],
//     'file_path': 'path/to/export.xlsx',  // only when export=true
//   }

import 'dart:async';
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import '../../providers/job_provider.dart';
import '../../providers/language_provider.dart';
import '../../services/ws_service.dart';
import '../../services/report_download_service.dart';
import '../../utils/bs_date_utils.dart';

// ── Screen ───────────────────────────────────────────────────────────────────

class MonthlyReportScreen extends StatefulWidget {
  const MonthlyReportScreen({super.key});
  @override
  State<MonthlyReportScreen> createState() => _MonthlyReportScreenState();
}

class _MonthlyReportScreenState extends State<MonthlyReportScreen> {
  int _year  = DateTime.now().year;
  int _month = DateTime.now().month;

  Map<String, dynamic>? _stats;
  List<Map<String, dynamic>> _daily     = [];
  List<Map<String, dynamic>> _breakdown = [];

  bool _loading   = false;
  bool _exporting = false;
  String? _exportMessage;
  bool _showDaily = false;   // collapsible day-by-day section

  StreamSubscription? _sub;

  // ── Lifecycle ─────────────────────────────────────────────────────────

  @override
  void initState() {
    super.initState();
    final ws = context.read<WsService>();
    _sub = ws.stream.listen(_onMessage);
    _fetch();
  }

  @override
  void dispose() {
    _sub?.cancel();
    super.dispose();
  }

  // ── WS handler ───────────────────────────────────────────────────────

  void _onMessage(Map<String, dynamic> msg) {
    if (msg['type'] != 'agro_result') return;
    final action = msg['action'] as String?;
    if (action != 'monthly_report') return;

    final data = msg['data'] as Map<String, dynamic>? ?? {};
    if (!mounted) return;

    final s         = data['stats'] as Map<String, dynamic>?;
    final daily     = (data['daily']     as List? ?? []).cast<Map<String, dynamic>>();
    final breakdown = (data['breakdown'] as List? ?? []).cast<Map<String, dynamic>>();
    final filePath  = data['file_path'] as String?;

    setState(() {
      _loading   = false;
      _exporting = false;
      if (s != null) {
        _stats     = s;
        _daily     = daily;
        _breakdown = breakdown;
      }
      if (filePath != null) {
        final isNe = context.read<LanguageProvider>().isNepali;
        _exportMessage = isNe ? 'फोनमा डाउनलोड हुँदैछ…' : 'Downloading to phone…';
      }
    });

    if (filePath != null) {
      _downloadToPhone(context.read<LanguageProvider>().isNepali);
    }
  }

  Future<void> _downloadToPhone(bool isNe) async {
    try {
      final period = '${_year.toString().padLeft(4, '0')}-${_month.toString().padLeft(2, '0')}';
      await ReportDownloadService.downloadAndOpenMonthly(period);
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

  // ── Actions ───────────────────────────────────────────────────────────

  void _fetch() {
    setState(() { _loading = true; _stats = null; _daily = []; _breakdown = []; _exportMessage = null; });
    context.read<JobProvider>().requestMonthlyReport(_year, _month);
  }

  void _export() {
    setState(() { _exporting = true; _exportMessage = null; });
    // Send again but server knows to generate Excel when export flag set
    context.read<WsService>().sendAgroAction('monthly_report', {
      'year':   _year,
      'month':  _month,
      'export': true,
    });
  }

  Future<void> _pickMonth() async {
    // Simple year/month picker using two ListWheelScrollViews in a dialog
    int tempYear  = _year;
    int tempMonth = _month;

    await showDialog<void>(
      context: context,
      builder: (ctx) {
        final isNe = context.read<LanguageProvider>().isNepali;
        return AlertDialog(
          title: Text(isNe ? 'महिना छान्नुस्' : 'Pick Month'),
          content: SizedBox(
            height: 160,
            child: StatefulBuilder(
              builder: (ctx2, setS) => Row(
                children: [
                  // Year
                  Expanded(
                    child: ListWheelScrollView.useDelegate(
                      itemExtent: 40,
                      perspective: 0.003,
                      onSelectedItemChanged: (i) => setS(() => tempYear = 2024 + i),
                      controller: FixedExtentScrollController(initialItem: tempYear - 2024),
                      childDelegate: ListWheelChildBuilderDelegate(
                        childCount: 12,
                        builder: (_, i) => Center(
                          child: Text('${2024 + i}',
                            style: TextStyle(
                              fontSize: 18,
                              fontWeight: (2024 + i) == tempYear
                                  ? FontWeight.bold : FontWeight.normal,
                            ),
                          ),
                        ),
                      ),
                    ),
                  ),
                  const SizedBox(width: 12),
                  // Month
                  Expanded(
                    child: ListWheelScrollView.useDelegate(
                      itemExtent: 40,
                      perspective: 0.003,
                      onSelectedItemChanged: (i) => setS(() => tempMonth = i + 1),
                      controller: FixedExtentScrollController(initialItem: tempMonth - 1),
                      childDelegate: ListWheelChildBuilderDelegate(
                        childCount: 12,
                        builder: (_, i) => Center(
                          child: Text(_monthName(i + 1, isNe),
                            style: TextStyle(
                              fontSize: 16,
                              fontWeight: (i + 1) == tempMonth
                                  ? FontWeight.bold : FontWeight.normal,
                            ),
                          ),
                        ),
                      ),
                    ),
                  ),
                ],
              ),
            ),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(ctx),
              child: Text(isNe ? 'रद्द' : 'Cancel'),
            ),
            ElevatedButton(
              style: ElevatedButton.styleFrom(
                backgroundColor: const Color(0xFF1565C0),
                foregroundColor: Colors.white,
              ),
              onPressed: () {
                setState(() { _year = tempYear; _month = tempMonth; });
                Navigator.pop(ctx);
                _fetch();
              },
              child: Text(isNe ? 'ठीक छ' : 'OK'),
            ),
          ],
        );
      },
    );
  }

  // ── Build ─────────────────────────────────────────────────────────────

  @override
  Widget build(BuildContext context) {
    final lang = context.watch<LanguageProvider>();
    final isNe = lang.isNepali;
    final headerColor = const Color(0xFF1565C0);

    return Scaffold(
      appBar: AppBar(
        backgroundColor: headerColor,
        foregroundColor: Colors.white,
        title: Text(isNe ? 'मासिक रिपोर्ट' : 'Monthly Report'),
        actions: [
          IconButton(
            icon: const Icon(Icons.calendar_month),
            tooltip: isNe ? 'महिना छान्नुस्' : 'Pick month',
            onPressed: _pickMonth,
          ),
        ],
      ),
      body: RefreshIndicator(
        onRefresh: () async => _fetch(),
        child: SingleChildScrollView(
          physics: const AlwaysScrollableScrollPhysics(),
          padding: const EdgeInsets.all(16),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              // Month header
              _MonthHeader(
                year: _year,
                month: _month,
                isNe: isNe,
                onTap: _pickMonth,
                color: headerColor,
              ),
              const SizedBox(height: 16),

              if (_loading)
                const Center(
                  child: Padding(
                    padding: EdgeInsets.all(48),
                    child: CircularProgressIndicator(),
                  ),
                )
              else if (_stats == null)
                Center(
                  child: Padding(
                    padding: const EdgeInsets.all(48),
                    child: Column(
                      children: [
                        Icon(Icons.bar_chart, size: 56, color: Colors.black12),
                        const SizedBox(height: 12),
                        Text(
                          isNe
                              ? 'यस महिनाको डाटा उपलब्ध छैन'
                              : 'No data for this month',
                          style: const TextStyle(color: Colors.black38, fontSize: 15),
                        ),
                      ],
                    ),
                  ),
                )
              else ...[
                // ── Summary cards ────────────────────────────────────────
                _SummaryGrid(stats: _stats!, isNe: isNe),
                const SizedBox(height: 16),

                // ── P&L card ─────────────────────────────────────────────
                _PnLCard(stats: _stats!, isNe: isNe),
                const SizedBox(height: 16),

                // ── Service breakdown ─────────────────────────────────────
                if (_breakdown.isNotEmpty) ...[
                  _SectionTitle(isNe ? 'सेवाअनुसार' : 'By Service'),
                  const SizedBox(height: 8),
                  _BreakdownBars(breakdown: _breakdown, isNe: isNe),
                  const SizedBox(height: 16),
                ],

                // ── Daily detail (collapsible) ────────────────────────────
                if (_daily.isNotEmpty) ...[
                  InkWell(
                    onTap: () => setState(() => _showDaily = !_showDaily),
                    borderRadius: BorderRadius.circular(8),
                    child: Padding(
                      padding: const EdgeInsets.symmetric(vertical: 4),
                      child: Row(
                        children: [
                          _SectionTitle(isNe ? 'दैनिक विवरण' : 'Daily Breakdown'),
                          const Spacer(),
                          Icon(
                            _showDaily ? Icons.expand_less : Icons.expand_more,
                            color: Colors.black45,
                          ),
                        ],
                      ),
                    ),
                  ),
                  if (_showDaily) ...[
                    const SizedBox(height: 8),
                    ..._daily.map((d) => _DayTile(day: d, isNe: isNe)),
                  ],
                  const SizedBox(height: 16),
                ],
              ],

              // ── Export button ────────────────────────────────────────────
              SizedBox(
                width: double.infinity,
                height: 50,
                child: ElevatedButton.icon(
                  style: ElevatedButton.styleFrom(
                    backgroundColor: headerColor,
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
                  onPressed: (_exporting || _stats == null) ? null : _export,
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
                      Expanded(
                        child: Text(_exportMessage!,
                            style: const TextStyle(color: Colors.green, fontSize: 13)),
                      ),
                    ],
                  ),
                ),
              ],

              const SizedBox(height: 24),
            ],
          ),
        ),
      ),
    );
  }

  // ── Helpers ───────────────────────────────────────────────────────────

  static String _monthName(int m, bool isNe) {
    const en = ['Jan','Feb','Mar','Apr','May','Jun',
                 'Jul','Aug','Sep','Oct','Nov','Dec'];
    const ne = ['जनवरी','फेब्रुअरी','मार्च','अप्रिल','मे','जुन',
                 'जुलाई','अगस्ट','सेप्टेम्बर','अक्टोबर','नोभेम्बर','डिसेम्बर'];
    return isNe ? ne[m - 1] : en[m - 1];
  }
}

// ── Sub-widgets ──────────────────────────────────────────────────────────────

class _MonthHeader extends StatelessWidget {
  final int year;
  final int month;
  final bool isNe;
  final VoidCallback onTap;
  final Color color;
  const _MonthHeader({
    required this.year, required this.month, required this.isNe,
    required this.onTap, required this.color,
  });

  static const _months = ['January','February','March','April','May','June',
                           'July','August','September','October','November','December'];
  static const _monthsNe = ['जनवरी','फेब्रुअरी','मार्च','अप्रिल','मे','जुन',
                              'जुलाई','अगस्ट','सेप्टेम्बर','अक्टोबर','नोभेम्बर','डिसेम्बर'];

  @override
  Widget build(BuildContext context) {
    final label = isNe
        ? '${_monthsNe[month - 1]} $year'
        : '${_months[month - 1]} $year';
    final firstOfMonth = DateTime(year, month, 1);
    final lastOfMonth  = DateTime(year, month + 1, 0);
    final bsStart = formatBsShort(firstOfMonth);
    final bsEnd   = formatBsShort(lastOfMonth);
    return InkWell(
      onTap: onTap,
      borderRadius: BorderRadius.circular(10),
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
        decoration: BoxDecoration(
          color: color.withOpacity(0.08),
          borderRadius: BorderRadius.circular(10),
          border: Border.all(color: color.withOpacity(0.3)),
        ),
        child: Row(
          children: [
            Icon(Icons.calendar_month, size: 18, color: color),
            const SizedBox(width: 8),
            Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(label,
                  style: TextStyle(
                    fontWeight: FontWeight.bold, fontSize: 16, color: color)),
                Text(
                  isNe ? '≈ वि.सं. $bsStart – $bsEnd' : '≈ BS $bsStart – $bsEnd',
                  style: const TextStyle(fontSize: 11, color: Colors.black45),
                ),
              ],
            ),
            const Spacer(),
            Icon(Icons.edit, size: 16, color: Colors.black38),
          ],
        ),
      ),
    );
  }
}

class _SectionTitle extends StatelessWidget {
  final String text;
  const _SectionTitle(this.text);
  @override
  Widget build(BuildContext context) => Text(
    text,
    style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 15),
  );
}

class _SummaryGrid extends StatelessWidget {
  final Map<String, dynamic> stats;
  final bool isNe;
  const _SummaryGrid({required this.stats, required this.isNe});

  @override
  Widget build(BuildContext context) {
    double _d(String k) => (stats[k] as num?)?.toDouble() ?? 0;
    int    _i(String k) => (stats[k] as num?)?.toInt()    ?? 0;

    return GridView.count(
      crossAxisCount: 2,
      shrinkWrap: true,
      physics: const NeverScrollableScrollPhysics(),
      crossAxisSpacing: 10,
      mainAxisSpacing: 10,
      childAspectRatio: 1.55,
      children: [
        _StatTile(
          label: isNe ? 'जम्मा काम' : 'Total Jobs',
          value: '${_i('total_jobs')}',
          icon: Icons.work_outline,
          color: Colors.blue.shade700,
        ),
        _StatTile(
          label: isNe ? 'सकिएका' : 'Completed',
          value: '${_i('completed_jobs')}',
          icon: Icons.check_circle_outline,
          color: Colors.green.shade700,
        ),
        _StatTile(
          label: isNe ? 'आम्दानी' : 'Revenue',
          value: 'Rs ${_d('revenue').toStringAsFixed(0)}',
          icon: Icons.currency_rupee,
          color: Colors.teal.shade700,
        ),
        _StatTile(
          label: isNe ? 'नाफा' : 'Net Profit',
          value: 'Rs ${_d('profit').toStringAsFixed(0)}',
          icon: Icons.trending_up,
          color: _d('profit') >= 0 ? Colors.indigo.shade700 : Colors.red,
        ),
      ],
    );
  }
}

class _StatTile extends StatelessWidget {
  final String label, value;
  final IconData icon;
  final Color color;
  const _StatTile({required this.label, required this.value,
                   required this.icon, required this.color});
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
  final Map<String, dynamic> stats;
  final bool isNe;
  const _PnLCard({required this.stats, required this.isNe});

  double _d(String k) => (stats[k] as num?)?.toDouble() ?? 0;

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
          _PnLRow(isNe ? 'आम्दानी'    : 'Revenue',
              'Rs ${_d('revenue').toStringAsFixed(0)}',        Colors.green),
          _PnLRow(isNe ? 'इन्धन खर्च' : 'Fuel Cost',
              '- Rs ${_d('fuel_cost').toStringAsFixed(0)}',    Colors.orange),
          _PnLRow(isNe ? 'अन्य खर्च'  : 'Other Expenses',
              '- Rs ${_d('other_expenses').toStringAsFixed(0)}', Colors.red.shade400),
          const Divider(),
          _PnLRow(
            isNe ? 'नाफा' : 'Net Profit',
            'Rs ${_d('profit').toStringAsFixed(0)}',
            _d('profit') >= 0 ? Colors.green.shade800 : Colors.red,
            bold: true,
          ),
        ],
      ),
    ),
  );
}

class _PnLRow extends StatelessWidget {
  final String label, value;
  final Color color;
  final bool bold;
  const _PnLRow(this.label, this.value, this.color, {this.bold = false});
  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.symmetric(vertical: 5),
    child: Row(children: [
      Text(label, style: TextStyle(
        fontSize: 13,
        color: bold ? Colors.black87 : Colors.black54,
        fontWeight: bold ? FontWeight.bold : FontWeight.normal,
      )),
      const Spacer(),
      Text(value, style: TextStyle(
        fontSize: 13, color: color,
        fontWeight: bold ? FontWeight.bold : FontWeight.w600,
      )),
    ]),
  );
}

// Horizontal bar chart for service breakdown
class _BreakdownBars extends StatelessWidget {
  final List<Map<String, dynamic>> breakdown;
  final bool isNe;
  const _BreakdownBars({required this.breakdown, required this.isNe});

  @override
  Widget build(BuildContext context) {
    final maxRevenue = breakdown
        .map((b) => (b['revenue'] as num?)?.toDouble() ?? 0)
        .fold<double>(0, (a, b) => b > a ? b : a);

    const barColors = [
      Color(0xFF1565C0), Color(0xFF2E7D32), Color(0xFF6A1B9A),
      Color(0xFFE65100), Color(0xFF00838F), Color(0xFF558B2F),
    ];

    return Card(
      elevation: 1,
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
      child: Padding(
        padding: const EdgeInsets.all(14),
        child: Column(
          children: breakdown.asMap().entries.map((entry) {
            final i       = entry.key;
            final b       = entry.value;
            final service = b['service'] as String? ?? '';
            final count   = (b['count'] as num?)?.toInt() ?? 0;
            final revenue = (b['revenue'] as num?)?.toDouble() ?? 0;
            final frac    = maxRevenue > 0 ? revenue / maxRevenue : 0.0;
            final color   = barColors[i % barColors.length];

            return Padding(
              padding: const EdgeInsets.symmetric(vertical: 5),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(children: [
                    Expanded(
                      child: Text(service,
                        style: const TextStyle(fontSize: 12, fontWeight: FontWeight.w600),
                        overflow: TextOverflow.ellipsis,
                      ),
                    ),
                    Text(
                      '$count ${isNe ? 'काम' : 'jobs'}  •  Rs ${revenue.toStringAsFixed(0)}',
                      style: TextStyle(fontSize: 11, color: color, fontWeight: FontWeight.bold),
                    ),
                  ]),
                  const SizedBox(height: 4),
                  LayoutBuilder(
                    builder: (_, constraints) => Stack(
                      children: [
                        Container(
                          height: 8,
                          width: constraints.maxWidth,
                          decoration: BoxDecoration(
                            color: color.withOpacity(0.12),
                            borderRadius: BorderRadius.circular(4),
                          ),
                        ),
                        Container(
                          height: 8,
                          width: constraints.maxWidth * frac,
                          decoration: BoxDecoration(
                            color: color,
                            borderRadius: BorderRadius.circular(4),
                          ),
                        ),
                      ],
                    ),
                  ),
                ],
              ),
            );
          }).toList(),
        ),
      ),
    );
  }
}

class _DayTile extends StatelessWidget {
  final Map<String, dynamic> day;
  final bool isNe;
  const _DayTile({required this.day, required this.isNe});

  @override
  Widget build(BuildContext context) {
    final date    = day['date'] as String? ?? '';
    final jobs    = (day['jobs'] as num?)?.toInt() ?? 0;
    final revenue = (day['revenue'] as num?)?.toDouble() ?? 0;
    final profit  = (day['profit'] as num?)?.toDouble() ?? 0;

    return Card(
      margin: const EdgeInsets.only(bottom: 4),
      elevation: 0,
      color: Colors.grey.shade50,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(8),
        side: BorderSide(color: Colors.grey.shade200),
      ),
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 8),
        child: Row(children: [
          Text(date,
            style: const TextStyle(fontWeight: FontWeight.w600, fontSize: 13)),
          const SizedBox(width: 12),
          Text('$jobs ${isNe ? 'काम' : 'jobs'}',
            style: const TextStyle(color: Colors.black54, fontSize: 12)),
          const Spacer(),
          Column(
            crossAxisAlignment: CrossAxisAlignment.end,
            children: [
              Text('Rs ${revenue.toStringAsFixed(0)}',
                style: const TextStyle(
                  fontWeight: FontWeight.bold,
                  color: Color(0xFF1B5E20),
                  fontSize: 13,
                )),
              Text(
                '${isNe ? 'नाफा' : 'profit'}: Rs ${profit.toStringAsFixed(0)}',
                style: TextStyle(
                  fontSize: 10,
                  color: profit >= 0 ? Colors.green.shade700 : Colors.red,
                ),
              ),
            ],
          ),
        ]),
      ),
    );
  }
}