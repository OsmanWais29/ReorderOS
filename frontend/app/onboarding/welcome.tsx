// Welcome screen — first screen of onboarding.
// Mirrors the web prototype's Welcome step: logo, dual-line headline, three
// feature rows, primary + secondary CTAs, EN/FR toggle in the top-right.

import React from 'react';
import { View, Text, ScrollView, StyleSheet, Pressable } from 'react-native';
import { useRouter } from 'expo-router';
import { SafeAreaView } from 'react-native-safe-area-context';
import { Button, Card } from '@/components/atoms';
import { Icon, type IconName } from '@/components/Icon';
import { useLang } from '@/i18n/LangProvider';
import { T, TYPE } from '@/theme/tokens';

export default function Welcome() {
  const router = useRouter();
  const { t, toggle } = useLang();

  return (
    <SafeAreaView style={styles.safe} edges={['top', 'bottom']}>
      {/* Top bar — logo + EN/FR */}
      <View style={styles.topBar}>
        <View style={styles.brandRow}>
          <Icon name="logo" size={28} color={T.ac} />
          <Text style={styles.brand}>ReOrderOS</Text>
          <View style={styles.maple}>
            <Icon name="maple-leaf" size={12} color={T.red} fill={T.red} />
          </View>
        </View>
        <Pressable onPress={toggle} style={styles.langPill}>
          <Text style={styles.langPillLabel}>{t.switchLang}</Text>
        </Pressable>
      </View>

      <ScrollView
        contentContainerStyle={styles.scroll}
        showsVerticalScrollIndicator={false}
      >
        {/* Headline */}
        <Text style={styles.h1}>{t.headline1}</Text>
        <Text style={[styles.h1, { color: T.ac }]}>{t.headline2}</Text>
        <Text style={styles.sub}>{t.sub}</Text>

        {/* Feature rows */}
        <View style={styles.features}>
          <FeatureRow icon="trend-up" tone={T.ac} title={t.f1t} body={t.f1b} />
          <FeatureRow icon="send"     tone={T.blue} title={t.f2t} body={t.f2b} />
          <FeatureRow icon="shield"   tone={T.red}  title={t.f3t} body={t.f3b} />
        </View>

        <Text style={styles.timecost}>{t.timecost}</Text>
      </ScrollView>

      {/* Sticky CTA stack */}
      <View style={styles.cta}>
        <Button
          label={t.getStarted}
          fullWidth
          iconRight="arrow-right"
          onPress={() => router.push('/onboarding/account')}
        />
        <Button
          label={t.haveAccount}
          variant="ghost"
          fullWidth
          onPress={() => router.push('/onboarding/sign-in')}
        />
      </View>
    </SafeAreaView>
  );
}

function FeatureRow({ icon, tone, title, body }: { icon: IconName; tone: string; title: string; body: string }) {
  return (
    <Card style={{ flexDirection: 'row', gap: 14, alignItems: 'flex-start' }}>
      <View style={[styles.featureIcon, { backgroundColor: tone + '24' }]}>
        <Icon name={icon} size={18} color={tone} />
      </View>
      <View style={{ flex: 1 }}>
        <Text style={styles.featureTitle}>{title}</Text>
        <Text style={styles.featureBody}>{body}</Text>
      </View>
    </Card>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: T.bg },
  topBar: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingHorizontal: T.pad,
    paddingTop: 8,
    paddingBottom: 8,
  },
  brandRow: { flexDirection: 'row', alignItems: 'center', gap: 8 },
  brand: { ...TYPE.headline, color: T.text, letterSpacing: -0.3 },
  maple: { paddingHorizontal: 4 },
  langPill: {
    paddingHorizontal: 12,
    paddingVertical: 6,
    borderRadius: 999,
    backgroundColor: T.elev1,
    borderWidth: 1,
    borderColor: T.hairline,
  },
  langPillLabel: { ...TYPE.footnote, color: T.label, fontWeight: '600' },

  scroll: {
    paddingHorizontal: T.pad,
    paddingTop: 24,
    paddingBottom: 24,
  },
  h1: { ...TYPE.largeTitle, color: T.text },
  sub: {
    ...TYPE.body,
    color: T.sec,
    marginTop: 14,
    marginBottom: 28,
  },

  features: { gap: 10 },
  featureIcon: {
    width: 36,
    height: 36,
    borderRadius: 10,
    alignItems: 'center',
    justifyContent: 'center',
  },
  featureTitle: { ...TYPE.headline, color: T.text },
  featureBody:  { ...TYPE.subhead, color: T.sec, marginTop: 2 },

  timecost: {
    ...TYPE.footnote,
    color: T.ter,
    textAlign: 'center',
    marginTop: 24,
  },

  cta: {
    paddingHorizontal: T.pad,
    paddingTop: 8,
    paddingBottom: 8,
    gap: 8,
    borderTopWidth: 1,
    borderTopColor: T.hairline,
  },
});
