// lib/config/routes.dart
import 'package:flutter/material.dart';
import '../screens/home/home_screen.dart';
import '../screens/jobs/add_job_screen.dart';
import '../screens/jobs/job_list_screen.dart';
import '../screens/fuel/log_fuel_screen.dart';
import '../screens/expense/log_expense_screen.dart';
import '../screens/reports/daily_report_screen.dart';
import '../screens/reports/monthly_report_screen.dart';
import '../screens/settings/settings_screen.dart';

class AppRoutes {
  static const home          = '/';
  static const addJob        = '/jobs/add';
  static const jobList       = '/jobs';
  static const logFuel       = '/fuel/log';
  static const logExpense    = '/expense/log';
  static const dailyReport   = '/reports/daily';
  static const monthlyReport = '/reports/monthly';
  static const settings      = '/settings';

  static Map<String, WidgetBuilder> get routes => {
    home:          (_) => const HomeScreen(),
    addJob:        (_) => const AddJobScreen(),
    jobList:       (_) => const JobListScreen(),
    logFuel:       (_) => const LogFuelScreen(),
    logExpense:    (_) => const LogExpenseScreen(),
    dailyReport:   (_) => const DailyReportScreen(),
    monthlyReport: (_) => const MonthlyReportScreen(),
    settings:      (_) => const SettingsScreen(),
  };
}