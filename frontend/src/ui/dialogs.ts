// Platform-aware dialogs. React Native web's Alert.alert is a NO-OP — errors
// and confirm dialogs silently vanish (found in the PR #5 staging smoke test:
// a failed upload showed nothing). Web falls back to window.alert/confirm;
// native keeps the RN Alert UX.

import { Alert, Platform } from 'react-native';

export function showError(title: string, message: string): void {
  if (Platform.OS === 'web') {
    window.alert(`${title}\n\n${message}`);
  } else {
    Alert.alert(title, message);
  }
}

export function showSuccess(title: string, message: string, onDone: () => void): void {
  if (Platform.OS === 'web') {
    window.alert(`${title}\n\n${message}`);
    onDone();
  } else {
    Alert.alert(title, message, [{ text: 'OK', onPress: onDone }]);
  }
}

export function confirmDestructive(opts: {
  title: string;
  message?: string;
  confirmLabel: string;
  cancelLabel: string;
  onConfirm: () => void;
}): void {
  if (Platform.OS === 'web') {
    if (window.confirm(`${opts.title}${opts.message ? `\n\n${opts.message}` : ''}`)) {
      opts.onConfirm();
    }
  } else {
    Alert.alert(opts.title, opts.message, [
      { text: opts.cancelLabel, style: 'cancel' },
      { text: opts.confirmLabel, style: 'destructive', onPress: opts.onConfirm },
    ]);
  }
}
