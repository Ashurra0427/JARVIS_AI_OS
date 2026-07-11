// lib/services/report_download_service.dart
//
// Actually downloads a generated Excel report onto the phone and opens it.
//
// Previously "Download" in daily/monthly report screens only showed the
// SERVER's local file path in a SnackBar (e.g. "Excel ready:
// /home/.../datastore/agro/exports/daily_2026-07-01.xlsx") — a path on the
// backend machine, meaningless and inaccessible to a phone. This service
// fixes that: it fetches the actual file bytes over HTTP from the existing
// /api/report/* endpoints, writes them into the app's documents directory
// (always writable, no extra storage permission needed), then opens it with
// the device's default viewer. If nothing can open an .xlsx file, it falls
// back to the share sheet so the operator can still save it via Drive,
// WhatsApp, Bluetooth, etc.

import 'dart:io';
import 'package:http/http.dart' as http;
import 'package:open_filex/open_filex.dart';
import 'package:path_provider/path_provider.dart';
import 'package:share_plus/share_plus.dart';
import '../config/app_config.dart';

class ReportDownloadService {
  static Future<File> downloadDaily(String date) => _download(
        '/api/report/daily?date=$date',
        'daily_report_$date.xlsx',
      );

  static Future<File> downloadMonthly(String month) => _download(
        '/api/report/monthly?month=$month',
        'monthly_report_$month.xlsx',
      );

  static Future<File> _download(String path, String filename) async {
    final uri = Uri.parse('${AppConfig.serverUrl}$path');
    final resp = await http.get(uri).timeout(const Duration(seconds: 30));
    if (resp.statusCode != 200) {
      throw Exception('Server returned ${resp.statusCode} for $path');
    }
    final dir  = await getApplicationDocumentsDirectory();
    final file = File('${dir.path}/$filename');
    await file.writeAsBytes(resp.bodyBytes, flush: true);
    return file;
  }

  /// Opens the file with the device's default app for .xlsx. If none is
  /// installed (OpenFilex returns anything other than `done`), falls back
  /// to the OS share sheet so the operator can still get the file out —
  /// e.g. save to Drive/Files or send over WhatsApp.
  static Future<void> openOrShare(File file) async {
    final result = await OpenFilex.open(file.path);
    if (result.type != ResultType.done) {
      await Share.shareXFiles([XFile(file.path)]);
    }
  }

  /// Convenience: download + immediately open/share in one call.
  static Future<void> downloadAndOpenDaily(String date) async {
    final file = await downloadDaily(date);
    await openOrShare(file);
  }

  static Future<void> downloadAndOpenMonthly(String month) async {
    final file = await downloadMonthly(month);
    await openOrShare(file);
  }
}
