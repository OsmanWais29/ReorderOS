// MenuItemAccordion — a collapsible menu-item row (Appendix F.2 / §1): name, sales-volume
// context, ingredient count, status badge, expand/collapse. Presentational; the screen owns
// the expanded editing content (passed as children).

import React, { type ReactNode } from 'react';
import { View, Text, Pressable, StyleSheet } from 'react-native';
import { T, TYPE } from '../theme/tokens';
import { RecipeStatusBadge } from './RecipeBits';
import type { RecipeListItem } from '../api/recipes';

type Props = {
  item: RecipeListItem;
  statusLabel: string;
  expanded: boolean;
  onToggle: () => void;
  /** "{n} ingredients · {m} modifiers" — composed by the screen for i18n. */
  metaLabel: string;
  children?: ReactNode;
};

export function MenuItemAccordion({
  item,
  statusLabel,
  expanded,
  onToggle,
  metaLabel,
  children,
}: Props) {
  return (
    <View style={styles.card}>
      <Pressable
        onPress={onToggle}
        style={styles.head}
        accessibilityRole="button"
        accessibilityState={{ expanded }}
      >
        <View style={styles.headText}>
          <Text style={styles.name} numberOfLines={1}>
            {item.name}
          </Text>
          <Text style={styles.meta}>{metaLabel}</Text>
        </View>
        <RecipeStatusBadge status={item.status} label={statusLabel} />
        <Text style={styles.chevron}>{expanded ? '⌄' : '›'}</Text>
      </Pressable>
      {expanded ? <View style={styles.bodyContent}>{children}</View> : null}
    </View>
  );
}

const styles = StyleSheet.create({
  card: {
    backgroundColor: T.elev1,
    borderRadius: 14,
    borderWidth: 1,
    borderColor: T.hairline,
    overflow: 'hidden',
  },
  head: { flexDirection: 'row', alignItems: 'center', gap: 10, padding: 14 },
  headText: { flex: 1, gap: 2 },
  name: { ...TYPE.headline, color: T.text },
  meta: { ...TYPE.subhead, color: T.sec },
  chevron: { color: T.ter, fontSize: 20, width: 16, textAlign: 'center' },
  bodyContent: {
    paddingHorizontal: 14,
    paddingBottom: 14,
    borderTopWidth: 1,
    borderTopColor: T.hairline,
  },
});
