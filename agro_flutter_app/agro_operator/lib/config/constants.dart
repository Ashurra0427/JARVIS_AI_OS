// lib/config/constants.dart
// Mirrors agents/agro/constants.py — edit both together.
//
// ADDED (Phase 12 timer upgrade):
//   kBillingModes  — 'per_area', 'per_time', 'per_minute' for agriculture
//   'Per Minute'   — new time unit for live timer billing
//   kTransportUnits now leads with 'Tali' (per taali is standard in Nawal Parasi)

// ── Service / material keys (DB values — never change these) ─────────────────
const List<String> kAgriServices = [
  'Ploughing',        // जोताई
  'Rotavator',        // रोटाभेटर
  'Seed Sowing',      // बिउ छर्ने
  'Harvest Support',  // कटनी
  'Water Pumping',    // पानी पम्पिङ
  'Other',
];

const List<String> kTransportMaterials = [
  'Gitti',   // गिट्टी
  'Baluwa',  // बालुवा
  'Dhunga',  // ढुङ्गा
  'Cement',  // सिमेन्ट
  'Miscutt',        // मिसकट
  'Plaster Baluwa',  // प्लास्टर बालुवा
  'Jodai Baluwa',    // जोडाई बालुवा
  'Sand',
  'Other',
];

// ── Nepali display names ──────────────────────────────────────────────────────
const Map<String, String> kAgriServiceNe = {
  'Ploughing':       'जोताई',
  'Rotavator':       'रोटाभेटर',
  'Seed Sowing':     'बिउ छर्ने',
  'Harvest Support': 'कटनी',
  'Water Pumping':   'पानी पम्पिङ',
  'Other':           'अन्य',
};

const Map<String, String> kTransportMaterialNe = {
  'Gitti':  'गिट्टी',
  'Baluwa': 'बालुवा',
  'Dhunga': 'ढुङ्गा',
  'Cement': 'सिमेन्ट',
  'Miscutt':        'मिसकट',
  'Plaster Baluwa': 'प्लास्टर बालुवा',
  'Jodai Baluwa':   'जोडाई बालुवा',
  'Sand':   'बालुवा (बालु)',
  'Other':  'अन्य',
};

// ── Unit lists ────────────────────────────────────────────────────────────────
const List<String> kLandUnits      = ['Katha', 'Bigha', 'Ropani', 'Anna'];
const List<String> kTimeUnits      = ['Minute', 'Hour'];

// Transport: Tali (ताली) is the primary billing unit in Nawal Parasi.
// One taali = one trip load. Rate × taali count = total.
const List<String> kTransportUnits = ['Tali', 'Trip', 'Ton'];

// ── Agriculture billing modes ─────────────────────────────────────────────────
//
// 'per_area'   — rate × area (Katha, Bigha, etc.)   e.g. Rs 500/Katha
// 'per_time'   — rate × hours/minutes (manual input) e.g. Rs 50/Minute
// 'per_minute' — LIVE TIMER: rate_per_min × elapsed seconds / 60
//                The running total ticks in real time on screen.
//
// Only 'per_minute' uses the live timer widget.
// 'per_time' uses a plain number input (worker notes time on paper, enters later).
const String kBillingPerArea   = 'per_area';
const String kBillingPerTime   = 'per_time';
const String kBillingPerMinute = 'per_minute'; // ← live timer mode

// ── Nepali display for units ──────────────────────────────────────────────────
const Map<String, String> kUnitNe = {
  'Katha':   'कठ्ठा',
  'Bigha':   'बिघा',
  'Ropani':  'रोपनी',
  'Anna':    'आना',
  'Minute':  'मिनेट',
  'Hour':    'घण्टा',
  'Tali':    'ताली',
  'Trip':    'ट्रिप',
  'Ton':     'टन',
};

// Services that are ONLY billed by time (no land area makes sense)
const Set<String> kTimeBasedServices = {'Water Pumping'};

// Services that support BOTH land and time units
const Set<String> kLandOrTimeServices = {
  'Ploughing',
  'Rotavator',
  'Seed Sowing',
  'Harvest Support',
};

/// Returns the right unit list for the chosen agriculture service.
/// Always includes land + time options for land-based services so operators
/// can switch between per-area and per-minute billing.
List<String> agriUnitsFor(String service) {
  if (kTimeBasedServices.contains(service)) return kTimeUnits;
  if (kLandOrTimeServices.contains(service)) return [...kLandUnits, ...kTimeUnits];
  return kLandUnits;
}

// ── Status ────────────────────────────────────────────────────────────────────
const List<String> kJobStatuses = [
  'pending', 'confirmed', 'in_progress', 'completed', 'cancelled'
];

const List<String> kFuelTypes = ['Diesel', 'Petrol'];

const List<String> kExpenseCategories = [
  'Fuel', 'Maintenance', 'Repair', 'Operator Wage', 'Spare Parts', 'Other'
];

// ── Helper functions ──────────────────────────────────────────────────────────

String statusLabel(String status, bool nepali) {
  const map = {
    'pending':     ('Pending',     'बाँकी'),
    'confirmed':   ('Confirmed',   'पक्का'),
    'in_progress': ('In Progress', 'चलिरहेको'),
    'completed':   ('Completed',   'सकियो'),
    'cancelled':   ('Cancelled',   'रद्द'),
  };
  final pair = map[status];
  if (pair == null) return status;
  return nepali ? pair.$2 : pair.$1;
}

String serviceLabel(String key, bool nepali) {
  if (!nepali) return key;
  return kAgriServiceNe[key] ?? kTransportMaterialNe[key] ?? key;
}

String unitLabel(String unit, bool nepali) {
  if (!nepali) return unit;
  return kUnitNe[unit] ?? unit;
}

const Map<String, String> kNepaliLabels = {
  'job':          'काम',
  'agriculture':  'कृषि',
  'transport':    'यातायात',
  'customer':     'ग्राहक',
  'operator':     'चालक',
  'status':       'स्थिति',
  'fuel':         'इन्धन',
  'expense':      'खर्च',
  'revenue':      'आम्दानी',
  'profit':       'नाफा',
  'daily_report': 'दैनिक रिपोर्ट',
};
