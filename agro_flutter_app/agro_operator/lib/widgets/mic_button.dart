// lib/widgets/mic_button.dart
//
// Phase 4 — Mic Button Widget
// ─────────────────────────────────────────────────────────────────────────────
// Usage: drop into any screen that has access to VoiceService.
//
//   MicButton(
//     voiceService: voiceService,
//     sttPipeline: lang.sttPipeline,   // from LanguageProvider
//     onResult: (text) => setState(() => _inputController.text = text),
//   )
//
// The button shows:
//   • mic icon when idle
//   • animated red indicator while recording
//   • auto-sends on release (GestureDetector onLongPressEnd)
// ─────────────────────────────────────────────────────────────────────────────

import 'dart:async';
import 'package:flutter/material.dart';
import '../services/voice_service.dart';
import '../services/ws_service.dart';

class MicButton extends StatefulWidget {
  final VoiceService voiceService;
  final WsService wsService;
  final String sttPipeline;      // 'faster_whisper_ne' | 'groq_whisper_en'
  final ValueChanged<String>? onResult;  // called when stt_result arrives
  final bool isNepali;

  const MicButton({
    super.key,
    required this.voiceService,
    required this.wsService,
    required this.sttPipeline,
    required this.isNepali,
    this.onResult,
  });

  @override
  State<MicButton> createState() => _MicButtonState();
}

class _MicButtonState extends State<MicButton>
    with SingleTickerProviderStateMixin {
  bool _recording = false;
  late AnimationController _pulse;
  late Animation<double> _scale;
  StreamSubscription? _sttSub;

  @override
  void initState() {
    super.initState();
    _pulse = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 700),
    )..repeat(reverse: true);
    _scale = Tween<double>(begin: 1.0, end: 1.25).animate(
      CurvedAnimation(parent: _pulse, curve: Curves.easeInOut),
    );

    // Listen for stt_result from server. Stored + cancelled in dispose()
    // so a recreated MicButton (e.g. after a route replacement) doesn't
    // stack up duplicate listeners on the shared WsService stream — each
    // stale listener would otherwise keep firing onResult() against a
    // widget that's no longer mounted.
    _sttSub = widget.wsService.stream.listen((msg) {
      if (!mounted) return;
      if (msg['type'] == 'stt_result') {
        final text = (msg['text'] as String?) ?? '';
        if (text.isNotEmpty) {
          widget.onResult?.call(text);
        } else {
          // Recording completed and reached the server fine, but no
          // speech was recognised (too short, too quiet, silence). This
          // used to look identical to "nothing happened" — same as a
          // real failure — because empty text was just dropped instead
          // of ever reaching onResult(). Surface it so a genuinely empty
          // result is distinguishable from the button not working.
          ScaffoldMessenger.of(context).showSnackBar(
            SnackBar(
              content: Text(widget.isNepali
                  ? 'केही सुनिएन — फेरि प्रयास गर्नुस्'
                  : 'Didn\'t catch that — try again'),
              duration: const Duration(seconds: 2),
            ),
          );
        }
      }
    });
  }

  Future<void> _startRecording() async {
    final started = await widget.voiceService.startRecording();
    if (started && mounted) {
      setState(() => _recording = true);
      _pulse.forward();
    } else if (mounted) {
      // Permission denied or the recorder failed to start — this used to
      // just do nothing, indistinguishable from a tap that didn't
      // register at all.
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(widget.isNepali
              ? 'माइक्रोफोन अनुमति चाहियो'
              : 'Microphone permission needed'),
          backgroundColor: Colors.red,
        ),
      );
    }
  }

  Future<void> _stopRecording() async {
    if (!_recording) return;
    setState(() => _recording = false);
    _pulse.reset();
    await widget.voiceService.stopAndSend(sttPipeline: widget.sttPipeline);
  }

  @override
  void dispose() {
    _sttSub?.cancel();
    _pulse.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Tooltip(
      message: widget.isNepali
          ? 'बोल्न थिच्नुस् र समात्नुस्, छोड्दा पठाउँछ'
          : 'Press and hold to speak, release to send',
      preferBelow: false,
      child: GestureDetector(
      onLongPress: _startRecording,
      onLongPressEnd: (_) => _stopRecording(),
      onLongPressCancel: () async {
        if (_recording) {
          setState(() => _recording = false);
          _pulse.reset();
          await widget.voiceService.cancel();
        }
      },
      child: AnimatedBuilder(
        animation: _pulse,
        builder: (_, child) => Transform.scale(
          scale: _recording ? _scale.value : 1.0,
          child: child,
        ),
        child: Container(
          width: 56,
          height: 56,
          decoration: BoxDecoration(
            color: _recording ? Colors.red : const Color(0xFF003893),
            shape: BoxShape.circle,
            boxShadow: [
              BoxShadow(
                color: (_recording ? Colors.red : const Color(0xFF003893))
                    .withOpacity(0.4),
                blurRadius: 8,
                spreadRadius: 2,
              ),
            ],
          ),
          child: Icon(
            _recording ? Icons.stop : Icons.mic,
            color: Colors.white,
            size: 26,
          ),
        ),
      ),
      ),
    );
  }
}
