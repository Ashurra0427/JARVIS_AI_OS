// lib/screens/request/request_job_screen.dart
//
// Redesigned to match agro_operator's add_job_screen visual language:
//   • Same navy (0xFF003893) AppBar + buttons
//   • _Label / _TypeBtn / _TranslatedDropdown helper widgets (identical API)
//   • Service dropdown with Nepali translations instead of plain text input
//   • Transport material dropdown
//   • Preferred date picker with calendar-icon container (same as operator)
//   • CircularProgressIndicator on submit (not just "...")
//   • Consistent rounded OutlineInputBorder on all fields + navy focus ring
// ─────────────────────────────────────────────────────────────────────────────

import 'dart:async';
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import '../../providers/customer_auth_provider.dart';
import '../../providers/language_provider.dart';
import '../../services/ws_service.dart';
import '../../utils/bs_date_utils.dart';
import '../../widgets/bs_date_picker.dart';

// ── Inline constants (mirrors agro_operator/lib/config/constants.dart) ────────

const List<String> _kAgriServices = [
  'Ploughing',
  'Rotavator',
  'Seed Sowing',
  'Harvest Support',
  'Water Pumping',
  'Other',
];

const Map<String, String> _kAgriServiceNe = {
  'Ploughing':       'जोताई',
  'Rotavator':       'रोटाभेटर',
  'Seed Sowing':     'बिउ छर्ने',
  'Harvest Support': 'कटनी',
  'Water Pumping':   'पानी पम्पिङ',
  'Other':           'अन्य',
};

const List<String> _kTransportMaterials = [
  'Gitti',
  'Baluwa',
  'Dhunga',
  'Cement',
  'Miscutt',
  'Plaster Baluwa',
  'Jodai Baluwa',
  'Sand',
  'Other',
];

const Map<String, String> _kTransportMaterialNe = {
  'Gitti':  'गिट्टी',
  'Baluwa': 'बालुवा',
  'Dhunga': 'ढुङ्गा',
  'Cement': 'सिमेन्ट',
  'Miscutt':        'मिसकट',
  'Plaster Baluwa': 'प्लास्टर बालुवा',
  'Jodai Baluwa':   'जोडाई बालुवा',
  'Sand':   'बालुवा (बालु)',
  'Other':  'अन्य',
};

// ─────────────────────────────────────────────────────────────────────────────

class RequestJobScreen extends StatefulWidget {
  const RequestJobScreen({super.key});

  @override
  State<RequestJobScreen> createState() => _RequestJobScreenState();
}

class _RequestJobScreenState extends State<RequestJobScreen> {
  String    _jobType  = 'agriculture';
  String    _service  = _kAgriServices.first;
  String    _material = _kTransportMaterials.first;
  DateTime? _preferredDate;
  final     _notes    = TextEditingController();
  bool      _submitting = false;
  StreamSubscription? _sub;

  @override
  void dispose() {
    _notes.dispose();
    _sub?.cancel();
    super.dispose();
  }

  Future<void> _pickDate() async {
    final isNe = context.read<LanguageProvider>().isNepali;
    final picked = await showBsDatePicker(
      context: context,
      initialDate: DateTime.now().add(const Duration(days: 1)),
      firstDate: DateTime.now(),
      lastDate: DateTime.now().add(const Duration(days: 90)),
      isNe: isNe,
    );
    if (picked != null) setState(() => _preferredDate = picked);
  }

  Future<void> _submit() async {
    final isNe = context.read<LanguageProvider>().isNepali;
    setState(() => _submitting = true);

    final auth = context.read<CustomerAuthProvider>();
    final completer = Completer<Map<String, dynamic>>();
    _sub?.cancel();
    _sub = context.read<WsService>().stream.listen((msg) {
      if (msg['type'] == 'customer_result' && msg['action'] == 'request_job') {
        final data = (msg['data'] as Map<String, dynamic>?) ?? {};
        if (!completer.isCompleted) completer.complete(data);
      }
    });

    auth.sendCustomerAction('request_job', {
      'job_type': _jobType,
      'service':  _jobType == 'agriculture' ? _service : _material,
      'notes':    _notes.text.trim(),
      'preferred_date': _preferredDate?.toIso8601String().substring(0, 10),
    });

    final result = await Future.any([
      completer.future,
      Future.delayed(
        const Duration(seconds: 6),
        () => <String, dynamic>{'success': false, 'error': 'Timed out'},
      ),
    ]);
    _sub?.cancel();

    if (mounted) {
      setState(() => _submitting = false);
      if (result['success'] == true) {
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(
          content: Text(isNe ? 'अनुरोध पठाइयो!' : 'Request sent!'),
          backgroundColor: Colors.green,
        ));
        Navigator.pop(context);
      } else {
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(
          content: Text((result['error'] ?? 'Failed').toString()),
          backgroundColor: Colors.red,
        ));
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final isNe = context.watch<LanguageProvider>().isNepali;

    return Scaffold(
      appBar: AppBar(
        backgroundColor: const Color(0xFF003893),
        foregroundColor: Colors.white,
        title: Text(isNe ? 'काम मागनुहोस्' : 'Request a Job'),
      ),
      body: SingleChildScrollView(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [

            // ── Subtitle ─────────────────────────────────────────────
            Text(
              isNe
                  ? 'यो अनुरोध हो — पुष्टि अप्रेटरले गर्नेछ।'
                  : 'This is just a request — the operator will confirm it before it becomes a real job.',
              style: const TextStyle(color: Colors.black54, fontSize: 12.5),
            ),
            const SizedBox(height: 20),

            // ── Job type toggle ──────────────────────────────────────
            _Label(isNe ? 'काम को किसिम' : 'Job Type'),
            Row(children: [
              _TypeBtn(
                label: isNe ? '🌾 कृषि' : '🌾 Agriculture',
                selected: _jobType == 'agriculture',
                onTap: () => setState(() {
                  _jobType = 'agriculture';
                  _service = _kAgriServices.first;
                }),
              ),
              const SizedBox(width: 10),
              _TypeBtn(
                label: isNe ? '🚜 यातायात' : '🚜 Transport',
                selected: _jobType == 'transport',
                onTap: () => setState(() {
                  _jobType  = 'transport';
                  _material = _kTransportMaterials.first;
                }),
              ),
            ]),
            const SizedBox(height: 16),

            // ── Service / material dropdown ──────────────────────────
            if (_jobType == 'agriculture') ...[
              _Label(isNe ? 'सेवा' : 'Service'),
              _TranslatedDropdown(
                value: _service,
                items: _kAgriServices,
                nepaliMap: _kAgriServiceNe,
                isNe: isNe,
                onChanged: (v) => setState(() => _service = v!),
              ),
            ] else ...[
              _Label(isNe ? 'सामग्री' : 'Material'),
              _TranslatedDropdown(
                value: _material,
                items: _kTransportMaterials,
                nepaliMap: _kTransportMaterialNe,
                isNe: isNe,
                onChanged: (v) => setState(() => _material = v!),
              ),
            ],
            const SizedBox(height: 16),

            // ── Preferred date ───────────────────────────────────────
            _Label(isNe ? 'मिति (वैकल्पिक)' : 'Preferred Date (optional)'),
            InkWell(
              onTap: _pickDate,
              borderRadius: BorderRadius.circular(8),
              child: Container(
                padding: const EdgeInsets.symmetric(vertical: 14, horizontal: 12),
                decoration: BoxDecoration(
                  border: Border.all(color: Colors.black26),
                  borderRadius: BorderRadius.circular(8),
                ),
                child: Row(children: [
                  const Icon(Icons.calendar_today, size: 18, color: Colors.black54),
                  const SizedBox(width: 8),
                  _preferredDate == null
                      ? Text(
                          isNe ? 'मिति चयन गर्नुहोस्' : 'Select a date',
                          style: const TextStyle(color: Colors.black45),
                        )
                      : Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(
                              formatBsLong(_preferredDate!, isNe: isNe),
                              style: const TextStyle(
                                color: Colors.black87,
                                fontWeight: FontWeight.w600,
                              ),
                            ),
                            Text(
                              isNe
                                  ? '${_preferredDate!.toIso8601String().substring(0, 10)} (ई.)'
                                  : '${_preferredDate!.toIso8601String().substring(0, 10)} (AD)',
                              style: const TextStyle(fontSize: 11, color: Colors.black45),
                            ),
                          ],
                        ),
                  if (_preferredDate != null) ...[
                    const Spacer(),
                    GestureDetector(
                      onTap: () => setState(() => _preferredDate = null),
                      child: const Icon(Icons.close, size: 16, color: Colors.black38),
                    ),
                  ],
                ]),
              ),
            ),
            const SizedBox(height: 16),

            // ── Notes ────────────────────────────────────────────────
            _Label(isNe ? 'थप जानकारी' : 'Notes'),
            TextFormField(
              controller: _notes,
              maxLines: 4,
              decoration: _dec(isNe ? 'थप विवरण...' : 'Additional details...'),
            ),
            const SizedBox(height: 28),

            // ── Submit ───────────────────────────────────────────────
            SizedBox(
              width: double.infinity,
              height: 50,
              child: ElevatedButton(
                style: ElevatedButton.styleFrom(
                  backgroundColor: const Color(0xFF003893),
                  foregroundColor: Colors.white,
                  shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(10),
                  ),
                ),
                onPressed: _submitting ? null : _submit,
                child: _submitting
                    ? const CircularProgressIndicator(
                        color: Colors.white,
                        strokeWidth: 2,
                      )
                    : Text(
                        isNe ? 'पठाउनुहोस्' : 'Send Request',
                        style: const TextStyle(
                          fontSize: 16,
                          fontWeight: FontWeight.bold,
                        ),
                      ),
              ),
            ),
            const SizedBox(height: 20),
          ],
        ),
      ),
    );
  }

  InputDecoration _dec(String hint) => InputDecoration(
    hintText: hint,
    contentPadding: const EdgeInsets.symmetric(horizontal: 12, vertical: 12),
    border: OutlineInputBorder(borderRadius: BorderRadius.circular(8)),
    focusedBorder: OutlineInputBorder(
      borderRadius: BorderRadius.circular(8),
      borderSide: const BorderSide(color: Color(0xFF003893), width: 1.8),
    ),
  );
}

// ── Helper widgets (same API as agro_operator's add_job_screen) ───────────────

class _Label extends StatelessWidget {
  final String text;
  const _Label(this.text);

  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.only(bottom: 6),
    child: Text(
      text,
      style: const TextStyle(fontWeight: FontWeight.w600, fontSize: 13),
    ),
  );
}

class _TypeBtn extends StatelessWidget {
  final String label;
  final bool selected;
  final VoidCallback onTap;
  const _TypeBtn({required this.label, required this.selected, required this.onTap});

  @override
  Widget build(BuildContext context) => Expanded(
    child: InkWell(
      onTap: onTap,
      borderRadius: BorderRadius.circular(10),
      child: AnimatedContainer(
        duration: const Duration(milliseconds: 200),
        padding: const EdgeInsets.symmetric(vertical: 12),
        decoration: BoxDecoration(
          color: selected ? const Color(0xFF003893) : Colors.grey.shade100,
          borderRadius: BorderRadius.circular(10),
          border: Border.all(
            color: selected ? const Color(0xFF003893) : Colors.black12,
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
      contentPadding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
      border: OutlineInputBorder(borderRadius: BorderRadius.circular(8)),
      focusedBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(8),
        borderSide: const BorderSide(color: Color(0xFF003893), width: 1.8),
      ),
    ),
    items: items
        .map((key) => DropdownMenuItem(
              value: key,
              child: Text(isNe ? (nepaliMap[key] ?? key) : key),
            ))
        .toList(),
  );
}
