// ReOrderOS design tokens — ported from web prototype.
// Single source of truth for colors, fonts, spacing across the RN app.
//
// Original: app/Tokens.jsx (web prototype)
// Notes: rgba() strings work in RN style; SF Pro is iOS-default, Inter/Outfit
// must be loaded via expo-font on Android (see App.tsx).

import { Platform, type TextStyle } from 'react-native';

export const T = {
  // ── Base surfaces ──────────────────────────────────────────────────────
  bg:        '#000000',
  elev1:     '#1C1C1E',
  elev2:     '#2C2C2E',
  elev3:     '#3A3A3C',
  sep:       'rgba(84,84,88,0.32)',
  sepStrong: 'rgba(120,120,128,0.50)',
  hairline:  'rgba(255,255,255,0.06)',

  // ── Text ───────────────────────────────────────────────────────────────
  text:  '#FFFFFF',
  label: 'rgba(235,235,245,0.92)',
  sec:   'rgba(235,235,245,0.60)',
  ter:   'rgba(235,235,245,0.30)',
  quat:  'rgba(235,235,245,0.18)',

  // ── Accent — emerald ───────────────────────────────────────────────────
  ac:       '#34D399',
  acDeep:   '#10B981',
  acSoft:   'rgba(52,211,153,0.14)',
  acBorder: 'rgba(52,211,153,0.30)',

  // ── Semantics ──────────────────────────────────────────────────────────
  green:      '#30D158',
  greenSoft:  'rgba(48,209,88,0.14)',
  red:        '#FF453A',
  redSoft:    'rgba(255,69,58,0.14)',
  amber:      '#FF9F0A',
  amberSoft:  'rgba(255,159,10,0.14)',
  yellow:     '#FFD60A',
  blue:       '#0A84FF',
  blueSoft:   'rgba(10,132,255,0.14)',
  purple:     '#BF5AF2',
  purpleSoft: 'rgba(191,90,242,0.14)',
  teal:       '#64D2FF',
  pink:       '#FF375F',

  // ── Spacing & radii ────────────────────────────────────────────────────
  pad:      16,
  padSm:    12,
  gap:      8,
  radius:   14,
  radiusLg: 20,
  radiusSm: 10,
} as const;

// ── Fonts ─────────────────────────────────────────────────────────────────
// On iOS the system font (San Francisco) is the right default. On Android we
// fall back to Inter (loaded via expo-font in App.tsx).
export const FONT: Record<'ui' | 'display' | 'mono' | 'numeric', TextStyle> = {
  ui: {
    fontFamily: Platform.select({ ios: undefined, android: 'Inter_400Regular', default: undefined }),
  },
  display: {
    fontFamily: Platform.select({ ios: undefined, android: 'Inter_600SemiBold', default: undefined }),
  },
  mono: {
    fontFamily: Platform.select({ ios: 'Menlo', android: 'monospace', default: 'monospace' }),
  },
  numeric: {
    // Tabular figures for stats. iOS handles via fontVariant; Android via Inter weight.
    fontVariant: Platform.OS === 'ios' ? ['tabular-nums'] : undefined,
    fontFamily: Platform.select({ ios: undefined, android: 'Inter_500Medium', default: undefined }),
  },
};

// ── Type scale ────────────────────────────────────────────────────────────
// iOS Human Interface Guidelines numbers; close enough on Android.
export const TYPE = {
  largeTitle: { fontSize: 34, fontWeight: '700' as const, letterSpacing: -0.6, lineHeight: 41 },
  title1:     { fontSize: 28, fontWeight: '700' as const, letterSpacing: -0.4, lineHeight: 34 },
  title2:     { fontSize: 22, fontWeight: '700' as const, letterSpacing: -0.3, lineHeight: 28 },
  title3:     { fontSize: 20, fontWeight: '600' as const, letterSpacing: -0.2, lineHeight: 25 },
  headline:   { fontSize: 17, fontWeight: '600' as const, lineHeight: 22 },
  body:       { fontSize: 17, fontWeight: '400' as const, lineHeight: 22 },
  callout:    { fontSize: 16, fontWeight: '400' as const, lineHeight: 21 },
  subhead:    { fontSize: 15, fontWeight: '400' as const, lineHeight: 20 },
  footnote:   { fontSize: 13, fontWeight: '400' as const, lineHeight: 18 },
  caption1:   { fontSize: 12, fontWeight: '400' as const, lineHeight: 16 },
  caption2:   { fontSize: 11, fontWeight: '400' as const, lineHeight: 13 },
};

export type Theme = typeof T;
