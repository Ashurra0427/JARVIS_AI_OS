// lib/widgets/offline_banner.dart
import 'package:flutter/material.dart';

class OfflineBanner extends StatelessWidget {
  final bool connected;
  final int queuedCount;
  const OfflineBanner({
    super.key,
    required this.connected,
    required this.queuedCount,
  });

  @override
  Widget build(BuildContext context) {
    if (connected) return const SizedBox.shrink();
    return Material(
      color: Colors.red.shade700,
      child: Padding(
        padding: const EdgeInsets.symmetric(vertical: 6, horizontal: 16),
        child: Row(
          children: [
            const Icon(Icons.wifi_off, color: Colors.white, size: 16),
            const SizedBox(width: 8),
            Text(
              queuedCount > 0
                  ? 'Offline — $queuedCount action(s) queued'
                  : 'Offline — reconnecting…',
              style: const TextStyle(color: Colors.white, fontSize: 13),
            ),
          ],
        ),
      ),
    );
  }
}