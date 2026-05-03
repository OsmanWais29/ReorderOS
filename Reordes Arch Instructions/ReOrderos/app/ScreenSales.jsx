// Sales — the money screen. Menu engineering, opportunities, elasticity, dayparting.

function ScreenSales({ goto }) {
  const [tab, setTab] = React.useState('overview');
  const [selectedItem, setSelectedItem] = React.useState(DATA.menu[0]);
  const [selectedCell, setSelectedCell] = React.useState(null);

  return (
    <div style={{ paddingBottom: 100 }}>
      <LargeTitle title="Sales" eyebrow="Money · last 7 days"/>

      <div style={{ padding: '4px 16px 0' }}>
        <Segmented
          options={[
            { value: 'overview', label: 'Overview' },
            { value: 'menu',     label: 'Menu' },
            { value: 'dayparts', label: 'Dayparts' },
            { value: 'ops',      label: 'Moves' },
          ]}
          value={tab} onChange={setTab}
        />
      </div>

      {tab === 'overview' && <SalesOverview goto={goto}/>}
      {tab === 'menu' && <SalesMenu selected={selectedItem} onSelect={setSelectedItem}/>}
      {tab === 'dayparts' && <SalesDayparts selected={selectedCell} onSelect={setSelectedCell}/>}
      {tab === 'ops' && <SalesOps goto={goto}/>}
    </div>
  );
}

// ═══════════════ OVERVIEW ═══════════════════════════════════════════════════
function SalesOverview({ goto }) {
  const totalRev = 21284;
  const bestItem = DATA.menu[0];

  // Channel split
  const channels = [
    { label: 'Dine-in',  value: 11840, pct: 55.6, color: T.ac,     net: 11840 },
    { label: 'Takeout',  value: 5210,  pct: 24.5, color: T.blue,   net: 5210 },
    { label: 'DoorDash', value: 2640,  pct: 12.4, color: T.red,    net: 2040 },  // 23% fee
    { label: 'Uber Eats',value: 1594,  pct: 7.5,  color: T.purple, net: 1244 },
  ];

  return (
    <div style={{ padding: '20px 16px 0' }}>
      {/* Category mix donut + legend */}
      <Card>
        <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
          <Donut data={[
            { value: 38, color: T.ac },
            { value: 22, color: T.blue },
            { value: 18, color: T.purple },
            { value: 14, color: T.amber },
            { value: 8,  color: T.red },
          ]} size={120} thickness={14} center={
            <text x="60" y="56" textAnchor="middle" fontSize="22" fontWeight="700" fill={T.text}
              fontFamily={T.numeric}>$21.3k</text>
          }/>
          <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 7 }}>
            {[
              { c: T.ac, l: 'Mains', v: '38%', amt: '$8,090' },
              { c: T.blue, l: 'Drinks', v: '22%', amt: '$4,683' },
              { c: T.purple, l: 'Brunch', v: '18%', amt: '$3,831' },
              { c: T.amber, l: 'Sides', v: '14%', amt: '$2,980' },
              { c: T.red, l: 'Starters', v: '8%', amt: '$1,703' },
            ].map((d, i) => (
              <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <div style={{ width: 8, height: 8, borderRadius: 2, background: d.c }}/>
                <div style={{ flex: 1, fontSize: 13, color: T.label, fontWeight: 500 }}>{d.l}</div>
                <div style={{ fontSize: 13, color: T.text, fontWeight: 600, fontFamily: T.numeric }}>{d.v}</div>
              </div>
            ))}
          </div>
        </div>
      </Card>

      {/* Channels */}
      <SectionHeader title="CHANNELS"/>
      <Card pad={0} style={{ margin: '0 4px' }}>
        {channels.map((c, i) => (
          <div key={c.label} style={{
            padding: 14, borderBottom: i < channels.length - 1 ? `0.5px solid ${T.sep}` : 'none',
          }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <div style={{ width: 8, height: 8, borderRadius: 2, background: c.color }}/>
                <div style={{ fontSize: 14, fontWeight: 600, color: T.text }}>{c.label}</div>
                {c.value !== c.net && (
                  <Pill tone="red">-${(c.value - c.net).toFixed(0)} fees</Pill>
                )}
              </div>
              <div style={{ fontFamily: T.numeric, fontSize: 15, fontWeight: 700, color: T.text, fontVariantNumeric: 'tabular-nums' }}>
                ${c.value.toLocaleString()}
              </div>
            </div>
            <div style={{ height: 4, background: T.elev2, borderRadius: 2, overflow: 'hidden' }}>
              <div style={{ width: `${c.pct}%`, height: '100%', background: c.color, transition: 'width 1s' }}/>
            </div>
          </div>
        ))}
      </Card>

      {/* Top items bar list */}
      <SectionHeader title="TOP 5 · BY REVENUE" action={
        <button style={{ background: 'none', border: 'none', color: T.ac, fontSize: 13, fontWeight: 600, cursor: 'pointer' }}>
          See all 18 →
        </button>
      }/>
      <Card>
        <BarList data={[
          { label: 'Maple Latte',       value: 5811, display: '$5,811' },
          { label: 'Shawarma Poutine',  value: 6592, display: '$6,592' },
          { label: 'Butter Chicken',    value: 5508, display: '$5,508' },
          { label: 'Classic Poutine',   value: 4632, display: '$4,632' },
          { label: 'Iced Americano',    value: 3096, display: '$3,096' },
        ].sort((a,b) => b.value - a.value)} colorFn={(_, i) => [T.ac, T.blue, T.purple, T.amber, T.red][i]}/>
      </Card>

      {/* Star + Dog callout */}
      <SectionHeader title="THIS WEEK"/>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
        <Card tone="green" pad={14}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <Icon name="star" size={14} color={T.green}/>
            <div style={{ fontSize: 11, color: T.green, fontWeight: 700, letterSpacing: 0.4 }}>RISING</div>
          </div>
          <div style={{ fontSize: 16, fontWeight: 700, color: T.text, marginTop: 6, letterSpacing: -0.3 }}>
            Shawarma Poutine
          </div>
          <div style={{ fontSize: 12, color: T.sec, marginTop: 2 }}>412 sold · +48% WoW</div>
          <Stat value="$6,592" delta="+$2.1k" deltaTone="green" size="sm" style={{ marginTop: 8 }}/>
        </Card>
        <Card tone="red" pad={14}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <Icon name="trend-down" size={14} color={T.red}/>
            <div style={{ fontSize: 11, color: T.red, fontWeight: 700, letterSpacing: 0.4 }}>KILL</div>
          </div>
          <div style={{ fontSize: 16, fontWeight: 700, color: T.text, marginTop: 6, letterSpacing: -0.3 }}>
            Beet Hummus
          </div>
          <div style={{ fontSize: 12, color: T.sec, marginTop: 2 }}>26 sold · 42% margin</div>
          <Stat value="$286" delta="-$91" deltaTone="red" size="sm" style={{ marginTop: 8 }}/>
        </Card>
      </div>
    </div>
  );
}

// ═══════════════ MENU ENGINEERING ════════════════════════════════════════════
function SalesMenu({ selected, onSelect }) {
  return (
    <div style={{ padding: '20px 16px 0' }}>
      <div style={{ marginBottom: 10 }}>
        <div style={{ fontSize: 13, fontWeight: 600, color: T.sec, letterSpacing: 0.6, textTransform: 'uppercase' }}>
          Menu Engineering
        </div>
        <div style={{ fontSize: 12, color: T.sec, marginTop: 2, lineHeight: 1.4 }}>
          Popularity × margin.  Tap a dish.
        </div>
      </div>

      <Card pad={10} style={{ overflow: 'hidden' }}>
        <QuadrantChart
          items={DATA.menu}
          width={326} height={280}
          selected={selected}
          onSelect={onSelect}
        />
      </Card>

      {/* Selected item detail */}
      {selected && <MenuItemDetail item={selected}/>}

      {/* Legend */}
      <SectionHeader title="QUADRANT GUIDE"/>
      <Card pad={0}>
        <QuadRow color={T.green} label="Stars"      desc="High pop × high margin. Push these. Feature, bundle, hero."/>
        <QuadRow color={T.amber} label="Puzzles"    desc="Low pop × high margin. Reposition, re-photo, rename, promote."/>
        <QuadRow color={T.blue}  label="Plowhorses" desc="High pop × low margin. Raise price or rework recipe."/>
        <QuadRow color={T.red}   label="Dogs"       desc="Low pop × low margin. Remove unless strategic." last/>
      </Card>
    </div>
  );
}

function QuadRow({ color, label, desc, last }) {
  return (
    <div style={{
      padding: 14, display: 'flex', gap: 12, alignItems: 'flex-start',
      borderBottom: last ? 'none' : `0.5px solid ${T.sep}`,
    }}>
      <div style={{
        width: 10, height: 10, borderRadius: 5, background: color,
        marginTop: 5, flexShrink: 0,
      }}/>
      <div>
        <div style={{ fontSize: 14, fontWeight: 700, color: T.text }}>{label}</div>
        <div style={{ fontSize: 12.5, color: T.sec, marginTop: 2, lineHeight: 1.4 }}>{desc}</div>
      </div>
    </div>
  );
}

function MenuItemDetail({ item }) {
  // Mock elasticity curve centered on current price
  const p = item.price;
  const baseDemand = item.sold;
  // Rough linear-ish demand curve with elasticity ~1.2
  const elasticity = 1.3;
  const curve = [];
  for (let dp = -3; dp <= 3; dp += 0.5) {
    const newP = p + dp;
    const demand = baseDemand * Math.max(0, 1 - (dp / p) * elasticity);
    curve.push({ price: newP, demand });
  }
  const target = p + 1;
  const quadrant = item.popularity > 0.5 && item.margin > 0.5 ? 'Star'
                : item.popularity <= 0.5 && item.margin > 0.5 ? 'Puzzle'
                : item.popularity > 0.5 && item.margin <= 0.5 ? 'Plowhorse' : 'Dog';
  const tone = quadrant === 'Star' ? 'green' : quadrant === 'Puzzle' ? 'amber' : quadrant === 'Plowhorse' ? 'blue' : 'red';

  return (
    <>
      <SectionHeader title={`SELECTED · ${item.name.toUpperCase()}`}/>
      <Card tone={tone} pad={16}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
          <div>
            <div style={{ fontSize: 18, fontWeight: 700, color: T.text, letterSpacing: -0.4 }}>
              {item.name}
            </div>
            <div style={{ fontSize: 12, color: T.sec, marginTop: 2 }}>
              {item.category} · ${item.price} · {item.sold} sold this week
            </div>
          </div>
          <Pill tone={tone}>{quadrant.toUpperCase()}</Pill>
        </div>
        {/* Three mini stats */}
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 8, marginTop: 14 }}>
          <MiniStat label="Popularity" value={`${Math.round(item.popularity * 100)}%`}/>
          <MiniStat label="Margin" value={`${Math.round(item.margin * 100)}%`}/>
          <MiniStat label="Contribution" value={`$${(item.sold * item.price * item.margin).toFixed(0)}`}/>
        </div>
      </Card>

      {/* Elasticity */}
      <SectionHeader title="PRICE ELASTICITY"/>
      <Card pad={14}>
        <div style={{ fontSize: 13.5, color: T.label, lineHeight: 1.5, marginBottom: 10 }}>
          Raising <b style={{ color: T.text }}>${p} → ${target}</b> projects
          <b style={{ color: T.ac }}> +${Math.round((target - p) * item.sold * 52 * 0.92).toLocaleString()}/yr</b> with
          <b style={{ color: T.amber }}> ~8% demand risk</b>.
        </div>
        <ElasticityChart currentPrice={p} targetPrice={target} data={curve} width={326}/>
        <div style={{ display: 'flex', gap: 8, marginTop: 12 }}>
          <Button kind="primary" size="sm">Apply price change</Button>
          <Button kind="secondary" size="sm" style={{ width: 'auto', padding: '0 14px' }}>A/B test</Button>
        </div>
      </Card>

      {/* Action plan */}
      <SectionHeader title="ACTION PLAN · CLAUDE"/>
      <Card pad={14}>
        {[
          { icon: 'check-circle', color: T.green, title: `Feature ${item.name} on the home board this week`, sub: 'Handwritten chalk at the entrance' },
          { icon: 'sparkles', color: T.ac, title: 'Bundle with highest-margin drink', sub: `Pair with Maple Latte at $${(item.price + 4).toFixed(0)} — saves $2, lifts both` },
          { icon: 'camera', color: T.purple, title: 'Re-shoot the photo', sub: 'Current IG grid pic is from Nov · lighting is off' },
        ].map((a, i, arr) => (
          <div key={i} style={{
            display: 'flex', gap: 12, padding: i === 0 ? '0 0 12px' : '12px 0',
            borderTop: i > 0 ? `0.5px solid ${T.sep}` : 'none',
            borderBottom: i === arr.length - 1 ? 'none' : undefined,
          }}>
            <div style={{
              width: 28, height: 28, borderRadius: 7, background: `${a.color}26`,
              display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0,
            }}>
              <Icon name={a.icon} size={15} color={a.color}/>
            </div>
            <div>
              <div style={{ fontSize: 14, fontWeight: 600, color: T.text, letterSpacing: -0.2 }}>{a.title}</div>
              <div style={{ fontSize: 12, color: T.sec, marginTop: 1 }}>{a.sub}</div>
            </div>
          </div>
        ))}
      </Card>
    </>
  );
}

function MiniStat({ label, value }) {
  return (
    <div style={{
      padding: 10, background: 'rgba(0,0,0,0.25)', borderRadius: 9,
    }}>
      <div style={{ fontSize: 10, color: T.sec, fontWeight: 500, letterSpacing: 0.2, textTransform: 'uppercase' }}>{label}</div>
      <div style={{
        fontFamily: T.numeric, fontSize: 18, fontWeight: 700, color: T.text,
        letterSpacing: -0.6, marginTop: 2, fontVariantNumeric: 'tabular-nums',
      }}>{value}</div>
    </div>
  );
}

// ═══════════════ DAYPARTS ════════════════════════════════════════════════════
function SalesDayparts({ selected, onSelect }) {
  const sel = selected;
  const days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  const selValue = sel ? DATA.heatmap[sel.day][sel.hour] : null;
  const hourLabel = sel ? (sel.hour + 6 < 12 ? `${sel.hour + 6} AM` : sel.hour + 6 === 12 ? '12 PM' : `${sel.hour + 6 - 12} PM`) : null;

  return (
    <div style={{ padding: '20px 16px 0' }}>
      <div style={{ fontSize: 13, fontWeight: 600, color: T.sec, letterSpacing: 0.6, textTransform: 'uppercase', marginBottom: 10 }}>
        Heatmap · 7 days × 16 hours
      </div>
      <Card pad={14}>
        <Heatmap data={DATA.heatmap} width={326}
          onCell={(d, h) => onSelect({ day: d, hour: h })}
          selected={sel}/>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 12, paddingTop: 10, borderTop: `0.5px solid ${T.sep}` }}>
          <div style={{ fontSize: 11, color: T.sec }}>Volume</div>
          {[0.05, 0.18, 0.36, 0.6, 0.92].map((op, i) => (
            <div key={i} style={{ width: 20, height: 10, borderRadius: 2, background: `rgba(52,211,153,${op})` }}/>
          ))}
          <div style={{ fontSize: 11, color: T.sec, marginLeft: 'auto' }}>Low → Peak</div>
        </div>
      </Card>

      {sel && (
        <>
          <SectionHeader title={`${days[sel.day].toUpperCase()} · ${hourLabel}`}/>
          <Card pad={14}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
              <Stat label="Covers" value={selValue} size="md"/>
              <Stat label="Revenue" value={`$${(selValue * 19.5).toFixed(0)}`} size="md"/>
            </div>
          </Card>
        </>
      )}

      {/* Dead zone */}
      <SectionHeader title="DEAD ZONES"/>
      <Card tone="amber" pad={14}>
        <div style={{ fontSize: 16, fontWeight: 700, color: T.text, letterSpacing: -0.3, lineHeight: 1.3 }}>
          Tue–Thu, 2–4 PM
        </div>
        <div style={{ fontSize: 12.5, color: T.label, marginTop: 4, lineHeight: 1.4 }}>
          Flat zone — avg 22 covers/hour vs 68 for Fri same hour.
          Office workers within 400m (MaRS, TMU) are potential fit.
        </div>
        <div style={{ marginTop: 10, display: 'flex', gap: 8 }}>
          <Button kind="primary" size="sm">Run office lunch promo</Button>
        </div>
      </Card>

      <SectionHeader title="PEAK HOURS · STAFF ADEQUATELY"/>
      <Card pad={14}>
        <Stat label="Sat 12–1 PM" value="98 covers/hr" delta="+28% vs avg" size="md"/>
        <div style={{ fontSize: 12.5, color: T.sec, marginTop: 8, lineHeight: 1.4 }}>
          2 cooks + 2 FOH per hour historically drops ticket time 3:40 → 4:20. Consider a 3rd cook 11–2.
        </div>
      </Card>

      {/* Weather correlation */}
      <SectionHeader title="WEATHER SENSITIVITY"/>
      <Card pad={14}>
        <div style={{ display: 'flex', gap: 10, marginBottom: 10 }}>
          {[
            { icon: 'drop', label: 'Rain', delta: '-26%', dir: 'dinein', tone: 'red' },
            { icon: 'drop', label: 'Rain', delta: '+62%', dir: 'delivery', tone: 'green' },
            { icon: 'flame', label: 'Heat>28°', delta: '+18%', dir: 'patio', tone: 'green' },
            { icon: 'snowflake', label: 'Snow', delta: '-42%', dir: 'overall', tone: 'red' },
          ].map((w, i) => (
            <div key={i} style={{ flex: 1, padding: 10, background: T.elev2, borderRadius: 10, textAlign: 'center' }}>
              <Icon name={w.icon} size={16} color={w.tone === 'green' ? T.green : T.red}/>
              <div style={{ fontSize: 10, color: T.sec, fontWeight: 600, marginTop: 4 }}>{w.label}</div>
              <div style={{
                fontFamily: T.numeric, fontSize: 14, fontWeight: 700,
                color: w.tone === 'green' ? T.green : T.red, marginTop: 2,
              }}>{w.delta}</div>
              <div style={{ fontSize: 9, color: T.ter, marginTop: 1 }}>{w.dir}</div>
            </div>
          ))}
        </div>
        <div style={{ fontSize: 12, color: T.sec, lineHeight: 1.4 }}>
          Pulled from <b>Environment Canada</b> × 14mo POS history.  Push delivery on rain days, patio on hot days.
        </div>
      </Card>
    </div>
  );
}

// ═══════════════ OPPORTUNITIES / OPS ═════════════════════════════════════════
function SalesOps({ goto }) {
  return (
    <div style={{ padding: '20px 16px 0' }}>
      <div style={{ fontSize: 13, fontWeight: 600, color: T.sec, letterSpacing: 0.6, textTransform: 'uppercase', marginBottom: 10 }}>
        Ranked Moves · projected $ lift
      </div>

      {/* Summary */}
      <Card tone="accent" pad={16} style={{ marginBottom: 14 }}>
        <div style={{ fontSize: 11, color: T.ac, fontWeight: 700, letterSpacing: 0.4 }}>
          IF YOU RUN ALL 6 MOVES
        </div>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 12, marginTop: 4 }}>
          <div style={{
            fontFamily: T.numeric, fontSize: 38, fontWeight: 700, color: T.text,
            letterSpacing: -1.4, lineHeight: 1, fontVariantNumeric: 'tabular-nums',
          }}>+$40.5k</div>
          <div style={{ fontSize: 13, color: T.ac, fontWeight: 600 }}>/ year</div>
        </div>
        <div style={{ fontSize: 12.5, color: T.label, marginTop: 6, lineHeight: 1.4 }}>
          Revenue uplift at current cover trends.  Based on peer-cohort benchmarks (28 GTA cafés).
        </div>
      </Card>

      {DATA.opportunities.map((op, i) => (
        <OpportunityCard key={op.id} op={op} rank={i + 1}/>
      ))}
    </div>
  );
}

function OpportunityCard({ op, rank }) {
  const [expanded, setExpanded] = React.useState(rank === 1);
  return (
    <div style={{
      background: T.elev1, borderRadius: T.radius, marginBottom: 10, overflow: 'hidden',
    }}>
      <div onClick={() => setExpanded(!expanded)} style={{
        padding: 14, display: 'flex', gap: 12, alignItems: 'flex-start', cursor: 'pointer',
      }}>
        <div style={{
          width: 36, height: 36, borderRadius: 9, background: `${tonePri(op.tone)}26`,
          display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0,
        }}>
          <Icon name={op.icon} size={18} color={tonePri(op.tone)}/>
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 3 }}>
            <div style={{ fontSize: 10, fontWeight: 700, color: T.ter, letterSpacing: 0.4, fontFamily: T.numeric }}>
              #{rank}
            </div>
            <Pill tone={op.tone}>{op.projection}</Pill>
          </div>
          <div style={{ fontSize: 15, fontWeight: 600, color: T.text, letterSpacing: -0.24, lineHeight: 1.3 }}>
            {op.title}
          </div>
          <div style={{ fontSize: 12.5, color: T.sec, marginTop: 3, lineHeight: 1.4 }}>{op.sub}</div>
        </div>
        <Icon name={expanded ? 'chevron-up' : 'chevron-down'} size={16} color={T.ter} style={{ marginTop: 10 }}/>
      </div>
      {expanded && (
        <div style={{ padding: '0 14px 14px', borderTop: `0.5px solid ${T.sep}` }}>
          <div style={{ fontSize: 13, color: T.label, marginTop: 12, lineHeight: 1.5 }}>
            {op.detail}
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 12 }}>
            <div style={{ fontSize: 11, color: T.sec }}>Confidence</div>
            <div style={{ flex: 1, maxWidth: 100, height: 4, background: T.elev2, borderRadius: 2, overflow: 'hidden' }}>
              <div style={{ width: `${op.confidence * 100}%`, height: '100%', background: tonePri(op.tone) }}/>
            </div>
            <div style={{ fontSize: 11, color: tonePri(op.tone), fontWeight: 600, fontFamily: T.numeric }}>
              {Math.round(op.confidence * 100)}%
            </div>
          </div>
          <div style={{ display: 'flex', gap: 8, marginTop: 12 }}>
            <Button kind="primary" size="sm">Run this</Button>
            <Button kind="secondary" size="sm" style={{ width: 'auto', padding: '0 14px' }}>Show math</Button>
          </div>
        </div>
      )}
    </div>
  );
}

function tonePri(tone) {
  return tone === 'green' ? T.green : tone === 'blue' ? T.blue : tone === 'amber' ? T.amber
       : tone === 'red' ? T.red : tone === 'purple' ? T.purple : T.ac;
}

Object.assign(window, { ScreenSales });
