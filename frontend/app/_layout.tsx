// Root layout — wraps every route with providers + dark status bar.

import React, { useEffect } from 'react';
import { Stack } from 'expo-router';
import { StatusBar } from 'expo-status-bar';
import { GestureHandlerRootView } from 'react-native-gesture-handler';
import { SafeAreaProvider } from 'react-native-safe-area-context';
import * as SplashScreen from 'expo-splash-screen';
import { LangProvider } from '@/i18n/LangProvider';
import { AuthProvider } from '@/auth/AuthContext';
import { T } from '@/theme/tokens';

SplashScreen.preventAutoHideAsync().catch(() => {});

export default function RootLayout() {
  useEffect(() => {
    // Hide splash once first paint is ready. (Add font loading here when wired up.)
    SplashScreen.hideAsync().catch(() => {});
  }, []);

  return (
    <GestureHandlerRootView style={{ flex: 1, backgroundColor: T.bg }}>
      <SafeAreaProvider>
        <LangProvider>
          <AuthProvider>
          <StatusBar style="light" />
          <Stack
            screenOptions={{
              headerShown: false,
              contentStyle: { backgroundColor: T.bg },
              animation: 'slide_from_right',
            }}
          />
          </AuthProvider>
        </LangProvider>
      </SafeAreaProvider>
    </GestureHandlerRootView>
  );
}
