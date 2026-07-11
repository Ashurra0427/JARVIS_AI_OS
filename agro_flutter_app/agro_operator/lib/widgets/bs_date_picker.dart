// lib/widgets/bs_date_picker.dart
//
// BS (Bikram Sambat) date picker — primary calendar for the app.
// Shows the equivalent AD date live as a secondary reference underneath,
// and offers an explicit "Use AD calendar instead" fallback for anyone who
// prefers picking in Gregorian. Always resolves to and returns a plain AD
// DateTime — the BS/AD choice is purely how the operator picks the date,
// not how it's stored (see bs_date_utils.dart for why).

import 'package:flutter/material.dart';
import '../utils/bs_date_utils.dart';

/// Shows the BS date picker. Returns the picked date as AD [DateTime], or
/// null if cancelled. [firstDate]/[lastDate] are AD bounds (kept in AD so
/// callers don't need to think in BS years).
Future<DateTime?> showBsDatePicker({
  required BuildContext context,
  required DateTime initialDate,
  required DateTime firstDate,
  required DateTime lastDate,
  required bool isNe,
}) {
  return showDialog<DateTime>(
    context: context,
    builder: (ctx) => _BsDatePickerDialog(
      initialDate: initialDate,
      firstDate: firstDate,
      lastDate: lastDate,
      isNe: isNe,
    ),
  );
}

class _BsDatePickerDialog extends StatefulWidget {
  final DateTime initialDate;
  final DateTime firstDate;
  final DateTime lastDate;
  final bool isNe;

  const _BsDatePickerDialog({
    required this.initialDate,
    required this.firstDate,
    required this.lastDate,
    required this.isNe,
  });

  @override
  State<_BsDatePickerDialog> createState() => _BsDatePickerDialogState();
}

class _BsDatePickerDialogState extends State<_BsDatePickerDialog> {
  late int _year;
  late int _month;
  late int _day;
  late int _firstBsYear;
  late int _lastBsYear;

  @override
  void initState() {
    super.initState();
    final initBs = adToBs(widget.initialDate);
    _year  = initBs.year;
    _month = initBs.month;
    _day   = initBs.day;
    _firstBsYear = adToBs(widget.firstDate).year;
    _lastBsYear  = adToBs(widget.lastDate).year;
    if (_firstBsYear > _lastBsYear) {
      // Degenerate bounds guard — shouldn't happen, but keep the wheel valid.
      _lastBsYear = _firstBsYear;
    }
  }

  DateTime get _selectedAd {
    final maxDay = daysInBsMonth(_year, _month);
    final d = _day > maxDay ? maxDay : _day;
    return bsToAd(_year, _month, d);
  }

  @override
  Widget build(BuildContext context) {
    final isNe = widget.isNe;
    final yearCount = (_lastBsYear - _firstBsYear + 1).clamp(1, 200);
    final dayCount = daysInBsMonth(_year, _month);
    if (_day > dayCount) _day = dayCount;

    return AlertDialog(
      title: Text(isNe ? 'मिति छान्नुस् (वि.सं.)' : 'Pick Date (BS)'),
      content: SizedBox(
        height: 220,
        width: 300,
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            SizedBox(
              height: 160,
              child: Row(
                children: [
                  // Year
                  Expanded(
                    child: ListWheelScrollView.useDelegate(
                      itemExtent: 40,
                      perspective: 0.003,
                      controller: FixedExtentScrollController(
                        initialItem: _year - _firstBsYear,
                      ),
                      onSelectedItemChanged: (i) =>
                          setState(() => _year = _firstBsYear + i),
                      childDelegate: ListWheelChildBuilderDelegate(
                        childCount: yearCount,
                        builder: (_, i) => Center(
                          child: Text(
                            '${_firstBsYear + i}',
                            style: TextStyle(
                              fontSize: 17,
                              fontWeight: (_firstBsYear + i) == _year
                                  ? FontWeight.bold
                                  : FontWeight.normal,
                            ),
                          ),
                        ),
                      ),
                    ),
                  ),
                  // Month
                  Expanded(
                    flex: 2,
                    child: ListWheelScrollView.useDelegate(
                      itemExtent: 40,
                      perspective: 0.003,
                      controller: FixedExtentScrollController(initialItem: _month - 1),
                      onSelectedItemChanged: (i) => setState(() => _month = i + 1),
                      childDelegate: ListWheelChildBuilderDelegate(
                        childCount: 12,
                        builder: (_, i) => Center(
                          child: Text(
                            bsMonthName(i + 1, isNe),
                            style: TextStyle(
                              fontSize: 16,
                              fontWeight: (i + 1) == _month
                                  ? FontWeight.bold
                                  : FontWeight.normal,
                            ),
                          ),
                        ),
                      ),
                    ),
                  ),
                  // Day
                  Expanded(
                    child: ListWheelScrollView.useDelegate(
                      itemExtent: 40,
                      perspective: 0.003,
                      controller: FixedExtentScrollController(initialItem: _day - 1),
                      onSelectedItemChanged: (i) => setState(() => _day = i + 1),
                      childDelegate: ListWheelChildBuilderDelegate(
                        childCount: dayCount,
                        builder: (_, i) => Center(
                          child: Text(
                            '${i + 1}',
                            style: TextStyle(
                              fontSize: 17,
                              fontWeight: (i + 1) == _day
                                  ? FontWeight.bold
                                  : FontWeight.normal,
                            ),
                          ),
                        ),
                      ),
                    ),
                  ),
                ],
              ),
            ),
            const SizedBox(height: 8),
            Text(
              isNe
                  ? '= ${formatAdShort(_selectedAd)} (ई.)'
                  : '= ${formatAdShort(_selectedAd)} (AD)',
              style: const TextStyle(color: Colors.black54, fontSize: 13),
            ),
          ],
        ),
      ),
      actions: [
        TextButton(
          onPressed: () async {
            // Secondary path: fall back to the plain AD calendar for
            // anyone who'd rather pick in Gregorian. Stack the AD picker
            // on top of this dialog (don't pop first) so there's exactly
            // one pop, carrying the AD result straight back to whoever
            // called showBsDatePicker().
            final adPicked = await showDatePicker(
              context: context,
              initialDate: widget.initialDate,
              firstDate: widget.firstDate,
              lastDate: widget.lastDate,
            );
            if (adPicked != null && context.mounted) {
              Navigator.pop(context, adPicked);
            }
          },
          child: Text(isNe ? 'ई. पात्रो प्रयोग गर्नुस्' : 'Use AD calendar'),
        ),
        TextButton(
          onPressed: () => Navigator.pop(context),
          child: Text(isNe ? 'रद्द' : 'Cancel'),
        ),
        ElevatedButton(
          onPressed: () => Navigator.pop(context, _selectedAd),
          child: Text(isNe ? 'ठीक छ' : 'OK'),
        ),
      ],
    );
  }
}
