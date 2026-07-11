# JARVIS AGRO — Customer Portal (`agro_client`)

Customer-facing companion to `agro_operator`. Lets a customer:

- Log in with **phone number + 4-digit PIN** (issued by the operator from
  `agro_operator`'s "Customers" screen — free, no SMS cost).
- View their own job history.
- View their own outstanding balance.
- Submit a job request (goes into a request inbox the operator reviews —
  never directly creates a real job).

Talks to the **same backend** as `agro_operator` (`server.py` or
`agro_server.py`, whichever you have running) over the same WebSocket
protocol, using a separate `"customer"` message namespace that can never
trigger operator-only writes (see `agents/agro/customer_portal.py` in the
backend).

## ⚠️ One-time setup before this builds

This `lib/` + `pubspec.yaml` was generated without access to the Flutter
SDK, so the platform folders (`android/`, `ios/`, `windows/`, etc.) are
**not included**. To finish setup, from a machine with Flutter installed:

```bash
cd "agro_flutter app"
flutter create --org com.jarvis.agro --project-name jarvis_agro_client agro_client_scaffold

# Copy the generated platform folders into this project:
cp -r agro_client_scaffold/android agro_client_scaffold/ios \
      agro_client_scaffold/windows agro_client_scaffold/linux \
      agro_client_scaffold/macos agro_client_scaffold/web \
      agro_client/

rm -rf agro_client_scaffold
cd agro_client
flutter pub get
flutter run
```

(This two-step dance is only needed because platform folders are
generated boilerplate that depends on your local Flutter SDK version —
the actual app code in `lib/` is already complete and ready to run once
the platform folders exist.)

## Configuring the server URL

Same as `agro_operator` — `lib/config/app_config.dart` defaults to
`http://192.168.100.9:7788` with the shared `_jarvisSecret` token. If you
add a Settings screen later, point `AppConfig.save(url)` at whichever of
`server.py` / `agro_server.py` you're running; both implement the
`"customer"` WS namespace identically.

## Before a customer can log in

A customer needs at least one job already logged (so a `customers` row
exists for their phone number), and the operator must set their PIN once
from `agro_operator → Customers → Issue Customer PIN`.
