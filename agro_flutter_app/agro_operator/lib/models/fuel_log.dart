// lib/models/fuel_log.dart

class FuelLog {
  final int? id;
  final int? operatorId;
  final String? operatorName;
  final int? jobId;
  final String fuelType;
  final double liters;
  final double? pricePerLiter;
  final double? totalCost;
  final String? petrolPump;
  final String? notes;
  final double? loggedAt; // unix epoch

  const FuelLog({
    this.id,
    this.operatorId,
    this.operatorName,
    this.jobId,
    this.fuelType = 'Diesel',
    required this.liters,
    this.pricePerLiter,
    this.totalCost,
    this.petrolPump,
    this.notes,
    this.loggedAt,
  });

  factory FuelLog.fromJson(Map<String, dynamic> j) => FuelLog(
    id:             j['id'] as int?,
    operatorId:     j['operator_id'] as int?,
    operatorName:   j['operator_name'] as String?,
    jobId:          j['job_id'] as int?,
    fuelType:       j['fuel_type'] as String? ?? 'Diesel',
    liters:         (j['liters'] as num?)?.toDouble() ?? 0,
    pricePerLiter:  (j['price_per_liter'] as num?)?.toDouble(),
    totalCost:      (j['total_cost'] as num?)?.toDouble(),
    petrolPump:     j['petrol_pump'] as String?,
    notes:          j['notes'] as String?,
    loggedAt:       (j['logged_at'] as num?)?.toDouble(),
  );

  Map<String, dynamic> toJson() => {
    'operator_id':    operatorId,
    'job_id':         jobId,
    'fuel_type':      fuelType,
    'liters':         liters,
    'price_per_liter': pricePerLiter,
    'total_cost':     totalCost,
    'petrol_pump':    petrolPump,
    'notes':          notes,
  };

  DateTime? get loggedDate =>
      loggedAt != null ? DateTime.fromMillisecondsSinceEpoch((loggedAt! * 1000).round()) : null;
}