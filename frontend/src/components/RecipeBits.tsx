// Small presentational atoms for the Recipes surface (Appendix F.2):
// RecipeStatusBadge, ConfidenceBadge, ProgressCounter, RecipeSkeleton.

import React from 'react';
import { View, Text, StyleSheet, ActivityIndicator } from 'react-native';
import { T, TYPE } from '../theme/tokens';
import type { RecipeStatus, ModifierStatus, Confidence } from '../api/recipes';

// ── RecipeStatusBadge — draft (amber) / confirmed (green) / skipped (grey) ──────────────
export function RecipeStatusBadge({
  status,
  label,
}: {
  status: RecipeStatus | ModifierStatus;
  label: string;
}) {
  const tone =
    status === 'confirmed'
      ? { bg: T.greenSoft, fg: T.green }
      : status === 'draft'
        ? { bg: T.amberSoft, fg: T.amber }
        : { bg: T.elev2, fg: T.sec }; // skipped / none
  return (
    <View style={[styles.badge, { backgroundColor: tone.bg }]}>
      <Text style={[styles.badgeText, { color: tone.fg }]}>{label}</Text>
    </View>
  );
}

// ── ConfidenceBadge — confident / likely / uncertain (§2 LLM output) ─────────────────────
export function ConfidenceBadge({ confidence, label }: { confidence: Confidence; label: string }) {
  const fg =
    confidence === 'confident' ? T.green : confidence === 'likely' ? T.amber : T.sec;
  return (
    <View style={[styles.badge, { backgroundColor: T.elev2 }]}>
      <Text style={[styles.badgeText, { color: fg }]}>{label}</Text>
    </View>
  );
}

// ── ProgressCounter — confirmed / (total − skipped) (§1) ────────────────────────────────
export function ProgressCounter({
  confirmed,
  denominator,
  label,
}: {
  confirmed: number;
  denominator: number;
  label: string;
}) {
  const pct = denominator > 0 ? Math.round((confirmed / denominator) * 100) : 0;
  return (
    <View style={styles.progress}>
      <View style={styles.progressTop}>
        <Text style={styles.progressLabel}>{label}</Text>
        <Text style={styles.progressCount}>
          {confirmed}/{denominator}
        </Text>
      </View>
      <View style={styles.track}>
        <View style={[styles.fill, { width: `${pct}%` }]} />
      </View>
    </View>
  );
}

// ── RecipeSkeleton — loading placeholder ────────────────────────────────────────────────
export function RecipeSkeleton() {
  return (
    <View style={styles.skeleton}>
      <ActivityIndicator color={T.sec} />
    </View>
  );
}

const styles = StyleSheet.create({
  badge: { paddingVertical: 3, paddingHorizontal: 8, borderRadius: 6, alignSelf: 'flex-start' },
  badgeText: { ...TYPE.caption1, fontWeight: '600' },
  progress: { gap: 6, paddingVertical: 8 },
  progressTop: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'baseline' },
  progressLabel: { ...TYPE.subhead, color: T.sec },
  progressCount: { ...TYPE.subhead, color: T.text, fontWeight: '600' },
  track: { height: 6, borderRadius: 999, backgroundColor: T.elev2, overflow: 'hidden' },
  fill: { height: 6, borderRadius: 999, backgroundColor: T.ac },
  skeleton: { paddingVertical: 48, alignItems: 'center' },
});
