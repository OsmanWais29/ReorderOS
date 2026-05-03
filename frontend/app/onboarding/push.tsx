// Push notifications opt-in. Real prompt via expo-notifications when granted.

import React from 'react';
import { View, Text, StyleSheet } from 'react-native';
import { useRouter } from 'expo-router';
import { SafeAreaView } from 'react-native-safe-area-context';
import * as Notifications from 'expo-notifications';
import { Button } from '@/components/atoms';
import { Icon } from '@/components/Icon';
import { OnboardingHeader } from '@/components/OnboardingHeader';
import { useLang } from '@/i18n/LangProvider';
import { T, TYPE } from '@/theme/tokens';

export default function Push() {
  const router = useRouter();
  const { t } = useLang();

  const ask = async () => {
    try {
      await Notifications.requestPermissionsAsync();
    } catch {}
    router.push('/onboarding/pos-picker');
  };

  return (
    <SafeAreaView style={styles.safe} edges={['top', 'bottom']}>
      <OnboardingHeader step={3} totalSteps={14} onBack={() => router.back()} />
      <View style={styles.body}>
        <View style={styles.iconHero}>
          <Icon name="bell" size={48} color={T.ac} />
        </View>
        <Text style={styles.h2}>{t.pushTitle}</Text>
        <Text style={styles.sub}>{t.pushSub}</Text>
      </View>
      <View style={styles.cta}>
        <Button label={t.pushAllow} fullWidth iconRight="arrow-right" onPress={ask} />
        <Button label={t.pushLater} variant="ghost" fullWidth onPress={() => router.push('/onboarding/pos-picker')} />
      </View>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: T.bg },
  body: { flex: 1, paddingHorizontal: T.pad, alignItems: 'center', justifyContent: 'center' },
  iconHero: {
    width: 96, height: 96, borderRadius: 28,
    backgroundColor: T.acSoft, alignItems: 'center', justifyContent: 'center',
    marginBottom: 32, borderWidth: 1, borderColor: T.acBorder,
  },
  h2: { ...TYPE.title1, color: T.text, textAlign: 'center', marginBottom: 12 },
  sub: { ...TYPE.body, color: T.sec, textAlign: 'center', maxWidth: 320 },
  cta: { paddingHorizontal: T.pad, paddingVertical: 8, gap: 8, borderTopWidth: 1, borderTopColor: T.hairline },
});
