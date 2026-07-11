// lib/app.dart  [agro_client]
import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:provider/provider.dart';
import 'providers/language_provider.dart';
import 'providers/customer_auth_provider.dart';
import 'screens/auth/login_screen.dart';
import 'screens/home/customer_home_screen.dart';

class AgroClientApp extends StatelessWidget {
  const AgroClientApp({super.key});

  @override
  Widget build(BuildContext context) {
    final lang = context.watch<LanguageProvider>();
    final auth = context.watch<CustomerAuthProvider>();

    return MaterialApp(
      title: 'JARVIS AGRO',
      debugShowCheckedModeBanner: false,
      locale: Locale(lang.isNepali ? 'ne' : 'en'),
      localizationsDelegates: const [
        GlobalMaterialLocalizations.delegate,
        GlobalWidgetsLocalizations.delegate,
        GlobalCupertinoLocalizations.delegate,
      ],
      supportedLocales: const [Locale('en'), Locale('ne')],
      theme: ThemeData(
        primaryColor: const Color(0xFF003893),
        colorSchemeSeed: const Color(0xFF003893),
        useMaterial3: true,
        appBarTheme: const AppBarTheme(
          backgroundColor: Color(0xFF003893),
          foregroundColor: Colors.white,
          centerTitle: false,
          elevation: 0,
        ),
        elevatedButtonTheme: ElevatedButtonThemeData(
          style: ElevatedButton.styleFrom(
            backgroundColor: const Color(0xFF003893),
            foregroundColor: Colors.white,
            shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8)),
          ),
        ),
        inputDecorationTheme: InputDecorationTheme(
          border: OutlineInputBorder(borderRadius: BorderRadius.circular(8)),
          focusedBorder: OutlineInputBorder(
            borderRadius: BorderRadius.circular(8),
            borderSide: const BorderSide(color: Color(0xFF003893), width: 2),
          ),
          isDense: true,
        ),
      ),
      home: auth.isRestoring
          ? const _SplashScreen()
          : (auth.isLoggedIn ? const CustomerHomeScreen() : const LoginScreen()),
    );
  }
}

class _SplashScreen extends StatelessWidget {
  const _SplashScreen();
  @override
  Widget build(BuildContext context) => Scaffold(
    backgroundColor: const Color(0xFF003893),
    body: Column(
      mainAxisAlignment: MainAxisAlignment.center,
      children: const [
        Center(child: Icon(Icons.agriculture, color: Colors.white70, size: 80)),
        SizedBox(height: 20),
        Text('JARVIS AGRO', style: TextStyle(color: Colors.white, fontSize: 28,
            fontWeight: FontWeight.bold, letterSpacing: 2)),
        SizedBox(height: 8),
        Text('Customer App', style: TextStyle(color: Colors.white60, fontSize: 14)),
        SizedBox(height: 40),
        CircularProgressIndicator(color: Colors.white),
      ],
    ),
  );
}
