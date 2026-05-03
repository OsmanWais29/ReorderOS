// POS picker — pick which POS to connect (Square / Clover / Toast / etc).
// Selection routes to /onboarding/connecting?provider=...

import React from 'react';
import { View, Text, ScrollView, StyleSheet } from 'react-native';
import { useRouter } from 'expo-router';
import { SafeAreaView } from 'react-native-safe-area-context';
import { Row, Pill } from '@/components/atoms';
import { Icon, type IconName } from '@/components/Icon';
import { OnboardingHeader } from '@/components/OnboardingHeader';
import { useLang } from '@/i18n/LangProvider';
import { T, TYPE } from '@/theme/tokens';

type Provider = { id: string; label: string; iconName: IconName; tint: string; subtitle?: string };

const PROVIDERS: Provider[] = [
  { id: 'square',  label: 'Square',  iconName: 'package',  tint: '#000', subtitle: 'POS + payments' },
  { id: 'clover',  label: 'Clover',  iconName: 'flame',    tint: T.green,subtitle: 'POS + payments' },
  { id: 'toast',   label: 'Toast',   iconName: 'flame',    tint: T.amber,subtitle: 'Restaurant POS' },
  { id: 'lightspeed', label: 'Lightspeed', iconName: 'sparkles', tint: T.blue, subtitle: 'iPad POS' },
  { id: 'maitred', label: 'Maître\u2019D', iconName: 'book', tint: T.purple, subtitle: 'Quebec POS' },
  { id: 'veloce',  label: 'Veloce', iconName: 'sparkles', tint: T.teal, subtitle: 'Quebec POS' },
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

        <Pill label={t.posAbout} tone="green" iconLeft="shield" style={{ marginBottom: 16 }} />

        <View style={styles.list}>
          {PROVIDERS.map(p => (
            <Row key={p.id} onPress={() => choose(p.id)}>
              <View style={[styles.providerIcon, { backgroundColor: p.tint + '24' }]}>
                <Icon name={p.iconName} size={20} color={p.tint === '#000' ? T.text : p.tint} />
              </View>
              <View style={{ flex: 1 }}>
                <Text style={styles.providerLabel}>{p.label}</Text>
                {p.subtitle && <Text style={styles.providerSub}>{p.subtitle}</Text>}
              </View>
              <Icon name="chevron-right" size={18} color={T.ter} />
            </Row>
          ))}

          <Row onPress={() => router.push('/onboarding/manual-menu')}>
            <View style={[styles.providerIcon, { backgroundColor: T.elev2 }]}>
              <Icon name="x" size={18} color={T.sec} />
            </View>
            <Text style={[styles.providerLabel, { flex: 1 }]}>{t.posNone}</Text>
            <Icon name="chevron-right" size={18} color={T.ter} />
          </Row>
        </View>
      </ScrollView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe:   { flex: 1, backgroundColor: T.bg },
  scroll: { paddingHorizontal: T.pad, paddingTop: 16, paddingBottom: 32 },
  h2:     { ...TYPE.title1, color: T.text, marginBottom: 6 },
  sub:    { ...TYPE.body, color: T.sec, marginBottom: 16 },
  list:   { gap: 8 },
  providerIcon: {
    width: 44, height: 44, borderRadius: 10,
    alignItems: 'center', justifyContent: 'center',
  },
  providerLabel: { ...TYPE.headline, color: T.text },
  providerSub:   { ...TYPE.caption1, color: T.sec, marginTop: 2 },
});
