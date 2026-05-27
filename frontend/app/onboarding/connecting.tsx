import React, { useState, useEffect, useRef, useCallback } from 'react';
import { View, Text, StyleSheet, Platform, Animated, Easing } from 'react-native';
import { useLocalSearchParams, useRouter } from 'expo-router';
import { SafeAreaView } from 'react-native-safe-area-context';
import * as WebBrowser from 'expo-web-browser';
import * as Linking from 'expo-linking';
import { Button } from '@/components/atoms';
import { Icon } from '@/components/Icon';
import { useAuth } from '@/auth/AuthContext';
import { API_BASE } from '@/auth/config';
import { T, TYPE } from '@/theme/tokens';

const PROVIDER_LABELS: Record<string, string> = {
  clover: 'Clover',
  square: 'Square',
  lightspeed: 'Lightspeed',
  touchbistro: 'TouchBistro',
  maitred: "Maitre'D",
  toast: 'Toast',
  veloce: 'Veloce',
};

type SyncStep = {
  key: string;
  label: string;
  done: boolean;
  count?: number;
  total_cents?: number;
};

function SpinnerIcon() {
  const spin = useRef(new Animated.Value(0)).current;
  useEffect(() => {
    const loop = Animated.loop(
      Animated.timing(spin, {
        toValue: 1,
        duration: 1000,
        easing: Easing.linear,
        useNativeDriver: true,
      }),
    );
    loop.start();
    return () => loop.stop();
  }, [spin]);
  const rotate = spin.interpolate({ inputRange: [0, 1], outputRange: ['0deg', '360deg'] });
  return (
    <Animated.View style={{ transform: [{ rotate }] }}>
      <Icon name="sparkles" size={18} color={T.ac} />
    </Animated.View>
  );
}

function StepRow({ step, isActive }: { step: SyncStep; isActive: boolean }) {
  return (
    <View style={[styles.stepRow, step.done && styles.stepRowDone]}>
      <View style={[styles.stepIcon, step.done && styles.stepIconDone]}>
        {step.done ? (
          <Icon name="check" size={14} color="#FFF" />
        ) : isActive ? (
          <SpinnerIcon />
        ) : (
          <View style={styles.stepDot} />
        )}
      </View>
      <Text style={[styles.stepLabel, step.done && styles.stepLabelDone]}>
        {step.label}
      </Text>
    </View>
  );
}

export default function Connecting() {
  const { provider } = useLocalSearchParams<{ provider?: string }>();
  const { token, me } = useAuth();
  const router = useRouter();
  const [phase, setPhase] = useState<'pre' | 'connecting' | 'syncing' | 'done' | 'error'>('pre');
  const [error, setError] = useState<string | null>(null);
  const [steps, setSteps] = useState<SyncStep[]>([]);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const providerLabel = PROVIDER_LABELS[provider ?? ''] ?? provider ?? 'This POS';
  const tenantId = me?.tenants?.[0]?.id;

  const pollSyncProgress = useCallback(async () => {
    if (!token || !tenantId) return;
    try {
      const res = await fetch(`${API_BASE}/api/v1/pos/clover/sync-progress`, {
        headers: { Authorization: `Bearer ${token}`, 'X-Tenant-Id': tenantId },
      });
      if (!res.ok) return;
      const data = await res.json();
      if (data.steps) setSteps(data.steps);
      if (data.sync_completed) {
        setPhase('done');
        if (pollRef.current) clearInterval(pollRef.current);
        setTimeout(() => {
          router.replace('/onboarding/found-summary');
        }, 1200);
      }
    } catch {
      // silent — keep polling
    }
  }, [token, tenantId, router]);

  const handleConnect = async () => {
    if (!token || !me) {
      setError('Not signed in — please go back and sign in first.');
      return;
    }
    if (!tenantId) {
      setError('No tenant found — please complete account setup first.');
      return;
    }
    setPhase('connecting');
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/api/v1/pos/clover/connect-url`, {
        headers: { Authorization: `Bearer ${token}`, 'X-Tenant-Id': tenantId },
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail ?? `Server error ${res.status}`);
      }
      const { url } = await res.json();

      if (Platform.OS === 'web') {
        window.location.href = url;
      } else {
        const redirectUrl = Linking.createURL('/onboarding/found-summary');
        const result = await WebBrowser.openAuthSessionAsync(url, redirectUrl);
        if (result.type === 'success') {
          setPhase('syncing');
          pollRef.current = setInterval(pollSyncProgress, 3000);
          pollSyncProgress();
        } else {
          setPhase('pre');
        }
      }
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Something went wrong');
      setPhase('error');
    }
  };

  useEffect(() => {
    if (provider === 'clover' && token && tenantId) {
      fetch(`${API_BASE}/api/v1/pos/clover/status`, {
        headers: { Authorization: `Bearer ${token}`, 'X-Tenant-Id': tenantId },
      })
        .then(r => r.json())
        .then(d => {
          if (d.connected) {
            setPhase('syncing');
            pollRef.current = setInterval(pollSyncProgress, 3000);
            pollSyncProgress();
          }
        })
        .catch(() => {});
    }
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [provider, token, tenantId, pollSyncProgress]);

  if (provider !== 'clover') {
    return (
      <SafeAreaView style={styles.safe} edges={['top', 'bottom']}>
        <View style={styles.body}>
          <Text style={styles.h2}>{providerLabel} coming soon</Text>
          <Text style={styles.sub}>
            We're adding more integrations. Clover is available now — go back
            and select Clover to connect your POS.
          </Text>
        </View>
        <View style={styles.cta}>
          <Button label="Go back" variant="secondary" fullWidth onPress={() => router.back()} />
        </View>
      </SafeAreaView>
    );
  }

  if (phase === 'syncing' || phase === 'done') {
    const activeIdx = steps.findIndex(s => !s.done);
    return (
      <SafeAreaView style={styles.safe} edges={['top', 'bottom']}>
        <View style={styles.syncBody}>
          <View style={styles.syncIconWrap}>
            <View style={styles.syncIcon}>
              <Icon name="send" size={28} color="#000" />
            </View>
          </View>
          <Text style={styles.syncTitle}>Connecting to {providerLabel}</Text>
          <Text style={styles.syncSub}>
            Read-only: menu, sales, inventory. Never: customer data, payment info, employee records.
          </Text>
          <View style={styles.stepsList}>
            {steps.map((step, i) => (
              <StepRow key={step.key} step={step} isActive={i === activeIdx} />
            ))}
          </View>
        </View>
      </SafeAreaView>
    );
  }

  return (
    <SafeAreaView style={styles.safe} edges={['top', 'bottom']}>
      <View style={styles.body}>
        <Text style={styles.h2}>Connect {providerLabel}</Text>
        <Text style={styles.sub}>
          You'll be redirected to {providerLabel} to authorise ReorderOS. We read
          your orders and menu — we never write to your POS.
        </Text>
        {error && <Text style={styles.error}>{error}</Text>}
      </View>
      <View style={styles.cta}>
        <Button
          label={phase === 'connecting' ? `Opening ${providerLabel}…` : `Connect ${providerLabel}`}
          fullWidth
          iconRight={phase === 'connecting' ? undefined : 'arrow-right'}
          loading={phase === 'connecting'}
          onPress={handleConnect}
        />
        <Button label="Go back" variant="ghost" fullWidth onPress={() => router.back()} />
      </View>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: T.bg },
  body: { flex: 1, paddingHorizontal: T.pad, paddingTop: 32, gap: 12 },
  h2: { ...TYPE.title1, color: T.text },
  sub: { ...TYPE.body, color: T.sec },
  error: { ...TYPE.subhead, color: T.red, marginTop: 8 },
  cta: {
    paddingHorizontal: T.pad,
    paddingVertical: 12,
    gap: 12,
    borderTopWidth: 1,
    borderTopColor: T.hairline,
  },
  syncBody: { flex: 1, paddingHorizontal: T.pad, paddingTop: 48, alignItems: 'center' },
  syncIconWrap: { marginBottom: 20 },
  syncIcon: {
    width: 56,
    height: 56,
    borderRadius: 14,
    backgroundColor: T.ac,
    alignItems: 'center',
    justifyContent: 'center',
  },
  syncTitle: { ...TYPE.title2, color: T.text, textAlign: 'center', marginBottom: 8 },
  syncSub: { ...TYPE.body, color: T.sec, textAlign: 'center', marginBottom: 32, paddingHorizontal: 16 },
  stepsList: { width: '100%', gap: 12 },
  stepRow: {
    flexDirection: 'row',
    alignItems: 'center',
    backgroundColor: T.elev1,
    borderRadius: T.radius,
    paddingHorizontal: 16,
    paddingVertical: 16,
    gap: 12,
    borderWidth: 1,
    borderColor: T.hairline,
  },
  stepRowDone: { borderColor: T.acBorder },
  stepIcon: {
    width: 32,
    height: 32,
    borderRadius: 8,
    alignItems: 'center',
    justifyContent: 'center',
    backgroundColor: T.elev2,
  },
  stepIconDone: { backgroundColor: T.ac },
  stepDot: { width: 8, height: 8, borderRadius: 4, backgroundColor: T.ter },
  stepLabel: { ...TYPE.body, color: T.sec, flex: 1 },
  stepLabelDone: { color: T.text },
});
