// lib/models/daily_stats.dart

class DailyStats {
  final String date;
  final int totalJobs;
  final int completedJobs;
  final int pendingJobs;
  final double revenue;
  final double fuelCost;
  final double otherExpenses;
  final double totalExpenses;
  final double profit;

  const DailyStats({
    required this.date,
    required this.totalJobs,
    required this.completedJobs,
    required this.pendingJobs,
    required this.revenue,
    required this.fuelCost,
    required this.otherExpenses,
    required this.totalExpenses,
    required this.profit,
  });

  factory DailyStats.empty(String date) => DailyStats(
        date: date,
        totalJobs: 0,
        completedJobs: 0,
        pendingJobs: 0,
        revenue: 0,
        fuelCost: 0,
        otherExpenses: 0,
        totalExpenses: 0,
        profit: 0,
      );

  factory DailyStats.fromMap(Map<String, dynamic> map) => DailyStats(
        date: map['date'] as String? ?? '',
        totalJobs: (map['total_jobs'] as num?)?.toInt() ?? 0,
        completedJobs: (map['completed_jobs'] as num?)?.toInt() ?? 0,
        pendingJobs: (map['pending_jobs'] as num?)?.toInt() ?? 0,
        revenue: (map['revenue'] as num?)?.toDouble() ?? 0,
        fuelCost: (map['fuel_cost'] as num?)?.toDouble() ?? 0,
        otherExpenses: (map['other_expenses'] as num?)?.toDouble() ?? 0,
        totalExpenses: (map['total_expenses'] as num?)?.toDouble() ?? 0,
        profit: (map['profit'] as num?)?.toDouble() ?? 0,
      );
}