// lib/widgets/tts_speaking_bar.dart  [agro_client]
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import '../services/tts_playback_service.dart';

class TtsSpeakingBar extends StatelessWidget {
  const TtsSpeakingBar({super.key});

  @override
  Widget build(BuildContext context) {
    final tts = context.watch<TtsPlaybackService>();
    if (!tts.isSpeaking) return const SizedBox.shrink();
    return Material(
      color: const Color(0xFF003893).withOpacity(0.93),
      child: Padding(
        padding: const EdgeInsets.symmetric(vertical: 8, horizontal: 16),
        child: Row(
          children: [
            const SizedBox(
              width: 16, height: 16,
              child: CircularProgressIndicator(color: Colors.white, strokeWidth: 2),
            ),
            const SizedBox(width: 10),
            Expanded(
              child: Text(
                tts.lastText ?? 'Playing message…',
                style: const TextStyle(color: Colors.white, fontSize: 13),
                maxLines: 1, overflow: TextOverflow.ellipsis,
              ),
            ),
            IconButton(
              icon: const Icon(Icons.stop, color: Colors.white, size: 18),
              padding: EdgeInsets.zero,
              constraints: const BoxConstraints(),
              onPressed: tts.stopPlayback,
            ),
          ],
        ),
      ),
    );
  }
}
