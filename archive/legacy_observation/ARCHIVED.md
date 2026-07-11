# perception/observation/activity_tracker.py — archived (this pass)
===================================================================

## What this module is

`ActivityTracker` is a passive, thread-safe ring-buffer observer of user
activity events (key_press, mouse_click, window_focus) with start()/stop().

## Why it was archived

Superseded by `perception/observation/activity_observer.py`, which is already
wired into `server.py`'s Phase 3 block. `ActivityObserver` polls active
windows, idle time, and processes, and publishes `perception.activity.*`
events that feed the ProactiveEngine. A repo-wide import-graph scan confirmed
`ActivityTracker` is imported nowhere in the live system. Moved here rather
than deleted.

## To bring it back

1. Move `activity_tracker.py` back to `perception/observation/`.
2. Have `ActivityObserver` delegate fine-grained event recording to it, or
   keep `ActivityObserver`'s own snapshot model and drop this tracker.
