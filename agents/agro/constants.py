"""
AGRO_AGENT constants — Nawal Parasi family business.
All configurable lists are here. Edit this file to add new services or materials.
"""

# ── Agriculture services ───────────────────────────────────────────────
AGRI_SERVICES = [
    "Ploughing",          # जोताई
    "Rotavator",          # रोटाभेटर
    "Seed Sowing",        # बिउ छर्ने
    "Harvest Support",    # कटनी
    "Water Pumping",      # पानी पम्पिङ
    "Other",              # अन्य
]

# ── Transport materials ────────────────────────────────────────────────
TRANSPORT_MATERIALS = [
    "Gitti",         # गिट्टी (crushed stone)
    "Baluwa",        # बालुवा (sand/fine aggregate)
    "Dhunga",        # ढुङ्गा (stone/rock)
    "Cement",        # सिमेन्ट
    "Miscutt",       # मिसकट (crusher dust / M-sand)
    "Plaster Baluwa", # प्लास्टर बालुवा (fine plastering sand)
    "Jodai Baluwa",  # जोडाई बालुवा (masonry/joining sand)
    "Sand",          # बालुवा (coarse)
    "Other",         # अन्य
]

# ── Job types ──────────────────────────────────────────────────────────
JOB_TYPE_AGRICULTURE = "agriculture"
JOB_TYPE_TRANSPORT   = "transport"
JOB_TYPES = [JOB_TYPE_AGRICULTURE, JOB_TYPE_TRANSPORT]

# ── Land area units (Nepal) ────────────────────────────────────────────
LAND_UNITS = ["Katha", "Bigha", "Ropani", "Anna"]
# 1 Bigha = 20 Katha (Terai standard, Nawal Parasi)
# 1 Ropani = 16 Anna (hill standard — keep for completeness)
KATHA_PER_BIGHA = 20

# ── Transport volume unit ──────────────────────────────────────────────
TRANSPORT_UNITS = ["Tali", "Trip", "Ton"]
# Tali = one tractor trolley load (standard local unit)

# ── Job status lifecycle ───────────────────────────────────────────────
STATUS_PENDING     = "pending"
STATUS_CONFIRMED   = "confirmed"
STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETED   = "completed"
STATUS_CANCELLED   = "cancelled"
JOB_STATUSES = [
    STATUS_PENDING, STATUS_CONFIRMED,
    STATUS_IN_PROGRESS, STATUS_COMPLETED, STATUS_CANCELLED
]

# ── Fuel types ─────────────────────────────────────────────────────────
FUEL_TYPES = ["Diesel", "Petrol"]
DEFAULT_FUEL_TYPE = "Diesel"

# ── Expense categories ─────────────────────────────────────────────────
EXPENSE_CATEGORIES = [
    "Fuel",
    "Maintenance",
    "Repair",
    "Operator Wage",
    "Spare Parts",
    "Other",
]

# ── Operators (edit to match real names) ──────────────────────────────
DEFAULT_OPERATORS = [
    "Operator 1",
    "Operator 2",
]

# ── Nepali UI label map ────────────────────────────────────────────────
NEPALI_LABELS = {
    "job":          "काम",
    "agriculture":  "कृषि",
    "transport":    "यातायात",
    "customer":     "ग्राहक",
    "operator":     "चालक",
    "status":       "स्थिति",
    "pending":      "बाँकी",
    "confirmed":    "पक्का",
    "in_progress":  "चलिरहेको",
    "completed":    "सकियो",
    "cancelled":    "रद्द",
    "fuel":         "इन्धन",
    "expense":      "खर्च",
    "revenue":      "आम्दानी",
    "profit":       "नाफा",
    "daily_report": "दैनिक रिपोर्ट",
    "export_excel": "Excel निकाल्नुस्",
}
