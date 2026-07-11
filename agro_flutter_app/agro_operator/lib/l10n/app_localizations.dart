import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/widgets.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:intl/intl.dart' as intl;

import 'app_localizations_en.dart';
import 'app_localizations_ne.dart';

// ignore_for_file: type=lint

/// Callers can lookup localized strings with an instance of AppLocalizations
/// returned by `AppLocalizations.of(context)`.
///
/// Applications need to include `AppLocalizations.delegate()` in their app's
/// `localizationDelegates` list, and the locales they support in the app's
/// `supportedLocales` list. For example:
///
/// ```dart
/// import 'l10n/app_localizations.dart';
///
/// return MaterialApp(
///   localizationsDelegates: AppLocalizations.localizationsDelegates,
///   supportedLocales: AppLocalizations.supportedLocales,
///   home: MyApplicationHome(),
/// );
/// ```
///
/// ## Update pubspec.yaml
///
/// Please make sure to update your pubspec.yaml to include the following
/// packages:
///
/// ```yaml
/// dependencies:
///   # Internationalization support.
///   flutter_localizations:
///     sdk: flutter
///   intl: any # Use the pinned version from flutter_localizations
///
///   # Rest of dependencies
/// ```
///
/// ## iOS Applications
///
/// iOS applications define key application metadata, including supported
/// locales, in an Info.plist file that is built into the application bundle.
/// To configure the locales supported by your app, you’ll need to edit this
/// file.
///
/// First, open your project’s ios/Runner.xcworkspace Xcode workspace file.
/// Then, in the Project Navigator, open the Info.plist file under the Runner
/// project’s Runner folder.
///
/// Next, select the Information Property List item, select Add Item from the
/// Editor menu, then select Localizations from the pop-up menu.
///
/// Select and expand the newly-created Localizations item then, for each
/// locale your application supports, add a new item and select the locale
/// you wish to add from the pop-up menu in the Value field. This list should
/// be consistent with the languages listed in the AppLocalizations.supportedLocales
/// property.
abstract class AppLocalizations {
  AppLocalizations(String locale)
      : localeName = intl.Intl.canonicalizedLocale(locale.toString());

  final String localeName;

  static AppLocalizations? of(BuildContext context) {
    return Localizations.of<AppLocalizations>(context, AppLocalizations);
  }

  static const LocalizationsDelegate<AppLocalizations> delegate =
      _AppLocalizationsDelegate();

  /// A list of this localizations delegate along with the default localizations
  /// delegates.
  ///
  /// Returns a list of localizations delegates containing this delegate along with
  /// GlobalMaterialLocalizations.delegate, GlobalCupertinoLocalizations.delegate,
  /// and GlobalWidgetsLocalizations.delegate.
  ///
  /// Additional delegates can be added by appending to this list in
  /// MaterialApp. This list does not have to be used at all if a custom list
  /// of delegates is preferred or required.
  static const List<LocalizationsDelegate<dynamic>> localizationsDelegates =
      <LocalizationsDelegate<dynamic>>[
    delegate,
    GlobalMaterialLocalizations.delegate,
    GlobalCupertinoLocalizations.delegate,
    GlobalWidgetsLocalizations.delegate,
  ];

  /// A list of this localizations delegate's supported locales.
  static const List<Locale> supportedLocales = <Locale>[
    Locale('en'),
    Locale('ne')
  ];

  /// App title
  ///
  /// In en, this message translates to:
  /// **'JARVIS AGRO'**
  String get appTitle;

  /// No description provided for @homeTitle.
  ///
  /// In en, this message translates to:
  /// **'Today\'s Overview'**
  String get homeTitle;

  /// No description provided for @jobsTitle.
  ///
  /// In en, this message translates to:
  /// **'Jobs'**
  String get jobsTitle;

  /// No description provided for @addJob.
  ///
  /// In en, this message translates to:
  /// **'Add Job'**
  String get addJob;

  /// No description provided for @fuelTitle.
  ///
  /// In en, this message translates to:
  /// **'Log Fuel'**
  String get fuelTitle;

  /// No description provided for @expenseTitle.
  ///
  /// In en, this message translates to:
  /// **'Log Expense'**
  String get expenseTitle;

  /// No description provided for @reportsTitle.
  ///
  /// In en, this message translates to:
  /// **'Reports'**
  String get reportsTitle;

  /// No description provided for @settingsTitle.
  ///
  /// In en, this message translates to:
  /// **'Settings'**
  String get settingsTitle;

  /// No description provided for @agriculture.
  ///
  /// In en, this message translates to:
  /// **'Agriculture'**
  String get agriculture;

  /// No description provided for @transport.
  ///
  /// In en, this message translates to:
  /// **'Transport'**
  String get transport;

  /// No description provided for @ploughing.
  ///
  /// In en, this message translates to:
  /// **'Ploughing'**
  String get ploughing;

  /// No description provided for @rotavator.
  ///
  /// In en, this message translates to:
  /// **'Rotavator'**
  String get rotavator;

  /// No description provided for @seedSowing.
  ///
  /// In en, this message translates to:
  /// **'Seed Sowing'**
  String get seedSowing;

  /// No description provided for @harvestSupport.
  ///
  /// In en, this message translates to:
  /// **'Harvest Support'**
  String get harvestSupport;

  /// No description provided for @waterPumping.
  ///
  /// In en, this message translates to:
  /// **'Water Pumping'**
  String get waterPumping;

  /// No description provided for @other.
  ///
  /// In en, this message translates to:
  /// **'Other'**
  String get other;

  /// No description provided for @gitti.
  ///
  /// In en, this message translates to:
  /// **'Gitti'**
  String get gitti;

  /// No description provided for @baluwa.
  ///
  /// In en, this message translates to:
  /// **'Baluwa'**
  String get baluwa;

  /// No description provided for @dhunga.
  ///
  /// In en, this message translates to:
  /// **'Dhunga'**
  String get dhunga;

  /// No description provided for @cement.
  ///
  /// In en, this message translates to:
  /// **'Cement'**
  String get cement;

  /// No description provided for @bricks.
  ///
  /// In en, this message translates to:
  /// **'Bricks'**
  String get bricks;

  /// No description provided for @soil.
  ///
  /// In en, this message translates to:
  /// **'Soil'**
  String get soil;

  /// No description provided for @gravel.
  ///
  /// In en, this message translates to:
  /// **'Gravel'**
  String get gravel;

  /// No description provided for @sand.
  ///
  /// In en, this message translates to:
  /// **'Sand'**
  String get sand;

  /// No description provided for @pending.
  ///
  /// In en, this message translates to:
  /// **'Pending'**
  String get pending;

  /// No description provided for @confirmed.
  ///
  /// In en, this message translates to:
  /// **'Confirmed'**
  String get confirmed;

  /// No description provided for @inProgress.
  ///
  /// In en, this message translates to:
  /// **'In Progress'**
  String get inProgress;

  /// No description provided for @completed.
  ///
  /// In en, this message translates to:
  /// **'Completed'**
  String get completed;

  /// No description provided for @cancelled.
  ///
  /// In en, this message translates to:
  /// **'Cancelled'**
  String get cancelled;

  /// No description provided for @customer.
  ///
  /// In en, this message translates to:
  /// **'Customer'**
  String get customer;

  /// No description provided for @operator.
  ///
  /// In en, this message translates to:
  /// **'Operator'**
  String get operator;

  /// No description provided for @location.
  ///
  /// In en, this message translates to:
  /// **'Location'**
  String get location;

  /// No description provided for @area.
  ///
  /// In en, this message translates to:
  /// **'Area'**
  String get area;

  /// No description provided for @quantity.
  ///
  /// In en, this message translates to:
  /// **'Quantity'**
  String get quantity;

  /// No description provided for @rate.
  ///
  /// In en, this message translates to:
  /// **'Rate (NPR)'**
  String get rate;

  /// No description provided for @totalAmount.
  ///
  /// In en, this message translates to:
  /// **'Total Amount (NPR)'**
  String get totalAmount;

  /// No description provided for @advancePaid.
  ///
  /// In en, this message translates to:
  /// **'Advance Paid (NPR)'**
  String get advancePaid;

  /// No description provided for @balanceDue.
  ///
  /// In en, this message translates to:
  /// **'Balance Due'**
  String get balanceDue;

  /// No description provided for @notes.
  ///
  /// In en, this message translates to:
  /// **'Notes'**
  String get notes;

  /// No description provided for @date.
  ///
  /// In en, this message translates to:
  /// **'Date'**
  String get date;

  /// No description provided for @save.
  ///
  /// In en, this message translates to:
  /// **'Save'**
  String get save;

  /// No description provided for @cancel.
  ///
  /// In en, this message translates to:
  /// **'Cancel'**
  String get cancel;

  /// No description provided for @delete.
  ///
  /// In en, this message translates to:
  /// **'Delete'**
  String get delete;

  /// No description provided for @revenue.
  ///
  /// In en, this message translates to:
  /// **'Revenue'**
  String get revenue;

  /// No description provided for @fuel.
  ///
  /// In en, this message translates to:
  /// **'Fuel'**
  String get fuel;

  /// No description provided for @expense.
  ///
  /// In en, this message translates to:
  /// **'Expense'**
  String get expense;

  /// No description provided for @profit.
  ///
  /// In en, this message translates to:
  /// **'Profit'**
  String get profit;

  /// No description provided for @totalJobs.
  ///
  /// In en, this message translates to:
  /// **'Total Jobs'**
  String get totalJobs;

  /// No description provided for @completedJobs.
  ///
  /// In en, this message translates to:
  /// **'Completed'**
  String get completedJobs;

  /// No description provided for @pendingJobs.
  ///
  /// In en, this message translates to:
  /// **'Pending'**
  String get pendingJobs;

  /// No description provided for @liters.
  ///
  /// In en, this message translates to:
  /// **'Liters'**
  String get liters;

  /// No description provided for @pricePerLiter.
  ///
  /// In en, this message translates to:
  /// **'Price/Liter (NPR)'**
  String get pricePerLiter;

  /// No description provided for @petrolPump.
  ///
  /// In en, this message translates to:
  /// **'Petrol Pump'**
  String get petrolPump;

  /// No description provided for @fuelType.
  ///
  /// In en, this message translates to:
  /// **'Fuel Type'**
  String get fuelType;

  /// No description provided for @diesel.
  ///
  /// In en, this message translates to:
  /// **'Diesel'**
  String get diesel;

  /// No description provided for @petrol.
  ///
  /// In en, this message translates to:
  /// **'Petrol'**
  String get petrol;

  /// No description provided for @category.
  ///
  /// In en, this message translates to:
  /// **'Category'**
  String get category;

  /// No description provided for @amount.
  ///
  /// In en, this message translates to:
  /// **'Amount (NPR)'**
  String get amount;

  /// No description provided for @maintenance.
  ///
  /// In en, this message translates to:
  /// **'Maintenance'**
  String get maintenance;

  /// No description provided for @repair.
  ///
  /// In en, this message translates to:
  /// **'Repair'**
  String get repair;

  /// No description provided for @operatorWage.
  ///
  /// In en, this message translates to:
  /// **'Operator Wage'**
  String get operatorWage;

  /// No description provided for @spareParts.
  ///
  /// In en, this message translates to:
  /// **'Spare Parts'**
  String get spareParts;

  /// No description provided for @dailyReport.
  ///
  /// In en, this message translates to:
  /// **'Daily Report'**
  String get dailyReport;

  /// No description provided for @monthlyReport.
  ///
  /// In en, this message translates to:
  /// **'Monthly Report'**
  String get monthlyReport;

  /// No description provided for @generateExcel.
  ///
  /// In en, this message translates to:
  /// **'Generate Excel'**
  String get generateExcel;

  /// No description provided for @serverUrl.
  ///
  /// In en, this message translates to:
  /// **'Server URL'**
  String get serverUrl;

  /// No description provided for @connected.
  ///
  /// In en, this message translates to:
  /// **'Connected'**
  String get connected;

  /// No description provided for @disconnected.
  ///
  /// In en, this message translates to:
  /// **'Disconnected'**
  String get disconnected;

  /// No description provided for @connecting.
  ///
  /// In en, this message translates to:
  /// **'Connecting...'**
  String get connecting;

  /// No description provided for @offline.
  ///
  /// In en, this message translates to:
  /// **'Offline — jobs queued'**
  String get offline;

  /// No description provided for @katha.
  ///
  /// In en, this message translates to:
  /// **'Katha'**
  String get katha;

  /// No description provided for @bigha.
  ///
  /// In en, this message translates to:
  /// **'Bigha'**
  String get bigha;

  /// No description provided for @ropani.
  ///
  /// In en, this message translates to:
  /// **'Ropani'**
  String get ropani;

  /// No description provided for @anna.
  ///
  /// In en, this message translates to:
  /// **'Anna'**
  String get anna;

  /// No description provided for @tali.
  ///
  /// In en, this message translates to:
  /// **'Tali'**
  String get tali;

  /// No description provided for @trip.
  ///
  /// In en, this message translates to:
  /// **'Trip'**
  String get trip;

  /// No description provided for @ton.
  ///
  /// In en, this message translates to:
  /// **'Ton'**
  String get ton;

  /// No description provided for @jobType.
  ///
  /// In en, this message translates to:
  /// **'Job Type'**
  String get jobType;

  /// No description provided for @service.
  ///
  /// In en, this message translates to:
  /// **'Service'**
  String get service;

  /// No description provided for @material.
  ///
  /// In en, this message translates to:
  /// **'Material'**
  String get material;

  /// No description provided for @unit.
  ///
  /// In en, this message translates to:
  /// **'Unit'**
  String get unit;

  /// No description provided for @selectDate.
  ///
  /// In en, this message translates to:
  /// **'Select Date'**
  String get selectDate;

  /// No description provided for @jobLogged.
  ///
  /// In en, this message translates to:
  /// **'Job logged successfully'**
  String get jobLogged;

  /// No description provided for @fuelLogged.
  ///
  /// In en, this message translates to:
  /// **'Fuel logged'**
  String get fuelLogged;

  /// No description provided for @expenseLogged.
  ///
  /// In en, this message translates to:
  /// **'Expense logged'**
  String get expenseLogged;

  /// No description provided for @reportGenerated.
  ///
  /// In en, this message translates to:
  /// **'Report generated'**
  String get reportGenerated;

  /// No description provided for @errorOccurred.
  ///
  /// In en, this message translates to:
  /// **'Something went wrong'**
  String get errorOccurred;

  /// No description provided for @noJobsToday.
  ///
  /// In en, this message translates to:
  /// **'No jobs scheduled today'**
  String get noJobsToday;

  /// No description provided for @rupees.
  ///
  /// In en, this message translates to:
  /// **'Rs'**
  String get rupees;

  /// No description provided for @exportShared.
  ///
  /// In en, this message translates to:
  /// **'Export shared'**
  String get exportShared;

  /// No description provided for @language.
  ///
  /// In en, this message translates to:
  /// **'Language'**
  String get language;

  /// No description provided for @syncPending.
  ///
  /// In en, this message translates to:
  /// **'Sync pending: {count} items'**
  String syncPending(int count);
}

class _AppLocalizationsDelegate
    extends LocalizationsDelegate<AppLocalizations> {
  const _AppLocalizationsDelegate();

  @override
  Future<AppLocalizations> load(Locale locale) {
    return SynchronousFuture<AppLocalizations>(lookupAppLocalizations(locale));
  }

  @override
  bool isSupported(Locale locale) =>
      <String>['en', 'ne'].contains(locale.languageCode);

  @override
  bool shouldReload(_AppLocalizationsDelegate old) => false;
}

AppLocalizations lookupAppLocalizations(Locale locale) {
  // Lookup logic when only language code is specified.
  switch (locale.languageCode) {
    case 'en':
      return AppLocalizationsEn();
    case 'ne':
      return AppLocalizationsNe();
  }

  throw FlutterError(
      'AppLocalizations.delegate failed to load unsupported locale "$locale". This is likely '
      'an issue with the localizations generation tool. Please file an issue '
      'on GitHub with a reproducible sample app and the gen-l10n configuration '
      'that was used.');
}
