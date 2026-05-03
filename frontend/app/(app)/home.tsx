import React from 'react';
import { View, Text, ScrollView, StyleSheet } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { Card, Pill } from '@/components/atoms';
import { Icon } from '@/components/Icon';
import { T, TYPE } from '@/theme/tokens';

export default function Home() {
  return (
    <SafeAreaView style={styles.safe} edges={['top']}>
      <ScrollView contentContainerStyle={styles.scroll}>
        <Text style={styles.greeting}>Good morning</Text>
        <Text style={styles.h1}>Olivier</Text>

        <Card style={{ marginTop: 16 }}>
          <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8, marginBottom: 8 }}>
            <Icon name="sparkles" size={16} color={T.ac} />
            <Pill label="AI suggestion" tone="accent" />
          </View>
          <Text style={styles.cardTitle}>Order ground beef before Friday</Text>
          <Text style={styles.cardBody}>
            Sales for burgers up 18% this week. Current stock will run out by Saturday lunch
            based on your usage curve.
          </Text>
        </Card>

        <Text style={styles.section}>This week</Text>
        <View style={styles.statsRow}>
          <Stat label="Sales" value="$24.8k" delta="+12%" tone="green" />
          <Stat label="Food cost" value="28.4%" delta="-1.2pt" tone="green" />
          <Stat label="Open POs" value="3" tone="neutral" />
        </View>

        <Text style={styles.note}>
          This is a placeholder. Port the full home screen from{' '}
          <Text style={styles.code}>app/MainApp.jsx</Text>.
        </Text>
      </ScrollView>
    </SafeAreaView>
  );
}

function Stat({ label, value, delta, tone }: { label: string; value: string; delta?: string; tone: 'green' | 'neutral' }) {
  return (
    <Card style={{ flex: 1 }}>
      <Text style={styles.statLabel}>{label}</Text>
      <Text style={styles.statValue}>{value}</Text>
      {delta && <Text style={[styles.statDelta, tone === 'green' && { color: T.green }]}>{delta}</Text>}
    </Card>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: T.bg },
  scroll: { padding: T.pad, paddingBottom: 100 },
  greeting: { ...TYPE.subhead, color: T.sec },
  h1: { ...TYPE.largeTitle, color: T.text },
  section: { ...TYPE.headline, color: T.text, marginTop: 24, marginBottom: 12 },
  cardTitle: { ...TYPE.headline, color: T.text },
  cardBody: { ...TYPE.subhead, color: T.sec, marginTop: 6 },
  statsRow: { flexDirection: 'row', gap: 8 },
  statLabel: { ...TYPE.caption1, color: T.sec, textTransform: 'uppercase', letterSpacing: 0.5 },
  statValue: { ...TYPE.title2, color: T.text, marginTop: 4 },
  statDelta: { ...TYPE.caption1, color: T.sec, marginTop: 2 },
  note: { ...TYPE.footnote, color: T.ter, marginTop: 32, fontStyle: 'italic' },
  code: { fontFamily: 'Menlo', color: T.sec },
});
