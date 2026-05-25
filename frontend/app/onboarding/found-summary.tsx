import React from 'react';
import { View, Text, StyleSheet } from 'react-native';
import { useLocalSearchParams, useRouter } from 'expo-router';
import { SafeAreaView } from 'react-native-safe-area-context';
import { Button, Pill } from '@/components/atoms';
import { OnboardingHeader } from '@/components/OnboardingHeader';
import { T, TYPE } from '@/theme/tokens';

export default function FoundSummary() {
  const { connected } = useLocalSearchParams<{ connected?: string }>();
  const router = useRouter();
  const isConnected = connected === 'true';

  return (
    <SafeAreaView style={styles.safe} edges={['top', 'bottom']}>
      <OnboardingHeader step={6} totalSteps={14} onBack={() => router.back()} />
      <View style={styles.body}>
        {isConnected ? (
          <Pill label="Clover connected" tone="green" iconLeft="shield" />
        ) : (
          <Pill label="STUB" tone="amber" />
        )}
        <Text style={styles.h2}>Here's what we found</Text>
        <Text style={styles.sub}>
          {isConnected
            ? 'Your Clover account is connected. We\'ll pull your menu and recent orders to set up your inventory.'
            : 'Show pulled menu count, sales window, and confirm restaurant identity.'}
        </Text>
      </View>
      <View style={styles.cta}>
        <Button
          label="Continue"
          fullWidth
          iconRight="arrow-right"
          onPress={() => router.push('/onboarding/cleanup')}
        />
      </View>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: T.bg },
  body: { flex: 1, paddingHorizontal: T.pad, paddingTop: 24, gap: 16 },
  h2:   { ...TYPE.title1, color: T.text },
  sub:  { ...TYPE.body, color: T.sec },
  cta:  { paddingHorizontal: T.pad, paddingVertical: 8, borderTopWidth: 1, borderTopColor: T.hairline },
});
