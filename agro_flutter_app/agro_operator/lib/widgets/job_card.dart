// lib/widgets/job_card.dart
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import '../models/job.dart';
import '../providers/language_provider.dart';
import '../utils/bs_date_utils.dart';
import 'status_chip.dart';

class JobCard extends StatelessWidget {
  final Job job;
  final VoidCallback? onTap;
  const JobCard({super.key, required this.job, this.onTap});

  Color _statusBorder(String status) => switch (status) {
    'cancelled'   => Colors.red.shade200,
    'completed'   => Colors.green.shade200,
    'in_progress' => Colors.purple.shade200,
    _             => Colors.transparent,
  };

  @override
  Widget build(BuildContext context) {
    final lang = context.watch<LanguageProvider>();
    final isNe = lang.isNepali;
    final isCancelled = job.status == 'cancelled';
    final isCompleted = job.status == 'completed';

    final service = job.isAgriculture
        ? job.service
        : (job.material ?? job.service);
    final qty = job.isAgriculture
        ? '${job.areaValue ?? ''} ${job.areaUnit ?? ''}'
        : '${job.quantityValue ?? ''} ${job.quantityUnit ?? ''}';

    return Opacity(
      opacity: isCancelled ? 0.55 : 1.0,
      child: Card(
        margin: const EdgeInsets.symmetric(horizontal: 12, vertical: 5),
        elevation: isCancelled ? 0 : 2,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(12),
          side: BorderSide(color: _statusBorder(job.status), width: isCancelled || isCompleted ? 1.5 : 0),
        ),
        color: isCancelled ? Colors.grey.shade100 : null,
        child: InkWell(
          onTap: onTap,
          borderRadius: BorderRadius.circular(12),
          child: Padding(
            padding: const EdgeInsets.all(14),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    Icon(
                      job.isAgriculture ? Icons.agriculture : Icons.local_shipping,
                      color: isCancelled ? Colors.grey : const Color(0xFF1565C0),
                      size: 20,
                    ),
                    const SizedBox(width: 8),
                    Expanded(
                      child: Text(
                        service,
                        style: TextStyle(
                          fontWeight: FontWeight.bold,
                          fontSize: 15,
                          decoration: isCancelled ? TextDecoration.lineThrough : null,
                          color: isCancelled ? Colors.grey : null,
                        ),
                      ),
                    ),
                    StatusChip(job.status),
                  ],
                ),
                if (job.customerName != null) ...[
                  const SizedBox(height: 4),
                  Text(
                    '${isNe ? "ग्राहक" : "Customer"}: ${job.customerName}',
                    style: TextStyle(
                      color: isCancelled ? Colors.grey : Colors.black54,
                      fontSize: 13,
                    ),
                  ),
                ],
                const SizedBox(height: 4),
                Row(
                  children: [
                    if (qty.trim().isNotEmpty)
                      Text(
                        qty.trim(),
                        style: TextStyle(
                          color: isCancelled ? Colors.grey : Colors.black54,
                          fontSize: 13,
                        ),
                      ),
                    const Spacer(),
                    if (job.totalAmount != null)
                      Text(
                        'Rs ${job.totalAmount!.toStringAsFixed(0)}',
                        style: TextStyle(
                          fontWeight: FontWeight.bold,
                          color: isCancelled ? Colors.grey : const Color(0xFF1B5E20),
                          fontSize: 14,
                          decoration: isCancelled ? TextDecoration.lineThrough : null,
                        ),
                      ),
                  ],
                ),
                if (!isCancelled && job.hasDues)
                  Padding(
                    padding: const EdgeInsets.only(top: 4),
                    child: Text(
                      '${isNe ? "बाँकी" : "Due"}: Rs ${job.balanceDue!.toStringAsFixed(0)}',
                      style: const TextStyle(color: Colors.red, fontSize: 12),
                    ),
                  ),
                if (job.scheduledDate != null)
                  Padding(
                    padding: const EdgeInsets.only(top: 3),
                    child: Text(
                      '📅 ${formatBsShort(DateTime.parse(job.scheduledDate!))} (${job.scheduledDate})',
                      style: TextStyle(
                        color: isCancelled ? Colors.grey.shade400 : Colors.black38,
                        fontSize: 11,
                      ),
                    ),
                  ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}
