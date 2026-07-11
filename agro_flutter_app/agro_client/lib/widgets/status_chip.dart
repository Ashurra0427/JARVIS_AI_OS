// lib/widgets/status_chip.dart  [agro_client]
import 'package:flutter/material.dart';

class StatusChip extends StatelessWidget {
  final String status;
  final bool showIcon;
  const StatusChip(this.status, {super.key, this.showIcon = true});

  @override
  Widget build(BuildContext context) {
    final (color, label, icon) = _resolve(status);
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
      decoration: BoxDecoration(
        color: color.withOpacity(0.15),
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: color.withOpacity(0.4)),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          if (showIcon) ...[
            Icon(icon, color: color, size: 11),
            const SizedBox(width: 3),
          ],
          Text(label, style: TextStyle(
            color: color, fontSize: 11, fontWeight: FontWeight.w700, letterSpacing: 0.3)),
        ],
      ),
    );
  }

  (Color, String, IconData) _resolve(String s) => switch (s) {
    'pending'     => (const Color(0xFFE67E22), 'PENDING',     Icons.schedule),
    'confirmed'   => (const Color(0xFF2980B9), 'CONFIRMED',   Icons.thumb_up_outlined),
    'in_progress' => (const Color(0xFF8E44AD), 'IN PROGRESS', Icons.play_circle_outline),
    'completed'   => (const Color(0xFF27AE60), 'COMPLETED',   Icons.check_circle_outline),
    'cancelled'   => (const Color(0xFFE74C3C), 'CANCELLED',   Icons.cancel_outlined),
    _             => (Colors.grey,              s.toUpperCase(), Icons.help_outline),
  };
}
