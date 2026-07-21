import React, { useCallback, useState } from 'react';
import { View, Text, StyleSheet, Pressable, Platform } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { useFocusEffect, useRouter } from 'expo-router';
import { useLang } from '@/i18n/LangProvider';
import { useAuth } from '@/auth/AuthContext';
import { API_BASE } from '@/auth/config';
import { T, TYPE } from '@/theme/tokens';
import { Icon } from '@/components/Icon';

export default function More() {
  const { lang, toggle, t } = useLang();
  const router = useRouter();
  const { token, me, loading } = useAuth();
  const [cloverState, setCloverState] = useState<string | null>(null);
  const [connecting, setConnecting] = useState(false);
  const [cloverError, setCloverError] = useState<string | null>(null);

  const tenantId = me?.tenants?.[0]?.id;

  const fetchCloverStatus = useCallback(async () => {
    if (!token || !tenantId) return;
    try {
      const r = await fetch(`${API_BASE}/api/v1/pos/clover/status`, {
        headers: { Authorization: `Bearer ${token}`, 'X-Tenant-Id': tenantId },
      });
      const d = await r.json();
      setCloverState(d.connected ? `Connected · ${d.merchant_id}` : 'Not connected');
    } catch {
      setCloverState('Not connected');
    }
  }, [token, tenantId]);

  // Re-poll every time the tab is focused — catches returning from OAuth redirect.
  useFocusEffect(
    useCallback(() => {
      fetchCloverStatus();
    }, [fetchCloverStatus])
  );

  const connectClover = async () => {
    setCloverError(null);
    if (loading) return;
    if (!token || !tenantId) {
      setCloverError('Not signed in — please sign in first.');
      return;
    }
    setConnecting(true);
    try {
      const res = await fetch(`${API_BASE}/api/v1/pos/clover/connect-url`, {
        headers: { Authorization: `Bearer ${token}`, 'X-Tenant-Id': tenantId },
      });
      const body = await res.json();
      if (!res.ok) throw new Error(body.detail ?? `Error ${res.status}`);
      if (Platform.OS === 'web') {
        window.location.href = body.url;
      } else {
        throw new Error('Open this page in a browser to connect Clover.');
      }
    } catch (err: unknown) {
      setCloverError(err instanceof Error ? err.message : 'Something went wrong');
      setConnecting(false);
    }
  };

  const cloverRowValue = () => {
    if (loading) return '…';
    if (!token || !tenantId) return 'Sign in to connect';
    if (connecting) return 'Opening…';
    return cloverState ?? '…';
  };

  return (
    <SafeAreaView style={styles.safe} edges={['top']}>
      <View style={styles.body}>
        <Text style={styles.h1}>More</Text>

        <Pressable onPress={toggle} style={styles.row}>
          <Text style={styles.rowLabel}>Language</Text>
          <Text style={styles.rowValue}>{lang === 'en' ? 'English' : 'Français'}</Text>
        </Pressable>

        <Pressable onPress={connectClover} style={styles.row} disabled={connecting || loading}>
          <Text style={styles.rowLabel}>Clover POS</Text>
          <Text style={styles.rowValue}>{cloverRowValue()}</Text>
        </Pressable>
        {cloverError ? <Text style={styles.errorText}>{cloverError}</Text> : null}

        <Pressable
          onPress={() => router.push('/inbound-emails')}
          style={styles.actionRow}
          accessibilityRole="button"
          accessibilityLabel={t.inbTitle}
        >
          <View style={styles.actionIcon}>
            <Icon name="mail" size={20} color={T.ac} />
          </View>
          <View style={styles.actionTextWrap}>
            <Text style={styles.rowLabel}>{t.inbTitle}</Text>
            <Text style={styles.actionSub}>{t.inbSubtitle}</Text>
          </View>
          <Text style={styles.chevron}>›</Text>
        </Pressable>

        <View style={styles.row}><Text style={styles.rowLabel}>Team</Text><Text style={styles.rowValue}>—</Text></View>
        <View style={styles.row}><Text style={styles.rowLabel}>Suppliers</Text><Text style={styles.rowValue}>—</Text></View>
        <View style={styles.row}><Text style={styles.rowLabel}>Billing</Text><Text style={styles.rowValue}>Trial</Text></View>
        <View style={styles.row}><Text style={styles.rowLabel}>Sign out</Text><Text style={styles.rowValue}>—</Text></View>
      </View>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: T.bg },
  body: { padding: T.pad, gap: 8 },
  h1: { ...TYPE.largeTitle, color: T.text, marginBottom: 12 },
  row: {
    flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center',
    backgroundColor: T.elev1, borderRadius: T.radius, paddingHorizontal: 16, paddingVertical: 14,
    borderWidth: 1, borderColor: T.hairline,
  },
  rowLabel: { ...TYPE.body, color: T.text },
  rowValue: { ...TYPE.subhead, color: T.sec },
  // A row that GOES somewhere — icon + subtitle so it never reads as inert.
  actionRow: {
    flexDirection: 'row', alignItems: 'center', gap: 12,
    backgroundColor: T.elev1, borderRadius: T.radius, paddingHorizontal: 16, paddingVertical: 14,
    borderWidth: 1, borderColor: T.acBorder,
  },
  actionIcon: {
    width: 36, height: 36, borderRadius: 18, backgroundColor: T.acSoft,
    alignItems: 'center', justifyContent: 'center',
  },
  actionTextWrap: { flex: 1 },
  actionSub: { ...TYPE.footnote, color: T.sec, marginTop: 2 },
  chevron: { ...TYPE.title3, color: T.sec },
  errorText: { ...TYPE.subhead, color: T.red, paddingHorizontal: 4 },
});
