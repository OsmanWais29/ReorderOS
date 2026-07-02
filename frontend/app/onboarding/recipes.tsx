// Onboarding "Recipes" step (Appendix F.1 / §1). Accordion of ALL synced menu items, per-item
// confirm/skip, progress counter, field-blur auto-save to drafts. Replaces the legacy
// `cleanup.tsx` categorize stub. Bilingual via useLang (FE-8). Modifier sub-section lives on the
// post-onboarding detail screen (deferred slice); this is the base-recipe path.

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { View, Text, ScrollView, StyleSheet } from 'react-native';
import { useRouter } from 'expo-router';
import { SafeAreaView } from 'react-native-safe-area-context';
import { Button } from '@/components/atoms';
import { OnboardingHeader } from '@/components/OnboardingHeader';
import { MenuItemAccordion } from '@/components/MenuItemAccordion';
import { ModifierSubsection } from '@/components/ModifierSubsection';
import { ProgressCounter, RecipeSkeleton } from '@/components/RecipeBits';
import {
  IngredientRow,
  validateIngredient,
  validateDraft,
  type IngredientDraft,
} from '@/components/IngredientRow';
import { useAuth } from '@/auth/AuthContext';
import { useLang } from '@/i18n/LangProvider';
import { T, TYPE } from '@/theme/tokens';
import {
  getRecipes,
  getRecipe,
  getProgress,
  patchRecipe,
  confirmRecipe,
  unconfirmRecipe,
  skipRecipe,
  syncMenu,
  RecipeApiError,
  type RecipeListItem,
  type Progress,
  type IngredientIn,
} from '@/api/recipes';

const EMPTY_ROW: IngredientDraft = { name: '', quantity: '', unit: '' };

function toIngredientIn(rows: IngredientDraft[]): IngredientIn[] {
  return rows.map((r) => ({ name: r.name.trim(), quantity: Number(r.quantity), unit: r.unit }));
}

export default function Recipes() {
  const { token } = useAuth();
  const { t } = useLang();
  const router = useRouter();

  const [items, setItems] = useState<RecipeListItem[] | null>(null);
  const [progress, setProgress] = useState<Progress | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, IngredientDraft[]>>({});
  const [busyId, setBusyId] = useState<string | null>(null);
  const [showSkipped, setShowSkipped] = useState(false);

  const refresh = useCallback(async () => {
    if (!token) return;
    try {
      const [list, prog] = await Promise.all([getRecipes(token), getProgress(token)]);
      setItems(list);
      setProgress(prog);
      setError(null);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : t.recipesError);
    }
  }, [token, t.recipesError]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const openItem = useCallback(
    async (id: string) => {
      if (expandedId === id) {
        setExpandedId(null);
        return;
      }
      setExpandedId(id);
      if (!token || drafts[id]) return;
      try {
        const detail = await getRecipe(token, id);
        const rows: IngredientDraft[] = detail.ingredients.map((i) => ({
          name: String(i.name ?? ''),
          quantity: i.quantity != null ? String(i.quantity) : '',
          unit: String(i.unit ?? ''),
        }));
        setDrafts((d) => ({ ...d, [id]: rows.length ? rows : [{ ...EMPTY_ROW }] }));
      } catch {
        setDrafts((d) => ({ ...d, [id]: [{ ...EMPTY_ROW }] }));
      }
    },
    [expandedId, token, drafts],
  );

  const setRows = (id: string, rows: IngredientDraft[]) =>
    setDrafts((d) => ({ ...d, [id]: rows }));

  // Field-blur auto-save (v5 §1 / FE-5). The SERVER draft (recipe_drafts) is the only
  // persistence — no AsyncStorage, so there is one draft owner and app-close is covered by
  // re-hydrating from GET on reopen. NON-BLOCKING: a transient failure is swallowed (local
  // state is intact; the next blur retries); only a 409 (became confirmed) surfaces, subtly.
  const saveDraft = useCallback(
    async (id: string) => {
      if (!token) return;
      const rows = drafts[id] ?? [];
      try {
        await patchRecipe(token, id, toIngredientIn(rows.filter((r) => r.name.trim())));
      } catch (e: unknown) {
        if (e instanceof RecipeApiError && e.status === 409) setError(t.recipes409);
        // any other failure is non-blocking — the next blur retries.
      }
    },
    [token, drafts, t.recipes409],
  );

  // Debounce per-item blur saves so rapid field edits coalesce into one PATCH.
  const saveTimers = useRef<Record<string, ReturnType<typeof setTimeout>>>({});
  const scheduleSave = useCallback(
    (id: string) => {
      const timers = saveTimers.current;
      if (timers[id]) clearTimeout(timers[id]);
      timers[id] = setTimeout(() => void saveDraft(id), 600);
    },
    [saveDraft],
  );
  useEffect(() => {
    const timers = saveTimers.current;
    return () => Object.values(timers).forEach(clearTimeout);
  }, []);

  const doConfirm = useCallback(
    async (id: string) => {
      if (!token) return;
      setBusyId(id);
      try {
        await saveDraft(id);
        await confirmRecipe(token, id);
        await refresh();
      } catch (e: unknown) {
        setError(e instanceof RecipeApiError ? e.detail : t.recipesError);
      } finally {
        setBusyId(null);
      }
    },
    [token, saveDraft, refresh, t.recipesError],
  );

  const doAction = useCallback(
    async (id: string, fn: (tk: string, mid: string) => Promise<unknown>) => {
      if (!token) return;
      setBusyId(id);
      try {
        await fn(token, id);
        await refresh();
      } catch (e: unknown) {
        setError(e instanceof RecipeApiError ? e.detail : t.recipesError);
      } finally {
        setBusyId(null);
      }
    },
    [token, refresh, t.recipesError],
  );

  const statusLabel = useMemo(
    () => ({
      none: t.statusNone,
      draft: t.statusDraft,
      confirmed: t.statusConfirmed,
      skipped: t.statusSkipped,
    }),
    [t],
  );

  const visible = (items ?? []).filter((it) => showSkipped || it.status !== 'skipped');

  return (
    <SafeAreaView style={styles.safe} edges={['top', 'bottom']}>
      <OnboardingHeader step={6} totalSteps={14} onBack={() => router.back()} />
      <View style={styles.head}>
        <Text style={styles.h1}>{t.recipesTitle}</Text>
        <Text style={styles.sub}>{t.recipesSub}</Text>
        {progress ? (
          <ProgressCounter
            confirmed={progress.confirmed}
            denominator={progress.denominator}
            label={t.recipesProgress}
          />
        ) : null}
      </View>

      {items === null && !error ? (
        <RecipeSkeleton />
      ) : error ? (
        <View style={styles.center}>
          <Text style={styles.err}>{error}</Text>
          <Button label={t.recipesRetry} variant="secondary" onPress={() => void refresh()} />
        </View>
      ) : visible.length === 0 ? (
        <View style={styles.center}>
          <Text style={styles.sub}>{t.recipesEmpty}</Text>
          <Button
            label={t.recipesSync}
            variant="secondary"
            onPress={() => token && void syncMenu(token).then(refresh)}
          />
        </View>
      ) : (
        <ScrollView contentContainerStyle={styles.list}>
          {visible.map((it) => {
            const rows = drafts[it.menu_item_id] ?? [];
            const rowsInvalid = rows.some((r) => validateIngredient(r) !== null);
            const draftErr = validateDraft(rows.filter((r) => r.name.trim()));
            const canConfirm = !rowsInvalid && draftErr === null && busyId !== it.menu_item_id;
            return (
              <MenuItemAccordion
                key={it.menu_item_id}
                item={it}
                statusLabel={statusLabel[it.status]}
                metaLabel={`${it.ingredient_count} ${t.ingWord} · ${it.modifier_count} ${t.modWord}`}
                expanded={expandedId === it.menu_item_id}
                onToggle={() => void openItem(it.menu_item_id)}
              >
                {it.status === 'confirmed' ? (
                  <View style={styles.actions}>
                    <Button
                      label={t.recipesUnconfirm}
                      variant="secondary"
                      onPress={() => void doAction(it.menu_item_id, unconfirmRecipe)}
                    />
                  </View>
                ) : (
                  <>
                    {rows.map((r, i) => (
                      <IngredientRow
                        key={i}
                        value={r}
                        onChange={(next) =>
                          setRows(
                            it.menu_item_id,
                            rows.map((rr, j) => (j === i ? next : rr)),
                          )
                        }
                        onBlur={() => scheduleSave(it.menu_item_id)}
                        onRemove={
                          rows.length > 1
                            ? () => {
                                setRows(
                                  it.menu_item_id,
                                  rows.filter((_, j) => j !== i),
                                );
                                scheduleSave(it.menu_item_id);
                              }
                            : undefined
                        }
                      />
                    ))}
                    <Button
                      label={t.recipesAddIngredient}
                      variant="ghost"
                      onPress={() => setRows(it.menu_item_id, [...rows, { ...EMPTY_ROW }])}
                    />
                    <View style={styles.actions}>
                      <Button
                        label={t.recipesConfirm}
                        disabled={!canConfirm}
                        loading={busyId === it.menu_item_id}
                        onPress={() => void doConfirm(it.menu_item_id)}
                      />
                      <Button
                        label={t.recipesSkip}
                        variant="ghost"
                        onPress={() => void doAction(it.menu_item_id, skipRecipe)}
                      />
                    </View>
                  </>
                )}
                <ModifierSubsection menuItemId={it.menu_item_id} />
              </MenuItemAccordion>
            );
          })}
          <Button
            label={showSkipped ? t.recipesHideSkipped : t.recipesShowSkipped}
            variant="ghost"
            onPress={() => setShowSkipped((s) => !s)}
          />
        </ScrollView>
      )}

      <View style={styles.cta}>
        <Button
          label={t.recipesContinue}
          fullWidth
          iconRight="arrow-right"
          onPress={() => router.push('/onboarding/suppliers')}
        />
      </View>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: T.bg },
  head: { paddingHorizontal: T.pad, paddingTop: 16, gap: 8 },
  h1: { ...TYPE.title1, color: T.text },
  sub: { ...TYPE.body, color: T.sec },
  center: { flex: 1, alignItems: 'center', justifyContent: 'center', gap: 16, padding: T.pad },
  err: { ...TYPE.subhead, color: T.red, textAlign: 'center' },
  list: { padding: T.pad, gap: 12 },
  actions: { flexDirection: 'row', gap: 10, marginTop: 12, alignItems: 'center' },
  cta: {
    paddingHorizontal: T.pad,
    paddingVertical: 12,
    borderTopWidth: 1,
    borderTopColor: T.hairline,
  },
});
