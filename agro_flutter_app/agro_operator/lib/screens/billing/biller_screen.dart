// lib/screens/billing/biller_screen.dart
//
// Biller — outstanding dues, grouped into ONE bill per customer.
//
// A customer can have several unpaid completed jobs (e.g. ploughing last
// week, a transport job yesterday). Those used to show up as separate
// cards with no link between them. They're now grouped by customer_id
// server-side (see analytics.outstanding_balances), so this screen shows
// one card per customer with the combined total and an itemized list of
// every job — with its date and service — that makes up the bill. Tap the
// customer's name (or the "View bill" row) to expand that itemized list.
//
// Two distinct ways to clear a balance, kept from the original design:
//
//   • "Collect Payment" — records real cash received. Goes through
//     JobProvider.recordPaymentForBill(), which applies the amount across
//     the customer's unpaid jobs oldest-first and maps to the server's
//     record_payment action per job (adds to advance_paid and recomputes
//     balance_due from total_amount). This is the normal path.
//
//   • "Override" — a manual correction, NOT a payment, and deliberately
//     scoped to a single job (waiving/correcting a whole multi-job bill
//     at once would hide which specific job the correction belongs to).
//     Reachable per-job from the expanded bill. Requires a reason
//     (enforced client-side AND server-side) and is logged as a distinct
//     BALANCE_OVERRIDE audit entry on the backend rather than PAYMENT.

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import '../../providers/job_provider.dart';
import '../../providers/language_provider.dart';
import '../../utils/bs_date_utils.dart';

class BillerScreen extends StatefulWidget {
  const BillerScreen({super.key});
  @override
  State<BillerScreen> createState() => _BillerScreenState();
}

class _BillerScreenState extends State<BillerScreen> {
  // Which customer_id's bill is currently expanded (null = none / all collapsed).
  final Set<Object> _expanded = {};

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      context.read<JobProvider>().fetchOutstanding();
    });
  }

  void _showFeedback(JobProvider prov, bool isNe) {
    if (prov.billerMessage != null) {
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(prov.billerMessage!),
        backgroundColor: Colors.green,
      ));
      prov.clearBillerMessage();
    } else if (prov.billerError != null) {
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(prov.billerError!),
        backgroundColor: Colors.red,
      ));
      prov.clearBillerMessage();
    }
  }

  @override
  Widget build(BuildContext context) {
    final isNe = context.watch<LanguageProvider>().isNepali;
    final prov = context.watch<JobProvider>();

    WidgetsBinding.instance.addPostFrameCallback((_) => _showFeedback(prov, isNe));

    return Scaffold(
      appBar: AppBar(
        title: Text(isNe ? 'बिलर — बाँकी रकम' : 'Biller — Outstanding Dues'),
        backgroundColor: const Color(0xFF003893),
        foregroundColor: Colors.white,
      ),
      body: RefreshIndicator(
        onRefresh: () async => context.read<JobProvider>().fetchOutstanding(),
        child: Column(
          children: [
            _TotalHeader(total: prov.totalOutstanding, isNe: isNe),
            Expanded(
              child: prov.billerLoading && prov.outstanding.isEmpty
                  ? const Center(child: CircularProgressIndicator())
                  : prov.outstanding.isEmpty
                      ? _EmptyState(isNe: isNe)
                      : ListView.builder(
                          padding: const EdgeInsets.all(12),
                          itemCount: prov.outstanding.length,
                          itemBuilder: (_, i) {
                            final bill = prov.outstanding[i];
                            final key = bill['customer_id'] ??
                                bill['customer_name'] ??
                                i;
                            return _BillCard(
                              bill: bill,
                              isNe: isNe,
                              expanded: _expanded.contains(key),
                              onToggleExpanded: () => setState(() {
                                if (_expanded.contains(key)) {
                                  _expanded.remove(key);
                                } else {
                                  _expanded.add(key);
                                }
                              }),
                            );
                          },
                        ),
            ),
          ],
        ),
      ),
    );
  }
}

class _TotalHeader extends StatelessWidget {
  final double total;
  final bool isNe;
  const _TotalHeader({required this.total, required this.isNe});

  @override
  Widget build(BuildContext context) => Container(
    width: double.infinity,
    padding: const EdgeInsets.symmetric(vertical: 18, horizontal: 16),
    color: total > 0 ? Colors.red.shade50 : Colors.green.shade50,
    child: Column(
      children: [
        Text(
          isNe ? 'कुल बाँकी' : 'Total Outstanding',
          style: TextStyle(
            fontSize: 13,
            color: total > 0 ? Colors.red.shade700 : Colors.green.shade700,
          ),
        ),
        const SizedBox(height: 4),
        Text(
          'Rs ${total.toStringAsFixed(0)}',
          style: TextStyle(
            fontSize: 30,
            fontWeight: FontWeight.bold,
            color: total > 0 ? Colors.red.shade800 : Colors.green.shade800,
          ),
        ),
      ],
    ),
  );
}

class _EmptyState extends StatelessWidget {
  final bool isNe;
  const _EmptyState({required this.isNe});

  @override
  Widget build(BuildContext context) => Center(
    child: Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        const Text('🎉', style: TextStyle(fontSize: 48)),
        const SizedBox(height: 12),
        Text(
          isNe ? 'कुनै बाँकी छैन!' : 'No dues outstanding!',
          style: const TextStyle(fontSize: 16, color: Colors.black54),
        ),
      ],
    ),
  );
}

/// One customer's combined bill — may cover several jobs.
class _BillCard extends StatelessWidget {
  final Map<String, dynamic> bill;
  final bool isNe;
  final bool expanded;
  final VoidCallback onToggleExpanded;

  const _BillCard({
    required this.bill,
    required this.isNe,
    required this.expanded,
    required this.onToggleExpanded,
  });

  @override
  Widget build(BuildContext context) {
    final customer  = (bill['customer_name'] as String?) ?? (isNe ? 'ग्राहक' : 'Customer');
    final phone     = bill['customer_phone'] as String?;
    final total     = (bill['total_amount'] as num?)?.toDouble() ?? 0;
    final advance   = (bill['advance_paid'] as num?)?.toDouble() ?? 0;
    final balance   = (bill['balance_due'] as num?)?.toDouble() ?? 0;
    final jobs      = ((bill['jobs'] as List?) ?? [])
        .map((j) => j as Map<String, dynamic>)
        .toList();
    final jobsCount = jobs.length;

    return Card(
      margin: const EdgeInsets.only(bottom: 10),
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            // Tapping the name is how you "get the bill" — expands the
            // itemized job list below instead of navigating away, so the
            // Collect/Override actions stay right there with it.
            InkWell(
              onTap: onToggleExpanded,
              child: Row(
                children: [
                  Expanded(
                    child: Row(
                      children: [
                        Flexible(
                          child: Text(customer,
                              style: const TextStyle(
                                  fontWeight: FontWeight.bold,
                                  fontSize: 15,
                                  decoration: TextDecoration.underline)),
                        ),
                        const SizedBox(width: 4),
                        Icon(
                          expanded ? Icons.expand_less : Icons.expand_more,
                          size: 18,
                          color: Colors.black45,
                        ),
                      ],
                    ),
                  ),
                  Text('Rs ${balance.toStringAsFixed(0)}',
                      style: TextStyle(
                          fontWeight: FontWeight.bold,
                          fontSize: 16,
                          color: Colors.red.shade700)),
                ],
              ),
            ),
            if (phone != null && phone.isNotEmpty)
              Text(phone, style: const TextStyle(fontSize: 12, color: Colors.black54)),
            const SizedBox(height: 4),
            Text(
              jobsCount == 1
                  ? (isNe ? '१ काम' : '1 job')
                  : (isNe ? '$jobsCount काम' : '$jobsCount jobs'),
              style: const TextStyle(fontSize: 12, color: Colors.black45),
            ),
            const SizedBox(height: 8),
            Row(
              mainAxisAlignment: MainAxisAlignment.spaceBetween,
              children: [
                _MiniStat(isNe ? 'जम्मा' : 'Total', total),
                _MiniStat(isNe ? 'तिरेको' : 'Paid', advance),
                _MiniStat(isNe ? 'बाँकी' : 'Due', balance, highlight: true),
              ],
            ),
            if (expanded) ...[
              const Divider(height: 20),
              Text(
                isNe ? 'बिल विवरण' : 'Bill detail',
                style: const TextStyle(fontSize: 12, fontWeight: FontWeight.w600, color: Colors.black54),
              ),
              const SizedBox(height: 6),
              ...jobs.map((job) => _JobLine(job: job, isNe: isNe)),
            ],
            const SizedBox(height: 10),
            Row(
              children: [
                Expanded(
                  child: ElevatedButton.icon(
                    style: ElevatedButton.styleFrom(
                        backgroundColor: Colors.green.shade700,
                        foregroundColor: Colors.white),
                    icon: const Icon(Icons.payments, size: 18),
                    label: Text(isNe ? 'भुक्तानी लिनुस्' : 'Collect'),
                    onPressed: () => _showPaymentDialog(context, jobs, balance, isNe),
                  ),
                ),
                const SizedBox(width: 8),
                OutlinedButton.icon(
                  style: OutlinedButton.styleFrom(foregroundColor: Colors.orange.shade800),
                  icon: const Icon(Icons.edit_note, size: 18),
                  label: Text(isNe ? 'अधिलेखन' : 'Override'),
                  onPressed: jobsCount == 1
                      ? () => _showOverrideDialog(context, jobs.first['id'] as int, balance, isNe)
                      : () => _pickJobForOverride(context, jobs, isNe),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }

  void _showPaymentDialog(
    BuildContext context,
    List<Map<String, dynamic>> jobs,
    double balance,
    bool isNe,
  ) {
    final ctrl = TextEditingController(text: balance > 0 ? balance.toStringAsFixed(0) : '');
    showDialog(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text(isNe ? 'भुक्तानी रेकर्ड गर्नुस्' : 'Record Payment'),
        // (controller disposed via .then() below once this dialog closes)
        content: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            if (jobs.length > 1)
              Padding(
                padding: const EdgeInsets.only(bottom: 8),
                child: Text(
                  isNe
                      ? 'यो रकम पुरानो कामबाट सुरु गरेर लागू हुनेछ।'
                      : 'Applied to the oldest unpaid job first.',
                  style: const TextStyle(fontSize: 12, color: Colors.black54),
                ),
              ),
            TextField(
              controller: ctrl,
              keyboardType: const TextInputType.numberWithOptions(decimal: true),
              autofocus: true,
              decoration: InputDecoration(
                prefixText: 'Rs ',
                labelText: isNe ? 'रकम' : 'Amount',
              ),
            ),
          ],
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx),
            child: Text(isNe ? 'रद्द' : 'Cancel'),
          ),
          ElevatedButton(
            style: ElevatedButton.styleFrom(backgroundColor: Colors.green.shade700),
            onPressed: () {
              final amount = double.tryParse(ctrl.text.trim());
              if (amount == null || amount <= 0) return;
              Navigator.pop(ctx);
              context.read<JobProvider>().recordPaymentForBill(jobs, amount);
            },
            child: Text(isNe ? 'रेकर्ड गर्नुस्' : 'Record',
                style: const TextStyle(color: Colors.white)),
          ),
        ],
      ),
    ).then((_) => ctrl.dispose());
  }

  void _pickJobForOverride(
    BuildContext context,
    List<Map<String, dynamic>> jobs,
    bool isNe,
  ) {
    showModalBottomSheet(
      context: context,
      builder: (ctx) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Padding(
              padding: const EdgeInsets.all(12),
              child: Text(
                isNe ? 'कुन कामको बाँकी अधिलेखन गर्ने?' : 'Override which job\'s balance?',
                style: const TextStyle(fontWeight: FontWeight.w600),
              ),
            ),
            ...jobs.where((j) => ((j['balance_due'] as num?)?.toDouble() ?? 0) > 0).map((job) {
              final due = (job['balance_due'] as num?)?.toDouble() ?? 0;
              final date = job['scheduled_date'] as String?;
              final service = (job['service'] as String?) ?? '';
              return ListTile(
                title: Text(service),
                subtitle: date != null ? Text(formatBsShort(DateTime.parse(date))) : null,
                trailing: Text('Rs ${due.toStringAsFixed(0)}'),
                onTap: () {
                  Navigator.pop(ctx);
                  _showOverrideDialog(context, job['id'] as int, due, isNe);
                },
              );
            }),
          ],
        ),
      ),
    );
  }

  void _showOverrideDialog(BuildContext context, int jobId, double balance, bool isNe) {
    final balanceCtrl = TextEditingController(text: balance.toStringAsFixed(0));
    final reasonCtrl  = TextEditingController();
    showDialog(
      context: context,
      builder: (ctx) => StatefulBuilder(
        builder: (ctx, setDialogState) => AlertDialog(
          title: Text(isNe ? 'बाँकी अधिलेखन गर्नुस्' : 'Override Balance'),
          content: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Text(
                isNe
                    ? 'हालको बाँकी: Rs ${balance.toStringAsFixed(0)}'
                    : 'Current due: Rs ${balance.toStringAsFixed(0)}',
                style: const TextStyle(fontSize: 12, color: Colors.black54),
              ),
              const SizedBox(height: 8),
              TextField(
                controller: balanceCtrl,
                keyboardType: const TextInputType.numberWithOptions(decimal: true),
                decoration: InputDecoration(
                  prefixText: 'Rs ',
                  labelText: isNe ? 'नयाँ बाँकी' : 'New balance',
                ),
                onChanged: (_) => setDialogState(() {}),
              ),
              Align(
                alignment: Alignment.centerLeft,
                child: TextButton(
                  onPressed: () {
                    balanceCtrl.text = '0';
                    setDialogState(() {});
                  },
                  child: Text(isNe ? 'माफी दिनुस् (Rs 0)' : 'Write off (Rs 0)'),
                ),
              ),
              const SizedBox(height: 4),
              TextField(
                controller: reasonCtrl,
                maxLines: 2,
                decoration: InputDecoration(
                  labelText: isNe ? 'कारण (आवश्यक)' : 'Reason (required)',
                  hintText: isNe
                      ? 'जस्तै: ग्राहकले माफी माग्यो, गल्ती रकम'
                      : 'e.g. customer disputed, data-entry error',
                ),
                onChanged: (_) => setDialogState(() {}),
              ),
            ],
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(ctx),
              child: Text(isNe ? 'रद्द' : 'Cancel'),
            ),
            ElevatedButton(
              style: ElevatedButton.styleFrom(backgroundColor: Colors.orange.shade800),
              onPressed: reasonCtrl.text.trim().isEmpty
                  ? null
                  : () {
                      final newBalance = double.tryParse(balanceCtrl.text.trim());
                      if (newBalance == null || newBalance < 0) return;
                      Navigator.pop(ctx);
                      context
                          .read<JobProvider>()
                          .overrideBalance(jobId, newBalance, reasonCtrl.text.trim());
                    },
              child: Text(isNe ? 'अधिलेखन गर्नुस्' : 'Override',
                  style: const TextStyle(color: Colors.white)),
            ),
          ],
        ),
      ),
    ).then((_) {
      balanceCtrl.dispose();
      reasonCtrl.dispose();
    });
  }
}

/// Single job row shown inside an expanded bill — the "date of job
/// performed" the bill is made up of.
class _JobLine extends StatelessWidget {
  final Map<String, dynamic> job;
  final bool isNe;
  const _JobLine({required this.job, required this.isNe});

  @override
  Widget build(BuildContext context) {
    final service = (job['service'] as String?) ?? '';
    final date    = job['scheduled_date'] as String?;
    final total   = (job['total_amount'] as num?)?.toDouble() ?? 0;
    final due     = (job['balance_due'] as num?)?.toDouble() ?? 0;
    final operatorName = job['operator_name'] as String?;

    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 4),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(service, style: const TextStyle(fontSize: 13, fontWeight: FontWeight.w500)),
                Text(
                  [
                    if (date != null) '${formatBsShort(DateTime.parse(date))} ($date)',
                    if (operatorName != null && operatorName.isNotEmpty) operatorName,
                  ].join('  •  '),
                  style: const TextStyle(fontSize: 11, color: Colors.black45),
                ),
              ],
            ),
          ),
          Column(
            crossAxisAlignment: CrossAxisAlignment.end,
            children: [
              Text('Rs ${total.toStringAsFixed(0)}', style: const TextStyle(fontSize: 12, color: Colors.black54)),
              Text('Rs ${due.toStringAsFixed(0)} due',
                  style: TextStyle(fontSize: 12, fontWeight: FontWeight.w600, color: due > 0 ? Colors.red.shade700 : Colors.green.shade700)),
            ],
          ),
        ],
      ),
    );
  }
}

class _MiniStat extends StatelessWidget {
  final String label;
  final double value;
  final bool highlight;
  const _MiniStat(this.label, this.value, {this.highlight = false});

  @override
  Widget build(BuildContext context) => Column(
    children: [
      Text(label, style: const TextStyle(fontSize: 11, color: Colors.black45)),
      Text(
        'Rs ${value.toStringAsFixed(0)}',
        style: TextStyle(
          fontSize: 13,
          fontWeight: FontWeight.w600,
          color: highlight ? Colors.red.shade700 : Colors.black87,
        ),
      ),
    ],
  );
}
