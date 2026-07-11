// lib/screens/jobs/add_job_screen.dart
//
// HYBRID BILLING UPGRADE (Phase 12):
//
// Agriculture billing modes (toggle):
//   [Per Area]    — rate × area (Katha/Bigha) — existing behaviour
//   [Per Minute]  — LIVE TIMER: big Start/Stop button, ticking display,
//                   rate_per_min field, running total updates in real time.
//   [Manual Time] — enter elapsed minutes/hours by hand (no live timer),
//                   useful when worker noted the time on paper.
//
// Transport billing:
//   Per Taali (ताली) — rate × taali count = total.
//   No timer; no per-minute rate. Clean and simple.
//
// Timer state is managed by JobTimerService (ChangeNotifier) so the
// ticking number rebuilds only the small widget that needs it, not the
// whole screen.  The timer is keyed by a temporary local ID (-1) because
// the real job_id isn't assigned until the server responds.

import 'dart:async';
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import '../../config/constants.dart';
import '../../models/operator.dart';
import '../../providers/job_provider.dart';
import '../../providers/language_provider.dart';
import '../../services/job_timer_service.dart';
import '../../utils/bs_date_utils.dart';
import '../../widgets/bs_date_picker.dart';

// Temporary "job id" for the in-progress new job timer.
// JobTimerService supports any int key; we use -1 for the "add" flow.
const int _kNewJobTimerId = -1;

class AddJobScreen extends StatefulWidget {
  final String? voiceNote;
  const AddJobScreen({super.key, this.voiceNote});
  @override
  State<AddJobScreen> createState() => _AddJobScreenState();
}

class _AddJobScreenState extends State<AddJobScreen> {
  final _form = GlobalKey<FormState>();

  // ── Job type ─────────────────────────────────────────────────────────────
  String _jobType = 'agriculture';

  // ── Agriculture ───────────────────────────────────────────────────────────
  String _service  = kAgriServices.first;
  String _agriBilling = kBillingPerMinute; // default: live timer (per-min)

  // ── Transport ─────────────────────────────────────────────────────────────
  String _material = kTransportMaterials.first;
  String _tUnit    = kTransportUnits.first; // 'Tali' by default

  // ── Area / manual-time ────────────────────────────────────────────────────
  String _areaUnit = kLandUnits.first;
  String _timeUnit = kTimeUnits.first; // for manual time input

  // ── Controllers ───────────────────────────────────────────────────────────
  final _customer      = TextEditingController();
  final _customerPhone = TextEditingController();
  final _location      = TextEditingController();
  final _ratePerMin    = TextEditingController();   // per-minute rate
  final _ratePerArea   = TextEditingController();   // per-area rate
  final _ratePerTali   = TextEditingController();   // transport rate/tali
  final _areaQty       = TextEditingController();   // area amount
  final _taliQty       = TextEditingController();   // taali count
  final _manualMinutes = TextEditingController();   // manual time entry
  final _total         = TextEditingController();   // editable total
  final _advance       = TextEditingController(text: '0');
  final _notes         = TextEditingController();
  String _scheduledDate = DateTime.now().toIso8601String().substring(0, 10);
  bool _submitting = false;
  int? _selectedOperatorId;

  // Live running total (updated every second while timer is active)
  double _runningTotal = 0;
  StreamSubscription? _timerSub;

  @override
  void initState() {
    super.initState();
    if (widget.voiceNote?.isNotEmpty == true) _notes.text = widget.voiceNote!;
    // Listen to rate field to update running total while timer ticks
    _ratePerMin.addListener(_updateRunningTotal);
    // Populate the operator picker
    context.read<JobProvider>().fetchOperators();
  }

  @override
  void dispose() {
    // Stop the temporary new-job timer if user exits without saving
    final timerSvc = context.read<JobTimerService>();
    if (timerSvc.isRunning(_kNewJobTimerId)) {
      timerSvc.stop(_kNewJobTimerId);
    }
    for (final c in [
      _customer, _customerPhone, _location,
      _ratePerMin, _ratePerArea, _ratePerTali,
      _areaQty, _taliQty, _manualMinutes,
      _total, _advance, _notes,
    ]) {
      c.dispose();
    }
    super.dispose();
  }

  // ── Operator picker ──────────────────────────────────────────────────────

  Future<void> _showAddOperatorDialog(bool isNe) async {
    final nameCtrl  = TextEditingController();
    final phoneCtrl = TextEditingController();
    final added = await showDialog<bool>(
      context: context,
      builder: (ctx) => StatefulBuilder(
        builder: (ctx, setDialogState) => AlertDialog(
          title: Text(isNe ? 'नयाँ अपरेटर' : 'New Operator'),
          content: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              TextField(
                controller: nameCtrl,
                decoration: InputDecoration(
                  labelText: isNe ? 'नाम' : 'Name',
                ),
                autofocus: true,
                onChanged: (_) => setDialogState(() {}),
              ),
              const SizedBox(height: 8),
              TextField(
                controller: phoneCtrl,
                keyboardType: TextInputType.phone,
                decoration: InputDecoration(
                  labelText: isNe ? 'फोन (वैकल्पिक)' : 'Phone (optional)',
                ),
              ),
            ],
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(ctx, false),
              child: Text(isNe ? 'रद्द गर्नुस्' : 'Cancel'),
            ),
            ElevatedButton(
              onPressed: nameCtrl.text.trim().isEmpty
                  ? null
                  : () => Navigator.pop(ctx, true),
              child: Text(isNe ? 'थप्नुस्' : 'Add'),
            ),
          ],
        ),
      ),
    );
    if (added == true && nameCtrl.text.trim().isNotEmpty && mounted) {
      await context
          .read<JobProvider>()
          .addOperator(nameCtrl.text.trim(), phone: phoneCtrl.text.trim());
    }
  }

  // ── Running total helpers ─────────────────────────────────────────────────

  void _updateRunningTotal() {
    final timerSvc = context.read<JobTimerService>();
    final rate     = double.tryParse(_ratePerMin.text) ?? 0;
    final mins     = timerSvc.elapsedMinutes(_kNewJobTimerId);
    setState(() => _runningTotal = rate * mins);
  }

  void _computeTotalFromArea() {
    final r = double.tryParse(_ratePerArea.text);
    final q = double.tryParse(_areaQty.text);
    if (r != null && q != null) {
      setState(() => _total.text = (r * q).toStringAsFixed(0));
    }
  }

  void _computeTotalFromTali() {
    final r = double.tryParse(_ratePerTali.text);
    final q = double.tryParse(_taliQty.text);
    if (r != null && q != null) {
      setState(() => _total.text = (r * q).toStringAsFixed(0));
    }
  }

  void _computeTotalFromManualTime() {
    final r    = double.tryParse(_ratePerMin.text);
    final mins = double.tryParse(_manualMinutes.text);
    if (r != null && mins != null) {
      // Convert to minutes if unit is Hour
      final effectiveMins = _timeUnit == 'Hour' ? mins * 60 : mins;
      setState(() => _total.text = (r * effectiveMins).toStringAsFixed(0));
    }
  }

  // ── Timer control ─────────────────────────────────────────────────────────

  Future<void> _startTimer() async {
    final timerSvc = context.read<JobTimerService>();
    await timerSvc.start(_kNewJobTimerId);
    // Rebuild every second to show ticking total
    _timerSub?.cancel();
    _timerSub = Stream.periodic(const Duration(seconds: 1)).listen((_) {
      if (mounted) _updateRunningTotal();
    });
  }

  Future<void> _stopTimer() async {
    _timerSub?.cancel();
    final timerSvc = context.read<JobTimerService>();
    final mins = await timerSvc.stop(_kNewJobTimerId);
    final rate = double.tryParse(_ratePerMin.text) ?? 0;
    setState(() {
      _runningTotal = rate * mins;
      _total.text   = _runningTotal.toStringAsFixed(0);
    });
  }

  // ── Submit ────────────────────────────────────────────────────────────────

  Future<void> _submit() async {
    if (!_form.currentState!.validate()) return;

    // Stop timer if still running at submit
    final timerSvc = context.read<JobTimerService>();
    double timerMins = 0;
    if (timerSvc.isRunning(_kNewJobTimerId)) {
      timerMins = await timerSvc.stop(_kNewJobTimerId);
    }

    setState(() => _submitting = true);

    final data = <String, dynamic>{
      'job_type':       _jobType,
      'customer_name':  _customer.text.trim(),
      'customer_phone': _customerPhone.text.trim(),
      'location':       _location.text.trim(),
      'notes':          _notes.text.trim(),
      'scheduled_date': _scheduledDate,
      'advance_paid':   double.tryParse(_advance.text) ?? 0,
      if (_selectedOperatorId != null) 'operator_id': _selectedOperatorId,
    };

    if (_jobType == 'agriculture') {
      data['service'] = _service;

      switch (_agriBilling) {
        case kBillingPerMinute:
          final rate = double.tryParse(_ratePerMin.text) ?? 0;
          final mins = timerMins > 0
              ? timerMins
              : double.tryParse(_manualMinutes.text) ?? 0;
          data['rate_per_min'] = rate;
          data['time_value']   = mins;
          data['time_unit']    = 'Minute';
          data['total_amount'] = double.tryParse(_total.text) ?? (rate * mins);

        case kBillingPerTime:
          final rate = double.tryParse(_ratePerMin.text) ?? 0;
          var   mins = double.tryParse(_manualMinutes.text) ?? 0;
          if (_timeUnit == 'Hour') mins *= 60;
          data['rate_per_min'] = rate;
          data['time_value']   = mins;
          data['time_unit']    = 'Minute';
          data['total_amount'] = double.tryParse(_total.text) ?? (rate * mins);

        case kBillingPerArea:
          data['area_value']   = double.tryParse(_areaQty.text);
          data['area_unit']    = _areaUnit;
          data['rate']         = double.tryParse(_ratePerArea.text);
          data['total_amount'] = double.tryParse(_total.text);
      }
    } else {
      // Transport — per taali
      data['service']        = _material;
      data['material']       = _material;
      data['quantity_value'] = double.tryParse(_taliQty.text);
      data['quantity_unit']  = _tUnit;
      data['rate']           = double.tryParse(_ratePerTali.text);
      data['total_amount']   = double.tryParse(_total.text);
    }

    final jobProvider = context.read<JobProvider>();
    await jobProvider.createJob(data);

    // Wait for the server's agro_result confirmation (log_job) before
    // dismissing this screen — don't assume success just because the
    // WS message was sent.
    final isNe = mounted ? context.read<LanguageProvider>().isNepali : false;
    int? confirmedJobId;
    String? errorMessage;
    const pollInterval = Duration(milliseconds: 150);
    const timeout = Duration(seconds: 6);
    final deadline = DateTime.now().add(timeout);

    while (DateTime.now().isBefore(deadline)) {
      if (jobProvider.lastCreatedJobId != null) {
        confirmedJobId = jobProvider.lastCreatedJobId;
        break;
      }
      if (jobProvider.lastError != null) {
        errorMessage = jobProvider.lastError;
        break;
      }
      await Future.delayed(pollInterval);
    }

    if (!mounted) return;
    setState(() => _submitting = false);

    if (confirmedJobId != null) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(isNe
              ? 'काम सफलतापूर्वक सुरक्षित भयो!'
              : 'Job saved successfully!'),
          backgroundColor: Colors.green,
        ),
      );
      Navigator.pop(context);
    } else {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(errorMessage ??
              (isNe
                  ? 'काम सुरक्षित गर्न असफल भयो। फेरि प्रयास गर्नुहोस्।'
                  : 'Failed to save job. Please try again.')),
          backgroundColor: Colors.red,
        ),
      );
    }
  }

  // ── Build ─────────────────────────────────────────────────────────────────

  @override
  Widget build(BuildContext context) {
    final isNe = context.watch<LanguageProvider>().isNepali;

    return Scaffold(
      appBar: AppBar(
        backgroundColor: const Color(0xFF003893),
        foregroundColor: Colors.white,
        title: Text(isNe ? 'नयाँ काम थप्नुस्' : 'Add New Job'),
      ),
      body: Form(
        key: _form,
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(16),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [

              // ── Job type toggle ─────────────────────────────────────
              _Label(isNe ? 'काम को किसिम' : 'Job Type'),
              Row(children: [
                _TypeBtn(
                  label: isNe ? '🌾 कृषि' : '🌾 Agriculture',
                  selected: _jobType == 'agriculture',
                  onTap: () => setState(() {
                    _jobType  = 'agriculture';
                    _service  = kAgriServices.first;
                  }),
                ),
                const SizedBox(width: 10),
                _TypeBtn(
                  label: isNe ? '🚛 यातायात' : '🚛 Transport',
                  selected: _jobType == 'transport',
                  onTap: () => setState(() => _jobType = 'transport'),
                ),
              ]),

              const SizedBox(height: 16),

              // ══ AGRICULTURE SECTION ═══════════════════════════════════
              if (_jobType == 'agriculture') ...[

                // Service
                _Label(isNe ? 'सेवा' : 'Service'),
                _TranslatedDropdown(
                  value: _service,
                  items: kAgriServices,
                  nepaliMap: kAgriServiceNe,
                  isNe: isNe,
                  onChanged: (v) => setState(() => _service = v!),
                ),

                const SizedBox(height: 16),

                // Billing mode selector
                _Label(isNe ? 'बिलिङ तरिका' : 'Billing Method'),
                _BillingModeSelector(
                  value: _agriBilling,
                  isNe: isNe,
                  onChange: (m) => setState(() {
                    _agriBilling = m;
                    // Stop timer if switching away
                    final ts = context.read<JobTimerService>();
                    if (m != kBillingPerMinute && ts.isRunning(_kNewJobTimerId)) {
                      ts.stop(_kNewJobTimerId);
                    }
                    _total.text = '';
                    _runningTotal = 0;
                  }),
                ),

                const SizedBox(height: 16),

                // ── Per Minute (live timer) ─────────────────────────
                if (_agriBilling == kBillingPerMinute) ...[
                  _Label(isNe ? 'दर (प्रति मिनेट)' : 'Rate per Minute'),
                  TextFormField(
                    controller: _ratePerMin,
                    keyboardType: TextInputType.number,
                    decoration: _dec('Rs / min  (e.g. 5)'),
                    validator: (v) => (v?.isEmpty ?? true)
                        ? (isNe ? 'दर आवश्यक छ' : 'Rate required')
                        : null,
                  ),

                  const SizedBox(height: 16),

                  // Timer widget
                  Consumer<JobTimerService>(
                    builder: (_, ts, __) => _TimerWidget(
                      timerService: ts,
                      ratePerMin: double.tryParse(_ratePerMin.text) ?? 0,
                      runningTotal: _runningTotal,
                      isNe: isNe,
                      onStart: _startTimer,
                      onStop: _stopTimer,
                    ),
                  ),

                  const SizedBox(height: 16),

                  // Manual override for total (editable even after timer)
                  _Label(isNe
                      ? 'जम्मा रकम (स्वतः / बदल्न मिल्छ)'
                      : 'Total Amount (auto / override)'),
                  TextFormField(
                    controller: _total,
                    keyboardType: TextInputType.number,
                    decoration: _dec('Rs'),
                  ),
                ],

                // ── Per Time (manual) ──────────────────────────────
                if (_agriBilling == kBillingPerTime) ...[
                  _Label(isNe ? 'दर (प्रति मिनेट)' : 'Rate per Minute'),
                  TextFormField(
                    controller: _ratePerMin,
                    keyboardType: TextInputType.number,
                    decoration: _dec('Rs / min'),
                    onChanged: (_) => _computeTotalFromManualTime(),
                  ),

                  const SizedBox(height: 14),

                  _Label(isNe ? 'कुल समय' : 'Total Time'),
                  Row(children: [
                    Expanded(
                      flex: 2,
                      child: TextFormField(
                        controller: _manualMinutes,
                        keyboardType: TextInputType.number,
                        decoration: _dec(isNe ? 'संख्या' : 'Amount'),
                        onChanged: (_) => _computeTotalFromManualTime(),
                        validator: (v) => (v?.isEmpty ?? true)
                            ? (isNe ? 'आवश्यक छ' : 'Required')
                            : null,
                      ),
                    ),
                    const SizedBox(width: 10),
                    Expanded(
                      child: DropdownButtonFormField<String>(
                        value: _timeUnit,
                        onChanged: (v) {
                          setState(() => _timeUnit = v!);
                          _computeTotalFromManualTime();
                        },
                        decoration: _dec(''),
                        items: kTimeUnits
                            .map((u) => DropdownMenuItem(
                                value: u,
                                child: Text(isNe ? (kUnitNe[u] ?? u) : u)))
                            .toList(),
                      ),
                    ),
                  ]),

                  const SizedBox(height: 14),

                  _Label(isNe ? 'जम्मा रकम' : 'Total Amount'),
                  TextFormField(
                    controller: _total,
                    keyboardType: TextInputType.number,
                    decoration: _dec('Rs (auto-computed or override)'),
                  ),
                ],

                // ── Per Area ───────────────────────────────────────
                if (_agriBilling == kBillingPerArea) ...[
                  _Label(isNe ? 'क्षेत्रफल' : 'Area'),
                  Row(children: [
                    Expanded(
                      flex: 2,
                      child: TextFormField(
                        controller: _areaQty,
                        keyboardType: TextInputType.number,
                        decoration: _dec(isNe ? 'मात्रा' : 'Amount'),
                        onChanged: (_) => _computeTotalFromArea(),
                        validator: (v) => (v?.isEmpty ?? true)
                            ? (isNe ? 'आवश्यक छ' : 'Required')
                            : null,
                      ),
                    ),
                    const SizedBox(width: 10),
                    Expanded(
                      child: DropdownButtonFormField<String>(
                        value: _areaUnit,
                        onChanged: (v) => setState(() => _areaUnit = v!),
                        decoration: _dec(''),
                        items: kLandUnits
                            .map((u) => DropdownMenuItem(
                                value: u,
                                child: Text(isNe ? (kUnitNe[u] ?? u) : u)))
                            .toList(),
                      ),
                    ),
                  ]),

                  const SizedBox(height: 14),

                  _Label(isNe ? 'दर (प्रति एकाइ)' : 'Rate per unit'),
                  TextFormField(
                    controller: _ratePerArea,
                    keyboardType: TextInputType.number,
                    decoration: _dec('Rs / ${isNe ? (kUnitNe[_areaUnit] ?? _areaUnit) : _areaUnit}'),
                    onChanged: (_) => _computeTotalFromArea(),
                  ),

                  const SizedBox(height: 14),

                  _Label(isNe ? 'जम्मा रकम' : 'Total Amount'),
                  TextFormField(
                    controller: _total,
                    keyboardType: TextInputType.number,
                    decoration: _dec('Rs (auto-computed or override)'),
                  ),
                ],
              ],

              // ══ TRANSPORT SECTION ═════════════════════════════════════
              if (_jobType == 'transport') ...[

                // Material
                _Label(isNe ? 'सामग्री' : 'Material'),
                _TranslatedDropdown(
                  value: _material,
                  items: kTransportMaterials,
                  nepaliMap: kTransportMaterialNe,
                  isNe: isNe,
                  onChanged: (v) => setState(() => _material = v!),
                ),

                const SizedBox(height: 16),

                // Per-taali info banner
                Container(
                  padding: const EdgeInsets.symmetric(
                      horizontal: 12, vertical: 10),
                  decoration: BoxDecoration(
                    color: Colors.orange.shade50,
                    borderRadius: BorderRadius.circular(8),
                    border: Border.all(color: Colors.orange.shade200),
                  ),
                  child: Row(
                    children: [
                      Icon(Icons.info_outline,
                          color: Colors.orange.shade700, size: 18),
                      const SizedBox(width: 8),
                      Expanded(
                        child: Text(
                          isNe
                              ? 'यातायातमा प्रति ताली हिसाब गरिन्छ। एक ताली = एक भारी।'
                              : 'Transport is billed per taali (trip load). Rate × taali = total.',
                          style: TextStyle(
                              color: Colors.orange.shade800,
                              fontSize: 12.5),
                        ),
                      ),
                    ],
                  ),
                ),

                const SizedBox(height: 14),

                // Taali count + unit
                _Label(isNe ? 'ताली संख्या' : 'Taali Count'),
                Row(children: [
                  Expanded(
                    flex: 2,
                    child: TextFormField(
                      controller: _taliQty,
                      keyboardType: TextInputType.number,
                      decoration: _dec(isNe ? 'ताली' : 'Taali'),
                      onChanged: (_) => _computeTotalFromTali(),
                      validator: (v) => (v?.isEmpty ?? true)
                          ? (isNe ? 'आवश्यक छ' : 'Required')
                          : null,
                    ),
                  ),
                  const SizedBox(width: 10),
                  Expanded(
                    child: DropdownButtonFormField<String>(
                      value: _tUnit,
                      onChanged: (v) => setState(() => _tUnit = v!),
                      decoration: _dec(''),
                      items: kTransportUnits
                          .map((u) => DropdownMenuItem(
                              value: u,
                              child: Text(isNe ? (kUnitNe[u] ?? u) : u)))
                          .toList(),
                    ),
                  ),
                ]),

                const SizedBox(height: 14),

                _Label(isNe ? 'दर (प्रति ताली)' : 'Rate per Taali'),
                TextFormField(
                  controller: _ratePerTali,
                  keyboardType: TextInputType.number,
                  decoration: _dec('Rs / taali'),
                  onChanged: (_) => _computeTotalFromTali(),
                ),

                const SizedBox(height: 14),

                _Label(isNe ? 'जम्मा रकम' : 'Total Amount'),
                TextFormField(
                  controller: _total,
                  keyboardType: TextInputType.number,
                  decoration: _dec('Rs (auto-computed or override)'),
                ),
              ],

              // ══ SHARED FIELDS ═════════════════════════════════════════

              const SizedBox(height: 16),

              _Label(isNe ? 'ग्राहकको नाम' : 'Customer Name'),
              TextFormField(
                controller: _customer,
                decoration: _dec(isNe ? 'ग्राहकको नाम' : 'Customer name'),
                validator: (v) =>
                    (v?.isEmpty ?? true) ? (isNe ? 'आवश्यक छ' : 'Required') : null,
              ),

              const SizedBox(height: 14),

              _Label(isNe
                  ? 'ग्राहकको फोन (वैकल्पिक)'
                  : 'Customer Phone (optional)'),
              TextFormField(
                controller: _customerPhone,
                keyboardType: TextInputType.phone,
                decoration: _dec(isNe
                    ? 'फोन — agro_client का लागि चाहिन्छ'
                    : 'Phone — needed for customer app login'),
              ),

              const SizedBox(height: 14),

              _Label(isNe ? 'ठेगाना / टोल' : 'Location'),
              TextFormField(
                controller: _location,
                decoration: _dec(isNe ? 'गाउँ / वडा' : 'Village / ward'),
              ),

              const SizedBox(height: 14),

              _Label(isNe ? 'अपरेटर' : 'Operator'),
              Consumer<JobProvider>(
                builder: (context, jobProv, _) {
                  final ops = jobProv.operators;
                  return Row(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Expanded(
                        child: DropdownButtonFormField<int>(
                          value: _selectedOperatorId,
                          decoration: _dec(isNe ? 'अपरेटर छान्नुस्' : 'Select operator'),
                          items: ops
                              .map((o) => DropdownMenuItem(
                                    value: o.id,
                                    child: Text(o.name),
                                  ))
                              .toList(),
                          onChanged: (v) => setState(() => _selectedOperatorId = v),
                        ),
                      ),
                      const SizedBox(width: 8),
                      IconButton(
                        tooltip: isNe ? 'नयाँ अपरेटर थप्नुस्' : 'Add new operator',
                        icon: const Icon(Icons.person_add_alt_1),
                        onPressed: () => _showAddOperatorDialog(isNe),
                      ),
                    ],
                  );
                },
              ),

              const SizedBox(height: 14),

              _Label(isNe ? 'अग्रिम भुक्तानी' : 'Advance Paid'),
              TextFormField(
                controller: _advance,
                keyboardType: TextInputType.number,
                decoration: _dec('Rs 0'),
              ),

              const SizedBox(height: 14),

              _Label(isNe ? 'मिति (वि.सं.)' : 'Scheduled Date (BS)'),
              InkWell(
                onTap: () async {
                  final picked = await showBsDatePicker(
                    context: context,
                    initialDate: DateTime.tryParse(_scheduledDate) ?? DateTime.now(),
                    firstDate: DateTime(2024),
                    // 3-year rolling window instead of a hardcoded year —
                    // the old fixed DateTime(2027) upper bound was about
                    // to start silently blocking job scheduling.
                    lastDate: DateTime.now().add(const Duration(days: 365 * 3)),
                    isNe: isNe,
                  );
                  if (picked != null) {
                    setState(() => _scheduledDate =
                        picked.toIso8601String().substring(0, 10));
                  }
                },
                child: Container(
                  padding: const EdgeInsets.symmetric(
                      vertical: 14, horizontal: 12),
                  decoration: BoxDecoration(
                    border: Border.all(color: Colors.black26),
                    borderRadius: BorderRadius.circular(8),
                  ),
                  child: Row(children: [
                    const Icon(Icons.calendar_today,
                        size: 18, color: Colors.black54),
                    const SizedBox(width: 8),
                    Builder(builder: (_) {
                      final adDate = DateTime.parse(_scheduledDate);
                      return Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            formatBsLong(adDate, isNe: isNe),
                            style: const TextStyle(fontWeight: FontWeight.w600),
                          ),
                          Text(
                            isNe ? '${_scheduledDate} (ई.)' : '$_scheduledDate (AD)',
                            style: const TextStyle(fontSize: 12, color: Colors.black54),
                          ),
                        ],
                      );
                    }),
                  ]),
                ),
              ),

              const SizedBox(height: 14),

              _Label(isNe ? 'नोट' : 'Notes'),
              TextFormField(
                controller: _notes,
                maxLines: 3,
                decoration: _dec(isNe ? 'थप जानकारी...' : 'Additional info...'),
              ),

              const SizedBox(height: 24),

              // Submit
              SizedBox(
                width: double.infinity,
                height: 52,
                child: ElevatedButton(
                  style: ElevatedButton.styleFrom(
                    backgroundColor: const Color(0xFF003893),
                    foregroundColor: Colors.white,
                    shape: RoundedRectangleBorder(
                        borderRadius: BorderRadius.circular(10)),
                  ),
                  onPressed: _submitting ? null : _submit,
                  child: _submitting
                      ? const CircularProgressIndicator(
                          color: Colors.white, strokeWidth: 2)
                      : Text(
                          isNe ? 'सुरक्षित गर्नुस्' : 'Save Job',
                          style: const TextStyle(
                              fontSize: 16, fontWeight: FontWeight.bold),
                        ),
                ),
              ),
              const SizedBox(height: 30),
            ],
          ),
        ),
      ),
    );
  }

  InputDecoration _dec(String hint) => InputDecoration(
    hintText: hint,
    contentPadding:
        const EdgeInsets.symmetric(horizontal: 12, vertical: 12),
    border: OutlineInputBorder(borderRadius: BorderRadius.circular(8)),
    focusedBorder: OutlineInputBorder(
      borderRadius: BorderRadius.circular(8),
      borderSide: const BorderSide(color: Color(0xFF003893), width: 2),
    ),
  );
}

// ── Billing mode selector ────────────────────────────────────────────────────

class _BillingModeSelector extends StatelessWidget {
  final String value;
  final bool isNe;
  final ValueChanged<String> onChange;
  const _BillingModeSelector({
    required this.value,
    required this.isNe,
    required this.onChange,
  });

  @override
  Widget build(BuildContext context) {
    final modes = [
      (kBillingPerMinute, isNe ? '⏱ प्रति मिनेट' : '⏱ Per Minute',
          isNe ? 'लाइभ टाइमर' : 'Live timer'),
      (kBillingPerTime, isNe ? '🕐 म्यानुअल समय' : '🕐 Manual Time',
          isNe ? 'आफैँ लेख्नुस्' : 'Enter manually'),
      (kBillingPerArea, isNe ? '📐 प्रति क्षेत्र' : '📐 Per Area',
          isNe ? 'कठ्ठा/बिघा' : 'Katha / Bigha'),
    ];

    return Column(
      children: modes.map((m) {
        final selected = value == m.$1;
        return GestureDetector(
          onTap: () => onChange(m.$1),
          child: AnimatedContainer(
            duration: const Duration(milliseconds: 180),
            margin: const EdgeInsets.only(bottom: 8),
            padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
            decoration: BoxDecoration(
              color: selected
                  ? const Color(0xFF003893).withOpacity(0.08)
                  : Colors.grey.shade50,
              borderRadius: BorderRadius.circular(10),
              border: Border.all(
                color: selected
                    ? const Color(0xFF003893)
                    : Colors.grey.shade300,
                width: selected ? 2 : 1,
              ),
            ),
            child: Row(
              children: [
                Radio<String>(
                  value: m.$1,
                  groupValue: value,
                  onChanged: (v) => onChange(v!),
                  activeColor: const Color(0xFF003893),
                  materialTapTargetSize: MaterialTapTargetSize.shrinkWrap,
                ),
                const SizedBox(width: 8),
                Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(m.$2,
                        style: TextStyle(
                          fontWeight: FontWeight.bold,
                          color: selected
                              ? const Color(0xFF003893)
                              : Colors.black87,
                          fontSize: 13,
                        )),
                    Text(m.$3,
                        style: const TextStyle(
                            fontSize: 11, color: Colors.black45)),
                  ],
                ),
              ],
            ),
          ),
        );
      }).toList(),
    );
  }
}

// ── Live timer widget ────────────────────────────────────────────────────────

class _TimerWidget extends StatelessWidget {
  final JobTimerService timerService;
  final double ratePerMin;
  final double runningTotal;
  final bool isNe;
  final VoidCallback onStart;
  final VoidCallback onStop;

  const _TimerWidget({
    required this.timerService,
    required this.ratePerMin,
    required this.runningTotal,
    required this.isNe,
    required this.onStart,
    required this.onStop,
  });

  @override
  Widget build(BuildContext context) {
    final running = timerService.isRunning(_kNewJobTimerId);
    final elapsed = timerService.elapsedFormatted(_kNewJobTimerId);
    final mins    = timerService.elapsedMinutes(_kNewJobTimerId);

    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(20),
      decoration: BoxDecoration(
        color: running
            ? Colors.green.shade50
            : Colors.grey.shade100,
        borderRadius: BorderRadius.circular(16),
        border: Border.all(
          color: running ? Colors.green.shade400 : Colors.grey.shade300,
          width: running ? 2 : 1,
        ),
      ),
      child: Column(
        children: [
          // Timer display
          Text(
            running ? elapsed : '00:00',
            style: TextStyle(
              fontSize: 52,
              fontWeight: FontWeight.bold,
              fontFeatures: const [FontFeature.tabularFigures()],
              color: running ? Colors.green.shade800 : Colors.black38,
              letterSpacing: 4,
            ),
          ),

          if (running && ratePerMin > 0) ...[
            const SizedBox(height: 4),
            Text(
              'Rs ${runningTotal.toStringAsFixed(0)}',
              style: TextStyle(
                fontSize: 22,
                fontWeight: FontWeight.bold,
                color: Colors.green.shade700,
              ),
            ),
            Text(
              '${mins.toStringAsFixed(1)} ${isNe ? "मिनेट" : "min"} × Rs $ratePerMin',
              style: const TextStyle(fontSize: 12, color: Colors.black38),
            ),
          ],

          const SizedBox(height: 16),

          // Start / Stop button
          SizedBox(
            width: 180,
            height: 52,
            child: ElevatedButton.icon(
              style: ElevatedButton.styleFrom(
                backgroundColor:
                    running ? Colors.red.shade600 : Colors.green.shade700,
                foregroundColor: Colors.white,
                shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(30)),
                elevation: running ? 4 : 2,
              ),
              icon: Icon(
                  running ? Icons.stop_circle : Icons.play_circle_fill,
                  size: 24),
              label: Text(
                running
                    ? (isNe ? 'रोक्नुस्' : 'Stop')
                    : (isNe ? 'सुरु गर्नुस्' : 'Start'),
                style: const TextStyle(
                    fontSize: 16, fontWeight: FontWeight.bold),
              ),
              onPressed: running ? onStop : onStart,
            ),
          ),

          if (!running && mins > 0) ...[
            const SizedBox(height: 8),
            Text(
              isNe
                  ? '✓ ${mins.toStringAsFixed(1)} मिनेट रेकर्ड भयो'
                  : '✓ ${mins.toStringAsFixed(1)} min recorded',
              style: TextStyle(
                  color: Colors.green.shade700,
                  fontSize: 12,
                  fontWeight: FontWeight.w600),
            ),
          ],

          if (!running && mins == 0)
            Padding(
              padding: const EdgeInsets.only(top: 6),
              child: Text(
                isNe
                    ? 'काम सुरु हुँदा "सुरु गर्नुस्" थिच्नुस्'
                    : 'Press Start when the machine begins working',
                style: const TextStyle(
                    color: Colors.black38, fontSize: 11.5),
                textAlign: TextAlign.center,
              ),
            ),
        ],
      ),
    );
  }
}

// ── Shared helper widgets ────────────────────────────────────────────────────

class _Label extends StatelessWidget {
  final String text;
  const _Label(this.text);
  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.only(bottom: 6),
    child: Text(text,
        style: const TextStyle(
            fontWeight: FontWeight.w600, fontSize: 13)),
  );
}

class _TypeBtn extends StatelessWidget {
  final String label;
  final bool selected;
  final VoidCallback onTap;
  const _TypeBtn({
    required this.label,
    required this.selected,
    required this.onTap,
  });
  @override
  Widget build(BuildContext context) => Expanded(
    child: InkWell(
      onTap: onTap,
      borderRadius: BorderRadius.circular(10),
      child: AnimatedContainer(
        duration: const Duration(milliseconds: 200),
        padding: const EdgeInsets.symmetric(vertical: 12),
        decoration: BoxDecoration(
          color: selected
              ? const Color(0xFF003893)
              : Colors.grey.shade100,
          borderRadius: BorderRadius.circular(10),
          border: Border.all(
            color: selected
                ? const Color(0xFF003893)
                : Colors.black12,
          ),
        ),
        alignment: Alignment.center,
        child: Text(
          label,
          style: TextStyle(
            color: selected ? Colors.white : Colors.black87,
            fontWeight: FontWeight.bold,
            fontSize: 13,
          ),
        ),
      ),
    ),
  );
}

class _TranslatedDropdown extends StatelessWidget {
  final String value;
  final List<String> items;
  final Map<String, String> nepaliMap;
  final bool isNe;
  final ValueChanged<String?> onChanged;
  const _TranslatedDropdown({
    required this.value,
    required this.items,
    required this.nepaliMap,
    required this.isNe,
    required this.onChanged,
  });
  @override
  Widget build(BuildContext context) => DropdownButtonFormField<String>(
    value: value,
    onChanged: onChanged,
    decoration: InputDecoration(
      contentPadding:
          const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
      border: OutlineInputBorder(borderRadius: BorderRadius.circular(8)),
    ),
    items: items
        .map((key) => DropdownMenuItem(
              value: key,
              child: Text(isNe ? (nepaliMap[key] ?? key) : key),
            ))
        .toList(),
  );
}
