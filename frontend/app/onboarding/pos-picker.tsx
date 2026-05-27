import React from 'react';
import { View, Text, ScrollView, StyleSheet, Pressable } from 'react-native';
import { useRouter } from 'expo-router';
import { SafeAreaView } from 'react-native-safe-area-context';
import { Icon } from '@/components/Icon';
import { OnboardingHeader } from '@/components/OnboardingHeader';
import { useLang } from '@/i18n/LangProvider';
import { T, TYPE } from '@/theme/tokens';

type Provider = { id: string; label: string; letter: string; tint: string };

const PROVIDERS: Provider[] = [
  { id: 'clover',      label: 'Clover',      letter: 'C', tint: T.green },
  { id: 'square',      label: 'Square',      letter: 'S', tint: '#1C1C1E' },
  { id: 'lightspeed',  label: 'Lightspeed',  letter: 'L', tint: T.red },
  { id: 'touchbistro', label: 'TouchBistro', letter: 'T', tint: T.blue },
  { id: 'maitred',     label: "Maitre'D",    letter: 'M', tint: T.purple },
  { id: 'toast',       label: 'Toast',       letter: 'T', tint: T.amber },
  { id: 'veloce',      label: 'Veloce',      letter: 'V', tint: T.teal },
];

export default function PosPicker() {
  const router = useRouter();
  const { t } = useLang();

  const choose = (id: string) =>
    router.push({ pathname: '/onboarding/connecting', params: { provider: id } });

  return (
    <SafeAreaView style={styles.safe} edges={['top', 'bottom']}>
      <OnboardingHeader step={4} totalSteps={14} onBack={() => router.back()} />
      <ScrollView contentContainerStyle={styles.scroll} showsVerticalScrollIndicator={false}>
        <Text style={styles.h2}>{t.posTitle}</Text>
        <Text style={styles.sub}>{t.posSub}</Text>

        <View style={styles.list}>
          {PROVIDERS.map(p => (
            <Pressable
              key={p.id}
              onPress={() => choose(p.id)}
              style={({ pressed }) => [styles.row, pressed && { backgroundColor: T.elev2 }]}
            >
              <View style={[styles.badge, { backgroundColor: p.tint }]}>
                <Text style={styles.badgeLetter}>{p.letter}</Text>
              </View>
              <View style={{ flex: 1 }}>
                <Text style={styles.providerLabel}>{p.label}</Text>
                <Text style={styles.providerSub}>About 1–2 minutes.</Text>
              </View>
              <Icon name="chevron-right" size={18} color={T.ter} />
            </Pressable>
          ))}

          <Pressable
            onPress={() => router.push('/onboarding/manual-menu')}
            style={({ pressed }) => [
              styles.noPosRow,
              pressed && { backgroundColor: T.elev2 },
            ]}
          >
            <Text style={styles.noPosLabel}>My POS isn't here</Text>
          </Pressable>
        </View>
      </ScrollView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe:   { flex: 1, backgroundColor: T.bg },
  scroll: { paddingHorizontal: T.pad, paddingTop: 16, paddingBottom: 32 },
  h2:     { ...TYPE.title1, color: T.text, marginBottom: 6 },
  sub:    { ...TYPE.body, color: T.sec, marginBottom: 20 },
  list:   { gap: 10 },
  row: {
    flexDirection: 'row',
    alignItems: 'center',
    backgroundColor: T.elev1,
    borderRadius: T.radius,
    paddingHorizontal: 16,
    paddingVertical: 14,
    gap: 12,
    borderWidth: 1,
    borderColor: T.hairline,
  },
  badge: {
    width: 44, height: 44, borderRadius: 10,
    alignItems: 'center', justifyContent: 'center',
  },
  badgeLetter: {
    fontSize: 20, fontWeight: '700', color: '#FFFFFF',
  },
  providerLabel: { ...TYPE.headline, color: T.text },
  providerSub:   { ...TYPE.caption1, color: T.sec, marginTop: 2 },
  noPosRow: {
    alignItems: 'center',
    justifyContent: 'center',
    borderRadius: T.radius,
    paddingVertical: 16,
    borderWidth: 1,
    borderColor: T.sep,
    borderStyle: 'dashed',
  },
  noPosLabel: { ...TYPE.subhead, color: T.sec },
});
