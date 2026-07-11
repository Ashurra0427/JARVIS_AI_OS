// lib/screens/fuel/log_fuel_screen.dart
import 'dart:async';
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import '../../config/constants.dart';
import '../../providers/language_provider.dart';
import '../../services/ws_service.dart';
import '../../models/fuel_log.dart';

class LogFuelScreen extends StatefulWidget {
  const LogFuelScreen({super.key});
  @override
  State<LogFuelScreen> createState() => _LogFuelScreenState();
}

class _LogFuelScreenState extends State<LogFuelScreen> {
  final _form  = GlobalKey<FormState>();
  String _fuelType = 'Diesel';
  final _liters      = TextEditingController();
  final _pricePerL   = TextEditingController();
  final _totalCost   = TextEditingController();
  final _pump        = TextEditingController();
  final _notes       = TextEditingController();
  bool _submitting   = false;

  @override
  void dispose() {
    for (final c in [_liters, _pricePerL, _totalCost, _pump, _notes]) c.dispose();
    super.dispose();
  }

  void _computeTotal() {
    final l = double.tryParse(_liters.text);
    final p = double.tryParse(_pricePerL.text);
    if (l != null && p != null) _totalCost.text = (l * p).toStringAsFixed(0);
  }

  Future<void> _submit() async {
    if (!_form.currentState!.validate()) return;
    setState(() => _submitting = true);

    final ws = context.read<WsService>();
    ws.sendAgroAction('log_fuel',
      FuelLog(
        fuelType:      _fuelType,
        liters:        double.parse(_liters.text),
        pricePerLiter: double.tryParse(_pricePerL.text),
        totalCost:     double.tryParse(_totalCost.text),
        petrolPump:    _pump.text.trim(),
        notes:         _notes.text.trim(),
      ).toJson(),
    );

    // Wait for the server's agro_result confirmation (log_fuel) before
    // dismissing this screen instead of assuming the send succeeded.
    final completer = Completer<Map<String, dynamic>?>();
    StreamSubscription? sub;
    sub = ws.stream.listen((msg) {
      if (msg['type'] == 'agro_result' && msg['action'] == 'log_fuel') {
        if (!completer.isCompleted) completer.complete(msg['data'] as Map<String, dynamic>?);
      }
    });

    Map<String, dynamic>? result;
    try {
      result = await completer.future.timeout(const Duration(seconds: 6));
    } catch (_) {
      result = null; // timeout
    } finally {
      await sub.cancel();
    }

    if (!mounted) return;
    setState(() => _submitting = false);

    final isNe = context.read<LanguageProvider>().isNepali;
    final success = result != null && result['success'] != false;

    if (success) {
      ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('Fuel logged!'), backgroundColor: Colors.green));
      Navigator.pop(context);
    } else {
      final errorMsg = (result?['message'] as String?) ??
          (isNe
              ? 'इन्धन लग गर्न असफल भयो। फेरि प्रयास गर्नुहोस्।'
              : 'Failed to log fuel. Please try again.');
      ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(errorMsg), backgroundColor: Colors.red));
    }
  }

  @override
  Widget build(BuildContext context) {
    final lang = context.watch<LanguageProvider>();
    final isNe = lang.isNepali;
    return Scaffold(
      appBar: AppBar(
        backgroundColor: Colors.orange.shade700,
        foregroundColor: Colors.white,
        title: Text(isNe ? 'इन्धन रेकर्ड' : 'Log Fuel'),
      ),
      body: Form(
        key: _form,
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(16),
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Text(isNe ? 'इन्धनको किसिम' : 'Fuel Type',
                style: const TextStyle(fontWeight: FontWeight.w600)),
            const SizedBox(height: 6),
            Row(children: kFuelTypes.map((f) => Expanded(
              child: Padding(
                padding: EdgeInsets.only(right: f == kFuelTypes.last ? 0 : 10),
                child: InkWell(
                  onTap: () => setState(() => _fuelType = f),
                  borderRadius: BorderRadius.circular(10),
                  child: AnimatedContainer(
                    duration: const Duration(milliseconds: 200),
                    padding: const EdgeInsets.symmetric(vertical: 14),
                    decoration: BoxDecoration(
                      color: _fuelType == f ? Colors.orange.shade700 : Colors.grey.shade100,
                      borderRadius: BorderRadius.circular(10),
                    ),
                    alignment: Alignment.center,
                    child: Text(f,
                        style: TextStyle(
                          color: _fuelType == f ? Colors.white : Colors.black87,
                          fontWeight: FontWeight.bold,
                        )),
                  ),
                ),
              ),
            )).toList()),
            const SizedBox(height: 16),
            TextFormField(
              controller: _liters,
              keyboardType: TextInputType.number,
              decoration: InputDecoration(
                labelText: isNe ? 'लिटर' : 'Liters',
                border: const OutlineInputBorder(),
              ),
              validator: (v) => (v?.isEmpty ?? true) ? 'Required' : null,
              onChanged: (_) => _computeTotal(),
            ),
            const SizedBox(height: 14),
            TextFormField(
              controller: _pricePerL,
              keyboardType: TextInputType.number,
              decoration: InputDecoration(
                labelText: isNe ? 'प्रति लिटर मूल्य' : 'Price per Liter',
                border: const OutlineInputBorder(),
              ),
              onChanged: (_) => _computeTotal(),
            ),
            const SizedBox(height: 14),
            TextFormField(
              controller: _totalCost,
              keyboardType: TextInputType.number,
              decoration: InputDecoration(
                labelText: isNe ? 'जम्मा खर्च' : 'Total Cost',
                border: const OutlineInputBorder(),
              ),
            ),
            const SizedBox(height: 14),
            TextFormField(
              controller: _pump,
              decoration: InputDecoration(
                labelText: isNe ? 'पेट्रोल पम्पको नाम' : 'Petrol Pump Name',
                border: const OutlineInputBorder(),
              ),
            ),
            const SizedBox(height: 14),
            TextFormField(
              controller: _notes,
              maxLines: 2,
              decoration: InputDecoration(
                labelText: isNe ? 'नोट' : 'Notes',
                border: const OutlineInputBorder(),
              ),
            ),
            const SizedBox(height: 24),
            SizedBox(
              width: double.infinity, height: 48,
              child: ElevatedButton(
                style: ElevatedButton.styleFrom(
                  backgroundColor: Colors.orange.shade700,
                  foregroundColor: Colors.white,
                  shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
                ),
                onPressed: _submitting ? null : _submit,
                child: Text(_submitting ? '...' : (isNe ? 'सुरक्षित गर्नुस्' : 'Save'),
                    style: const TextStyle(fontSize: 16, fontWeight: FontWeight.bold)),
              ),
            ),
          ]),
        ),
      ),
    );
  }
}