// Stock — inventory, waste log, variance bridge, suppliers.

function ScreenStock({ goto }) {
  const [tab, setTab] = React.useState('variance');

  return (
    <div style={{ paddingBottom: 100 }}>
      <LargeTitle title="Stock" eyebrow="Inventory · waste · variance"/>
      <div style={{ padding: '4px 16px 0' }}>
        <Segmented
          options={[
            { value: 'variance',  label: 'Variance' },
            { value: 'waste',     label: 'Waste' },
            { value: 'suppliers', label: 'Suppliers' },
          ]}
          value={tab} onChange={setTab}
        />
      </div>
      {tab === 'variance'  && <StockVariance/>}
      {tab === 'waste'     && <StockWaste goto={goto}/>}
      {tab === 'suppliers' && <StockSuppliers/>}
    </div>
  );
}

// ═══════════════ Variance ════════════════════════════════════════════════════
function StockVariance() {
  const v = DATA.variance;

  return (
    <div style={{ padding: '20px 16px 0' }}>
      {/* Hero variance */}
      <Card tone="red" pad={16}>
        <div style={{ fontSize: 11, color: T.red, fontWeight: 700, letterSpacing: 0.4 }}>
          FOOD COST VARIANCE · {v.period.toUpperCase()}
        </div>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, marginTop: 6 }}>
          <div style={{
            fontFamily: T.numeric, fontSize: 42, fontWeight: 700, color: T.text,
            letterSpacing: -1.8, lineHeight: 1, fontVariantNumeric: 'tabular-nums',
          }}>+{v.gap}%</div>
          <div style={{ fontSize: 14, color: T.red, fontWeight: 600 }}>· ${v.dollars}</div>
        </div>
        <div style={{ fontSize: 12.5, color: T.label, marginTop: 6, lineHeight: 1.5 }}>
          Theoretical food cost was <b style={{ color: T.text }}>{v.theoretical}%</b>. Actual came in
          at <b style={{ color: T.text }}>{v.actual}%</b>. That gap cost you <b style={{ color: T.red }}>${v.dollars}</b> this period.
        </div>
      </Card>

      {/* Bridge chart */}
      <SectionHeader title="WHERE IT WENT · WATERFALL"/>
      <Card pad={14}>
        <WaterfallChart data={v.breakdown} width={326} height={200}/>
      </Card>

      {/* Top offenders */}
      <SectionHeader title={`TOP OFFENDERS · ${v.topOffenders.length}`}/>
      <Card pad={0}>
        {v.topOffenders.map((o, i) => (
          <div key={i} style={{
            padding: 14, borderBottom: i < v.topOffenders.length - 1 ? `0.5px solid ${T.sep}` : 'none',
          }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
              <div style={{ flex: 1, paddingRight: 8 }}>
                <div style={{ fontSize: 14.5, fontWeight: 600, color: T.text, letterSpacing: -0.2 }}>{o.name}</div>
                <div style={{ fontSize: 12, color: T.sec, marginTop: 3, lineHeight: 1.4 }}>{o.reason}</div>
              </div>
              <Stat value={o.cost} delta={`${o.pct}% of gap`} deltaTone="red" size="sm" style={{ textAlign: 'right' }}/>
            </div>
            {/* Sparkline / bar */}
            <div style={{ marginTop: 10, height: 4, background: T.elev2, borderRadius: 2, overflow: 'hidden' }}>
              <div style={{ width: `${o.pct * 4.5}%`, height: '100%', background: T.red }}/>
            </div>
          </div>
        ))}
      </Card>

      <SectionHeader title="FIX THE PORTIONING"/>
      <Card tone="accent" pad={14}>
        <div style={{ fontSize: 15, fontWeight: 700, color: T.text, letterSpacing: -0.3, lineHeight: 1.3 }}>
          Chicken thighs avg 182g vs 160g spec
        </div>
        <div style={{ fontSize: 12.5, color: T.label, marginTop: 6, lineHeight: 1.4 }}>
          Retraining on 160g portion saves ~$412/month. Print scale guide for line cooks?
        </div>
        <div style={{ display: 'flex', gap: 8, marginTop: 12 }}>
          <Button kind="primary" size="sm">Print guide</Button>
          <Button kind="secondary" size="sm" style={{ width: 'auto', padding: '0 14px' }}>Assign to Jordan</Button>
        </div>
      </Card>
    </div>
  );
}

function WaterfallChart({ data, width, height }) {
  const pad = { l: 40, r: 10, t: 10, b: 28 };
  const iw = width - pad.l - pad.r;
  const ih = height - pad.t - pad.b;
  const barW = iw / data.length * 0.65;
  const gap = iw / data.length * 0.35;

  // Compute cumulative positions
  let cum = 0;
  const boxes = data.map((d, i) => {
    let box;
    if (d.type === 'start') {
      box = { base: 0, top: d.value };
      cum = d.value;
    } else if (d.type === 'end') {
      box = { base: 0, top: d.value };
      cum = d.value;
    } else {
      box = { base: cum + d.value, top: cum };
      cum = cum + d.value;
    }
    return { ...d, ...box };
  });

  const max = Math.max(...boxes.flatMap(b => [b.top, b.base])) * 1.05;
  const y = v => pad.t + ih - (v / max) * ih;

  return (
    <svg width={width} height={height}>
      {/* Axis grid */}
      {[0, 0.5, 1].map((p, i) => (
        <line key={i} x1={pad.l} x2={width - pad.r} y1={pad.t + ih * p} y2={pad.t + ih * p}
          stroke={T.sep} strokeDasharray={i === 2 ? 'none' : '2 3'} strokeWidth={0.5}/>
      ))}

      {boxes.map((b, i) => {
        const x = pad.l + (iw / data.length) * i + gap / 2;
        const color = b.type === 'start' ? T.label : b.type === 'end' ? T.ac : b.type === 'neg' ? T.red : T.green;
        const yTop = y(Math.max(b.top, b.base));
        const h = Math.abs(y(b.top) - y(b.base));

        return (
          <g key={i}>
            <rect x={x} y={yTop} width={barW} height={h} rx={3} fill={color} opacity={0.85}/>
            {/* Value label */}
            <text x={x + barW / 2} y={yTop - 4} fontSize="9" fontWeight="700"
              fill={color} textAnchor="middle" fontFamily="ui-monospace">
              {b.type === 'start' || b.type === 'end' ? `$${(b.value / 1000).toFixed(1)}k` : `${b.value}`}
            </text>
            {/* Label */}
            <text x={x + barW / 2} y={height - 10} fontSize="9" fill={T.sec} textAnchor="middle" fontWeight="500">
              {b.label}
            </text>
            {/* Connector line */}
            {i < boxes.length - 1 && boxes[i + 1].type !== 'end' && (
              <line x1={x + barW} x2={x + barW + gap}
                y1={y(boxes[i].type === 'start' ? boxes[i].top : boxes[i + 1].base)}
                y2={y(boxes[i].type === 'start' ? boxes[i].top : boxes[i + 1].base)}
                stroke={T.ter} strokeDasharray="2 2" strokeWidth={1}/>
            )}
          </g>
        );
      })}
    </svg>
  );
}

// ═══════════════ Waste ═══════════════════════════════════════════════════════
function StockWaste({ goto }) {
  const wasteLog = [
    { id: 'w1', item: 'Avocados (Hass)', qty: 3, unit: 'ea', reason: 'Bruised', cost: 6.42, by: 'Jordan', time: '6:22 AM' },
    { id: 'w2', item: 'Croissants', qty: 4, unit: 'ea', reason: 'Expired', cost: 8.80, by: 'Mei', time: 'Mon 9:14 PM' },
    { id: 'w3', item: 'Chicken thighs', qty: 0.4, unit: 'kg', reason: 'Spec error', cost: 4.72, by: 'Nate', time: 'Mon 2:36 PM' },
    { id: 'w4', item: 'Kale', qty: 0.8, unit: 'kg', reason: 'Wilted', cost: 3.20, by: 'Mei', time: 'Mon 8:22 AM' },
    { id: 'w5', item: 'Roma tomatoes', qty: 1.2, unit: 'kg', reason: 'Overripe', cost: 3.84, by: 'Jordan', time: 'Sun 5:18 PM' },
  ];
  const totalToday = 27.0;
  const trend = [14, 18, 22, 16, 24, 19, 27];

  return (
    <div style={{ padding: '20px 16px 0' }}>
      <Card pad={14}>
        <div style={{ display: 'flex', justifyContent: 'space-between' }}>
          <div>
            <div style={{ fontSize: 11, color: T.sec, fontWeight: 600, letterSpacing: 0.4 }}>WASTE TODAY</div>
            <div style={{
              fontFamily: T.numeric, fontSize: 32, fontWeight: 700, color: T.text,
              letterSpacing: -1.2, marginTop: 4, fontVariantNumeric: 'tabular-nums',
            }}>
              ${totalToday.toFixed(2)}
            </div>
            <div style={{ fontSize: 12, color: T.red, fontWeight: 600, marginTop: 2 }}>
              +42% vs 7-day avg
            </div>
          </div>
          <div style={{ width: 120 }}>
            <Sparkline data={trend} width={120} height={50} stroke={T.red} strokeWidth={2}/>
            <div style={{ fontSize: 10, color: T.ter, textAlign: 'right', marginTop: 2 }}>7 DAY TREND</div>
          </div>
        </div>
      </Card>

      <div style={{ marginTop: 14 }}>
        <Button kind="primary" size="md" icon="plus" onClick={() => goto('waste')}>
          Log waste
        </Button>
      </div>

      <SectionHeader title="TODAY'S LOG"/>
      <Card pad={0}>
        {wasteLog.map((w, i) => (
          <div key={w.id} style={{
            padding: 14, borderBottom: i < wasteLog.length - 1 ? `0.5px solid ${T.sep}` : 'none',
            display: 'flex', alignItems: 'center', gap: 12,
          }}>
            <div style={{
              width: 36, height: 36, borderRadius: 9, background: 'rgba(255,69,58,0.15)',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
            }}>
              <Icon name="trash" size={16} color={T.red}/>
            </div>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: 14, fontWeight: 600, color: T.text, letterSpacing: -0.2 }}>{w.item}</div>
              <div style={{ fontSize: 12, color: T.sec, marginTop: 1 }}>
                {w.qty} {w.unit} · {w.reason} · {w.by} · {w.time}
              </div>
            </div>
            <div style={{
              fontFamily: T.numeric, fontSize: 13, fontWeight: 700, color: T.red,
              fontVariantNumeric: 'tabular-nums',
            }}>-${w.cost.toFixed(2)}</div>
          </div>
        ))}
      </Card>

      <SectionHeader title="BY REASON · THIS WEEK"/>
      <Card pad={14}>
        <BarList data={[
          { label: 'Bruised / spoiled', value: 48.20, display: '$48.20' },
          { label: 'Portioning error',  value: 32.10, display: '$32.10' },
          { label: 'Expired',           value: 28.60, display: '$28.60' },
          { label: 'Wilted',            value: 18.40, display: '$18.40' },
          { label: 'Dropped / comp',    value: 12.80, display: '$12.80' },
        ]}/>
      </Card>
    </div>
  );
}

// ═══════════════ Waste Log Sheet ═════════════════════════════════════════════
function WasteLogScreen({ goto }) {
  const [reason, setReason] = React.useState('Bruised');
  const [item, setItem] = React.useState('Avocados (Hass)');
  const [qty, setQty] = React.useState(3);

  return (
    <div style={{ paddingBottom: 100 }}>
      <LargeTitle eyebrow="QUICK LOG" title="Log waste"/>

      <div style={{ padding: '0 16px' }}>
        <Card pad={14}>
          <FormRow label="Item">
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <Icon name="search" size={16} color={T.sec}/>
              <input value={item} onChange={e => setItem(e.target.value)}
                style={inputStyle}/>
            </div>
          </FormRow>
          <Divider/>
          <FormRow label="Quantity">
            <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
              <button onClick={() => setQty(Math.max(0, qty - 0.5))} style={{
                width: 36, height: 36, borderRadius: 10, background: T.elev2, border: 'none', cursor: 'pointer',
              }}><Icon name="minus" size={16} color={T.label}/></button>
              <div style={{
                flex: 1, fontFamily: T.numeric, fontSize: 28, fontWeight: 700, color: T.text,
                letterSpacing: -1, textAlign: 'center', fontVariantNumeric: 'tabular-nums',
              }}>{qty}</div>
              <button onClick={() => setQty(qty + 0.5)} style={{
                width: 36, height: 36, borderRadius: 10, background: T.elev2, border: 'none', cursor: 'pointer',
              }}><Icon name="plus" size={16} color={T.label}/></button>
            </div>
          </FormRow>
        </Card>
      </div>

      <SectionHeader title="REASON"/>
      <div style={{ padding: '0 16px' }}>
        <Card pad={0}>
          {['Bruised / spoiled', 'Portioning error', 'Expired', 'Wilted', 'Dropped / comp'].map((r, i, arr) => (
            <div key={r} onClick={() => setReason(r)} style={{
              padding: 14, display: 'flex', alignItems: 'center', gap: 10,
              borderBottom: i < arr.length - 1 ? `0.5px solid ${T.sep}` : 'none',
              cursor: 'pointer',
            }}>
              <div style={{
                width: 22, height: 22, borderRadius: 11,
                border: `2px solid ${reason === r ? T.ac : T.ter}`,
                display: 'flex', alignItems: 'center', justifyContent: 'center',
              }}>
                {reason === r && <div style={{ width: 10, height: 10, borderRadius: 5, background: T.ac }}/>}
              </div>
              <div style={{ flex: 1, fontSize: 15, color: T.text, fontWeight: 500 }}>{r}</div>
            </div>
          ))}
        </Card>
      </div>

      <div style={{ padding: 16 }}>
        <Button kind="primary" size="lg" icon="check" onClick={() => goto('stock')}>
          Log waste · -$6.42
        </Button>
        <div style={{ fontSize: 11, color: T.ter, textAlign: 'center', marginTop: 10 }}>
          Logged by Mei · deducts from inventory & food cost
        </div>
      </div>
    </div>
  );
}

function Divider() {
  return <div style={{ height: 0.5, background: T.sep, margin: '10px 0' }}/>;
}

function FormRow({ label, children }) {
  return (
    <div>
      <div style={{ fontSize: 11, color: T.sec, fontWeight: 600, letterSpacing: 0.4, textTransform: 'uppercase', marginBottom: 8 }}>
        {label}
      </div>
      {children}
    </div>
  );
}

const inputStyle = {
  flex: 1, background: 'transparent', border: 'none', color: T.text,
  fontSize: 16, fontWeight: 500, outline: 'none', fontFamily: T.sans,
  padding: '6px 0',
};

// ═══════════════ Suppliers ═══════════════════════════════════════════════════
function StockSuppliers() {
  return (
    <div style={{ padding: '20px 16px 0' }}>
      <Card tone="accent" pad={14}>
        <div style={{ fontSize: 11, color: T.ac, fontWeight: 700, letterSpacing: 0.4 }}>PRICE ALERT · 2 ITEMS</div>
        <div style={{ fontSize: 15, fontWeight: 700, color: T.text, marginTop: 4, letterSpacing: -0.3, lineHeight: 1.3 }}>
          OFT tomatoes up 12% MoM
        </div>
        <div style={{ fontSize: 12.5, color: T.sec, marginTop: 4, lineHeight: 1.4 }}>
          GFS has Roma at $2.84/kg vs OFT $3.20/kg. Switch could save <b style={{ color: T.green }}>$86/mo</b>.
        </div>
        <div style={{ display: 'flex', gap: 8, marginTop: 12 }}>
          <Button kind="primary" size="sm">Compare all</Button>
        </div>
      </Card>

      <SectionHeader title={`SUPPLIERS · ${DATA.suppliers.length}`}/>
      {DATA.suppliers.map((s, i) => (
        <SupplierCard key={s.id} s={s}/>
      ))}
    </div>
  );
}

function SupplierCard({ s }) {
  const isUp = s.change > 0;
  return (
    <div style={{
      background: T.elev1, borderRadius: T.radius, padding: 14, marginBottom: 10,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        <div style={{
          width: 44, height: 44, borderRadius: 10, background: T.elev2,
          display: 'flex', alignItems: 'center', justifyContent: 'center',
        }}>
          <Icon name="truck" size={20} color={T.label}/>
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 15, fontWeight: 700, color: T.text, letterSpacing: -0.24 }}>{s.name}</div>
          <div style={{ fontSize: 12, color: T.sec, marginTop: 1 }}>
            {s.rep || 'Walk-in'} · next {s.nextDeliver}
          </div>
        </div>
      </div>
      <div style={{ display: 'flex', marginTop: 12, paddingTop: 12, borderTop: `0.5px solid ${T.sep}` }}>
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 10, color: T.ter, fontWeight: 600, letterSpacing: 0.3 }}>SKUS</div>
          <div style={{ fontSize: 15, fontWeight: 700, color: T.text, fontFamily: T.numeric, marginTop: 2 }}>{s.skuCount}</div>
        </div>
        <div style={{ flex: 1, borderLeft: `0.5px solid ${T.sep}`, paddingLeft: 12 }}>
          <div style={{ fontSize: 10, color: T.ter, fontWeight: 600, letterSpacing: 0.3 }}>LAST ORDER</div>
          <div style={{ fontSize: 15, fontWeight: 700, color: T.text, marginTop: 2 }}>{s.lastOrder}</div>
        </div>
        <div style={{ flex: 1, borderLeft: `0.5px solid ${T.sep}`, paddingLeft: 12 }}>
          <div style={{ fontSize: 10, color: T.ter, fontWeight: 600, letterSpacing: 0.3 }}>PRICE TREND</div>
          <div style={{
            fontSize: 15, fontWeight: 700, marginTop: 2,
            color: isUp ? T.red : T.green, fontFamily: T.numeric,
          }}>
            {isUp ? '+' : ''}{s.change}%
          </div>
        </div>
      </div>
    </div>
  );
}

Object.assign(window, { ScreenStock, WasteLogScreen });
