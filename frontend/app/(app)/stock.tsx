// Stock tab (Sprint 6 FE) — makes the depletion engine's output visible.
// Reads GET /inventory/items (computed on_hand + par_level + stock_status), offers a
// low-stock filter, and a one-shot "set starting stock" form (POST opening-balance —
// server-idempotent per item, so it can never double-add) for items with nothing on hand.
// Bilingual via useLang.

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  View,
  Text,
  FlatList,
  Pressable,
  RefreshControl,
  StyleSheet,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { Button, Field, Pill } from '@/components/atoms';
import { useAuth } from '@/auth/AuthContext';
import { useLang } from '@/i18n/LangProvider';
import { T, TYPE } from '@/theme/tokens';
import {
  getItems,
  postOpeningBalance,
  ItemsApiError,
  type StockItem,
  type StockStatus,
} from '@/api/items';

type Filter = 'all' | 'low';

const STATUS_TONE: Record<StockStatus, 'green' | 'amber' | 'red' | 'neutral'> = {
  ok: 'green',
  low_stock: 'amber',
  out_of_stock: 'red',
  count_required: 'amber',
  count_stale: 'neutral',
  count_expired: 'amber',
};

function fmtQty(n: number): string {
  // Trim float noise without padding: 12 → "12", 2.5 → "2.5", 0.333333 → "0.33".
  return String(Math.round(n * 100) / 100);
}

export default function Stock() {
  const { token } = useAuth();
  const { t } = useLang();

  const [items, setItems] = useState<StockItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>('all');
  const [refreshing, setRefreshing] = useState(false);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [qty, setQty] = useState('');
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!token) return;
    try {
      const list = await getItems(token);
      setItems(list.items);
      setError(null);
    } catch (e: unknown) {
      setError(e instanceof ItemsApiError ? e.detail : t.stockError);
    }
  }, [token, t.stockError]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const onPullRefresh = useCallback(async () => {
    setRefreshing(true);
    await refresh();
    setRefreshing(false);
  }, [refresh]);

  const statusLabel = useMemo(
    () =>
      ({
        ok: t.stockStatusOk,
        low_stock: t.stockStatusLow,
        out_of_stock: t.stockStatusOut,
        count_required: t.stockStatusCountRequired,
        count_stale: t.stockStatusCountStale,
        count_expired: t.stockStatusCountExpired,
      }) satisfies Record<StockStatus, string>,
    [t],
  );

  const visible = useMemo(() => {
    const all = items ?? [];
    if (filter === 'all') return all;
    return all.filter((it) => it.low_stock || it.out_of_stock);
  }, [items, filter]);

  const toggleItem = (id: string) => {
    setExpandedId((cur) => (cur === id ? null : id));
    setQty('');
    setSaveError(null);
  };

  const saveOpeningBalance = useCallback(
    async (itemId: string) => {
      if (!token) return;
      const n = Number(qty.replace(',', '.')); // FR keyboards produce a comma decimal
      if (!Number.isFinite(n) || n < 0) return;
      setSaving(true);
      setSaveError(null);
      try {
        await postOpeningBalance(token, itemId, n);
        await refresh();
        setExpandedId(null);
        setQty('');
      } catch (e: unknown) {
        setSaveError(e instanceof ItemsApiError ? e.detail : t.stockSaveError);
      } finally {
        setSaving(false);
      }
    },
    [token, qty, refresh, t.stockSaveError],
  );

  const qtyValid = useMemo(() => {
    const n = Number(qty.replace(',', '.'));
    return qty.trim() !== '' && Number.isFinite(n) && n >= 0;
  }, [qty]);

  const renderItem = ({ item }: { item: StockItem }) => {
    // The one-shot starting-stock form: only for items with nothing recorded yet
    // (on_hand null = Mode B unanchored, 0 = Mode A with no movements). The endpoint
    // is idempotent per item, so a stale UI can't double-add.
    const canSetStock = item.on_hand === null || item.on_hand === 0;
    const expanded = expandedId === item.id;
    return (
      <View style={styles.card}>
        <Pressable
          onPress={() => canSetStock && toggleItem(item.id)}
          style={({ pressed }) => [styles.row, pressed && canSetStock && styles.rowPressed]}
        >
          <View style={styles.rowText}>
            <Text style={styles.name} numberOfLines={1}>
              {item.name}
            </Text>
            <Text style={styles.meta}>
              {t.stockOnHand}: {item.on_hand === null ? '—' : fmtQty(item.on_hand)}
              {item.par_level !== null ? `   ${t.stockPar}: ${fmtQty(item.par_level)}` : ''}
            </Text>
          </View>
          <Pill label={statusLabel[item.stock_status]} tone={STATUS_TONE[item.stock_status]} />
        </Pressable>

        {expanded && canSetStock ? (
          <View style={styles.form}>
            <Field
              label={t.stockQtyLabel}
              hint={t.stockQtyHint}
              error={saveError ?? undefined}
              value={qty}
              onChangeText={setQty}
              keyboardType="decimal-pad"
              autoFocus
            />
            <Button
              label={t.stockSave}
              size="md"
              loading={saving}
              disabled={!qtyValid}
              onPress={() => void saveOpeningBalance(item.id)}
            />
          </View>
        ) : null}
      </View>
    );
  };

  return (
    <SafeAreaView style={styles.safe} edges={['top']}>
      <View style={styles.head}>
        <Text style={styles.h1}>{t.stockTitle}</Text>
        <Text style={styles.sub}>{t.stockSub}</Text>
        <View style={styles.filters}>
          {(['all', 'low'] as const).map((f) => (
            <Pressable
              key={f}
              onPress={() => setFilter(f)}
              style={[styles.filterChip, filter === f && styles.filterChipOn]}
            >
              <Text style={[styles.filterLabel, filter === f && styles.filterLabelOn]}>
                {f === 'all' ? t.stockFilterAll : t.stockFilterLow}
              </Text>
            </Pressable>
          ))}
        </View>
      </View>

      {error ? (
        <View style={styles.center}>
          <Text style={styles.err}>{error}</Text>
          <Button label={t.stockRetry} variant="secondary" onPress={() => void refresh()} />
        </View>
      ) : items !== null && visible.length === 0 ? (
        <View style={styles.center}>
          <Text style={styles.sub}>{filter === 'low' ? t.stockLowEmpty : t.stockEmpty}</Text>
        </View>
      ) : (
        <FlatList
          data={visible}
          keyExtractor={(it) => it.id}
          renderItem={renderItem}
          contentContainerStyle={styles.list}
          refreshControl={
            <RefreshControl refreshing={refreshing} onRefresh={() => void onPullRefresh()} />
          }
        />
      )}
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: T.bg },
  head: { padding: T.pad, gap: 6 },
  h1: { ...TYPE.largeTitle, color: T.text },
  sub: { ...TYPE.body, color: T.sec },
  filters: { flexDirection: 'row', gap: 8, marginTop: 10 },
  filterChip: {
    paddingHorizontal: 14,
    paddingVertical: 7,
    borderRadius: 16,
    backgroundColor: T.elev1,
  },
  filterChipOn: { backgroundColor: T.acSoft },
  filterLabel: { ...TYPE.subhead, color: T.sec },
  filterLabelOn: { color: T.ac },
  list: { paddingHorizontal: T.pad, paddingBottom: 32, gap: 10 },
  card: { backgroundColor: T.elev1, borderRadius: 14, overflow: 'hidden' },
  row: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    padding: 14,
    gap: 10,
  },
  rowPressed: { backgroundColor: T.elev2 },
  rowText: { flex: 1, gap: 3 },
  name: { ...TYPE.headline, color: T.text },
  meta: { ...TYPE.footnote, color: T.sec },
  form: {
    padding: 14,
    paddingTop: 4,
    gap: 12,
    borderTopWidth: StyleSheet.hairlineWidth,
    borderTopColor: T.sep,
  },
  center: { flex: 1, alignItems: 'center', justifyContent: 'center', padding: T.pad, gap: 16 },
  err: { ...TYPE.body, color: T.red, textAlign: 'center' },
});
