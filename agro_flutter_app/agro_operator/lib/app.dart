import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:provider/provider.dart';
import 'providers/language_provider.dart';
import 'services/auth_service.dart';
import 'screens/auth/pin_screen.dart';
import 'screens/home/home_screen.dart';

class AgroApp extends StatelessWidget {
  const AgroApp({super.key});

  // Lets code outside the widget tree (main.dart's global WS error
  // listener) show a SnackBar without needing a BuildContext.
  static final GlobalKey<ScaffoldMessengerState> scaffoldMessengerKey =
      GlobalKey<ScaffoldMessengerState>();

  @override
  Widget build(BuildContext context) {
    final lang = context.watch<LanguageProvider>();
    final auth = context.watch<AuthService>();
    return MaterialApp(
      title: 'JARVIS AGRO',
      debugShowCheckedModeBanner: false,
      scaffoldMessengerKey: scaffoldMessengerKey,
      locale: lang.locale,

      // ── Localisation ───────────────────────────────────────────────
      // Bilingual (ne/en) strings are handled at runtime via LanguageProvider.t()
      // throughout the screens, not via generated ARB-based AppLocalizations —
      // this delegate list just wires up Flutter/Material/Cupertino's own
      // built-in translations (date pickers, "OK"/"Cancel", etc.) for both locales.
      supportedLocales: const [Locale('en'), Locale('ne')],
      localizationsDelegates: const [
        GlobalMaterialLocalizations.delegate,
        GlobalWidgetsLocalizations.delegate,
        GlobalCupertinoLocalizations.delegate,
      ],

      theme: ThemeData(
        colorScheme: ColorScheme.fromSeed(
          seedColor: const Color(0xFF003893),
          brightness: Brightness.light,
        ),
        useMaterial3: true,
        appBarTheme: const AppBarTheme(
          backgroundColor: Color(0xFF003893),
          foregroundColor: Colors.white,
          elevation: 0,
        ),
        cardTheme: CardThemeData(
          elevation: 2,
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
        ),
        inputDecorationTheme: InputDecorationTheme(
          border: OutlineInputBorder(borderRadius: BorderRadius.circular(8)),
          contentPadding: const EdgeInsets.symmetric(horizontal: 12, vertical: 12),
        ),
      ),
      home: auth.isAuthenticated ? const HomeScreen() : const PinScreen(),
    );
  }
}