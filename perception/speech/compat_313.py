"""
JARVIS AI OS — Python 3.13 Compatibility Shim  (P-32)
=======================================================
Python 3.13 removes several stdlib modules that older audio / speech code
relied on.  This shim re-implements the minimal subset actually used in
JARVIS and installs them into sys.modules before any import can fail.

Usage
-----
Import this module at the TOP of any file that uses a removed module, or
call ``install()`` once at process startup (e.g., in boot/startup.py or
start.py) before any other imports.

    from perception.speech.compat_313 import install
    install()   # safe no-op on Python < 3.13

Removed modules covered
------------------------
  ``audioop``   — audio sample conversion (rms, lin2lin, bias, etc.)
                  Used by: PyAudio, openwakeword, hotword.py, microphone.py
                  Replacement: struct + math (pure Python, always available)

  ``cgi``       — (not used in JARVIS core, shim provided for transitive deps)
  ``cgitb``     — (same)

Modules NOT shimmed (not used in JARVIS)
-----------------------------------------
  aifc, chunk, crypt, imghdr, mailcap, msilib, nis, nntplib, ossaudiodev,
  pipes, sndhdr, spwd, sunau, telnetlib, uu, xdrlib
"""

from __future__ import annotations

import math
import struct
import sys
from typing import Union

__all__ = ["install", "audioop"]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_INSTALLED = False


def _clamp(val: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, val))


def _sample_width_info(width: int) -> tuple[str, int, int]:
    """Return (struct_fmt, min_val, max_val) for a given byte width."""
    if width == 1:
        return "b", -128, 127
    elif width == 2:
        return "<h", -32768, 32767
    elif width == 4:
        return "<i", -2147483648, 2147483647
    else:
        raise ValueError(f"Unsupported sample width: {width}")


def _unpack_samples(fragment: bytes, width: int) -> list[int]:
    fmt, _, _ = _sample_width_info(width)
    n = len(fragment) // width
    return list(struct.unpack_from(f"{n}{fmt}", fragment))


def _pack_samples(samples: list[int], width: int) -> bytes:
    fmt, lo, hi = _sample_width_info(width)
    clamped = [_clamp(s, lo, hi) for s in samples]
    n = len(clamped)
    return struct.pack(f"{n}{fmt}", *clamped)


# ---------------------------------------------------------------------------
# audioop shim
# ---------------------------------------------------------------------------

class _AudioopModule:
    """
    Pure-Python re-implementation of the ``audioop`` C module removed in 3.13.

    Covers only the functions used by JARVIS and its direct dependencies
    (PyAudio's convenience wrappers, openwakeword's VAD, hotword.py RMS checks).
    Functions not listed here raise ``NotImplementedError`` with a clear message.
    """

    error = type("error", (Exception,), {})  # audioop.error exception class

    # ------------------------------------------------------------------
    # RMS amplitude — used by hotword.py and noise_calibrator for VAD
    # ------------------------------------------------------------------

    def rms(self, fragment: bytes, width: int) -> int:
        """Root-mean-square amplitude of the audio fragment."""
        if not fragment:
            return 0
        samples = _unpack_samples(fragment, width)
        mean_sq = sum(s * s for s in samples) / len(samples)
        return int(math.sqrt(mean_sq))

    # ------------------------------------------------------------------
    # Sample conversion (lin2lin) — used by PyAudio internals
    # ------------------------------------------------------------------

    def lin2lin(self, fragment: bytes, width: int, newwidth: int) -> bytes:
        """Convert between 8/16/32-bit linear PCM."""
        if width == newwidth:
            return fragment
        samples = _unpack_samples(fragment, width)
        _, old_lo, old_hi = _sample_width_info(width)
        _, new_lo, new_hi = _sample_width_info(newwidth)
        scale = (new_hi - new_lo) / (old_hi - old_lo)
        converted = [int(s * scale) for s in samples]
        return _pack_samples(converted, newwidth)

    # ------------------------------------------------------------------
    # Bias (DC offset) — sometimes used for audio pre-processing
    # ------------------------------------------------------------------

    def bias(self, fragment: bytes, width: int, bias: int) -> bytes:
        """Add a constant bias to all samples."""
        samples = _unpack_samples(fragment, width)
        _, lo, hi = _sample_width_info(width)
        biased = [_clamp(s + bias, lo, hi) for s in samples]
        return _pack_samples(biased, width)

    # ------------------------------------------------------------------
    # Max amplitude
    # ------------------------------------------------------------------

    def max(self, fragment: bytes, width: int) -> int:
        """Return maximum absolute sample value."""
        if not fragment:
            return 0
        samples = _unpack_samples(fragment, width)
        return builtins_max(abs(s) for s in samples)

    # ------------------------------------------------------------------
    # Average amplitude
    # ------------------------------------------------------------------

    def avg(self, fragment: bytes, width: int) -> int:
        """Return average of signed sample values."""
        if not fragment:
            return 0
        samples = _unpack_samples(fragment, width)
        return int(sum(samples) / len(samples))

    # ------------------------------------------------------------------
    # Multiply (volume scaling)
    # ------------------------------------------------------------------

    def mul(self, fragment: bytes, width: int, factor: float) -> bytes:
        """Multiply each sample by ``factor``."""
        samples = _unpack_samples(fragment, width)
        _, lo, hi = _sample_width_info(width)
        scaled = [_clamp(int(s * factor), lo, hi) for s in samples]
        return _pack_samples(scaled, width)

    # ------------------------------------------------------------------
    # Cross-correlation (used by openwakeword for alignment)
    # ------------------------------------------------------------------

    def cross(self, fragment: bytes, width: int) -> int:
        """Return number of zero-crossings in the fragment."""
        if not fragment:
            return 0
        samples = _unpack_samples(fragment, width)
        crossings = sum(
            1 for i in range(1, len(samples))
            if (samples[i] >= 0) != (samples[i - 1] >= 0)
        )
        return crossings

    # ------------------------------------------------------------------
    # Fragment length in samples
    # ------------------------------------------------------------------

    def getsample(self, fragment: bytes, width: int, index: int) -> int:
        """Return a single sample at position ``index``."""
        samples = _unpack_samples(fragment, width)
        return samples[index]

    # ------------------------------------------------------------------
    # Stereo → mono / mono → stereo
    # ------------------------------------------------------------------

    def tomono(self, fragment: bytes, width: int, lfac: float, rfac: float) -> bytes:
        """Mix stereo to mono with left and right factors."""
        samples = _unpack_samples(fragment, width)
        if len(samples) % 2 != 0:
            raise self.error("Stereo fragment must have even number of samples")
        _, lo, hi = _sample_width_info(width)
        mono = [_clamp(int(samples[i] * lfac + samples[i + 1] * rfac), lo, hi)
                for i in range(0, len(samples), 2)]
        return _pack_samples(mono, width)

    def tostereo(self, fragment: bytes, width: int, lfac: float, rfac: float) -> bytes:
        """Convert mono to stereo."""
        samples = _unpack_samples(fragment, width)
        _, lo, hi = _sample_width_info(width)
        stereo = []
        for s in samples:
            stereo.append(_clamp(int(s * lfac), lo, hi))
            stereo.append(_clamp(int(s * rfac), lo, hi))
        return _pack_samples(stereo, width)

    # ------------------------------------------------------------------
    # Stub for rarely-used functions: raise with a useful message
    # ------------------------------------------------------------------

    def __getattr__(self, name: str):
        def _stub(*args, **kwargs):
            raise NotImplementedError(
                f"audioop.{name}() is not implemented in the JARVIS 3.13 shim. "
                "File an issue or add the implementation to perception/speech/compat_313.py"
            )
        return _stub


# Singleton instance (behaves like a module)
audioop = _AudioopModule()

# Fix: pull builtins.max before we shadow the name in _AudioopModule.max
import builtins as _builtins
builtins_max = _builtins.max


# ---------------------------------------------------------------------------
# Minimal cgi shim (for transitive deps only — not used directly in JARVIS)
# ---------------------------------------------------------------------------

class _CgiModule:
    """Stub for ``cgi`` module removed in Python 3.13."""

    class FieldStorage:
        def __init__(self, *args, **kwargs):
            raise NotImplementedError(
                "cgi.FieldStorage is not available in Python 3.13. "
                "Use multipart or python-multipart instead."
            )

    def escape(self, s: str, quote: bool = False) -> str:
        """html.escape replacement."""
        import html
        return html.escape(s, quote=quote)

    def parse_header(self, line: str) -> tuple[str, dict]:
        """Parse a Content-type like header."""
        parts = [p.strip() for p in line.split(";")]
        key = parts[0]
        params: dict[str, str] = {}
        for part in parts[1:]:
            if "=" in part:
                k, _, v = part.partition("=")
                params[k.strip()] = v.strip().strip('"')
        return key, params

    def __getattr__(self, name: str):
        def _stub(*args, **kwargs):
            raise NotImplementedError(
                f"cgi.{name}() is not available in Python 3.13. "
                "See: https://docs.python.org/3/whatsnew/3.13.html#removed"
            )
        return _stub


# ---------------------------------------------------------------------------
# install() — call once at process startup
# ---------------------------------------------------------------------------

def install() -> None:
    """
    Install compatibility shims into sys.modules for Python 3.13+.

    Safe to call on Python 3.11 / 3.12 — existing modules are NOT replaced.
    Idempotent: subsequent calls are no-ops.
    """
    global _INSTALLED
    if _INSTALLED:
        return

    _INSTALLED = True
    py_ver = sys.version_info[:2]

    # Only install shims for removed modules on 3.13+
    if py_ver >= (3, 13):
        if "audioop" not in sys.modules:
            sys.modules["audioop"] = audioop  # type: ignore[assignment]

        if "cgi" not in sys.modules:
            sys.modules["cgi"] = _CgiModule()  # type: ignore[assignment]

        if "cgitb" not in sys.modules:
            # cgitb was rarely used; a no-op module is sufficient
            import types
            cgitb_shim = types.ModuleType("cgitb")
            def _noop(*a, **k): pass
            cgitb_shim.enable = _noop  # type: ignore[attr-defined]
            sys.modules["cgitb"] = cgitb_shim


# ---------------------------------------------------------------------------
# Auto-install on import (optional — call install() explicitly for clarity)
# ---------------------------------------------------------------------------

# Uncomment the line below to auto-install on import.
# install()