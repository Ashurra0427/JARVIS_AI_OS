// lib/screens/jobs/customer_job_detail_screen.dart  [agro_client]
// Read-only job detail — improved with:
//   ✓ Cancelled/Completed banner
//   ✓ Job status timeline progress indicator
//   ✓ Payment due call-to-action
//   ✓ Proper cancelled visual treatment
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import '../../providers/language_provider.dart';
import '../../utils/bs_date_utils.dart';
import '../../widgets/status_chip.dart';

class CustomerJobDetailScreen extends StatelessWidget {
  final Map<String, dynamic> job;
  const CustomerJobDetailScreen({super.key, required this.job});

  @override
  Widget build(BuildContext context) {
    final isNe = context.watch<LanguageProvider>().isNepali;

    final status      = (job['status'] ?? 'pending').toString();
    final jobType     = (job['job_type'] ?? 'agriculture').toString();
    final service     = (job['service'] ?? '').toString();
    final location    = job['location'] as String?;
    final date        = job['scheduled_date'] as String?;
    final total       = (job['total_amount'] as num?)?.toDouble();
    final advance     = (job['advance_paid'] as num?)?.toDouble() ?? 0;
    final due         = (job['balance_due'] as num?)?.toDouble();
    final notes       = (job['notes'] ?? '').toString();
    final areaVal     = job['area_value'];
    final areaUnit    = job['area_unit'] as String?;
    final qtyVal      = job['quantity_value'];
    final qtyUnit     = job['quantity_unit'] as String?;
    final ratePerMin  = (job['rate_per_min'] as num?)?.toDouble();
    final timeValue   = (job['time_value'] as num?)?.toDouble();
    final material    = job['material'] as String?;

    final isPerMin    = jobType == 'agriculture' && ratePerMin != null;
    final isCancelled = status == 'cancelled';
    final isCompleted = status == 'completed';

    final appBarColor = isCancelled
        ? Colors.red.shade700
        : isCompleted
            ? Colors.green.shade700
            : const Color(0xFF003893);

    return Scaffold(
      appBar: AppBar(
        backgroundColor: appBarColor,
        foregroundColor: Colors.white,
        title: Text('${isNe ? "काम" : "Job"} #${job['id'] ?? ""}'),
        actions: [
          Padding(
            padding: const EdgeInsets.only(right: 12),
            child: StatusChip(status, showIcon: false),
          ),
        ],
      ),
      body: SingleChildScrollView(
        padding: const EdgeInsets.all(16),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [

          // ── Status banner ──────────────────────────────────────────
          if (isCancelled)
            _Banner(
              color: Colors.red,
              icon: Icons.cancel_outlined,
              text: isNe
                  ? 'यो काम रद्द गरिएको छ।'
                  : 'This job has been cancelled.',
            ),

          if (isCompleted)
            _Banner(
              color: Colors.green,
              icon: Icons.check_circle_outline,
              text: isNe
                  ? 'काम सफलतापूर्वक सकियो।'
                  : 'Job completed successfully.',
            ),

          // ── Status progress timeline (hidden for cancelled) ────────
          if (!isCancelled) ...[
            const SizedBox(height: 8),
            _StatusTimeline(status: status, isNe: isNe),
          ],

          const SizedBox(height: 16),

          // ── Details ────────────────────────────────────────────────
          _Section(isNe ? 'विवरण' : 'Details', [
            _Row(isNe ? 'किसिम' : 'Type',
                jobType == 'transport'
                    ? (isNe ? '🚛 यातायात' : '🚛 Transport')
                    : (isNe ? '🌾 कृषि' : '🌾 Agriculture')),
            _Row(isNe ? 'सेवा' : 'Service', service),
            if (material != null && material.isNotEmpty)
              _Row(isNe ? 'सामग्री' : 'Material', material),
            if (location != null && location.isNotEmpty)
              _Row(isNe ? 'ठेगाना' : 'Location', location),
            if (date != null)
              _Row(isNe ? 'मिति' : 'Date',
                  '${formatBsLong(DateTime.parse(date), isNe: isNe)} ($date)'),
          ]),

          const SizedBox(height: 12),

          // ── Billing ────────────────────────────────────────────────
          if (isPerMin) ...[
            _Section(isNe ? 'मिनेट बिलिङ' : 'Per-Minute Billing', [
              _Row(isNe ? 'दर' : 'Rate',
                  'Rs ${ratePerMin!.toStringAsFixed(0)} / min'),
              if (timeValue != null)
                _Row(isNe ? 'कुल समय' : 'Total Time',
                    '${timeValue.toStringAsFixed(1)} min'),
              if (total != null)
                _Row(isNe ? 'जम्मा' : 'Total',
                    'Rs ${total.toStringAsFixed(0)}'),
              _Row(isNe ? 'अग्रिम तिरिएको' : 'Advance Paid',
                  'Rs ${advance.toStringAsFixed(0)}'),
              if (due != null)
                _Row(isNe ? 'बाँकी' : 'Balance Due',
                    'Rs ${due.toStringAsFixed(0)}', highlight: due > 0),
            ]),
          ] else ...[
            _Section(isNe ? 'मात्रा / भुक्तानी' : 'Quantity / Financials', [
              if (areaVal != null)
                _Row(isNe ? 'क्षेत्रफल' : 'Area', '$areaVal ${areaUnit ?? ""}'),
              if (qtyVal != null)
                _Row(isNe ? 'ताली/मात्रा' : 'Taali / Qty',
                    '$qtyVal ${qtyUnit ?? ""}'),
              if (total != null)
                _Row(isNe ? 'जम्मा' : 'Total', 'Rs ${total.toStringAsFixed(0)}'),
              _Row(isNe ? 'अग्रिम तिरिएको' : 'Advance Paid',
                  'Rs ${advance.toStringAsFixed(0)}'),
              if (due != null)
                _Row(isNe ? 'बाँकी' : 'Balance Due',
                    'Rs ${due.toStringAsFixed(0)}', highlight: due > 0),
            ]),
          ],

          if (notes.isNotEmpty) ...[
            const SizedBox(height: 12),
            _Section(isNe ? 'नोट' : 'Notes', [
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 4),
                child: Text(notes, style: const TextStyle(color: Colors.black87)),
              ),
            ]),
          ],

          const SizedBox(height: 20),

          // ── Payment due warning ────────────────────────────────────
          if (!isCancelled && due != null && due > 0)
            _Banner(
              color: Colors.red,
              icon: Icons.payment,
              text: isNe
                  ? 'बाँकी रकम: Rs ${due.toStringAsFixed(0)} — अपरेटरलाई तिर्नुस्'
                  : 'Balance due: Rs ${due.toStringAsFixed(0)} — please pay the operator',
            ),

          // ── Contact info ───────────────────────────────────────────
          if (!isCancelled)
            Container(
              padding: const EdgeInsets.all(12),
              decoration: BoxDecoration(
                color: Colors.blue.shade50,
                borderRadius: BorderRadius.circular(8),
                border: Border.all(color: Colors.blue.shade100),
              ),
              child: Row(children: [
                Icon(Icons.info_outline, color: Colors.blue.shade700, size: 18),
                const SizedBox(width: 8),
                Expanded(child: Text(
                  isNe
                      ? 'काम सम्बन्धी प्रश्नका लागि अपरेटरलाई सम्पर्क गर्नुस्।'
                      : 'Contact the operator for any queries about this job.',
                  style: TextStyle(color: Colors.blue.shade700, fontSize: 12.5),
                )),
              ]),
            ),
        ]),
      ),
    );
  }
}

// ── Status timeline ──────────────────────────────────────────────────────────

class _StatusTimeline extends StatelessWidget {
  final String status;
  final bool isNe;
  const _StatusTimeline({required this.status, required this.isNe});

  @override
  Widget build(BuildContext context) {
    const steps = ['pending', 'confirmed', 'in_progress', 'completed'];
    final stepLabels = {
      'pending':     'Pending',
      'confirmed':   'Confirmed',
      'in_progress': 'In Progress',
      'completed':   'Done',
    };
    final stepLabelsNe = {
      'pending':     'पेन्डिंग',
      'confirmed':   'पुष्टि',
      'in_progress': 'चलिरहेको',
      'completed':   'सकियो',
    };
    final currentIdx = steps.indexOf(status);

    return Card(
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
      elevation: 1,
      child: Padding(
        padding: const EdgeInsets.symmetric(vertical: 16, horizontal: 8),
        child: Row(
          children: List.generate(steps.length * 2 - 1, (i) {
            if (i.isOdd) {
              // Connector line
              final stepIdx = i ~/ 2;
              final isPast = stepIdx < currentIdx;
              return Expanded(child: Container(
                height: 3,
                color: isPast ? const Color(0xFF003893) : Colors.grey.shade300,
              ));
            }
            final stepIdx = i ~/ 2;
            final isActive = stepIdx == currentIdx;
            final isPast   = stepIdx < currentIdx;
            final color = isPast || isActive
                ? const Color(0xFF003893)
                : Colors.grey.shade400;
            return Column(mainAxisSize: MainAxisSize.min, children: [
              Container(
                width: 28, height: 28,
                decoration: BoxDecoration(
                  shape: BoxShape.circle,
                  color: isActive ? const Color(0xFF003893) : (isPast ? const Color(0xFF003893).withOpacity(0.15) : Colors.grey.shade200),
                  border: Border.all(color: color, width: 2),
                ),
                child: isPast
                    ? const Icon(Icons.check, size: 14, color: Color(0xFF003893))
                    : isActive
                        ? const Icon(Icons.circle, size: 10, color: Colors.white)
                        : null,
              ),
              const SizedBox(height: 4),
              Text(
                (isNe ? stepLabelsNe : stepLabels)[steps[stepIdx]] ?? '',
                style: TextStyle(
                  fontSize: 9,
                  color: isActive ? const Color(0xFF003893) : Colors.grey,
                  fontWeight: isActive ? FontWeight.bold : FontWeight.normal,
                ),
                textAlign: TextAlign.center,
              ),
            ]);
          }),
        ),
      ),
    );
  }
}

// ── Banner ───────────────────────────────────────────────────────────────────

class _Banner extends StatelessWidget {
  final Color color;
  final IconData icon;
  final String text;
  const _Banner({required this.color, required this.icon, required this.text});

  @override
  Widget build(BuildContext context) => Container(
    margin: const EdgeInsets.only(bottom: 12),
    padding: const EdgeInsets.all(12),
    decoration: BoxDecoration(
      color: color.withOpacity(0.08),
      borderRadius: BorderRadius.circular(10),
      border: Border.all(color: color.withOpacity(0.3)),
    ),
    child: Row(children: [
      Icon(icon, color: color, size: 20),
      const SizedBox(width: 10),
      Expanded(child: Text(text, style: TextStyle(
          color: color.withOpacity(0.9), fontSize: 13, fontWeight: FontWeight.w500))),
    ]),
  );
}

// ── Helpers ──────────────────────────────────────────────────────────────────

class _Section extends StatelessWidget {
  final String title;
  final List<Widget> children;
  const _Section(this.title, this.children);
  @override
  Widget build(BuildContext context) => Card(
    shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
    elevation: 1,
    child: Padding(
      padding: const EdgeInsets.all(14),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Text(title, style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 14,
            color: Color(0xFF003893))),
        const Divider(),
        ...children,
      ]),
    ),
  );
}

class _Row extends StatelessWidget {
  final String label, value;
  final bool highlight;
  const _Row(this.label, this.value, {this.highlight = false});
  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.symmetric(vertical: 4),
    child: Row(children: [
      SizedBox(width: 120, child: Text(label,
          style: const TextStyle(color: Colors.black45, fontSize: 13))),
      Expanded(child: Text(value, style: TextStyle(
          fontSize: 13,
          fontWeight: highlight ? FontWeight.bold : FontWeight.normal,
          color: highlight ? Colors.red.shade700 : Colors.black87),
          textAlign: TextAlign.right)),
    ]),
  );
}
