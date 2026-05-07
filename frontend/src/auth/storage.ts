// Token and PKCE verifier storage.
// Uses sessionStorage on web (survives the OAuth redirect within the same tab)
// and AsyncStorage on native.

import { Platform } from 'react-native';
import AsyncStorage from '@react-native-async-storage/async-storage';

const TOKEN_KEY = 'auth_token';
const VERIFIER_KEY = 'pkce_verifier';

export async function saveToken(token: string): Promise<void> {
  if (Platform.OS === 'web') {
    localStorage.setItem(TOKEN_KEY, token);
  } else {
    await AsyncStorage.setItem(TOKEN_KEY, token);
  }
}

export async function loadToken(): Promise<string | null> {
  if (Platform.OS === 'web') {
    return localStorage.getItem(TOKEN_KEY);
  }
  return AsyncStorage.getItem(TOKEN_KEY);
}

export async function clearToken(): Promise<void> {
  if (Platform.OS === 'web') {
    localStorage.removeItem(TOKEN_KEY);
  } else {
    await AsyncStorage.removeItem(TOKEN_KEY);
  }
}

export function saveVerifier(verifier: string): void {
  // sessionStorage survives the OAuth redirect in the same tab.
  if (Platform.OS === 'web') {
    sessionStorage.setItem(VERIFIER_KEY, verifier);
  }
}

export function loadVerifier(): string | null {
  if (Platform.OS === 'web') {
    return sessionStorage.getItem(VERIFIER_KEY);
  }
  return null;
}
