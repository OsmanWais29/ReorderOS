import React from 'react';
import { View, Text, StyleSheet } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { Pill } from '@/components/atoms';
import { T, TYPE } from '@/theme/tokens';

export function TabPlaceholder({ title, description }: { title: string; description: string }) {
  return (
    <SafeAreaView style={styles.safe} edges={['top']}>
      <View style={styles.body}>
        <Pill label="STUB" tone="amber" />
        <Text style={styles.h1}>{title}</Text>
        <Text style={styles.body_}>{description}</Text>
      </View>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: T.bg },
  body: { padding: T.pad, gap: 12 },
  h1: { ...TYPE.largeTitle, color: T.text },
  body_: { ...TYPE.body, color: T.sec },
});
