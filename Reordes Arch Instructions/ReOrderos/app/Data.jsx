// ReorderOS — restaurant data (Bluebird Café, Toronto).
// Single source of truth used across all screens.

const DATA = {
  restaurant: {
    name: 'Bluebird Café',
    address: '482 Queen St W',
    city: 'Toronto',
    cover: 340, // typical covers / day
    foodCost: 28.4,
  },
  today: {
    date: 'Tue · Apr 22',
    weather: '14°C · Rain',
    covers: 168,
    revenue: 3284,
    foodCost: 27.1,
    labor: 29.8,
  },
  // 14 days actual + 7 days forecast
  revenueSeries: [
    { label: 'Apr 8', actual: 2890 },
    { label: '', actual: 3120 },
    { label: '', actual: 3480 },
    { label: 'Apr 11', actual: 4180 },
    { label: '', actual: 4620 },
    { label: '', actual: 2740 },
    { label: '', actual: 2110 },
    { label: 'Apr 15', actual: 3290 },
    { label: '', actual: 3410 },
    { label: '', actual: 3680 },
    { label: 'Apr 18', actual: 4240 },
    { label: '', actual: 4890 },
    { label: '', actual: 2610 },
    { label: '', actual: 1980 },
    { label: 'Apr 22', actual: 3284, forecast: 3284, fLow: 3284, fHigh: 3284 },
    { label: '', forecast: 3410, fLow: 3100, fHigh: 3760 },
    { label: '', forecast: 4380, fLow: 3900, fHigh: 4820 },
    { label: 'Apr 25', forecast: 4920, fLow: 4420, fHigh: 5480 },
    { label: '', forecast: 5180, fLow: 4600, fHigh: 5760 },
    { label: '', forecast: 2740, fLow: 2400, fHigh: 3080 },
    { label: 'Apr 28', forecast: 2210, fLow: 1920, fHigh: 2520 },
  ],
  // Dayparting — 7 days × 16 hours (6a-10p)
  heatmap: [
    // Mon
    [2,8,28,45,38,32,45,62,88,76,52,38,32,48,42,18],
    // Tue
    [2,10,32,52,48,28,38,58,80,68,44,32,28,42,36,16],
    // Wed
    [3,12,36,58,52,30,34,54,76,66,42,28,24,38,32,14],
    // Thu
    [2,11,34,56,54,32,38,52,72,64,38,24,18,30,26,12],
    // Fri
    [3,14,42,64,60,36,42,68,92,86,74,82,72,64,54,38],
    // Sat
    [4,18,52,82,96,72,68,78,98,94,82,84,76,68,56,40],
    // Sun
    [5,22,56,88,92,62,52,64,82,70,54,46,36,28,22,10],
  ],
  // Menu items for engineering quadrant
  menu: [
    { name: 'Shawarma Poutine', popularity: 0.88, margin: 0.78, price: 16, sold: 412, color: '#30D158', category: 'Mains', size: 1.2 },
    { name: 'Butter Chicken Bowl', popularity: 0.72, margin: 0.68, price: 17, sold: 324, color: '#30D158', category: 'Mains', size: 1.0 },
    { name: 'Classic Poutine', popularity: 0.82, margin: 0.58, price: 12, sold: 386, color: '#0A84FF', category: 'Sides', size: 1.1 },
    { name: 'Peameal Sandwich', popularity: 0.64, margin: 0.55, price: 14, sold: 276, color: '#0A84FF', category: 'Mains', size: 0.9 },
    { name: 'Maple Latte', popularity: 0.78, margin: 0.82, price: 6.5, sold: 894, color: '#30D158', category: 'Drinks', size: 1.4 },
    { name: 'Kale Caesar', popularity: 0.22, margin: 0.72, price: 15, sold: 48, color: '#FF9F0A', category: 'Salads', size: 0.6 },
    { name: 'Nduja Toast', popularity: 0.18, margin: 0.76, price: 13, sold: 38, color: '#FF9F0A', category: 'Brunch', size: 0.5 },
    { name: 'Beet Hummus', popularity: 0.14, margin: 0.42, price: 11, sold: 26, color: '#FF453A', category: 'Starters', size: 0.4 },
    { name: 'Veggie Wrap', popularity: 0.28, margin: 0.38, price: 11, sold: 62, color: '#FF453A', category: 'Mains', size: 0.5 },
    { name: 'Iced Americano', popularity: 0.68, margin: 0.78, price: 4.5, sold: 688, color: '#30D158', category: 'Drinks', size: 1.0 },
  ],
  // At-risk inventory
  atRisk: [
    {
      id: 'avocado', name: 'Avocados (Hass)', sku: 'PRO-AV-001',
      stock: 6, par: 24, unit: 'ea', cover: 1.2,
      stockoutIn: '48h', stockoutDay: 'Thursday',
      severity: 'critical', supplier: 'GFS', unitCost: 2.14,
      velocity: 12.4, // per day
      // 14-day burn including forecast dropping to 0
      burndown: [
        { label: '-7d', stock: 22, par: 24 },
        { label: '', stock: 18, par: 24 },
        { label: '', stock: 14, par: 24 },
        { label: '', stock: 20, par: 24 },
        { label: '', stock: 16, par: 24 },
        { label: '', stock: 11, par: 24 },
        { label: '', stock: 6, par: 24 },
        { label: 'Today', stock: 6, par: 24, forecast: 6 },
        { label: '', forecast: 0, par: 24 },
        { label: '+2d', forecast: 0, par: 24 },
      ],
      consumption: [4,6,5,8,12,14,11,9,10,15,18,14,12,13],
      why: [
        'Brunch demand up 34% WoW (Easter weekend tailwind)',
        'Lunch bowl special driving extra usage',
        'Last order Fri Apr 18, arrived Sat (too late for this cycle)',
      ],
    },
    {
      id: 'chicken', name: 'Chicken thighs (boneless)', sku: 'PRO-CH-044',
      stock: 8.4, par: 28, unit: 'kg', cover: 0.9,
      stockoutIn: '36h', stockoutDay: 'Wednesday',
      severity: 'critical', supplier: 'GFS', unitCost: 11.80,
      velocity: 9.6,
      burndown: [
        { label: '-7d', stock: 26, par: 28 },
        { label: '', stock: 21, par: 28 },
        { label: '', stock: 18, par: 28 },
        { label: '', stock: 14, par: 28 },
        { label: '', stock: 10, par: 28 },
        { label: '', stock: 8.4, par: 28 },
        { label: 'Today', stock: 8.4, par: 28, forecast: 8.4 },
        { label: '', forecast: 0, par: 28 },
        { label: '+2d', forecast: 0, par: 28 },
      ],
      consumption: [8,6,9,11,12,9,10,9,11,13,11,9,10,11],
      why: ['Shawarma poutine running hot (+41% WoW)', 'Wed order missed cutoff'],
    },
    {
      id: 'maple', name: 'Maple syrup (Grade A)', sku: 'PAN-MS-012',
      stock: 2.1, par: 6, unit: 'L', cover: 1.8,
      stockoutIn: '3d', stockoutDay: 'Friday',
      severity: 'warning', supplier: 'OFT', unitCost: 42.00,
      velocity: 1.15,
      burndown: [
        { label: '-7d', stock: 5.2, par: 6 },
        { label: '', stock: 4.4, par: 6 },
        { label: '', stock: 3.6, par: 6 },
        { label: '', stock: 3.0, par: 6 },
        { label: '', stock: 2.6, par: 6 },
        { label: '', stock: 2.1, par: 6 },
        { label: 'Today', stock: 2.1, par: 6, forecast: 2.1 },
        { label: '', forecast: 0.9, par: 6 },
        { label: '+2d', forecast: 0, par: 6 },
      ],
      consumption: [1.2,1.0,0.8,1.4,0.9,1.1,1.0,1.3,1.2,0.8,1.1,0.9,1.2,1.0],
      why: ['Maple latte launch driving syrup usage +2.4×', 'Supplier OFT raised price 8% — switch to Costco?'],
    },
    {
      id: 'cream', name: 'Heavy cream 35%', sku: 'DAI-CR-007',
      stock: 4.2, par: 10, unit: 'L', cover: 1.6,
      stockoutIn: '3d', stockoutDay: 'Friday',
      severity: 'warning', supplier: 'GFS', unitCost: 8.90,
      velocity: 2.6,
      burndown: [
        { label: '-7d', stock: 9.2, par: 10 }, { label: '', stock: 7.8, par: 10 },
        { label: '', stock: 6.6, par: 10 }, { label: '', stock: 5.4, par: 10 },
        { label: '', stock: 4.9, par: 10 }, { label: '', stock: 4.2, par: 10 },
        { label: 'Today', stock: 4.2, par: 10, forecast: 4.2 },
        { label: '', forecast: 1.6, par: 10 }, { label: '+2d', forecast: 0, par: 10 },
      ],
      consumption: [2.8,2.4,2.1,2.9,2.4,2.6,2.8,3.0,2.6,2.1,2.4,2.2,2.6,2.4],
      why: ['Pasta special pulls cream', 'Weekend brunch cream needs'],
    },
    {
      id: 'tomato', name: 'Roma tomatoes', sku: 'PRO-TM-022',
      stock: 5.8, par: 14, unit: 'kg', cover: 2.1,
      stockoutIn: '4d', stockoutDay: 'Saturday',
      severity: 'warning', supplier: 'OFT', unitCost: 3.20,
      velocity: 2.8,
      burndown: [
        { label: '-7d', stock: 13, par: 14 }, { label: '', stock: 11, par: 14 },
        { label: '', stock: 9, par: 14 }, { label: '', stock: 8, par: 14 },
        { label: '', stock: 7, par: 14 }, { label: '', stock: 5.8, par: 14 },
        { label: 'Today', stock: 5.8, par: 14, forecast: 5.8 },
        { label: '', forecast: 2.4, par: 14 }, { label: '+2d', forecast: 0, par: 14 },
      ],
      consumption: [3.2,2.8,2.4,2.6,2.8,3.0,2.8,2.4,2.6,3.2,2.8,2.4,2.6,2.8],
      why: ['Brunch egg station high usage', 'OFT price just jumped 12%'],
    },
  ],
  // Draft PO
  draftPO: {
    number: 'PO-2026-0042',
    supplier: 'Gordon Food Service',
    rep: 'Dave Chan',
    deliverDate: 'Fri Apr 25',
    deliverWindow: '7–9 AM',
    items: [
      { name: 'Avocados (Hass)', qty: 48, unit: 'ea', cost: 2.14 },
      { name: 'Chicken thighs (boneless)', qty: 28, unit: 'kg', cost: 11.80 },
      { name: 'Heavy cream 35%', qty: 12, unit: 'L', cost: 8.90 },
      { name: 'Butter unsalted', qty: 6, unit: 'kg', cost: 14.20 },
      { name: 'AP flour (25kg bag)', qty: 2, unit: 'bag', cost: 34.50 },
    ],
  },
  suppliers: [
    { name: 'Gordon Food Service', id: 'gfs', lastOrder: '3d ago', nextDeliver: 'Fri Apr 25', skuCount: 42, change: -2.1, rep: 'Dave Chan' },
    { name: 'Sysco Canada', id: 'sysco', lastOrder: '1w ago', nextDeliver: 'Mon Apr 28', skuCount: 18, change: +3.4, rep: 'Priya Singh' },
    { name: 'Ontario Fresh Tomato', id: 'oft', lastOrder: '4d ago', nextDeliver: 'Wed Apr 23', skuCount: 8, change: +12.0, rep: 'Marco Rossi' },
    { name: 'Restaurant Depot', id: 'rd', lastOrder: '2d ago', nextDeliver: 'Walk-in', skuCount: 14, change: -0.8, rep: null },
    { name: 'Mario\'s Butcher', id: 'mario', lastOrder: '5d ago', nextDeliver: 'Thu Apr 24', skuCount: 6, change: +1.8, rep: 'Mario' },
  ],
  // Opportunities (sales)
  opportunities: [
    {
      id: 'poutine-thur', icon: 'flame', tone: 'green',
      title: 'Push poutine on Thursday nights',
      sub: '$2 off poutine+drink combo · 7–10 PM',
      projection: '+$17.6k/yr',
      detail: 'Thursdays are dead after 8 PM (TMU study crowd needs a hook). 71% of poutines currently land Fri–Sat. A Thu bundle moves an estimated 34 extra orders/week.',
      confidence: 0.82,
    },
    {
      id: 'shawarma-price', icon: 'trend-up', tone: 'green',
      title: 'Raise Shawarma Poutine $14 → $15',
      sub: 'Demand risk 6%, revenue lift +$3.2k/yr',
      projection: '+$3.2k/yr',
      detail: 'Peer median price in GTA cafés: $15.50. Your price elasticity curve shows +$1 loses only 6% of volume, net +$272/month.',
      confidence: 0.78,
    },
    {
      id: 'patio-rain', icon: 'drop', tone: 'blue',
      title: 'Rain push: delivery discount',
      sub: 'Rain forecast Thu & Sat · patio -40% usual',
      projection: '+$1.8k this wk',
      detail: '3 rainy days forecast this week. Historically, delivery orders spike 62% on rain days. Push a 15% off delivery code from 11 AM.',
      confidence: 0.71,
    },
    {
      id: 'dead-hour', icon: 'clock', tone: 'amber',
      title: 'Fill the 2–4 PM dead zone',
      sub: 'Office lunch bundle: $14 bowl+drink',
      projection: '+$9.4k/yr',
      detail: 'Tue–Thu 2–4 PM is flat (15% of Friday volume). Office workers within 400m (MaRS, TMU, Much HQ) are a fit. Run a "remote work bundle" 2–4 PM.',
      confidence: 0.68,
    },
    {
      id: 'winback-lamb', icon: 'refresh', tone: 'purple',
      title: 'Winback: Lamb Tacos',
      sub: 'Sold great in Jan-Feb, quietly 86\'d Mar 8',
      projection: '+$6.1k/yr',
      detail: 'Lamb tacos were your #4 margin item. You removed them when lamb price spiked. Lamb is back to normal (-11% MoM). Bring them back on Thu special.',
      confidence: 0.74,
    },
    {
      id: 'pair-slow', icon: 'sparkles', tone: 'blue',
      title: 'Bundle Kale Caesar with Maple Latte',
      sub: 'Slow mover × star — both see +22% lift',
      projection: '+$2.4k/yr',
      detail: 'Maple Latte is your highest-margin drink. Kale Caesar is a puzzle — great margin, low volume. Pair them as a "Brunch for One" combo at $18 (save $3.50).',
      confidence: 0.66,
    },
  ],
  // Variance
  variance: {
    theoretical: 28.0,
    actual: 31.4,
    gap: 3.4,
    dollars: 1114,
    period: 'Apr 1-21',
    breakdown: [
      { label: 'Theoretical', value: 7860, type: 'start' },
      { label: 'Waste', value: -412, type: 'neg' },
      { label: 'Yield loss', value: -296, type: 'neg' },
      { label: 'Shrink', value: -184, type: 'neg' },
      { label: 'Comps', value: -222, type: 'neg' },
      { label: 'Actual', value: 8974, type: 'end' },
    ],
    topOffenders: [
      { name: 'Chicken thighs', cost: '$412 over', pct: 18.4, reason: 'Portioning drift — avg 182g vs 160g spec' },
      { name: 'Cheese curds', cost: '$284 over', pct: 14.2, reason: 'Unaccounted comps + family meal' },
      { name: 'Avocados', cost: '$196 over', pct: 9.4, reason: 'Discard from bruising (Tue delivery)' },
      { name: 'Heavy cream', cost: '$158 over', pct: 8.8, reason: 'Sauce yield below recipe' },
    ],
  },
  // Peer benchmark (k-anon, 28 GTA cafés)
  benchmark: {
    cohort: 'GTA cafés · $800k–$1.5M revenue · 28 peers',
    metrics: [
      { label: 'Food cost %', value: 28.4, percentile: 58, benchmark: 28.8, dir: 'lower-is-better' },
      { label: 'Waste %', value: 3.4, percentile: 24, benchmark: 2.4, dir: 'lower-is-better' },
      { label: 'Menu mix', value: '18 items', percentile: 72, benchmark: '22 items', dir: 'contextual' },
      { label: 'Repeat rate', value: '34%', percentile: 84, benchmark: '28%', dir: 'higher-is-better' },
    ],
  },
};

window.DATA = DATA;
