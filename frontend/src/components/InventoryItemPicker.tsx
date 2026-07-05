// InventoryItemPicker — modal for linking a receipt line to an inventory item
// (spec §7). Search the tenant's existing items, or create a new one (name +
// canonical unit). SUGGESTION-driven linking happens inline on the line row;
// this picker is the full search / create-new path. The choice is reported to
// the parent — the parent owns the PUT (D-606-26: item set only by explicit
// operator action).

import React, { useEffect, useMemo, useState } from 'react';
import {
  View,
  Text,
  Modal,
  TextInput,
  Pressable,
  FlatList,
  StyleSheet,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { Button } from '@/components/atoms';
import { useAuth } from '@/auth/AuthContext';
import { useLang } from '@/i18n/LangProvider';
import { T, TYPE } from '@/theme/tokens';
import { getItems, type StockItem } from '@/api/items';
import { CANONICAL_UNITS_BY_DIMENSION, type Dimension } from '@/api/units';

export type ItemChoice =
  | { kind: 'existing'; id: string; name: string }
  | { kind: 'new'; name: string; unit: string };

export function InventoryItemPicker({
  visible,
  initialQuery,
  onPick,
  onClose,
}: {
  visible: boolean;
  initialQuery: string;
  onPick: (choice: ItemChoice) => void;
  onClose: () => void;
}) {
  const { token } = useAuth();
  const { t } = useLang();
  const [items, setItems] = useState<StockItem[]>([]);
  const [query, setQuery] = useState(initialQuery);
  const [newUnit, setNewUnit] = useState<string | null>(null);

  useEffect(() => {
    if (!visible) return;
    setQuery(initialQuery);
    setNewUnit(null);
    if (token) {
      getItems(token)
        .then((r) => setItems(r.items))
        .catch(() => setItems([]));
    }
  }, [visible, initialQuery, token]);

  const matches = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return items;
    return items.filter((it) => it.name.toLowerCase().includes(q));
  }, [items, query]);

  const canCreate = query.trim().length > 0 && newUnit !== null;

  return (
    <Modal visible={visible} animationType="slide" onRequestClose={onClose}>
      <SafeAreaView style={styles.safe} edges={['top', 'bottom']}>
        <View style={styles.head}>
          <Text style={styles.h1}>{t.rcptPickItem}</Text>
          <Pressable onPress={onClose} hitSlop={8}>
            <Text style={styles.close}>{t.rcptPickClose}</Text>
          </Pressable>
        </View>
        <TextInput
          style={styles.search}
          value={query}
          onChangeText={setQuery}
          placeholder={t.rcptPickSearch}
          placeholderTextColor={T.ter}
          autoFocus
        />
        <FlatList
          data={matches}
          keyExtractor={(it) => it.id}
          contentContainerStyle={styles.list}
          keyboardShouldPersistTaps="handled"
          renderItem={({ item }) => (
            <Pressable
              style={({ pressed }) => [styles.row, pressed && styles.rowPressed]}
              onPress={() => onPick({ kind: 'existing', id: item.id, name: item.name })}
            >
              <Text style={styles.rowText}>{item.name}</Text>
            </Pressable>
          )}
          ListEmptyComponent={<Text style={styles.empty}>{t.rcptPickNoMatch}</Text>}
        />
        {/* create-new: name = current query, unit = canonical picker */}
        <View style={styles.createBox}>
          <Text style={styles.createLabel}>{t.rcptPickCreate}</Text>
          <View style={styles.units}>
            {(Object.keys(CANONICAL_UNITS_BY_DIMENSION) as Dimension[]).flatMap((dim) =>
              CANONICAL_UNITS_BY_DIMENSION[dim].map((u) => (
                <Pressable
                  key={u}
                  onPress={() => setNewUnit(u)}
                  style={[styles.unitChip, newUnit === u && styles.unitChipOn]}
                >
                  <Text style={[styles.unitLabel, newUnit === u && styles.unitLabelOn]}>{u}</Text>
                </Pressable>
              )),
            )}
          </View>
          <Button
            label={`${t.rcptPickCreateCta} “${query.trim()}”`}
            size="md"
            disabled={!canCreate}
            onPress={() =>
              newUnit && onPick({ kind: 'new', name: query.trim(), unit: newUnit })
            }
          />
        </View>
      </SafeAreaView>
    </Modal>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: T.bg },
  head: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    padding: T.pad,
  },
  h1: { ...TYPE.title2, color: T.text },
  close: { ...TYPE.body, color: T.ac },
  search: {
    ...TYPE.body,
    color: T.text,
    backgroundColor: T.elev1,
    borderRadius: 12,
    paddingHorizontal: 14,
    paddingVertical: 10,
    marginHorizontal: T.pad,
  },
  list: { padding: T.pad, gap: 6 },
  row: { backgroundColor: T.elev1, borderRadius: 10, padding: 14 },
  rowPressed: { backgroundColor: T.elev2 },
  rowText: { ...TYPE.body, color: T.text },
  empty: { ...TYPE.body, color: T.sec, textAlign: 'center', paddingVertical: 16 },
  createBox: {
    padding: T.pad,
    gap: 10,
    borderTopWidth: StyleSheet.hairlineWidth,
    borderTopColor: T.sep,
  },
  createLabel: { ...TYPE.subhead, color: T.sec },
  units: { flexDirection: 'row', flexWrap: 'wrap', gap: 6 },
  unitChip: {
    paddingHorizontal: 10,
    paddingVertical: 6,
    borderRadius: 12,
    backgroundColor: T.elev1,
  },
  unitChipOn: { backgroundColor: T.acSoft },
  unitLabel: { ...TYPE.footnote, color: T.sec },
  unitLabelOn: { color: T.ac },
});
