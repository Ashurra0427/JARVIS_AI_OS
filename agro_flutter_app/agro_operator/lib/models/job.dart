// lib/models/job.dart
//
// ADDED FIELDS (Phase 12 timer upgrade):
//   timeValue      — recorded elapsed minutes (agri only)
//   timeUnit       — always 'Minute' for per-min billing
//   ratePerMin     — Rs per minute rate (agri only)
//   timerStartedAt — ISO8601 UTC string if a live timer is active on the server
//                    (informational; live timer is tracked client-side by JobTimerService)

class Job {
  final int? id;
  final String jobType;
  final String service;
  final int? customerId;
  final String? customerName;
  final int? operatorId;
  final String? operatorName;
  final String status;
  final double? areaValue;
  final String? areaUnit;
  final String? material;
  final double? quantityValue;
  final String? quantityUnit;
  // ── Time billing fields (agriculture only) ─────────────────────────────────
  final double? timeValue;       // elapsed minutes recorded at stop
  final String? timeUnit;        // 'Minute' (always)
  final double? ratePerMin;      // Rs per minute
  // ──────────────────────────────────────────────────────────────────────────
  final String? location;
  final double? rate;            // generic rate (kept for backward compat)
  final double? totalAmount;
  final double advancePaid;
  final double? balanceDue;
  final String? notes;
  final String? scheduledDate;
  final String? signatureName;   // "received by" name captured on completion

  const Job({
    this.id,
    required this.jobType,
    required this.service,
    this.customerId,
    this.customerName,
    this.operatorId,
    this.operatorName,
    this.status = 'pending',
    this.areaValue,
    this.areaUnit,
    this.material,
    this.quantityValue,
    this.quantityUnit,
    this.timeValue,
    this.timeUnit,
    this.ratePerMin,
    this.location,
    this.rate,
    this.totalAmount,
    this.advancePaid = 0,
    this.balanceDue,
    this.notes,
    this.scheduledDate,
    this.signatureName,
  });

  bool get isAgriculture => jobType == 'agriculture';
  bool get isCompleted   => status == 'completed';
  bool get hasDues       => (balanceDue ?? 0) > 0;

  // Is this job billed per minute?
  bool get isPerMinute   => isAgriculture && ratePerMin != null;

  // Computed: what's the effective rate displayed to user?
  String get rateDisplay {
    if (ratePerMin != null) return 'Rs ${ratePerMin!.toStringAsFixed(0)}/min';
    if (rate != null)       return 'Rs ${rate!.toStringAsFixed(0)}/${areaUnit ?? timeUnit ?? 'unit'}';
    return '';
  }

  String get displayService =>
      isAgriculture ? service : (material ?? service);

  // ── Serialisation ──────────────────────────────────────────────────────────

  Map<String, dynamic> toJson() => {
    'job_type':         jobType,
    'service':          service,
    'customer_id':      customerId,
    'customer_name':    customerName,
    'operator_id':      operatorId,
    'operator_name':    operatorName,
    'area_value':       areaValue,
    'area_unit':        areaUnit,
    'material':         material,
    'quantity_value':   quantityValue,
    'quantity_unit':    quantityUnit,
    'time_value':       timeValue,
    'time_unit':        timeUnit,
    'rate_per_min':     ratePerMin,
    'location':         location,
    'rate':             rate,
    'total_amount':     totalAmount,
    'advance_paid':     advancePaid,
    'notes':            notes,
    'scheduled_date':   scheduledDate,
  };

  Map<String, dynamic> toMap() => {
    'id':               id,
    'job_type':         jobType,
    'service':          service,
    'customer_id':      customerId,
    'customer_name':    customerName,
    'operator_id':      operatorId,
    'operator_name':    operatorName,
    'status':           status,
    'area_value':       areaValue,
    'area_unit':        areaUnit,
    'material':         material,
    'quantity_value':   quantityValue,
    'quantity_unit':    quantityUnit,
    'time_value':       timeValue,
    'time_unit':        timeUnit,
    'rate_per_min':     ratePerMin,
    'location':         location,
    'rate':             rate,
    'total_amount':     totalAmount,
    'advance_paid':     advancePaid,
    'balance_due':      balanceDue,
    'notes':            notes,
    'scheduled_date':   scheduledDate,
    'signature_name':   signatureName,
  };

  factory Job.fromJson(Map<String, dynamic> j) => Job(
    id:             j['id'] as int?,
    jobType:        j['job_type'] as String? ?? 'agriculture',
    service:        j['service'] as String? ?? '',
    customerId:     j['customer_id'] as int?,
    customerName:   j['customer_name'] as String?,
    operatorId:     j['operator_id'] as int?,
    operatorName:   j['operator_name'] as String?,
    status:         j['status'] as String? ?? 'pending',
    areaValue:      (j['area_value'] as num?)?.toDouble(),
    areaUnit:       j['area_unit'] as String?,
    material:       j['material'] as String?,
    quantityValue:  (j['quantity_value'] as num?)?.toDouble(),
    quantityUnit:   j['quantity_unit'] as String?,
    timeValue:      (j['time_value'] as num?)?.toDouble(),
    timeUnit:       j['time_unit'] as String?,
    ratePerMin:     (j['rate_per_min'] as num?)?.toDouble(),
    location:       j['location'] as String?,
    rate:           (j['rate'] as num?)?.toDouble(),
    totalAmount:    (j['total_amount'] as num?)?.toDouble(),
    advancePaid:    (j['advance_paid'] as num?)?.toDouble() ?? 0,
    balanceDue:     (j['balance_due'] as num?)?.toDouble(),
    notes:          j['notes'] as String?,
    scheduledDate:  j['scheduled_date'] as String?,
    signatureName:  j['signature_name'] as String?,
  );

  factory Job.fromMap(Map<String, dynamic> j) => Job.fromJson(j);

  Job copyWith({
    String? status,
    double? timeValue,
    double? totalAmount,
    double? balanceDue,
    String? signatureName,
  }) => Job(
    id:             id,
    jobType:        jobType,
    service:        service,
    customerId:     customerId,
    customerName:   customerName,
    operatorId:     operatorId,
    operatorName:   operatorName,
    status:         status ?? this.status,
    areaValue:      areaValue,
    areaUnit:       areaUnit,
    material:       material,
    quantityValue:  quantityValue,
    quantityUnit:   quantityUnit,
    timeValue:      timeValue ?? this.timeValue,
    timeUnit:       timeUnit,
    ratePerMin:     ratePerMin,
    location:       location,
    rate:           rate,
    totalAmount:    totalAmount ?? this.totalAmount,
    advancePaid:    advancePaid,
    balanceDue:     balanceDue ?? this.balanceDue,
    notes:          notes,
    scheduledDate:  scheduledDate,
    signatureName:  signatureName ?? this.signatureName,
  );
}
