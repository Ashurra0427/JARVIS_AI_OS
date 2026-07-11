// lib/screens/operators/operator_performance_screen.dart
//
// Operator payout tracking (new screen).
//
// Data source: the `operator_performance` WS action already existed
// server-side (agents/agro/analytics.py → agro_server.py) and worked —
// jobs done, revenue generated, and fuel cost consumed per operator for a
// given month — it just had no screen showing it. That part of this feature
// is "wire up existing data."
//
// Payout math: how much of that revenue an operator actually gets paid is a
// business decision (flat wage? % of job revenue? per-job rate?) that only
// you know, so it can't be hardcoded here. What this screen does instead:
// shows the raw stats always, and lets you optionally set a commission %
// per operator, stored locally on this device (SharedPreferences — nothing
// sent to the server, no schema change needed), which multiplies against
// revenue_generated to show an estimated payout. Defaults to 0% until you
// set it, so nobody sees a made-up payout number.
import 'dart:async';
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import '../../providers/language_provider.dart';
import '../../services/ws_service.dart';

class OperatorPerformanceScreen extends StatefulWidget {
  const OperatorPerformanceScreen({super.key});
  @override
  State<OperatorPerformanceScreen> createState() => _OperatorPerformanceScreenState();
}

class _OperatorPerformanceScreenState extends State<OperatorPerformanceScreen> {
  StreamSubscription? _sub;
  bool _loading = true;
  List<Map<String, dynamic>> _rows = [];
  Map<String, double> _commissionPct = {}; // operator_name -> %
  late DateTime _month;

  static const _prefsPrefix = 'commission_pct_';

  @override
  void initState() {
    super.initState();
    _month = DateTime.now();
    _loadCommissionPrefs().then((_) => _fetch());
  }

  @override
  void dispose() {
    _sub?.cancel();
    super.dispose();
  }

  Future<void> _loadCommissionPrefs() async {
    final prefs = await SharedPreferences.getInstance();
    final map = <String, double>{};
    for (final key in prefs.getKeys()) {
      if (key.startsWith(_prefsPrefix)) {
        final name = key.substring(_prefsPrefix.length);
        map[name] = prefs.getDouble(key) ?? 0;
      }
    }
    if (mounted) setState(() => _commissionPct = map);
  }

  Future<void> _setCommissionPct(String operatorName, double pct) async {
    setState(() => _commissionPct[operatorName] = pct);
    final prefs = await SharedPreferences.getInstance();
    await prefs.setDouble('$_prefsPrefix$operatorName', pct);
  }

  String get _monthStr =>
      '${_month.year.toString().padLeft(4, '0')}-${_month.month.toString().padLeft(2, '0')}';

  void _fetch() {
    setState(() => _loading = true);
    final ws = context.read<WsService>();
    _sub?.cancel();
    _sub = ws.stream.listen((msg) {
      if (msg['type'] == 'agro_result' && msg['action'] == 'operator_performance') {
        final data = msg['data'] as Map<String, dynamic>?;
        if (!mounted) return;
        setState(() {
          _rows = ((data?['operators'] as List?) ?? [])
              .map((r) => Map<String, dynamic>.from(r as Map))
              .toList();
          _loading = false;
        });
      }
    });
    ws.sendAgroAction('operator_performance', {'month': _monthStr});
    // Don't hang forever if the server's unreachable / offline.
    Future.delayed(const Duration(seconds: 6), () {
      if (mounted && _loading) setState(() => _loading = false);
    });
  }

  void _changeMonth(int delta) {
    setState(() => _month = DateTime(_month.year, _month.month + delta, 1));
    _fetch();
  }

  @override
  Widget build(BuildContext context) {
    final isNe = context.watch<LanguageProvider>().isNepali;
    final monthLabel = _monthName(_month.month, isNe);

    return Scaffold(
      backgroundColor: const Color(0xFFF5F7FA),
      appBar: AppBar(
        backgroundColor: const Color(0xFF003893),
        title: Text(isNe ? 'अपरेटर प्रदर्शन र भुक्तानी' : 'Operator Performance & Payouts',
            style: const TextStyle(color: Colors.white, fontSize: 16)),
        iconTheme: const IconThemeData(color: Colors.white),
      ),
      body: Column(
        children: [
          Container(
            color: Colors.white,
            padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 6),
            child: Row(
              mainAxisAlignment: MainAxisAlignment.center,
              children: [
                IconButton(icon: const Icon(Icons.chevron_left), onPressed: () => _changeMonth(-1)),
                Text('$monthLabel ${_month.year}',
                    style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 15)),
                IconButton(icon: const Icon(Icons.chevron_right), onPressed: () => _changeMonth(1)),
              ],
            ),
          ),
          if (_loading) const Expanded(child: Center(child: CircularProgressIndicator())),
          if (!_loading && _rows.isEmpty)
            Expanded(
              child: Center(
                child: Text(isNe ? 'यस महिना कुनै डाटा छैन' : 'No data for this month',
                    style: TextStyle(color: Colors.grey.shade600)),
              ),
            ),
          if (!_loading && _rows.isNotEmpty)
            Expanded(
              child: ListView.builder(
                padding: const EdgeInsets.all(12),
                itemCount: _rows.length,
                itemBuilder: (_, i) => _OperatorCard(
                  row: _rows[i],
                  isNe: isNe,
                  commissionPct: _commissionPct[_rows[i]['operator_name'] as String? ?? ''] ?? 0,
                  onCommissionChanged: (pct) =>
                      _setCommissionPct(_rows[i]['operator_name'] as String? ?? '', pct),
                ),
              ),
            ),
        ],
      ),
    );
  }

  String _monthName(int m, bool isNe) {
    const en = ['', 'January', 'February', 'March', 'April', 'May', 'June',
      'July', 'August', 'September', 'October', 'November', 'December'];
    const ne = ['', 'जनवरी', 'फेब्रुअरी', 'मार्च', 'अप्रिल', 'मे', 'जुन',
      'जुलाई', 'अगस्ट', 'सेप्टेम्बर', 'अक्टोबर', 'नोभेम्बर', 'डिसेम्बर'];
    return isNe ? ne[m] : en[m];
  }
}

class _OperatorCard extends StatelessWidget {
  final Map<String, dynamic> row;
  final bool isNe;
  final double commissionPct;
  final ValueChanged<double> onCommissionChanged;

  const _OperatorCard({
    required this.row,
    required this.isNe,
    required this.commissionPct,
    required this.onCommissionChanged,
  });

  @override
  Widget build(BuildContext context) {
    final name    = row['operator_name'] as String? ?? '—';
    final jobs    = (row['jobs_done'] as num?)?.toInt() ?? 0;
    final revenue = (row['revenue_generated'] as num?)?.toDouble() ?? 0;
    final fuel    = (row['fuel_consumed_cost'] as num?)?.toDouble() ?? 0;
    final payout  = revenue * commissionPct / 100;

    return Card(
      margin: const EdgeInsets.only(bottom: 10),
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
      child: Padding(
        padding: const EdgeInsets.all(14),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                CircleAvatar(
                  backgroundColor: const Color(0xFF003893).withOpacity(0.12),
                  child: Text(name.isNotEmpty ? name[0].toUpperCase() : '?',
                      style: const TextStyle(color: Color(0xFF003893), fontWeight: FontWeight.bold)),
                ),
                const SizedBox(width: 10),
                Expanded(
                  child: Text(name, style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 15)),
                ),
                Text('$jobs ${isNe ? "काम" : "jobs"}',
                    style: TextStyle(color: Colors.grey.shade700, fontSize: 13)),
              ],
            ),
            const Divider(height: 20),
            _statRow(isNe ? 'राजस्व उत्पन्न' : 'Revenue generated', 'Rs ${revenue.toStringAsFixed(0)}'),
            _statRow(isNe ? 'इन्धन लागत' : 'Fuel cost', 'Rs ${fuel.toStringAsFixed(0)}'),
            const SizedBox(height: 10),
            Row(
              children: [
                Expanded(
                  child: Text(
                    isNe ? 'कमिसन %' : 'Commission %',
                    style: TextStyle(color: Colors.grey.shade700, fontSize: 13),
                  ),
                ),
                SizedBox(
                  width: 90,
                  child: TextFormField(
                    initialValue: commissionPct == 0 ? '' : commissionPct.toStringAsFixed(1),
                    keyboardType: const TextInputType.numberWithOptions(decimal: true),
                    textAlign: TextAlign.right,
                    decoration: InputDecoration(
                      isDense: true,
                      hintText: '0',
                      suffixText: '%',
                      border: const OutlineInputBorder(),
                      contentPadding: const EdgeInsets.symmetric(horizontal: 8, vertical: 8),
                    ),
                    onFieldSubmitted: (v) {
                      final pct = double.tryParse(v) ?? 0;
                      onCommissionChanged(pct.clamp(0, 100));
                    },
                  ),
                ),
              ],
            ),
            if (commissionPct > 0) ...[
              const SizedBox(height: 8),
              Container(
                width: double.infinity,
                padding: const EdgeInsets.symmetric(vertical: 8, horizontal: 10),
                decoration: BoxDecoration(
                  color: Colors.green.shade50,
                  borderRadius: BorderRadius.circular(8),
                  border: Border.all(color: Colors.green.shade200),
                ),
                child: Row(
                  mainAxisAlignment: MainAxisAlignment.spaceBetween,
                  children: [
                    Text(isNe ? 'अनुमानित भुक्तानी' : 'Estimated payout',
                        style: TextStyle(color: Colors.green.shade800, fontSize: 13)),
                    Text('Rs ${payout.toStringAsFixed(0)}',
                        style: TextStyle(color: Colors.green.shade800, fontWeight: FontWeight.bold, fontSize: 15)),
                  ],
                ),
              ),
            ],
          ],
        ),
      ),
    );
  }

  Widget _statRow(String label, String value) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 2),
        child: Row(
          mainAxisAlignment: MainAxisAlignment.spaceBetween,
          children: [
            Text(label, style: TextStyle(color: Colors.grey.shade700, fontSize: 13)),
            Text(value, style: const TextStyle(fontWeight: FontWeight.w600, fontSize: 13)),
          ],
        ),
      );
}
