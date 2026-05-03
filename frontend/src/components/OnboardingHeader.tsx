// Shared onboarding header — back button + step indicator (e.g. "2/14").

import React from 'react';
import { View, Text, Pressable, StyleSheet } from 'react-native';
import { Icon } from './Icon';
import { T, TYPE } from '../theme/tokens';

type Props = {
  step: number;
  totalSteps: number;
  onBack?: () => void;
  onSkip?: () => void;
  skipLabel?: string;
};

export function OnboardingHeader({ step, totalSteps, onBack, onSkip, skipLabel }: Props) {
  const pct = step / totalSteps;
  return (
    <View style={styles.wrap}>
      <View style={styles.row}>
        {onBack ? (
          <Pressable onPress={onBack} hitSlop={12} style={styles.btn}>
            <Icon name="chevron-left" size={22} color={T.label} />
          </Pressable>
        ) : <View style={styles.btn} />}

        <Text style={styles.step}>{step}/{totalSteps}</Text>

        {onSkip ? (
          <Pressable onPress={onSkip} hitSlop={12} style={[styles.btn, { alignItems: 'flex-end' }]}>
            <Text style={styles.skip}>{skipLabel ?? 'Skip'}</Text>
          </Pressable>
        ) : <View style={styles.btn} />}
      </View>
      <View style={styles.track}>
        <View style={[styles.fill, { width: `${pct * 100}%` }]} />
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  wrap: { paddingHorizontal: T.pad, paddingTop: 4, paddingBottom: 8 },
  row: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', height: 36 },
  btn: { width: 60, height: 36, justifyContent: 'center' },
  step: { ...TYPE.footnote, color: T.sec, fontWeight: '600', textAlign: 'center', flex: 1 },
  skip: { ...TYPE.subhead, color: T.label, fontWeight: '600' },
  track: { height: 3, backgroundColor: T.hairline, borderRadius: 2, overflow: 'hidden', marginTop: 6 },
  fill:  { height: '100%', backgroundColor: T.ac, borderRadius: 2 },
});
