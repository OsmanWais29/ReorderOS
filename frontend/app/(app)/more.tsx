import React from 'react';
import { View, Text, StyleSheet, Pressable } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { useLang } from '@/i18n/LangProvider';
import { T, TYPE } from '@/theme/tokens';

export default function More() {
  const { lang, toggle } = useLang();
  return (
    <SafeAreaView style={styles.safe} edges={['top']}>
      <View style={styles.body}>
        <Text style={styles.h1}>More</Text>
        <Pressable onPress={toggle} style={styles.row}>
          <Text style={styles.rowLabel}>Language</Text>
          <Text style={styles.rowValue}>{lang === 'en' ? 'English' : 'Français'}</Text>
        </Pressable>
        <View style={styles.row}><Text style={styles.rowLabel}>Team</Text><Text style={styles.rowValue}>—</Text></View>
        <View style={styles.row}><Text style={styles.rowLabel}>Suppliers</Text><Text style={styles.rowValue}>—</Text></View>
        <View style={styles.row}><Text style={styles.rowLabel}>Billing</Text><Text style={styles.rowValue}>Trial</Text></View>
        <View style={styles.row}><Text style={styles.rowLabel}>Sign out</Text><Text style={styles.rowValue}>—</Text></View>
      </View>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: T.bg },
  body: { padding: T.pad, gap: 8 },
  h1: { ...TYPE.largeTitle, color: T.text, marginBottom: 12 },
  row: {
    flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center',
    backgroundColor: T.elev1, borderRadius: T.radius, paddingHorizontal: 16, paddingVertical: 14,
    borderWidth: 1, borderColor: T.hairline,
  },
  rowLabel: { ...TYPE.body, color: T.text },
  rowValue: { ...TYPE.subhead, color: T.sec },
});
