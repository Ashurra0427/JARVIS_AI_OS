// lib/services/local_db_service.dart
// Offline-first local SQLite via sqflite.
// Jobs created offline are queued here and synced when reconnected.

import 'dart:convert';
import 'package:sqflite/sqflite.dart';
import 'package:path/path.dart' as p;
import '../models/job.dart';
import '../models/daily_stats.dart';

class LocalDbService {
  static Database? _db;

  static Future<Database> get db async {
    _db ??= await _open();
    return _db!;
  }

  static Future<Database> _open() async {
    final path = p.join(await getDatabasesPath(), 'agro_local.db');
    return openDatabase(
      path,
      version: 1,
      onCreate: (db, v) async {
        await db.execute('''
          CREATE TABLE IF NOT EXISTS offline_queue (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            action   TEXT NOT NULL,
            payload  TEXT NOT NULL,
            created  INTEGER DEFAULT (strftime('%s','now'))
          )
        ''');
        await db.execute('''
          CREATE TABLE IF NOT EXISTS cached_jobs (
            id             INTEGER PRIMARY KEY,
            job_type       TEXT,
            service        TEXT,
            customer_name  TEXT,
            operator_name  TEXT,
            status         TEXT,
            total_amount   REAL,
            balance_due    REAL,
            scheduled_date TEXT,
            location       TEXT,
            raw_json       TEXT
          )
        ''');
        await db.execute('''
          CREATE TABLE IF NOT EXISTS cached_stats (
            date           TEXT PRIMARY KEY,
            total_jobs     INTEGER,
            completed_jobs INTEGER,
            pending_jobs   INTEGER,
            revenue        REAL,
            fuel_cost      REAL,
            other_expenses REAL,
            profit         REAL
          )
        ''');
      },
    );
  }

  // ── Offline queue ─────────────────────────────────────────────────────

  static Future<int> enqueue(String action, Map<String, dynamic> payload) async {
    final d = await db;
    return d.insert('offline_queue', {
      'action': action,
      'payload': jsonEncode(payload),
    });
  }

  static Future<List<Map<String, dynamic>>> getPendingQueue() async {
    final d = await db;
    final rows = await d.query('offline_queue', orderBy: 'created ASC');
    return rows.map((r) => {
      'id': r['id'],
      'action': r['action'],
      'data': jsonDecode(r['payload'] as String),
    }).toList();
  }

  static Future<void> removeFromQueue(int id) async {
    final d = await db;
    await d.delete('offline_queue', where: 'id = ?', whereArgs: [id]);
  }

  static Future<int> queueCount() async {
    final d = await db;
    final result = await d.rawQuery('SELECT COUNT(*) as c FROM offline_queue');
    return (result.first['c'] as int?) ?? 0;
  }

  // ── Job cache ─────────────────────────────────────────────────────────

  static Future<void> cacheJobs(List<Job> jobs) async {
    final d = await db;
    final batch = d.batch();
    for (final j in jobs) {
      batch.insert(
        'cached_jobs',
        {
          'id': j.id,
          'job_type': j.jobType,
          'service': j.displayService,
          'customer_name': j.customerName ?? '',
          'operator_name': j.operatorName ?? '',
          'status': j.status,
          'total_amount': j.totalAmount,
          'balance_due': j.balanceDue,
          'scheduled_date': j.scheduledDate,
          'location': j.location ?? '',
          'raw_json': jsonEncode(j.toMap()),
        },
        conflictAlgorithm: ConflictAlgorithm.replace,
      );
    }
    await batch.commit(noResult: true);
  }

  static Future<List<Job>> getCachedJobs({String? date}) async {
    final d = await db;
    final where = date != null ? 'scheduled_date = ?' : null;
    final args = date != null ? [date] : null;
    final rows = await d.query(
      'cached_jobs',
      where: where,
      whereArgs: args,
      orderBy: 'id DESC',
      limit: 100,
    );
    return rows.map((r) {
      try {
        return Job.fromMap(jsonDecode(r['raw_json'] as String) as Map<String, dynamic>);
      } catch (_) {
        return Job(
          id: r['id'] as int?,
          jobType: r['job_type'] as String? ?? 'agriculture',
          service: r['service'] as String? ?? '',
          customerName: r['customer_name'] as String?,
          status: r['status'] as String? ?? 'pending',
          totalAmount: (r['total_amount'] as num?)?.toDouble(),
          scheduledDate: r['scheduled_date'] as String?,
        );
      }
    }).toList();
  }

  // ── Stats cache ───────────────────────────────────────────────────────

  static Future<void> cacheStats(DailyStats stats) async {
    final d = await db;
    await d.insert(
      'cached_stats',
      {
        'date': stats.date,
        'total_jobs': stats.totalJobs,
        'completed_jobs': stats.completedJobs,
        'pending_jobs': stats.pendingJobs,
        'revenue': stats.revenue,
        'fuel_cost': stats.fuelCost,
        'other_expenses': stats.otherExpenses,
        'profit': stats.profit,
      },
      conflictAlgorithm: ConflictAlgorithm.replace,
    );
  }

  static Future<DailyStats?> getCachedStats(String date) async {
    final d = await db;
    final rows = await d.query('cached_stats', where: 'date = ?', whereArgs: [date]);
    if (rows.isEmpty) return null;
    final r = rows.first;
    return DailyStats(
      date: date,
      totalJobs: (r['total_jobs'] as int?) ?? 0,
      completedJobs: (r['completed_jobs'] as int?) ?? 0,
      pendingJobs: (r['pending_jobs'] as int?) ?? 0,
      revenue: (r['revenue'] as num?)?.toDouble() ?? 0,
      fuelCost: (r['fuel_cost'] as num?)?.toDouble() ?? 0,
      otherExpenses: (r['other_expenses'] as num?)?.toDouble() ?? 0,
      totalExpenses: ((r['fuel_cost'] as num?)?.toDouble() ?? 0) +
          ((r['other_expenses'] as num?)?.toDouble() ?? 0),
      profit: (r['profit'] as num?)?.toDouble() ?? 0,
    );
  }
}