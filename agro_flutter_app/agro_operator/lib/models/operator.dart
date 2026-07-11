// lib/models/operator.dart

class Operator {
  final int? id;
  final String name;
  final String? phone;
  final bool isActive;

  const Operator({
    this.id,
    required this.name,
    this.phone,
    this.isActive = true,
  });

  factory Operator.fromJson(Map<String, dynamic> j) => Operator(
    id:       j['id'] as int?,
    name:     j['name'] as String? ?? '',
    phone:    j['phone'] as String?,
    isActive: (j['is_active'] as int? ?? 1) == 1,
  );

  Map<String, dynamic> toJson() => {
    'id':        id,
    'name':      name,
    'phone':     phone,
    'is_active': isActive ? 1 : 0,
  };

  @override
  String toString() => name;
}