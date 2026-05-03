# ReOrderOS — React Native (Expo) scaffold

This is a **starter** React Native project that ports the ReOrderOS HTML prototype to a real native app you can ship to iOS and Android. It is intentionally **not** a complete port — see "What's done / what's pending" below.

The HTML prototype lives in `../app/` and remains the source of truth for design and copy. This RN project is where the iOS/Android codebase lives.

---

## How to run

You need **Node 20+**, **Bun or npm**, **Xcode** (for iOS) and/or **Android Studio** (for Android) on your machine.

```bash
cd rn
npm install        # or: bun install
npx expo start
```

Then press `i` for iOS Simulator, `a` for Android emulator, or scan the QR code with Expo Go on your phone.

---

## Architecture

```
rn/
├─ app/                          # expo-router file-based routing
│  ├─ _layout.tsx                # Root: providers + status bar
│  ├─ index.tsx                  # Redirect → /onboarding/welcome
│  ├─ onboarding/                # 14-step onboarding flow
│  │  ├─ _layout.tsx             # Stack navigator
│  │  ├─ welcome.tsx             # ✅ FULL PORT
│  │  ├─ account.tsx             # ✅ FULL PORT
│  │  ├─ push.tsx                # ✅ FULL PORT
│  │  ├─ pos-picker.tsx          # ✅ FULL PORT
│  │  ├─ connecting.tsx          # ⚠️ STUB
│  │  ├─ found-summary.tsx       # ⚠️ STUB
│  │  ├─ cleanup.tsx             # ⚠️ STUB
│  │  ├─ suppliers.tsx           # ⚠️ STUB
│  │  ├─ par-levels.tsx          # ⚠️ STUB
│  │  ├─ team.tsx                # ⚠️ STUB
│  │  ├─ pin.tsx                 # ⚠️ STUB
│  │  ├─ biometric.tsx           # ⚠️ STUB
│  │  ├─ billing.tsx             # ⚠️ STUB
│  │  ├─ done.tsx                # ⚠️ STUB
│  │  ├─ sign-in.tsx             # ⚠️ STUB
│  │  └─ manual-menu.tsx         # ⚠️ STUB
│  └─ (app)/                     # Main app (after onboarding)
│     ├─ _layout.tsx             # Bottom-tab navigator
│     ├─ home.tsx                # ⚠️ Partial — sample card + stat row
│     ├─ stock.tsx               # ⚠️ STUB
│     ├─ orders.tsx              # ⚠️ STUB
│     ├─ sales.tsx               # ⚠️ STUB
│     └─ more.tsx                # ⚠️ Partial — language toggle works
└─ src/
   ├─ theme/tokens.ts            # ✅ Full port of T (colors, spacing, type)
   ├─ i18n/
   │  ├─ strings.ts              # ✅ EN + FR (Quebec) strings
   │  └─ LangProvider.tsx        # ✅ Context + AsyncStorage persistence
   └─ components/
      ├─ Icon.tsx                # ✅ react-native-svg port (~30 icons)
      ├─ atoms.tsx               # ✅ Button, Card, Field, Pill, Row
      ├─ OnboardingHeader.tsx    # ✅ Back + step indicator + progress bar
      ├─ Stub.tsx                # placeholder for unported screens
      └─ TabPlaceholder.tsx      # placeholder for unported tabs
```

---

## What's done

- **Project chrome.** Expo SDK 51, TypeScript, expo-router, dark mode, Reanimated, Gesture Handler, SafeAreaProvider, splash screen.
- **Design tokens** ported 1:1 from `app/Tokens.jsx`. Same colors, same spacing, same radii. Type scale added (iOS HIG-aligned).
- **i18n** wired up with EN/FR (Quebec). Detects device locale on first launch, persists choice with AsyncStorage. The toggle on Welcome and More both work end-to-end.
- **Atoms** — `Button` (4 variants × 3 sizes, with haptics + loading + iconLeft/iconRight), `Card`, `Field` (label + hint/error + icon), `Pill` (6 tones), `Row`. Closely mirror the web atoms but built on `Pressable`/`View`/`Text`/`TextInput` with `StyleSheet`.
- **Icon** — react-native-svg port of the most-used Lucide icons. Add more by extending the switch in `Icon.tsx`.
- **OnboardingHeader** — back arrow, step indicator (`2/14`), progress bar.
- **Welcome screen** — pixel-close port. Logo, dual-line headline (with accent), 3 feature rows, primary + ghost CTAs, language toggle. Deeplinks to next steps.
- **Account screen** — full keyboard-aware form with validation. 4 fields (name, restaurant, email, password), password rules, disabled-until-valid CTA.
- **Push screen** — calls real `Notifications.requestPermissionsAsync()`.
- **POS picker** — full port. 6 providers + "I don't use any of these" fallback, navigates to Connecting with provider param.
- **Tab navigator** — bottom tabs (Home/Stock/Orders/Sales/More) using the same icons and colors as the web prototype's footer.

## What's pending (in priority order)

### 1. Onboarding tail (10 steps)
Each is currently a 1-line `Stub` route. Open `../app/Onboarding.jsx` and port the matching step:

- `connecting.tsx` — Use **expo-auth-session** for the OAuth handoff to Square / Clover / Toast.
- `found-summary.tsx` — Pull menu/sales counts from your backend; show "is this your restaurant?" confirm.
- `cleanup.tsx` — Swipe-to-categorize stack. Use `react-native-reanimated` + `react-native-gesture-handler` (already installed). The swipe-card pattern in the web `SwipeStack` component maps to the [Tinder swipe deck recipe](https://github.com/3DJakob/react-native-deck-swiper) or a hand-rolled Reanimated worklet.
- `suppliers.tsx` — List + add modal.
- `par-levels.tsx` — Per-item slider with explanatory chart.
- `team.tsx` — Email invite + role picker.
- `pin.tsx` — 4-digit numeric pad. Persist hash with **expo-secure-store** (not AsyncStorage).
- `biometric.tsx` — `expo-local-authentication` `authenticateAsync()` to enable Face ID / fingerprint unlock.
- `billing.tsx` — `@stripe/stripe-react-native` PaymentSheet for the 14-day trial card capture.
- `done.tsx` — Lottie celebration → push to `(app)/home`.

### 2. Main app tabs
- **Home** — port the AI-suggestion card, stat row, and rolling activity feed from `MainApp.jsx`.
- **Stock** — `FlashList` with par levels, current count, low/ok/over states. Pull-to-refresh.
- **Orders** — open POs (segmented), historical, detail screen with PDF preview.
- **Sales** — charts. Use **react-native-svg** + `d3-shape` for the line/bar charts (same approach as the web prototype). Or **victory-native v37+** for less code.
- **More** — settings: profile, suppliers, team, billing, sign out, legal.

### 3. Real integrations to wire
- **Auth** — Supabase Auth, Auth0, or Firebase. Add Apple Sign In with `expo-apple-authentication`.
- **Backend** — TanStack Query + your REST/GraphQL layer. Or Supabase JS.
- **Push** — get APNs token via `Notifications.getExpoPushTokenAsync()` and ship it to your backend.
- **Crash + analytics** — Sentry (`@sentry/react-native`) and PostHog or Amplitude.
- **OTA updates** — `expo-updates` is already in the SDK; turn on EAS Update for branch-based rollouts.

### 4. Android polish
This scaffold is iOS-styled across both platforms. Before shipping Android, decide whether to:
- **Keep iOS look on Android** — fine for MVP. Make sure tap targets are ≥48dp and add Material ripple to `Pressable` with `android_ripple={{ color: T.elev2 }}`.
- **Diverge to Material 3** — different `Tabs` component, different elevation, system fonts (Roboto/Inter), Android-specific status bar handling.

### 5. Fonts
The web prototype uses SF Pro (free on iOS) and Inter as fallback. To match on Android, install:
```bash
npx expo install expo-font @expo-google-fonts/inter
```
Then load in `app/_layout.tsx`:
```tsx
const [loaded] = useFonts({
  Inter_400Regular: require('@expo-google-fonts/inter/Inter_400Regular.ttf'),
  Inter_500Medium: require('@expo-google-fonts/inter/Inter_500Medium.ttf'),
  Inter_600SemiBold: require('@expo-google-fonts/inter/Inter_600SemiBold.ttf'),
  Inter_700Bold: require('@expo-google-fonts/inter/Inter_700Bold.ttf'),
});
if (!loaded) return null;
```

---

## Web → RN translation cheatsheet

Used while porting? Keep this open.

| Web (HTML/JSX)              | React Native                                      |
|-----------------------------|---------------------------------------------------|
| `<div>`                     | `<View>`                                          |
| `<span>`, `<p>`, `<h1>` etc | `<Text>` (text **must** be inside `<Text>`)       |
| `<button>`                  | `<Pressable>` or `<TouchableOpacity>`             |
| `<input>`                   | `<TextInput>`                                     |
| `<img>`                     | `<Image source={{uri:...}}>` or `require(...)`    |
| `<svg>`                     | `<Svg>` from `react-native-svg`                   |
| `style={{ display: 'flex' }}` | implicit — every `View` is flex by default      |
| `onClick`                   | `onPress`                                         |
| `className=""` + CSS file   | `style={...}` with `StyleSheet.create({...})`     |
| `:hover`                    | none (mobile) — use `Pressable`'s `pressed` state |
| `position: fixed`           | `position: 'absolute'` inside a flex parent       |
| `vh` / `vw` / `%`           | rare — use `Dimensions.get('window')` or `useWindowDimensions()` |
| `transition: 0.2s`          | `Animated` API or Reanimated worklets             |
| `localStorage`              | `AsyncStorage` (already installed)                |
| `localStorage` for secrets  | `expo-secure-store`                               |
| `console.log(..)`           | same — but use Flipper or React DevTools          |

**Style gotchas the team will hit:**
- `flexDirection` defaults to `'column'` in RN (vs `row` on web). Add `flexDirection: 'row'` everywhere a row is wanted.
- `flex: 1` does **not** distribute the same as web flex-grow; it sets `flexGrow: 1, flexShrink: 1, flexBasis: 0`.
- Margins **don't collapse**.
- `gap` works (RN 0.71+), use it.
- Shadows: iOS uses `shadowColor/shadowOffset/shadowOpacity/shadowRadius`; Android uses `elevation`. Provide both.
- Text inheritance is limited — `<Text>` inside `<Text>` inherits styles, but `<Text>` inside `<View>` does **not** inherit color from the View.

---

## Recommended next session

1. Run the project on your local Simulator and confirm Welcome → Account → Push → POS picker → (stub) flow works end-to-end.
2. Pick **one** stub onboarding screen and do a real port — `cleanup.tsx` (swipe-stack) is the most interesting and unblocks the most product value.
3. Replace the placeholder Home with the real Home from `MainApp.jsx`.
4. Wire one real integration. Stripe is the most impactful for monetization; Supabase Auth is the most impactful for user accounts.
