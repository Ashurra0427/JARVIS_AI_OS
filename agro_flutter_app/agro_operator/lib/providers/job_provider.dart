// lib/providers/job_provider.dart
//
// ADDED (Phase 12 timer upgrade):
//   updateJobTime()  — sends elapsed minutes + computed total to server
//                      when the live timer is stopped.  Optimistically
//                      patches the in-memory job list so the UI reflects
//                      the new time/total before the server round-trip.
//   _onMessage       — handles 'update_job_time' server confirmation.

import 'dart:async';
import 'package:flutter/material.dart';
import '../models/job.dart';
import '../models/operator.dart';
import '../services/ws_service.dart';

class JobProvider extends ChangeNotifier {
  final WsService _ws;
  StreamSubscription? _sub;

  List<Job> _jobs        = [];
  Map<String, dynamic> _todayStats = {};
  bool  _loading         = false;
  String? _lastError;
  int?  _lastCreatedJobId;
  List<Operator> _operators = [];

  // ── Biller / outstanding dues ─────────────────────────────────────────
  List<Map<String, dynamic>> _outstanding = [];
  double  _totalOutstanding = 0;
  bool    _billerLoading    = false;
  String? _billerError;
  String? _billerMessage;

  List<Job>             get jobs             => _jobs;
  Map<String, dynamic>  get todayStats       => _todayStats;
  bool                  get loading          => _loading;
  String?               get lastError        => _lastError;
  int?                  get lastCreatedJobId => _lastCreatedJobId;
  List<Operator>        get operators        => _operators;

  List<Map<String, dynamic>> get outstanding       => _outstanding;
  double                     get totalOutstanding  => _totalOutstanding;
  bool                        get billerLoading     => _billerLoading;
  String?                     get billerError       => _billerError;
  String?                     get billerMessage     => _billerMessage;

  JobProvider(this._ws) {
    _sub = _ws.stream.listen(_onMessage);
  }

  void _onMessage(Map<String, dynamic> msg) {
    final type   = msg['type']   as String?;
    final action = msg['action'] as String?;
    final data   = (msg['data']  as Map<String, dynamic>?) ?? {};

    if (type != 'agro_result') return;

    switch (action) {
      case 'get_jobs':
        _jobs = ((data['jobs'] as List?) ?? [])
            .map((j) => Job.fromJson(j as Map<String, dynamic>))
            .toList();
        _loading = false;
        notifyListeners();

      case 'get_stats':
        _todayStats = (data['stats'] as Map<String, dynamic>?) ?? {};
        notifyListeners();

      case 'get_operators':
        _operators = ((data['operators'] as List?) ?? [])
            .map((o) => Operator.fromJson(o as Map<String, dynamic>))
            .toList();
        notifyListeners();

      case 'add_operator':
        if (data['success'] != false) fetchOperators();

      case 'log_job':
        if (data['success'] == false) {
          _lastError = data['message'] as String? ?? 'Failed to save job';
          _loading   = false;
          notifyListeners();
          return;
        }
        _lastCreatedJobId = data['job_id'] as int?;
        _loading          = false;
        notifyListeners();
        fetchTodayJobs();

      case 'update_job':
        // Server confirmed status update — do a fresh fetch
        fetchTodayJobs();

      case 'update_job_time':
        // Server confirmed timer update — patch in-memory job
        final jobId = data['job_id'] as int?;
        final mins  = (data['time_value'] as num?)?.toDouble();
        final total = (data['total_amount'] as num?)?.toDouble();
        if (jobId != null) {
          final idx = _jobs.indexWhere((j) => j.id == jobId);
          if (idx != -1) {
            final updated = List<Job>.from(_jobs);
            updated[idx]  = updated[idx].copyWith(
              timeValue:   mins,
              totalAmount: total,
            );
            _jobs = updated;
            notifyListeners();
          }
        }

      case 'daily_report':
      case 'monthly_report':
        notifyListeners();

      case 'outstanding_balances':
        _billerLoading = false;
        if (data['success'] == false) {
          _billerError = data['message'] as String? ?? 'Failed to load dues';
          notifyListeners();
          return;
        }
        _outstanding = ((data['balances'] as List?) ?? [])
            .map((b) => b as Map<String, dynamic>)
            .toList();
        _totalOutstanding = (data['total_outstanding'] as num?)?.toDouble() ?? 0;
        _billerError = null;
        notifyListeners();

      case 'record_payment':
        _billerLoading = false;
        if (data['success'] == false) {
          _billerError = data['message'] as String? ?? 'Payment failed';
          notifyListeners();
          return;
        }
        _billerError = null;
        final overpaidBy = (data['overpaid_by'] as num?)?.toDouble() ?? 0;
        _billerMessage = overpaidBy > 0
            ? 'Payment recorded (Rs ${overpaidBy.toStringAsFixed(0)} over the due amount)'
            : 'Payment recorded';
        notifyListeners();
        fetchOutstanding();

      case 'override_balance':
        _billerLoading = false;
        if (data['success'] == false) {
          _billerError = data['message'] as String? ?? 'Override failed';
          notifyListeners();
          return;
        }
        _billerError   = null;
        _billerMessage = 'Balance overridden';
        notifyListeners();
        fetchOutstanding();
    }
  }

  // ── Fetch ──────────────────────────────────────────────────────────────────

  void fetchTodayJobs({String? date}) {
    _loading = true;
    notifyListeners();
    final today = date ?? DateTime.now().toIso8601String().substring(0, 10);
    _ws.sendAgroAction('get_jobs',  {'date': today});
    _ws.sendAgroAction('get_stats', {'date': today});
  }

  void fetchAllJobs() {
    _loading = true;
    notifyListeners();
    _ws.sendAgroAction('get_jobs', {});
  }

  void fetchOperators() {
    _ws.sendAgroAction('get_operators', {});
  }

  Future<void> addOperator(String name, {String? phone}) async {
    _ws.sendAgroAction('add_operator', {'name': name, 'phone': phone ?? ''});
  }

  // ── Mutations ──────────────────────────────────────────────────────────────

  Future<void> createJob(Map<String, dynamic> data) async {
    _loading          = true;
    _lastError        = null;
    _lastCreatedJobId = null;
    notifyListeners();
    _ws.sendAgroAction('log_job', data);
  }

  Future<void> updateJobStatus(int jobId, String status, {String? signatureName}) async {
    // Optimistic update
    final idx = _jobs.indexWhere((j) => j.id == jobId);
    if (idx != -1) {
      final updated = List<Job>.from(_jobs);
      updated[idx]  = updated[idx].copyWith(
        status: status,
        signatureName: signatureName,
      );
      _jobs = updated;
      notifyListeners();
    }
    _ws.sendAgroAction('update_job', {
      'job_id': jobId,
      'status': status,
      if (signatureName != null && signatureName.isNotEmpty)
        'signature_name': signatureName,
    });
  }

  /// Called when the live timer is stopped.
  /// Optimistically patches the local job, then notifies the server.
  Future<void> updateJobTime({
    required int    jobId,
    required double elapsedMins,
    required double total,
  }) async {
    // Optimistic patch
    final idx = _jobs.indexWhere((j) => j.id == jobId);
    if (idx != -1) {
      final updated = List<Job>.from(_jobs);
      updated[idx]  = updated[idx].copyWith(
        timeValue:   elapsedMins,
        totalAmount: total,
      );
      _jobs = updated;
      notifyListeners();
    }
    _ws.sendAgroAction('update_job_time', {
      'job_id':      jobId,
      'time_value':  elapsedMins,
      'time_unit':   'Minute',
      'total_amount': total,
    });
  }

  void requestDailyReport(String date) =>
      _ws.sendAgroAction('daily_report', {'date': date});

  void requestMonthlyReport(int year, int month) =>
      _ws.sendAgroAction('monthly_report', {'year': year, 'month': month});

  // ── Biller / outstanding dues ─────────────────────────────────────────

  void fetchOutstanding() {
    _billerLoading = true;
    _billerError    = null;
    notifyListeners();
    _ws.sendAgroAction('outstanding_balances', {});
  }

  /// Records a real cash payment against a job's balance. Rejects
  /// non-positive amounts client-side too (fast feedback), but the server
  /// is the actual source of truth for that validation.
  Future<void> recordPayment(int jobId, double amount) async {
    if (amount <= 0) {
      _billerError = 'Payment amount must be greater than zero.';
      notifyListeners();
      return;
    }
    _billerLoading = true;
    _billerMessage = null;
    _billerError   = null;
    notifyListeners();
    _ws.sendAgroAction('record_payment', {'job_id': jobId, 'amount': amount});
  }

  /// Records a payment against a customer's whole bill rather than a
  /// single job. The backend still books each payment against one job
  /// row at a time (that's what keeps the audit log meaningful), so this
  /// just applies the amount to the customer's unpaid jobs oldest-first —
  /// e.g. Rs 500 against a bill of a Rs 300 job + a Rs 400 job pays the
  /// first off completely and puts the remaining Rs 200 on the second.
  /// `jobs` should be a bill's job list, already oldest-first as returned
  /// by outstanding_balances().
  Future<void> recordPaymentForBill(
    List<Map<String, dynamic>> jobs,
    double amount,
  ) async {
    if (amount <= 0) {
      _billerError = 'Payment amount must be greater than zero.';
      notifyListeners();
      return;
    }
    _billerLoading = true;
    _billerMessage = null;
    _billerError   = null;
    notifyListeners();

    double remaining = amount;
    for (final job in jobs) {
      if (remaining <= 0) break;
      final due = (job['balance_due'] as num?)?.toDouble() ?? 0;
      if (due <= 0) continue;
      final portion = remaining < due ? remaining : due;
      final jobId = job['id'] as int?;
      if (jobId == null) continue;
      _ws.sendAgroAction('record_payment', {'job_id': jobId, 'amount': portion});
      remaining -= portion;
    }
    // Any amount left over the whole bill (customer overpaying) is applied
    // as an overpayment on the last job in the list, same as
    // record_payment() already does for a single job — surfaced there
    // rather than silently dropped.
    if (remaining > 0 && jobs.isNotEmpty) {
      final lastJobId = jobs.last['id'] as int?;
      if (lastJobId != null) {
        _ws.sendAgroAction('record_payment', {'job_id': lastJobId, 'amount': remaining});
      }
    }
  }

  /// Manually overrides a job's balance_due — for waived/written-off dues
  /// or corrections, NOT for recording an actual payment (use
  /// recordPayment for that). A reason is required, matching the server's
  /// own validation, so the operator gets immediate feedback instead of a
  /// round-trip failure.
  Future<void> overrideBalance(int jobId, double newBalance, String reason) async {
    if (reason.trim().isEmpty) {
      _billerError = 'A reason is required to override a balance.';
      notifyListeners();
      return;
    }
    if (newBalance < 0) {
      _billerError = 'Balance cannot be negative.';
      notifyListeners();
      return;
    }
    _billerLoading = true;
    _billerMessage = null;
    _billerError   = null;
    notifyListeners();
    _ws.sendAgroAction('override_balance', {
      'job_id': jobId,
      'new_balance': newBalance,
      'reason': reason.trim(),
    });
  }

  void clearBillerMessage() {
    _billerMessage = null;
    _billerError = null;
  }

  @override
  void dispose() {
    _sub?.cancel();
    super.dispose();
  }
}
