// lib/screens/jobs/job_detail_screen.dart
//
// IMPROVEMENTS (Phase 12 robustness):
//   • Confirmation dialog before cancel with reason selection
//   • "Mark as Done" button for non-timer jobs (in_progress → completed)
//   • Timer running guard — can't cancel while timer is running
//   • Cancelled job shows read-only banner with reason
//   • Completed job shows read-only summary with payment status
//   • Auto-refresh parent list on pop
//   • Nepali labels on all status buttons

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import '../../models/job.dart';
import '../../providers/job_provider.dart';
import '../../utils/bs_date_utils.dart';
import '../../providers/language_provider.dart';
import '../../services/job_timer_service.dart';
import '../../widgets/status_chip.dart';

class JobDetailScreen extends StatefulWidget {
  final Job job;
  const JobDetailScreen({super.key, required this.job});
  @override
  State<JobDetailScreen> createState() => _JobDetailScreenState();
}

class _JobDetailScreenState extends State<JobDetailScreen> {
  late TextEditingController _manualTotal;
  late TextEditingController _manualMins;
  bool _saving = false;

  // Track the latest job object (updated after status changes)
  late Job _job;

  @override
  void initState() {
    super.initState();
    _job = widget.job;
    _manualTotal = TextEditingController(
        text: widget.job.totalAmount?.toStringAsFixed(0) ?? '');
    _manualMins = TextEditingController(
        text: widget.job.timeValue?.toStringAsFixed(1) ?? '');
  }

  @override
  void dispose() {
    _manualTotal.dispose();
    _manualMins.dispose();
    super.dispose();
  }

  // ── Stop timer and patch job ──────────────────────────────────────────────

  Future<void> _stopAndSave() async {
    final ts   = context.read<JobTimerService>();
    final prov = context.read<JobProvider>();
    final id   = _job.id!;

    setState(() => _saving = true);

    final mins  = await ts.stop(id);
    final rate  = _job.ratePerMin ?? 0;
    final total = double.tryParse(_manualTotal.text) ?? (rate * mins);

    _manualMins.text  = mins.toStringAsFixed(1);
    _manualTotal.text = total.toStringAsFixed(0);

    await prov.updateJobTime(
      jobId:       id,
      elapsedMins: mins,
      total:       total,
    );

    setState(() => _saving = false);

    if (mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(context.read<LanguageProvider>().isNepali
              ? 'समय र रकम सुरक्षित भयो'
              : 'Time & amount saved'),
          backgroundColor: Colors.green,
        ),
      );
    }
  }

  Future<void> _saveManualOverride() async {
    final prov  = context.read<JobProvider>();
    final id    = _job.id!;
    final mins  = double.tryParse(_manualMins.text) ?? 0;
    final total = double.tryParse(_manualTotal.text) ?? 0;

    setState(() => _saving = true);
    await prov.updateJobTime(jobId: id, elapsedMins: mins, total: total);
    setState(() => _saving = false);

    if (mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(context.read<LanguageProvider>().isNepali
              ? 'बदलिएको रकम सुरक्षित भयो'
              : 'Override saved'),
          backgroundColor: Colors.teal,
        ),
      );
    }
  }

  // ── Status transitions with confirmation ──────────────────────────────────

  Future<void> _advanceStatus(String nextStatus) async {
    final isNe = context.read<LanguageProvider>().isNepali;
    final labels = {
      'confirmed':   isNe ? 'पक्का गर्नुस्?' : 'Confirm this job?',
      'in_progress': isNe ? 'काम सुरु गर्नुस्?' : 'Start this job?',
      'completed':   isNe ? 'काम सकिएको मार्क गर्नुस्?' : 'Mark job as completed?',
    };
    final isCompleting = nextStatus == 'completed';
    final signatureCtrl = TextEditingController(text: _job.signatureName ?? '');

    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text(labels[nextStatus] ?? ''),
        content: isCompleting
            ? TextField(
                controller: signatureCtrl,
                autofocus: true,
                decoration: InputDecoration(
                  labelText: isNe
                      ? 'प्राप्त गर्ने व्यक्तिको नाम (वैकल्पिक)'
                      : 'Received by / signed by (optional)',
                ),
              )
            : null,
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx, false),
            child: Text(isNe ? 'होइन' : 'No'),
          ),
          ElevatedButton(
            style: ElevatedButton.styleFrom(
              backgroundColor: nextStatus == 'completed' ? Colors.green : const Color(0xFF003893),
              foregroundColor: Colors.white,
            ),
            onPressed: () => Navigator.pop(ctx, true),
            child: Text(isNe ? 'हो' : 'Yes'),
          ),
        ],
      ),
    );

    if (confirmed != true || !mounted) return;
    final signatureName = isCompleting ? signatureCtrl.text.trim() : null;

    setState(() => _saving = true);
    await context.read<JobProvider>().updateJobStatus(
          _job.id!,
          nextStatus,
          signatureName: (signatureName?.isNotEmpty ?? false) ? signatureName : null,
        );
    if (mounted) {
      setState(() {
        _job = _job.copyWith(
          status: nextStatus,
          signatureName: (signatureName?.isNotEmpty ?? false) ? signatureName : null,
        );
        _saving = false;
      });
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(nextStatus == 'completed'
            ? (isNe ? '✅ काम सकियो!' : '✅ Job completed!')
            : (isNe ? 'अवस्था अपडेट भयो' : 'Status updated')),
        backgroundColor: nextStatus == 'completed' ? Colors.green : Colors.blue,
      ));
    }
  }

  Future<void> _cancelJob() async {
    final isNe = context.read<LanguageProvider>().isNepali;
    final ts = context.read<JobTimerService>();

    // Guard: timer still running
    if (ts.isRunning(_job.id ?? -1)) {
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(isNe
            ? 'टाइमर चलिरहेको छ — पहिले रोक्नुस्'
            : 'Timer is running — stop it before cancelling'),
        backgroundColor: Colors.orange,
      ));
      return;
    }

    final reasons = isNe
        ? ['ग्राहकले रद्द गरे', 'मौसम खराब', 'मेसिन बिग्रियो', 'अन्य']
        : ['Customer cancelled', 'Bad weather', 'Machine breakdown', 'Other'];
    String? selectedReason = reasons.first;

    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => StatefulBuilder(
        builder: (ctx, setDialogState) => AlertDialog(
          title: Row(children: [
            const Icon(Icons.cancel_outlined, color: Colors.red),
            const SizedBox(width: 8),
            Text(isNe ? 'काम रद्द गर्नुस्' : 'Cancel Job'),
          ]),
          content: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(isNe
                  ? 'यो काम रद्द गर्न चाहनुहुन्छ?'
                  : 'Are you sure you want to cancel this job?',
                  style: const TextStyle(fontSize: 14)),
              const SizedBox(height: 16),
              Text(isNe ? 'कारण छान्नुस्:' : 'Select reason:',
                  style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 13)),
              const SizedBox(height: 8),
              ...reasons.map((r) => RadioListTile<String>(
                title: Text(r, style: const TextStyle(fontSize: 13)),
                value: r,
                groupValue: selectedReason,
                dense: true,
                onChanged: (v) => setDialogState(() => selectedReason = v),
              )),
            ],
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(ctx, false),
              child: Text(isNe ? 'वापस जानुस्' : 'Go Back'),
            ),
            ElevatedButton(
              style: ElevatedButton.styleFrom(
                  backgroundColor: Colors.red, foregroundColor: Colors.white),
              onPressed: () => Navigator.pop(ctx, true),
              child: Text(isNe ? 'रद्द गर्नुस्' : 'Cancel Job'),
            ),
          ],
        ),
      ),
    );

    if (confirmed != true || !mounted) return;

    ts.clear(_job.id!);
    setState(() => _saving = true);
    await context.read<JobProvider>().updateJobStatus(_job.id!, 'cancelled');
    if (mounted) {
      setState(() {
        _job = _job.copyWith(status: 'cancelled');
        _saving = false;
      });
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(isNe ? 'काम रद्द गरियो' : 'Job cancelled'),
        backgroundColor: Colors.red.shade700,
      ));
    }
  }

  @override
  Widget build(BuildContext context) {
    final lang    = context.watch<LanguageProvider>();
    final isNe    = lang.isNepali;
    final jobProv = context.read<JobProvider>();
    final ts      = context.watch<JobTimerService>();

    final isAgri       = _job.isAgriculture;
    final isPerMin     = _job.isPerMinute;
    final isCancelled  = _job.status == 'cancelled';
    final isCompleted  = _job.status == 'completed';
    final isActive     = _job.status == 'in_progress' || _job.status == 'confirmed';
    final timerRunning = ts.isRunning(_job.id ?? -1);

    return Scaffold(
      appBar: AppBar(
        backgroundColor: isCancelled
            ? Colors.red.shade700
            : isCompleted
                ? Colors.green.shade700
                : const Color(0xFF003893),
        foregroundColor: Colors.white,
        title: Text('${isNe ? "काम" : "Job"} #${_job.id ?? ""}'),
        actions: [
          StatusChip(_job.status, showIcon: false),
          const SizedBox(width: 12),
        ],
      ),
      body: SingleChildScrollView(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [

            // ── Cancelled banner ──────────────────────────────────────
            if (isCancelled)
              _Banner(
                color: Colors.red,
                icon: Icons.cancel_outlined,
                text: isNe ? 'यो काम रद्द गरिएको छ।' : 'This job has been cancelled.',
              ),

            // ── Completed banner ──────────────────────────────────────
            if (isCompleted)
              _Banner(
                color: Colors.green,
                icon: Icons.check_circle_outline,
                text: isNe
                    ? 'काम सफलतापूर्वक सकियो।'
                    : 'Job completed successfully.',
              ),

            if (isCancelled || isCompleted) const SizedBox(height: 12),

            // ── Details card ──────────────────────────────────────────
            _Section(isNe ? 'विवरण' : 'Details', [
              _Row(isNe ? 'किसिम' : 'Type',
                  isAgri ? (isNe ? '🌾 कृषि' : '🌾 Agriculture')
                         : (isNe ? '🚛 यातायात' : '🚛 Transport')),
              _Row(isNe ? 'सेवा' : 'Service', _job.displayService),
              if (_job.customerName != null)
                _Row(isNe ? 'ग्राहक' : 'Customer', _job.customerName!),
              if (_job.location != null && _job.location!.isNotEmpty)
                _Row(isNe ? 'ठेगाना' : 'Location', _job.location!),
              if (_job.operatorName != null && _job.operatorName!.isNotEmpty)
                _Row(isNe ? 'अपरेटर' : 'Operator', _job.operatorName!),
              if (_job.scheduledDate != null)
                _Row(
                  isNe ? 'मिति' : 'Date',
                  '${formatBsLong(DateTime.parse(_job.scheduledDate!), isNe: isNe)}'
                  ' (${_job.scheduledDate})',
                ),
              if (_job.signatureName != null && _job.signatureName!.isNotEmpty)
                _Row(isNe ? 'हस्ताक्षर' : 'Received by', _job.signatureName!),
            ]),

            const SizedBox(height: 12),

            // ── Billing card ──────────────────────────────────────────
            if (isAgri && isPerMin) ...[
              _Section(isNe ? 'मिनेट बिलिङ' : 'Per-Minute Billing', [
                _Row(isNe ? 'दर' : 'Rate',
                    'Rs ${_job.ratePerMin!.toStringAsFixed(0)} / min'),
                if (_job.timeValue != null)
                  _Row(isNe ? 'रेकर्ड समय' : 'Recorded Time',
                      '${_job.timeValue!.toStringAsFixed(1)} min'),
                if (_job.totalAmount != null)
                  _Row(isNe ? 'जम्मा' : 'Total',
                      'Rs ${_job.totalAmount!.toStringAsFixed(0)}'),
                _Row(isNe ? 'अग्रिम' : 'Advance',
                    'Rs ${_job.advancePaid.toStringAsFixed(0)}'),
                if (_job.balanceDue != null)
                  _Row(isNe ? 'बाँकी' : 'Balance Due',
                      'Rs ${_job.balanceDue!.toStringAsFixed(0)}',
                      highlight: _job.hasDues),
              ]),

              // Live timer — only on active non-cancelled jobs
              if (isActive && !isCancelled) ...[
                const SizedBox(height: 12),
                _DetailTimerWidget(
                  ts:           ts,
                  job:          _job,
                  isNe:         isNe,
                  saving:       _saving,
                  manualTotal:  _manualTotal,
                  manualMins:   _manualMins,
                  onStart: () async {
                    await ts.start(_job.id!);
                    if (_job.status == 'confirmed') {
                      await jobProv.updateJobStatus(_job.id!, 'in_progress');
                      setState(() => _job = _job.copyWith(status: 'in_progress'));
                    }
                    setState(() {});
                  },
                  onStop:         _stopAndSave,
                  onSaveOverride: _saveManualOverride,
                ),
              ],
            ] else if (isAgri) ...[
              _Section(isNe ? 'मात्रा / भुक्तानी' : 'Quantity / Financials', [
                if (_job.areaValue != null)
                  _Row(isNe ? 'क्षेत्रफल' : 'Area',
                      '${_job.areaValue} ${_job.areaUnit ?? ""}'),
                if (_job.timeValue != null)
                  _Row(isNe ? 'समय' : 'Time',
                      '${_job.timeValue!.toStringAsFixed(1)} min'),
                if (_job.rate != null)
                  _Row(isNe ? 'दर' : 'Rate', _job.rateDisplay),
                if (_job.totalAmount != null)
                  _Row(isNe ? 'जम्मा' : 'Total',
                      'Rs ${_job.totalAmount!.toStringAsFixed(0)}'),
                _Row(isNe ? 'अग्रिम' : 'Advance',
                    'Rs ${_job.advancePaid.toStringAsFixed(0)}'),
                if (_job.balanceDue != null)
                  _Row(isNe ? 'बाँकी' : 'Balance Due',
                      'Rs ${_job.balanceDue!.toStringAsFixed(0)}',
                      highlight: _job.hasDues),
              ]),
            ] else ...[
              _Section(isNe ? 'ताली बिलिङ' : 'Per-Taali Billing', [
                if (_job.quantityValue != null)
                  _Row(isNe ? 'ताली संख्या' : 'Taali Count',
                      '${_job.quantityValue!.toStringAsFixed(0)} ${_job.quantityUnit ?? "Tali"}'),
                if (_job.rate != null)
                  _Row(isNe ? 'दर (प्रति ताली)' : 'Rate / Taali',
                      'Rs ${_job.rate!.toStringAsFixed(0)}'),
                if (_job.totalAmount != null)
                  _Row(isNe ? 'जम्मा' : 'Total',
                      'Rs ${_job.totalAmount!.toStringAsFixed(0)}'),
                _Row(isNe ? 'अग्रिम' : 'Advance',
                    'Rs ${_job.advancePaid.toStringAsFixed(0)}'),
                if (_job.balanceDue != null)
                  _Row(isNe ? 'बाँकी' : 'Balance Due',
                      'Rs ${_job.balanceDue!.toStringAsFixed(0)}',
                      highlight: _job.hasDues),
              ]),
            ],

            if (_job.notes != null && _job.notes!.isNotEmpty) ...[
              const SizedBox(height: 12),
              _Section(isNe ? 'नोट' : 'Notes', [
                Padding(
                  padding: const EdgeInsets.symmetric(vertical: 4),
                  child: Text(_job.notes!,
                      style: const TextStyle(color: Colors.black87)),
                ),
              ]),
            ],

            const SizedBox(height: 20),

            // ── Action buttons — only shown for non-terminal states ───
            if (!isCancelled && !isCompleted)
              _StatusButtons(
                job:          _job,
                isNe:         isNe,
                saving:       _saving,
                timerRunning: timerRunning,
                onAdvance:    _advanceStatus,
                onCancel:     _cancelJob,
              ),

            // ── Payment summary for completed jobs with dues ──────────
            if (isCompleted && _job.hasDues)
              _Banner(
                color: Colors.red,
                icon: Icons.payment,
                text: isNe
                    ? 'बाँकी रकम: Rs ${_job.balanceDue!.toStringAsFixed(0)} — ग्राहकसँग असुल गर्नुस्'
                    : 'Balance due: Rs ${_job.balanceDue!.toStringAsFixed(0)} — collect from customer',
              ),
          ],
        ),
      ),
    );
  }
}

// ── Status transition buttons ────────────────────────────────────────────────

class _StatusButtons extends StatelessWidget {
  final Job job;
  final bool isNe;
  final bool saving;
  final bool timerRunning;
  final Future<void> Function(String) onAdvance;
  final Future<void> Function() onCancel;

  const _StatusButtons({
    required this.job,
    required this.isNe,
    required this.saving,
    required this.timerRunning,
    required this.onAdvance,
    required this.onCancel,
  });

  @override
  Widget build(BuildContext context) {
    const transitions = <String, String>{
      'pending':     'confirmed',
      'confirmed':   'in_progress',
      'in_progress': 'completed',
    };
    final nextStatus = transitions[job.status];

    final nextLabels = {
      'confirmed':   isNe ? '✅ पक्का गर्नुस्'   : '✅ Confirm Job',
      'in_progress': isNe ? '▶️ काम सुरु'         : '▶️ Start Job',
      'completed':   isNe ? '🏁 काम सकियो'        : '🏁 Mark as Done',
    };

    final nextColors = {
      'confirmed':   Colors.blue.shade700,
      'in_progress': Colors.purple.shade700,
      'completed':   Colors.green.shade700,
    };

    // If timer is running, only show stop message instead of advance
    if (timerRunning && nextStatus == 'completed') {
      return Column(children: [
        Container(
          padding: const EdgeInsets.all(12),
          decoration: BoxDecoration(
            color: Colors.orange.shade50,
            borderRadius: BorderRadius.circular(10),
            border: Border.all(color: Colors.orange.shade200),
          ),
          child: Row(children: [
            Icon(Icons.timer, color: Colors.orange.shade700),
            const SizedBox(width: 8),
            Expanded(child: Text(
              isNe
                  ? 'टाइमर चलिरहेको छ — काम सकाउनु अघि रोक्नुस्'
                  : 'Timer is running — stop the timer before marking done',
              style: TextStyle(color: Colors.orange.shade800, fontSize: 13),
            )),
          ]),
        ),
        const SizedBox(height: 10),
        _CancelButton(isNe: isNe, enabled: !saving && !timerRunning, onCancel: onCancel),
      ]);
    }

    return Column(children: [
      if (nextStatus != null)
        SizedBox(
          width: double.infinity,
          child: ElevatedButton(
            style: ElevatedButton.styleFrom(
              backgroundColor: nextColors[nextStatus] ?? Colors.blue,
              foregroundColor: Colors.white,
              padding: const EdgeInsets.symmetric(vertical: 14),
              shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
            ),
            onPressed: saving ? null : () => onAdvance(nextStatus),
            child: saving
                ? const SizedBox(width: 20, height: 20,
                    child: CircularProgressIndicator(color: Colors.white, strokeWidth: 2))
                : Text(nextLabels[nextStatus]!,
                    style: const TextStyle(fontSize: 16, fontWeight: FontWeight.bold)),
          ),
        ),
      const SizedBox(height: 10),
      _CancelButton(isNe: isNe, enabled: !saving, onCancel: onCancel),
    ]);
  }
}

class _CancelButton extends StatelessWidget {
  final bool isNe;
  final bool enabled;
  final Future<void> Function() onCancel;
  const _CancelButton({required this.isNe, required this.enabled, required this.onCancel});

  @override
  Widget build(BuildContext context) => SizedBox(
    width: double.infinity,
    child: OutlinedButton.icon(
      style: OutlinedButton.styleFrom(
        foregroundColor: Colors.red,
        side: const BorderSide(color: Colors.red),
        padding: const EdgeInsets.symmetric(vertical: 14),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
      ),
      onPressed: enabled ? onCancel : null,
      icon: const Icon(Icons.cancel_outlined, size: 18),
      label: Text(isNe ? 'काम रद्द गर्नुस्' : 'Cancel Job',
          style: const TextStyle(fontWeight: FontWeight.bold)),
    ),
  );
}

// ── Info Banner ──────────────────────────────────────────────────────────────

class _Banner extends StatelessWidget {
  final Color color;
  final IconData icon;
  final String text;
  const _Banner({required this.color, required this.icon, required this.text});

  @override
  Widget build(BuildContext context) => Container(
    margin: const EdgeInsets.only(bottom: 8),
    padding: const EdgeInsets.all(12),
    decoration: BoxDecoration(
      color: color.withOpacity(0.08),
      borderRadius: BorderRadius.circular(10),
      border: Border.all(color: color.withOpacity(0.3)),
    ),
    child: Row(children: [
      Icon(icon, color: color, size: 20),
      const SizedBox(width: 10),
      Expanded(child: Text(text,
          style: TextStyle(color: color.withOpacity(0.9), fontSize: 13,
              fontWeight: FontWeight.w500))),
    ]),
  );
}

// ── Timer widget ─────────────────────────────────────────────────────────────

class _DetailTimerWidget extends StatelessWidget {
  final JobTimerService ts;
  final Job job;
  final bool isNe, saving;
  final TextEditingController manualTotal, manualMins;
  final VoidCallback onStart, onStop, onSaveOverride;

  const _DetailTimerWidget({
    required this.ts, required this.job, required this.isNe,
    required this.saving, required this.manualTotal, required this.manualMins,
    required this.onStart, required this.onStop, required this.onSaveOverride,
  });

  @override
  Widget build(BuildContext context) {
    final id      = job.id!;
    final running = ts.isRunning(id);
    final elapsed = ts.elapsedFormatted(id);
    final mins    = ts.elapsedMinutes(id);
    final rate    = job.ratePerMin ?? 0;
    final live    = rate * mins;

    return Card(
      elevation: 3,
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(16)),
      color: running ? Colors.green.shade50 : Colors.grey.shade50,
      child: Padding(
        padding: const EdgeInsets.all(18),
        child: Column(
          children: [
            Text(isNe ? '⏱ समय मिटर' : '⏱ Time Meter',
                style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 15)),
            const SizedBox(height: 12),
            Text(
              running ? elapsed : (job.timeValue != null
                  ? _fmt(job.timeValue! * 60) : '00:00'),
              style: TextStyle(
                fontSize: 48, fontWeight: FontWeight.bold,
                color: running ? Colors.green.shade800 : Colors.black45,
                letterSpacing: 4,
              ),
            ),
            if (running && rate > 0) ...[
              const SizedBox(height: 4),
              Text('Rs ${live.toStringAsFixed(0)}',
                  style: TextStyle(fontSize: 20, fontWeight: FontWeight.bold,
                      color: Colors.green.shade700)),
              Text('${mins.toStringAsFixed(1)} min × Rs $rate',
                  style: const TextStyle(fontSize: 11, color: Colors.black38)),
            ],
            const SizedBox(height: 14),
            SizedBox(
              width: 180, height: 50,
              child: ElevatedButton.icon(
                style: ElevatedButton.styleFrom(
                  backgroundColor: running ? Colors.red.shade600 : Colors.green.shade700,
                  foregroundColor: Colors.white,
                  shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(30)),
                ),
                icon: Icon(running ? Icons.stop_circle : Icons.play_circle_fill, size: 22),
                label: Text(
                  running ? (isNe ? 'रोक्नुस् + सेभ' : 'Stop & Save')
                          : (isNe ? 'सुरु गर्नुस्' : 'Start Timer'),
                  style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 14),
                ),
                onPressed: saving ? null : (running ? onStop : onStart),
              ),
            ),
            if (!running) ...[
              const Divider(height: 28),
              Text(isNe ? 'म्यानुअल सुधार' : 'Manual Override',
                  style: const TextStyle(fontSize: 12, color: Colors.black45,
                      fontWeight: FontWeight.w600)),
              const SizedBox(height: 10),
              Row(children: [
                Expanded(child: TextField(
                  controller: manualMins,
                  keyboardType: TextInputType.number,
                  decoration: InputDecoration(
                    labelText: isNe ? 'मिनेट' : 'Minutes',
                    isDense: true,
                    border: OutlineInputBorder(borderRadius: BorderRadius.circular(8)),
                    suffixText: 'min',
                  ),
                )),
                const SizedBox(width: 10),
                Expanded(child: TextField(
                  controller: manualTotal,
                  keyboardType: TextInputType.number,
                  decoration: InputDecoration(
                    labelText: isNe ? 'जम्मा' : 'Total',
                    isDense: true,
                    border: OutlineInputBorder(borderRadius: BorderRadius.circular(8)),
                    prefixText: 'Rs ',
                  ),
                )),
                const SizedBox(width: 8),
                ElevatedButton(
                  style: ElevatedButton.styleFrom(
                    backgroundColor: Colors.teal, foregroundColor: Colors.white,
                    padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 12),
                    shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8)),
                  ),
                  onPressed: saving ? null : onSaveOverride,
                  child: saving
                      ? const SizedBox(width: 16, height: 16,
                          child: CircularProgressIndicator(color: Colors.white, strokeWidth: 2))
                      : Text(isNe ? 'सेभ' : 'Save',
                          style: const TextStyle(fontSize: 12)),
                ),
              ]),
            ],
          ],
        ),
      ),
    );
  }

  String _fmt(double totalSeconds) {
    final s = totalSeconds.round();
    return '${(s ~/ 60).toString().padLeft(2, '0')}:${(s % 60).toString().padLeft(2, '0')}';
  }
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
        Text(title, style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 14)),
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
      Text('$label: ', style: const TextStyle(color: Colors.black54, fontSize: 13)),
      Expanded(child: Text(value,
          style: TextStyle(fontWeight: FontWeight.w600,
              color: highlight ? Colors.red : Colors.black87, fontSize: 13),
          textAlign: TextAlign.right)),
    ]),
  );
}
