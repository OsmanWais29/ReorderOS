// Core atoms — RN versions of the web prototype's Button / Card / Field / Pill.

import React, { type ReactNode } from 'react';
import {
  View,
  Text,
  Pressable,
  TextInput,
  StyleSheet,
  ActivityIndicator,
  type ViewStyle,
  type TextStyle,
  type TextInputProps,
  type StyleProp,
  type PressableProps,
} from 'react-native';
import * as Haptics from 'expo-haptics';
import { T, TYPE } from '../theme/tokens';
import { Icon, type IconName } from './Icon';

// ── Button ────────────────────────────────────────────────────────────────
type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'destructive';
type ButtonSize = 'lg' | 'md' | 'sm';

type ButtonProps = {
  label: string;
  onPress?: () => void;
  variant?: ButtonVariant;
  size?: ButtonSize;
  fullWidth?: boolean;
  loading?: boolean;
  disabled?: boolean;
  iconLeft?: IconName;
  iconRight?: IconName;
  style?: StyleProp<ViewStyle>;
  haptic?: boolean;
};

export function Button({
  label,
  onPress,
  variant = 'primary',
  size = 'lg',
  fullWidth,
  loading,
  disabled,
  iconLeft,
  iconRight,
  style,
  haptic = true,
}: ButtonProps) {
  const handlePress = () => {
    if (haptic) Haptics.selectionAsync().catch(() => {});
    onPress?.();
  };

  const sizeStyle = SIZE_STYLES[size];
  const variantStyle = VARIANT_STYLES[variant];
  const isDisabled = disabled || loading;

  return (
    <Pressable
      onPress={handlePress}
      disabled={isDisabled}
      style={({ pressed }) => [
        styles.btn,
        sizeStyle.btn,
        variantStyle.btn,
        fullWidth && { alignSelf: 'stretch' },
        pressed && !isDisabled && variantStyle.btnPressed,
        isDisabled && { opacity: 0.4 },
        style,
      ]}
    >
      {loading ? (
        <ActivityIndicator color={variantStyle.label.color as string} />
      ) : (
        <>
          {iconLeft && <Icon name={iconLeft} size={sizeStyle.iconSize} color={variantStyle.label.color as string} />}
          <Text style={[styles.label, sizeStyle.label, variantStyle.label]}>{label}</Text>
          {iconRight && <Icon name={iconRight} size={sizeStyle.iconSize} color={variantStyle.label.color as string} />}
        </>
      )}
    </Pressable>
  );
}

const SIZE_STYLES = {
  lg: {
    btn: { height: 52, paddingHorizontal: 24, borderRadius: T.radius, gap: 8 },
    label: { fontSize: 17, fontWeight: '600' as const },
    iconSize: 18,
  },
  md: {
    btn: { height: 44, paddingHorizontal: 18, borderRadius: T.radius, gap: 6 },
    label: { fontSize: 15, fontWeight: '600' as const },
    iconSize: 16,
  },
  sm: {
    btn: { height: 34, paddingHorizontal: 12, borderRadius: T.radiusSm, gap: 4 },
    label: { fontSize: 13, fontWeight: '600' as const },
    iconSize: 14,
  },
} satisfies Record<ButtonSize, { btn: ViewStyle; label: TextStyle; iconSize: number }>;

const VARIANT_STYLES = {
  primary: {
    btn:        { backgroundColor: T.ac },
    btnPressed: { backgroundColor: T.acDeep },
    label:      { color: '#000' },
  },
  secondary: {
    btn:        { backgroundColor: T.elev1, borderWidth: 1, borderColor: T.sep },
    btnPressed: { backgroundColor: T.elev2 },
    label:      { color: T.text },
  },
  ghost: {
    btn:        { backgroundColor: 'transparent' },
    btnPressed: { backgroundColor: T.elev1 },
    label:      { color: T.text },
  },
  destructive: {
    btn:        { backgroundColor: T.redSoft, borderWidth: 1, borderColor: 'rgba(255,69,58,0.30)' },
    btnPressed: { backgroundColor: 'rgba(255,69,58,0.22)' },
    label:      { color: T.red },
  },
} satisfies Record<ButtonVariant, { btn: ViewStyle; btnPressed: ViewStyle; label: TextStyle }>;

// ── Card ──────────────────────────────────────────────────────────────────
type CardProps = {
  children: ReactNode;
  style?: StyleProp<ViewStyle>;
  raised?: boolean;
  onPress?: () => void;
};

export function Card({ children, style, raised, onPress }: CardProps) {
  const cardStyle: StyleProp<ViewStyle> = [
    styles.card,
    raised && { backgroundColor: T.elev2 },
    style,
  ];
  if (onPress) {
    return (
      <Pressable onPress={onPress} style={({ pressed }) => [cardStyle, pressed && { opacity: 0.7 }]}>
        {children}
      </Pressable>
    );
  }
  return <View style={cardStyle}>{children}</View>;
}

// ── Field (TextInput with label) ──────────────────────────────────────────
type FieldProps = {
  label?: string;
  hint?: string;
  error?: string;
  iconLeft?: IconName;
  containerStyle?: StyleProp<ViewStyle>;
} & TextInputProps;

export function Field({ label, hint, error, iconLeft, containerStyle, style, ...rest }: FieldProps) {
  return (
    <View style={containerStyle}>
      {label && <Text style={styles.fieldLabel}>{label}</Text>}
      <View style={[styles.fieldRow, error ? styles.fieldRowError : null]}>
        {iconLeft && <Icon name={iconLeft} size={18} color={T.sec} />}
        <TextInput
          placeholderTextColor={T.ter}
          {...rest}
          style={[styles.fieldInput, style]}
        />
      </View>
      {(hint || error) && (
        <Text style={[styles.fieldHint, error ? { color: T.red } : null]}>
          {error || hint}
        </Text>
      )}
    </View>
  );
}

// ── Pill (small status chip) ──────────────────────────────────────────────
type PillTone = 'neutral' | 'green' | 'amber' | 'red' | 'blue' | 'accent';
type PillProps = {
  label: string;
  tone?: PillTone;
  iconLeft?: IconName;
  style?: StyleProp<ViewStyle>;
};

export function Pill({ label, tone = 'neutral', iconLeft, style }: PillProps) {
  const tones: Record<PillTone, { bg: string; fg: string }> = {
    neutral: { bg: T.elev1,    fg: T.label },
    green:   { bg: T.greenSoft,fg: T.green },
    amber:   { bg: T.amberSoft,fg: T.amber },
    red:     { bg: T.redSoft,  fg: T.red },
    blue:    { bg: T.blueSoft, fg: T.blue },
    accent:  { bg: T.acSoft,   fg: T.ac },
  };
  const c = tones[tone];
  return (
    <View style={[styles.pill, { backgroundColor: c.bg }, style]}>
      {iconLeft && <Icon name={iconLeft} size={12} color={c.fg} />}
      <Text style={[styles.pillLabel, { color: c.fg }]}>{label}</Text>
    </View>
  );
}

// ── PressableRow (for option lists) ───────────────────────────────────────
type RowProps = {
  children: ReactNode;
  onPress?: () => void;
  style?: StyleProp<ViewStyle>;
} & Pick<PressableProps, 'disabled'>;

export function Row({ children, onPress, style, disabled }: RowProps) {
  return (
    <Pressable
      onPress={onPress}
      disabled={disabled || !onPress}
      style={({ pressed }) => [
        styles.row,
        pressed && { backgroundColor: T.elev2 },
        disabled && { opacity: 0.4 },
        style,
      ]}
    >
      {children}
    </Pressable>
  );
}

const styles = StyleSheet.create({
  // Button
  btn: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'center',
  },
  label: {
    textAlign: 'center',
  },

  // Card
  card: {
    backgroundColor: T.elev1,
    borderRadius: T.radius,
    padding: T.pad,
    borderWidth: 1,
    borderColor: T.hairline,
  },

  // Field
  fieldLabel: {
    ...TYPE.footnote,
    color: T.sec,
    marginBottom: 6,
    textTransform: 'uppercase',
    letterSpacing: 0.5,
  },
  fieldRow: {
    flexDirection: 'row',
    alignItems: 'center',
    backgroundColor: T.elev1,
    borderRadius: T.radius,
    paddingHorizontal: 14,
    height: 50,
    gap: 10,
    borderWidth: 1,
    borderColor: T.hairline,
  },
  fieldRowError: { borderColor: T.red },
  fieldInput: {
    flex: 1,
    color: T.text,
    fontSize: 17,
    height: '100%',
  },
  fieldHint: {
    ...TYPE.caption1,
    color: T.sec,
    marginTop: 6,
  },

  // Pill
  pill: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 4,
    paddingHorizontal: 8,
    paddingVertical: 4,
    borderRadius: 999,
    alignSelf: 'flex-start',
  },
  pillLabel: {
    fontSize: 12,
    fontWeight: '600',
  },

  // Row
  row: {
    flexDirection: 'row',
    alignItems: 'center',
    backgroundColor: T.elev1,
    borderRadius: T.radius,
    paddingHorizontal: 16,
    paddingVertical: 14,
    gap: 12,
    borderWidth: 1,
    borderColor: T.hairline,
  },
});
