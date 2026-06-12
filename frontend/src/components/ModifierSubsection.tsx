// Modifier configuration sub-section (Appendix F.2/F.3, §3) — shown inside an expanded menu
// item. Lists POS-detected modifiers; ADDITIVE ones are editable/confirmable (reusing
// IngredientRow + the single-source validation); subtractive/substitution are shown disabled
// as "Not yet supported" (only skip). Inherits slice-2 patterns: per-blur saves into
// modifier_drafts, getModifier re-hydration, per-call 409 (non-additive gets its own message).

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { View, Text, Pressable, StyleSheet } from 'react-native';
import { Button } from './atoms';
import { RecipeStatusBadge } from './RecipeBits';
import {
  IngredientRow,
  validateIngredient,
  validateDraft,
  type IngredientDraft,
} from './IngredientRow';
import { useAuth } from '../auth/AuthContext';
import { useLang } from '../i18n/LangProvider';
import { T, TYPE } from '../theme/tokens';
import {
  getModifiers,
  getModifier,
  patchModifier,
  confirmModifier,
  unconfirmModifier,
  skipModifier,
  RecipeApiError,
  type ModifierListItem,
  type ModifierStatus,
  type IngredientIn,
} from '../api/recipes';

const EMPTY_ROW: IngredientDraft = { name: '', quantity: '', unit: '' };

function toIngredientIn(rows: IngredientDraft[]): IngredientIn[] {
  return rows.map((r) => ({ name: r.name.trim(), quantity: Number(r.quantity), unit: r.unit }));
}

export function ModifierSubsection({ menuItemId }: { menuItemId: string }) {
  const { token } = useAuth();
  const { t } = useLang();
  const [mods, setMods] = useState<ModifierListItem[] | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, IngredientDraft[]>>({});
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!token) return;
    try {
      setMods(await getModifiers(token, menuItemId));
      setError(null);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : t.recipesError);
    }
  }, [token, menuItemId, t.recipesError]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const open = useCallback(
    async (modId: string) => {
      if (expandedId === modId) {
        setExpandedId(null);
        return;
      }
      setExpandedId(modId);
      if (!token || drafts[modId]) return;
      try {
        const detail = await getModifier(token, menuItemId, modId); // re-hydrate (the new GET)
        const rows: IngredientDraft[] = detail.ingredients.map((i) => ({
          name: String(i.name ?? ''),
          quantity: i.quantity != null ? String(i.quantity) : '',
          unit: String(i.unit ?? ''),
        }));
        setDrafts((d) => ({ ...d, [modId]: rows.length ? rows : [{ ...EMPTY_ROW }] }));
      } catch {
        setDrafts((d) => ({ ...d, [modId]: [{ ...EMPTY_ROW }] }));
      }
    },
    [expandedId, token, menuItemId, drafts],
  );

  const setRows = (id: string, rows: IngredientDraft[]) =>
    setDrafts((d) => ({ ...d, [id]: rows }));

  const saveDraft = useCallback(
    async (id: string) => {
      if (!token) return;
      const rows = drafts[id] ?? [];
      try {
        await patchModifier(token, menuItemId, id, toIngredientIn(rows.filter((r) => r.name.trim())));
      } catch (e: unknown) {
        if (e instanceof RecipeApiError && e.status === 409) setError(t.recipes409);
        // non-409 failures are non-blocking; next blur retries.
      }
    },
    [token, menuItemId, drafts, t.recipes409],
  );

  const timers = useRef<Record<string, ReturnType<typeof setTimeout>>>({});
  const scheduleSave = useCallback(
    (id: string) => {
      const ts = timers.current;
      if (ts[id]) clearTimeout(ts[id]);
      ts[id] = setTimeout(() => void saveDraft(id), 600);
    },
    [saveDraft],
  );
  useEffect(() => {
    const ts = timers.current;
    return () => Object.values(ts).forEach(clearTimeout);
  }, []);

  const confirm = useCallback(
    async (m: ModifierListItem) => {
      if (!token) return;
      setBusyId(m.modifier_id);
      try {
        await saveDraft(m.modifier_id);
        await confirmModifier(token, menuItemId, m.modifier_id);
        await refresh();
      } catch (e: unknown) {
        // per-call 409 branch: a non-additive confirm gets its OWN message (skip resolves it).
        if (e instanceof RecipeApiError && e.status === 409 && m.modifier_type !== 'additive') {
          setError(t.modNonAdditive409);
        } else {
          setError(e instanceof RecipeApiError ? e.detail : t.recipesError);
        }
      } finally {
        setBusyId(null);
      }
    },
    [token, menuItemId, saveDraft, refresh, t.modNonAdditive409, t.recipesError],
  );

  const act = useCallback(
    async (id: string, fn: (tk: string, mi: string, md: string) => Promise<unknown>) => {
      if (!token) return;
      setBusyId(id);
      try {
        await fn(token, menuItemId, id);
        await refresh();
      } catch (e: unknown) {
        setError(e instanceof RecipeApiError ? e.detail : t.recipesError);
      } finally {
        setBusyId(null);
      }
    },
    [token, menuItemId, refresh, t.recipesError],
  );

  if (!mods || mods.length === 0) return null; // no modifiers → no sub-section

  // Explicit, exhaustive label map (NOT a computed `t[`status${...}`]` key): every access is a
  // literal the used⊆defined i18n guard can see, AND tsc enforces a label for every
  // ModifierStatus — a future status becomes a compile error, not a silent missing-key fallback.
  const statusLabels: Record<ModifierStatus, string> = {
    draft: t.statusDraft,
    confirmed: t.statusConfirmed,
    skipped: t.statusSkipped,
  };

  return (
    <View style={styles.wrap}>
      <Text style={styles.title}>{t.modSectionTitle}</Text>
      {error ? <Text style={styles.err}>{error}</Text> : null}
      {mods.map((m) => {
        const additive = m.modifier_type === 'additive';
        const rows = drafts[m.modifier_id] ?? [];
        const rowsInvalid = rows.some((r) => validateIngredient(r) !== null);
        const draftErr = validateDraft(rows.filter((r) => r.name.trim()));
        const canConfirm = additive && !rowsInvalid && draftErr === null && busyId !== m.modifier_id;
        const expanded = expandedId === m.modifier_id;
        return (
          <View key={m.modifier_id} style={styles.mod}>
            <Pressable
              onPress={() => additive && void open(m.modifier_id)}
              style={styles.modHead}
              accessibilityState={{ expanded }}
            >
              <Text style={styles.modName} numberOfLines={1}>
                {m.name}
              </Text>
              <RecipeStatusBadge status={m.status} label={statusLabels[m.status]} />
            </Pressable>

            {!additive ? (
              <View style={styles.notSupported}>
                <Text style={styles.notSupportedText}>{t.modNotSupported}</Text>
                <Button
                  label={t.recipesSkip}
                  variant="ghost"
                  size="sm"
                  onPress={() => void act(m.modifier_id, skipModifier)}
                />
              </View>
            ) : expanded ? (
              m.status === 'confirmed' ? (
                <View style={styles.actions}>
                  <Button
                    label={t.recipesUnconfirm}
                    variant="secondary"
                    size="sm"
                    onPress={() => void act(m.modifier_id, unconfirmModifier)}
                  />
                </View>
              ) : (
                <View>
                  {rows.map((r, i) => (
                    <IngredientRow
                      key={i}
                      value={r}
                      onChange={(next) =>
                        setRows(
                          m.modifier_id,
                          rows.map((rr, j) => (j === i ? next : rr)),
                        )
                      }
                      onBlur={() => scheduleSave(m.modifier_id)}
                      onRemove={
                        rows.length > 1
                          ? () => {
                              setRows(
                                m.modifier_id,
                                rows.filter((_, j) => j !== i),
                              );
                              scheduleSave(m.modifier_id);
                            }
                          : undefined
                      }
                    />
                  ))}
                  <Button
                    label={t.recipesAddIngredient}
                    variant="ghost"
                    size="sm"
                    onPress={() => setRows(m.modifier_id, [...rows, { ...EMPTY_ROW }])}
                  />
                  <View style={styles.actions}>
                    <Button
                      label={t.recipesConfirm}
                      size="sm"
                      disabled={!canConfirm}
                      loading={busyId === m.modifier_id}
                      onPress={() => void confirm(m)}
                    />
                    <Button
                      label={t.recipesSkip}
                      variant="ghost"
                      size="sm"
                      onPress={() => void act(m.modifier_id, skipModifier)}
                    />
                  </View>
                </View>
              )
            ) : null}
          </View>
        );
      })}
    </View>
  );
}

const styles = StyleSheet.create({
  wrap: { marginTop: 14, paddingTop: 12, borderTopWidth: 1, borderTopColor: T.hairline, gap: 8 },
  title: { ...TYPE.subhead, color: T.sec, fontWeight: '600' },
  err: { ...TYPE.caption1, color: T.red },
  mod: { backgroundColor: T.elev2, borderRadius: 10, padding: 10, gap: 6 },
  modHead: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', gap: 8 },
  modName: { ...TYPE.body, color: T.text, flex: 1 },
  notSupported: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' },
  notSupportedText: { ...TYPE.caption1, color: T.ter },
  actions: { flexDirection: 'row', gap: 8, marginTop: 8, alignItems: 'center' },
});
