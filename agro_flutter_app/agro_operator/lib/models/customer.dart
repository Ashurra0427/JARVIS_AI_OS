// lib/models/customer.dart

class Customer {
  final int? id;
  final String name;
  final String? phone;
  final String? address;

  const Customer({
    this.id,
    required this.name,
    this.phone,
    this.address,
  });

  factory Customer.fromJson(Map<String, dynamic> j) => Customer(
    id:      j['id'] as int?,
    name:    j['name'] as String? ?? '',
    phone:   j['phone'] as String?,
    address: j['address'] as String?,
  );

  Map<String, dynamic> toJson() => {
    'id':      id,
    'name':    name,
    'phone':   phone,
    'address': address,
  };

  @override
  String toString() => name;
}   