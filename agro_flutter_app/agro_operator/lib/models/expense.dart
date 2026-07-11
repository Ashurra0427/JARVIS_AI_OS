// lib/models/expense.dart

class Expense {
  final int? id;
  final String category;
  final double amount;
  final int? jobId;
  final int? operatorId;
  final String? operatorName;
  final String? description;
  final String? receiptRef;
  final double? loggedAt; // unix epoch

  const Expense({
    this.id,
    required this.category,
    required this.amount,
    this.jobId,
    this.operatorId,
    this.operatorName,
    this.description,
    this.receiptRef,
    this.loggedAt,
  });

  factory Expense.fromJson(Map<String, dynamic> j) => Expense(
    id:           j['id'] as int?,
    category:     j['category'] as String? ?? 'Other',
    amount:       (j['amount'] as num?)?.toDouble() ?? 0,
    jobId:        j['job_id'] as int?,
    operatorId:   j['operator_id'] as int?,
    operatorName: j['operator_name'] as String?,
    description:  j['description'] as String?,
    receiptRef:   j['receipt_ref'] as String?,
    loggedAt:     (j['logged_at'] as num?)?.toDouble(),
  );

  Map<String, dynamic> toJson() => {
    'category':    category,
    'amount':      amount,
    'job_id':      jobId,
    'operator_id': operatorId,
    'description': description,
    'receipt_ref': receiptRef,
  };

  DateTime? get loggedDate =>
      loggedAt != null ? DateTime.fromMillisecondsSinceEpoch((loggedAt! * 1000).round()) : null;
}