// ReorderOS design tokens — iOS-native, Robinhood-balanced density.
// Near-black base with emerald accent. SF Pro / Outfit.

const T = {
  // Base surfaces
  bg:      '#000000',       // true black, OLED
  elev1:   '#1C1C1E',       // iOS secondary system bg (cards)
  elev2:   '#2C2C2E',       // iOS tertiary system bg (raised)
  elev3:   '#3A3A3C',       // iOS quaternary
  sep:     'rgba(84,84,88,0.32)',   // iOS separator
  sepStrong: 'rgba(120,120,128,0.5)',
  hairline: 'rgba(255,255,255,0.06)',

  // Text
  text:    '#FFFFFF',
  label:   'rgba(235,235,245,0.92)',
  sec:     'rgba(235,235,245,0.60)',
  ter:     'rgba(235,235,245,0.30)',
  quat:    'rgba(235,235,245,0.18)',

  // Accent — emerald (food/money positive)
  ac:      '#34D399',       // primary action
  acDeep:  '#10B981',       // pressed / fill
  acSoft:  'rgba(52,211,153,0.14)',
  acBorder:'rgba(52,211,153,0.30)',

  // Semantics
  green:   '#30D158',       // iOS systemGreen
  greenSoft:'rgba(48,209,88,0.14)',
  red:     '#FF453A',       // iOS systemRed
  redSoft: 'rgba(255,69,58,0.14)',
  amber:   '#FF9F0A',       // iOS systemOrange
  amberSoft:'rgba(255,159,10,0.14)',
  yellow:  '#FFD60A',       // iOS systemYellow
  blue:    '#0A84FF',       // iOS systemBlue
  blueSoft:'rgba(10,132,255,0.14)',
  purple:  '#BF5AF2',       // iOS systemPurple
  purpleSoft:'rgba(191,90,242,0.14)',
  teal:    '#64D2FF',
  pink:    '#FF375F',

  // Fonts
  ui:      '-apple-system, "SF Pro Text", "Outfit", system-ui, sans-serif',
  display: '-apple-system, "SF Pro Display", "Outfit", system-ui, sans-serif',
  mono:    'ui-monospace, "SF Mono", Menlo, monospace',
  numeric: '"Outfit", -apple-system, system-ui, sans-serif', // tabular numbers

  // Spacing
  pad:     16,
  padSm:   12,
  gap:     8,
  radius:  14,
  radiusLg:20,
  radiusSm:10,
};

window.T = T;
window.ROO = T; // backward-compat in case anything still refs ROO
