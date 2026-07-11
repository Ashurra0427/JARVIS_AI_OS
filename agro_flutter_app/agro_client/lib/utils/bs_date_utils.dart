// lib/utils/bs_date_utils.dart
//
// Bikram Sambat (BS) calendar helpers, built on the `nepali_utils` package.
//
// Design choice: BS is a DISPLAY/INPUT layer only. Every date this app
// stores or sends to the server (job.scheduledDate, report periods, DB
// columns) stays a plain Gregorian (AD) ISO string exactly as before — the
// backend, database schema, and Excel exporter all already assume AD dates,
// and changing that contract would be a much bigger, riskier change than
// what was actually asked for. So: operators pick/see BS dates, the app
// converts to/from AD at the boundary, and everything downstream is
// unaffected.
//
// Deliberately built only on NepaliDateTime's constructor and
// fromDateTime()/toDateTime() conversion — the stable, well-documented core
// of the package — rather than newer/less-common helper methods, since
// those are the calls this app can rely on staying available across
// package versions.

import 'package:nepali_utils/nepali_utils.dart';

/// BS month names, index 0 = Baishakh .. 11 = Chaitra.
const List<String> bsMonthNamesEn = [
  'Baishakh', 'Jestha', 'Ashadh', 'Shrawan', 'Bhadra', 'Ashwin',
  'Kartik', 'Mangsir', 'Poush', 'Magh', 'Falgun', 'Chaitra',
];

const List<String> bsMonthNamesNe = [
  'बैशाख', 'जेठ', 'असार', 'श्रावण', 'भदौ', 'आश्विन',
  'कार्तिक', 'मंसिर', 'पुष', 'माघ', 'फाल्गुन', 'चैत',
];

String bsMonthName(int month, bool isNe) =>
    (isNe ? bsMonthNamesNe : bsMonthNamesEn)[(month - 1).clamp(0, 11)];

/// Today, in BS.
NepaliDateTime nowBs() => NepaliDateTime.now();

/// Convert an AD [DateTime] to its BS equivalent.
NepaliDateTime adToBs(DateTime ad) => NepaliDateTime.fromDateTime(ad);

/// Convert a BS y/m/d to the equivalent AD [DateTime].
DateTime bsToAd(int year, int month, int day) =>
    NepaliDateTime(year, month, day).toDateTime();

/// Number of days in a given BS month. BS months don't have fixed lengths
/// (they vary year to year), so this is computed by diffing the AD-mapped
/// start of this BS month against the start of the next one — safe because
/// it only relies on the constructor + toDateTime(), not on any
/// calendar-length lookup API that may differ between package versions.
int daysInBsMonth(int year, int month) {
  final thisStart = NepaliDateTime(year, month, 1).toDateTime();
  final nextYear  = month == 12 ? year + 1 : year;
  final nextMonth = month == 12 ? 1 : month + 1;
  final nextStart = NepaliDateTime(nextYear, nextMonth, 1).toDateTime();
  return nextStart.difference(thisStart).inDays;
}

/// "2083 Ashadh 18" / "२०८३ असार १८" style label.
String formatBsLong(DateTime ad, {required bool isNe}) {
  final bs = adToBs(ad);
  final y  = isNe ? _toNepaliDigits(bs.year) : '${bs.year}';
  final d  = isNe ? _toNepaliDigits(bs.day)  : '${bs.day}';
  return '$y ${bsMonthName(bs.month, isNe)} $d';
}

/// "2083-03-18" style — used where a compact BS label is wanted (e.g. next
/// to the AD date in a subtitle) without pulling in month names.
String formatBsShort(DateTime ad) {
  final bs = adToBs(ad);
  return '${bs.year}-${bs.month.toString().padLeft(2, '0')}-${bs.day.toString().padLeft(2, '0')}';
}

/// "July 2, 2026" / plain ISO AD label — kept short since AD is the
/// secondary reference, not the primary label, once BS is wired in.
String formatAdShort(DateTime ad) =>
    '${ad.year}-${ad.month.toString().padLeft(2, '0')}-${ad.day.toString().padLeft(2, '0')}';

const _neDigits = ['०', '१', '२', '३', '४', '५', '६', '७', '८', '९'];

String _toNepaliDigits(int n) =>
    n.toString().split('').map((c) {
      final i = int.tryParse(c);
      return i == null ? c : _neDigits[i];
    }).join();
