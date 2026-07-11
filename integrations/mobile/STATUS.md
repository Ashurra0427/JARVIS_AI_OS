# STATUS: Scaffolded, not yet implemented

This folder is empty scaffolding for future mobile-specific integration
code (push notifications, mobile-only auth, or native bridge endpoints
beyond the existing mobile HTML HUD client). No code exists here yet.

**Intended scope (not yet designed in detail):**
- Push notification delivery for proactive alerts (see
  `cognition/intelligence/proactive_engine.py`) to a mobile client
- Any mobile-specific auth/session handling beyond what the existing
  WebSocket transport already provides to the mobile HTML HUD

Note: the existing mobile HTML HUD client itself already works and is not
blocked on this folder — this is for capability beyond that, not a
prerequisite for current mobile usage.

Do not assume any of the above exists. This is a placeholder so the
directory's intent is documented instead of being a bare empty folder.

(Phase 4.5)
