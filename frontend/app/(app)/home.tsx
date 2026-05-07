import React from 'react';
import { View, Text, ScrollView, StyleSheet } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { useRouter } from 'expo-router';
import { Card, Pill, Button } from '@/components/atoms';
import { Icon } from '@/components/Icon';
import { useAuth } from '@/auth/AuthContext';
import { T, TYPE } from '@/theme/tokens';

export default function Home() {
  const { me, signOut } = useAuth();
  const router = useRouter();

  const firstName = me?.user.first_name ?? me?.user.email ?? 'there';
  const tenantName = me?.tenants[0]?.name ?? null;

  const handleSignOut = async () => {
    await signOut();
    router.replace('/onboarding/welcome');
  };

  return (
    <SafeAreaView style={styles.safe} edges={['top']}>
      <ScrollView contentContainerStyle={styles.scroll}>

        <View style={styles.topRow}>
          <View>
            <Text style={styles.greeting}>Good morning</Text>
            <Text style={styles.h1}>{firstName}</Text>
            {tenantName && <Text style={styles.tenant}>{tenantName}</Text>}
          </View>
          <Button label="Sign out" variant="ghost" onPress={handleSignOut} />
        </View>

        {me && (
          <Card style={{ marginTop: 4, marginBottom: 16 }}>
            <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8, marginBottom: 6 }}>
              <Icon name="shield" size={16} color={T.green} />
              <Pill label="Authenticated" tone="accent" />
            </View>
            <Text style={styles.authLine}>Signed in as <Text style={styles.bold}>{me.user.email}</Text></Text>
            <Text style={styles.authLine}>WorkOS ID: <Text style={styles.mono}>{me.user.workos_id}</Text></Text>
            {me.tenants.length > 0 && (
              <Text style={styles.authLine}>
                Tenants: {me.tenants.map(t => t.slug).join(', ')}
              </Text>
            )}
            {me.tenants.length === 0 && (
              <Text style={[styles.authLine, { color: T.amber }]}>
                No tenant yet — call register-tenant to set one up.
              </Text>
            )}
          </Card>
        )}

        <Card style={{ marginTop: 0 }}>
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
  topRow: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 8 },
  greeting: { ...TYPE.subhead, color: T.sec },
  h1: { ...TYPE.largeTitle, color: T.text },
  tenant: { ...TYPE.footnote, color: T.label, marginTop: 2 },
  authLine: { ...TYPE.footnote, color: T.sec, marginTop: 2 },
  bold: { color: T.text, fontWeight: '600' },
  mono: { fontFamily: 'Menlo', color: T.label },
  section: { ...TYPE.headline, color: T.text, marginTop: 24, marginBottom: 12 },
  cardTitle: { ...TYPE.headline, color: T.text },
  cardBody: { ...TYPE.subhead, color: T.sec, marginTop: 6 },
  statsRow: { flexDirection: 'row', gap: 8 },
  statLabel: { ...TYPE.caption1, color: T.sec, textTransform: 'uppercase', letterSpacing: 0.5 },
  statValue: { ...TYPE.title2, color: T.text, marginTop: 4 },
  statDelta: { ...TYPE.caption1, color: T.sec, marginTop: 2 },
  amber: T.amber,
});
