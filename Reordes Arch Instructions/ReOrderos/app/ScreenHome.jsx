// Home — dashboard. Revenue hero, today's briefing, at-risk peek, heatmap peek.

function ScreenHome({ goto }) {
  const [period, setPeriod] = React.useState('14D');
  const t = DATA.today;

  return (
    <div style={{ paddingBottom: 100 }}>
      {/* Top bar — greeting + avatar/search */}
      <div style={{ padding: '56px 20px 0', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div>
          <div style={{ fontSize: 13, color: T.sec, fontWeight: 500, letterSpacing: -0.08 }}>
            {t.date}  ·  {t.weather}
          </div>
          <div style={{ fontSize: 20, color: T.text, fontWeight: 700, letterSpacing: -0.4, marginTop: 2 }}>
            Good morning, Mei
          </div>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <button onClick={() => goto('search')} style={{
            width: 36, height: 36, borderRadius: 18, border: 'none',
            background: T.elev1, display: 'flex', alignItems: 'center', justifyContent: 'center', cursor: 'pointer',
          }}>
            <Icon name="search" size={18} color={T.label}/>
          </button>
          <button onClick={() => goto('notifications')} style={{
            width: 36, height: 36, borderRadius: 18, border: 'none', position: 'relative',
            background: T.elev1, display: 'flex', alignItems: 'center', justifyContent: 'center', cursor: 'pointer',
          }}>
            <Icon name="bell" size={18} color={T.label}/>
            <div style={{ position: 'absolute', top: 8, right: 9, width: 8, height: 8, borderRadius: 4, background: T.red, border: `2px solid ${T.elev1}` }}/>
          </button>
        </div>
      </div>

      {/* Revenue hero */}
      <div style={{ padding: '18px 20px 0' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <div style={{ fontSize: 13, color: T.sec, fontWeight: 500 }}>Revenue · this week</div>
          <Pill tone="green">
            <Icon name="trend-up" size={10} color={T.green}/> +12.4% WoW
          </Pill>
        </div>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginTop: 4 }}>
          <div style={{
            fontFamily: T.numeric, fontSize: 44, fontWeight: 700, color: T.text,
            letterSpacing: -1.8, lineHeight: 1.02, fontVariantNumeric: 'tabular-nums',
          }}>$21,284</div>
          <div style={{ fontSize: 15, color: T.green, fontWeight: 600 }}>+$2,347</div>
        </div>
        <div style={{ fontSize: 12, color: T.ter, marginTop: 2 }}>
          Forecast next 7d: <span style={{ color: T.label, fontWeight: 500 }}>$27.6k</span> · ±8%
        </div>
      </div>

      {/* Big chart */}
      <div style={{ margin: '14px 20px 0' }}>
        <RevenueLineChart data={DATA.revenueSeries} width={358} height={164}/>
        <div style={{ marginTop: 10 }}>
          <Segmented options={[{value:'7D',label:'7D'},{value:'14D',label:'14D'},{value:'MTD',label:'MTD'},{value:'90D',label:'90D'}]}
            value={period} onChange={setPeriod}/>
        </div>
      </div>

      {/* 4 metric tiles */}
      <div style={{
        margin: '20px 20px 0', display: 'grid',
        gridTemplateColumns: '1fr 1fr', gap: 10,
      }}>
        <MetricTile label="Covers today" value={t.covers} spark={[120,145,160,140,155,168]}
          delta="+14%" tone="green" onClick={() => goto('sales')}/>
        <MetricTile label="Food cost" value={`${t.foodCost}%`} spark={[30,29,28,30,27,27]}
          delta="-1.3%" tone="green" onClick={() => goto('variance')}/>
        <MetricTile label="At-risk SKUs" value="5" spark={[2,2,3,3,4,5]}
          delta="+2 today" tone="red" onClick={() => goto('orders')}/>
        <MetricTile label="Labor %" value={`${t.labor}%`} spark={[31,30,32,30,29,30]}
          delta="on target" tone="neutral" onClick={() => goto('sales')}/>
      </div>

      {/* Critical briefing */}
      <div style={{ margin: '22px 20px 0' }}>
        <div style={{ fontSize: 13, fontWeight: 600, color: T.red, letterSpacing: 0.6, textTransform: 'uppercase', marginBottom: 8 }}>
          <Icon name="alert" size={12} color={T.red} style={{ verticalAlign: -2, marginRight: 4 }}/>
          Act today · 2 critical
        </div>
        <Card tone="red" pad={0} onClick={() => goto('orders')}>
          <div style={{ padding: 16 }}>
            <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 12 }}>
              <div style={{ flex: 1 }}>
                <div style={{ fontSize: 17, fontWeight: 600, color: T.text, letterSpacing: -0.32, lineHeight: 1.25 }}>
                  You'll run out of avocados Thursday.
                </div>
                <div style={{ fontSize: 13, color: T.label, marginTop: 6, lineHeight: 1.4 }}>
                  GFS delivers Wed & Fri. Order by <b style={{ color: T.text }}>4 PM today</b> or you're making Thursday brunch without guac.
                </div>
              </div>
              <div style={{ width: 58, height: 58, borderRadius: 12, background: 'rgba(255,69,58,0.2)',
                display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>
                <div style={{ fontFamily: T.numeric, fontSize: 22, fontWeight: 800, color: T.red, lineHeight: 1 }}>48</div>
                <div style={{ fontSize: 9, color: T.red, fontWeight: 700, letterSpacing: 0.4 }}>HOURS</div>
              </div>
            </div>
            <div style={{ marginTop: 14, display: 'flex', gap: 8 }}>
              <Button kind="primary" size="sm" onClick={(e) => { e.stopPropagation(); goto('po'); }}>
                Review order · $341
              </Button>
              <Button kind="secondary" size="sm" style={{ width: 'auto', padding: '0 14px' }} onClick={(e) => { e.stopPropagation(); goto('orders'); }}>
                All 5 at-risk
              </Button>
            </div>
          </div>
        </Card>
      </div>

      {/* Today's opportunity */}
      <div style={{ margin: '22px 20px 0' }}>
        <div style={{ fontSize: 13, fontWeight: 600, color: T.ac, letterSpacing: 0.6, textTransform: 'uppercase', marginBottom: 8 }}>
          <Icon name="sparkles" size={12} color={T.ac} style={{ verticalAlign: -2, marginRight: 4 }}/>
          Today's opportunity
        </div>
        <Card tone="accent" onClick={() => goto('sales')}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <div style={{ fontSize: 11, color: T.sec, fontWeight: 500, letterSpacing: 0.2 }}>RAIN FORECAST · PATIO -40%</div>
            <Icon name="drop" size={16} color={T.blue}/>
          </div>
          <div style={{ fontSize: 17, fontWeight: 600, color: T.text, letterSpacing: -0.32, marginTop: 6, lineHeight: 1.3 }}>
            Push a 15% delivery code today.
          </div>
          <div style={{ fontSize: 13, color: T.label, marginTop: 4, lineHeight: 1.4 }}>
            Rain days historically spike delivery +62%. A 15% promo 11 AM–8 PM nets a projected <b style={{ color: T.ac }}>+$480 today</b>.
          </div>
          <div style={{ marginTop: 12, display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
              <div style={{ fontSize: 11, color: T.sec }}>Confidence</div>
              <div style={{ width: 48, height: 4, background: T.elev2, borderRadius: 2, overflow: 'hidden' }}>
                <div style={{ width: '71%', height: '100%', background: T.ac }}/>
              </div>
              <div style={{ fontSize: 11, color: T.ac, fontWeight: 600, fontFamily: T.numeric }}>71%</div>
            </div>
            <div style={{ fontSize: 13, color: T.ac, fontWeight: 600, display: 'flex', alignItems: 'center', gap: 2 }}>
              6 more  <Icon name="chevron-right" size={14} color={T.ac}/>
            </div>
          </div>
        </Card>
      </div>

      {/* Live pulse — dayparting peek */}
      <div style={{ margin: '22px 20px 0' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: T.sec, letterSpacing: 0.6, textTransform: 'uppercase' }}>
            Sales by hour · last 7 days
          </div>
          <button onClick={() => goto('sales')} style={{ background: 'none', border: 'none', color: T.ac, fontSize: 13, fontWeight: 600, cursor: 'pointer' }}>
            Explore
          </button>
        </div>
        <Card pad={14}>
          <Heatmap data={DATA.heatmap} width={330}/>
          <div style={{ marginTop: 10, padding: 10, background: T.amberSoft, borderRadius: 8,
            fontSize: 12.5, color: T.label, lineHeight: 1.4 }}>
            <b style={{ color: T.amber }}>Tue–Thu 2–4 PM is a dead zone.</b> A lunch bundle could add +$9.4k/yr.
          </div>
        </Card>
      </div>

      {/* Shortcuts */}
      <div style={{ margin: '22px 20px 0' }}>
        <div style={{ fontSize: 13, fontWeight: 600, color: T.sec, letterSpacing: 0.6, textTransform: 'uppercase', marginBottom: 8 }}>
          Shortcuts
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
          <ShortcutTile icon="scan" label="Scan invoice" color={T.blue} onClick={() => goto('invoice')}/>
          <ShortcutTile icon="trash" label="Log waste" color={T.red} onClick={() => goto('waste')}/>
          <ShortcutTile icon="book" label="Recipes" color={T.purple} onClick={() => goto('recipes')}/>
          <ShortcutTile icon="message" label="Ask Claude" color={T.ac} onClick={() => goto('chat')}/>
        </div>
      </div>
    </div>
  );
}

function MetricTile({ label, value, spark, delta, tone = 'green', onClick }) {
  const col = tone === 'red' ? T.red : tone === 'green' ? T.green : tone === 'amber' ? T.amber : T.sec;
  return (
    <div onClick={onClick} style={{
      background: T.elev1, borderRadius: T.radius, padding: 14,
      cursor: onClick ? 'pointer' : 'default',
    }}>
      <div style={{ fontSize: 12, color: T.sec, fontWeight: 500, letterSpacing: -0.08 }}>{label}</div>
      <div style={{ display: 'flex', alignItems: 'flex-end', justifyContent: 'space-between', marginTop: 4 }}>
        <div style={{
          fontFamily: T.numeric, fontSize: 24, fontWeight: 700, color: T.text,
          letterSpacing: -0.8, lineHeight: 1, fontVariantNumeric: 'tabular-nums',
        }}>{value}</div>
        <Sparkline data={spark} width={48} height={20} stroke={col} strokeWidth={1.6}/>
      </div>
      <div style={{ fontSize: 11, color: col, fontWeight: 600, marginTop: 6 }}>{delta}</div>
    </div>
  );
}

function ShortcutTile({ icon, label, color, onClick }) {
  return (
    <button onClick={onClick} style={{
      background: T.elev1, border: 'none', borderRadius: T.radius, padding: 14,
      display: 'flex', alignItems: 'center', gap: 10, cursor: 'pointer', textAlign: 'left',
    }}>
      <div style={{
        width: 34, height: 34, borderRadius: 9,
        background: `${color}26`, display: 'flex', alignItems: 'center', justifyContent: 'center',
      }}>
        <Icon name={icon} size={18} color={color}/>
      </div>
      <div style={{ fontSize: 14, fontWeight: 600, color: T.text, letterSpacing: -0.2 }}>{label}</div>
    </button>
  );
}

Object.assign(window, { ScreenHome });
