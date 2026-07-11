// lib/screens/expense/log_expense_screen.dart
import 'dart:async';
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import '../../config/constants.dart';
import '../../providers/language_provider.dart';
import '../../services/ws_service.dart';

class LogExpenseScreen extends StatefulWidget {
  const LogExpenseScreen({super.key});
  @override
  State<LogExpenseScreen> createState() => _LogExpenseScreenState();
}

class _LogExpenseScreenState extends State<LogExpenseScreen> {
  final _form     = GlobalKey<FormState>();
  String _category = kExpenseCategories.first;
  final _amount   = TextEditingController();
  final _desc     = TextEditingController();
  final _receipt  = TextEditingController();
  bool _submitting = false;
  StreamSubscription? _sub;

  @override
  void dispose() {
    for (final c in [_amount, _desc, _receipt]) c.dispose();
    _sub?.cancel();
    super.dispose();
  }

  Future<void> _submit() async {
    if (!_form.currentState!.validate()) return;
    setState(() => _submitting = true);

    final ws = context.read<WsService>();
    final lang = context.read<LanguageProvider>();
    final isNe = lang.isNepali;

    final completer = Completer<bool>();
    _sub?.cancel();
    _sub = ws.stream.listen((msg) {
      if (msg['type'] == 'agro_result' && msg['action'] == 'log_expense') {
        if (!completer.isCompleted) completer.complete(true);
      }
    });

    ws.sendAgroAction('log_expense', {
      'category':    _category,
      'amount':      double.parse(_amount.text),
      'description': _desc.text.trim(),
      'receipt_ref': _receipt.text.trim(),
    });

    await Future.any([completer.future, Future.delayed(const Duration(seconds: 5))]);
    _sub?.cancel();

    if (mounted) {
      setState(() => _submitting = false);
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(isNe ? 'खर्च दर्ता भयो!' : 'Expense logged!'),
        backgroundColor: Colors.green,
      ));
      Navigator.pop(context);
    }
  }

  @override
  Widget build(BuildContext context) {
    final lang = context.watch<LanguageProvider>();
    final isNe = lang.isNepali;
    return Scaffold(
      appBar: AppBar(
        backgroundColor: Colors.red.shade700,
        foregroundColor: Colors.white,
        title: Text(isNe ? 'खर्च रेकर्ड' : 'Log Expense'),
      ),
      body: Form(
        key: _form,
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(16),
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Text(isNe ? 'खर्चको वर्ग' : 'Category',
                style: const TextStyle(fontWeight: FontWeight.w600)),
            const SizedBox(height: 6),
            Wrap(spacing: 8, runSpacing: 8,
                children: kExpenseCategories.map((cat) => ChoiceChip(
                  label: Text(cat),
                  selected: _category == cat,
                  onSelected: (_) => setState(() => _category = cat),
                  selectedColor: Colors.red.shade700,
                  labelStyle: TextStyle(
                      color: _category == cat ? Colors.white : Colors.black87),
                )).toList()),
            const SizedBox(height: 16),
            TextFormField(
              controller: _amount,
              keyboardType: TextInputType.number,
              decoration: InputDecoration(
                labelText: isNe ? 'रकम (Rs)' : 'Amount (Rs)',
                border: const OutlineInputBorder(),
              ),
              validator: (v) => (v?.isEmpty ?? true) ? 'Required' : null,
            ),
            const SizedBox(height: 14),
            TextFormField(
              controller: _desc,
              maxLines: 2,
              decoration: InputDecoration(
                labelText: isNe ? 'विवरण' : 'Description',
                border: const OutlineInputBorder(),
              ),
            ),
            const SizedBox(height: 14),
            TextFormField(
              controller: _receipt,
              decoration: InputDecoration(
                labelText: isNe ? 'रसिद नम्बर' : 'Receipt Ref',
                border: const OutlineInputBorder(),
              ),
            ),
            const SizedBox(height: 24),
            SizedBox(
              width: double.infinity, height: 48,
              child: ElevatedButton(
                style: ElevatedButton.styleFrom(
                  backgroundColor: Colors.red.shade700,
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