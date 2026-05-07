// Orders — at-risk list, item detail with burndown, PO review, PIN, send.

function ScreenOrders({ goto }) {
  const [tab, setTab] = React.useState('risk');

  return (
    <div style={{ paddingBottom: 100 }}>
      <LargeTitle title="Orders"
        eyebrow={`${DATA.atRisk.length} at-risk · 1 draft PO`}/>

      <div style={{ padding: '4px 16px 0' }}>
        <Segmented
          options={[
            { value: 'risk',    label: 'At risk' },
            { value: 'drafts',  label: 'Drafts' },
            { value: 'history', label: 'History' },
          ]}
          value={tab} onChange={setTab}
        />
      </div>

      {tab === 'risk' && <OrdersRiskList goto={goto}/>}
      {tab === 'drafts' && <OrdersDrafts goto={goto}/>}
      {tab === 'history' && <OrdersHistory/>}
    </div>
  );
}

function OrdersRiskList({ goto }) {
  const crit = DATA.atRisk.filter(r => r.severity === 'critical');
  const warn = DATA.atRisk.filter(r => r.severity === 'warning');

  return (
    <div style={{ padding: '16px 16px 0' }}>
      {/* Summary tile */}
      <Card tone="red" pad={14}>
        <div style={{ display: 'flex', justifyContent: 'space-between' }}>
          <div>
            <div style={{ fontSize: 11, color: T.red, fontWeight: 700, letterSpacing: 0.4 }}>CRITICAL · NEXT 48H</div>
            <div style={{ fontSize: 22, fontWeight: 700, color: T.text, marginTop: 4, letterSpacing: -0.5 }}>
              {crit.length} items will stock out
            </div>
            <div style={{ fontSize: 12.5, color: T.label, marginTop: 2 }}>
              GFS delivers Wed & Fri · Order by 4 PM today
            </div>
          </div>
          <Icon name="alert" size={36} color={T.red} style={{ opacity: 0.6 }}/>
        </div>
        <div style={{ display: 'flex', gap: 8, marginTop: 12 }}>
          <Button kind="primary" size="sm" onClick={() => goto('po')}>
            Review draft PO · $341
          </Button>
        </div>
      </Card>

      <SectionHeader title={`CRITICAL · ${crit.length}`}/>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {crit.map(it => (
          <RiskRow key={it.id} item={it} onClick={() => goto('item-detail', it)}/>
        ))}
      </div>

      <SectionHeader title={`WARNING · ${warn.length}`}/>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {warn.map(it => (
          <RiskRow key={it.id} item={it} onClick={() => goto('item-detail', it)}/>
        ))}
      </div>
    </div>
  );
}

function RiskRow({ item, onClick }) {
  const isCrit = item.severity === 'critical';
  const color = isCrit ? T.red : T.amber;
  const pct = Math.min(1, item.stock / item.par);
  return (
    <div onClick={onClick} style={{
      background: T.elev1, borderRadius: T.radius, padding: 14, cursor: 'pointer',
    }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 12 }}>
        <div style={{
          width: 48, height: 48, borderRadius: 11, background: `${color}22`,
          display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', flexShrink: 0,
        }}>
          <div style={{ fontFamily: T.numeric, fontSize: 16, fontWeight: 800, color, lineHeight: 1 }}>
            {item.stockoutIn.replace('h','').replace('d','')}
          </div>
          <div style={{ fontSize: 8, color, fontWeight: 700, letterSpacing: 0.3 }}>
            {item.stockoutIn.includes('h') ? 'HOURS' : 'DAYS'}
          </div>
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
            <div style={{ fontSize: 15, fontWeight: 600, color: T.text, letterSpacing: -0.24 }}>{item.name}</div>
            <div style={{ fontSize: 12, color: T.ter, fontFamily: T.mono }}>{item.sku}</div>
          </div>
          <div style={{ fontSize: 12, color: T.sec, marginTop: 2 }}>
            <span style={{ color: T.label, fontWeight: 600 }}>{item.stock}</span>
            <span> / {item.par} {item.unit}</span>
            <span style={{ color: T.ter }}> · </span>
            <span>{item.supplier}</span>
            <span style={{ color: T.ter }}> · out {item.stockoutDay}</span>
          </div>
          {/* Mini gauge */}
          <div style={{ marginTop: 8, height: 5, background: T.elev2, borderRadius: 3, overflow: 'hidden' }}>
            <div style={{ width: `${pct * 100}%`, height: '100%', background: color, transition: 'width 1s' }}/>
          </div>
        </div>
      </div>
    </div>
  );
}

function OrdersDrafts({ goto }) {
  const po = DATA.draftPO;
  const total = po.items.reduce((s, i) => s + i.qty * i.cost, 0);
  return (
    <div style={{ padding: '16px 16px 0' }}>
      <Card onClick={() => goto('po')} pad={14}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <div>
            <div style={{ fontSize: 11, color: T.ac, fontWeight: 700, letterSpacing: 0.4 }}>
              DRAFT · READY TO SEND
            </div>
            <div style={{ fontSize: 18, fontWeight: 700, color: T.text, marginTop: 3, letterSpacing: -0.4 }}>
              {po.number}
            </div>
            <div style={{ fontSize: 12.5, color: T.sec, marginTop: 2 }}>
              {po.supplier} · {po.items.length} items · deliver {po.deliverDate}
            </div>
          </div>
          <Stat value={`$${Math.round(total).toLocaleString()}`} size="sm" style={{ textAlign: 'right' }}/>
        </div>
      </Card>
    </div>
  );
}

function OrdersHistory() {
  const past = [
    { n: 'PO-2026-0041', supplier: 'GFS', date: 'Apr 18', amt: 412, status: 'Delivered' },
    { n: 'PO-2026-0040', supplier: 'OFT', date: 'Apr 17', amt: 184, status: 'Delivered' },
    { n: 'PO-2026-0039', supplier: 'Sysco', date: 'Apr 15', amt: 828, status: 'Delivered · $14 short' },
    { n: 'PO-2026-0038', supplier: 'GFS', date: 'Apr 14', amt: 356, status: 'Delivered' },
    { n: 'PO-2026-0037', supplier: 'Mario', date: 'Apr 11', amt: 242, status: 'Delivered' },
  ];
  return (
    <div style={{ padding: '16px 16px 0' }}>
      <List>
        {past.map((p, i) => (
          <ListRow key={p.n} icon="file" title={p.n} subtitle={`${p.supplier} · ${p.date} · ${p.status}`}
            detail={`$${p.amt}`} isLast={i === past.length - 1}/>
        ))}
      </List>
    </div>
  );
}

// ═══════════════ Item Detail ═════════════════════════════════════════════════
function ScreenItemDetail({ item, goto }) {
  if (!item) return null;
  const isCrit = item.severity === 'critical';
  const color = isCrit ? T.red : T.amber;
  const stockoutDay = item.burndown.findIndex(d => d.forecast === 0);

  return (
    <div style={{ paddingBottom: 100 }}>
      <LargeTitle eyebrow={`${item.severity.toUpperCase()} · STOCK OUT ${item.stockoutIn.toUpperCase()}`} title={item.name}/>

      {/* Hero stock gauge */}
      <div style={{ padding: '0 16px' }}>
        <Card pad={16}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-end' }}>
            <Stat label="Stock on hand" value={`${item.stock}`} delta={item.unit} deltaTone="green" size="lg"/>
            <Stat label="Par level" value={`${item.par}`} delta={item.unit} deltaTone="green" size="md"/>
          </div>
          <div style={{ marginTop: 16, height: 10, background: T.elev2, borderRadius: 5, overflow: 'hidden', position: 'relative' }}>
            <div style={{ width: `${(item.stock / item.par) * 100}%`, height: '100%', background: color, borderRadius: 5 }}/>
          </div>
          <div style={{ fontSize: 12, color: T.sec, marginTop: 8 }}>
            {Math.round((item.stock / item.par) * 100)}% of par · {item.cover}d cover at current velocity
          </div>
        </Card>
      </div>

      <SectionHeader title="BURNDOWN · 14 DAYS"/>
      <div style={{ padding: '0 16px' }}>
        <Card pad={14}>
          <BurndownChart data={item.burndown} width={326} height={150} stockoutDay={stockoutDay !== -1 ? stockoutDay : null}/>
          <div style={{ display: 'flex', gap: 16, marginTop: 10, paddingTop: 10, borderTop: `0.5px solid ${T.sep}` }}>
            <LegendDot color={T.ac} label="Actual stock"/>
            <LegendDot color={T.red} label="Forecast" dashed/>
            <LegendDot color={T.amber} label="Par" dashed/>
          </div>
        </Card>
      </div>

      <SectionHeader title="WHY THIS IS HAPPENING"/>
      <div style={{ padding: '0 16px' }}>
        <Card pad={0}>
          {item.why.map((w, i) => (
            <div key={i} style={{
              padding: 14, display: 'flex', gap: 10, alignItems: 'flex-start',
              borderBottom: i < item.why.length - 1 ? `0.5px solid ${T.sep}` : 'none',
            }}>
              <div style={{ width: 20, height: 20, borderRadius: 10, background: T.acSoft,
                display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>
                <div style={{ fontSize: 11, fontWeight: 700, color: T.ac, fontFamily: T.numeric }}>{i + 1}</div>
              </div>
              <div style={{ fontSize: 13.5, color: T.label, lineHeight: 1.4 }}>{w}</div>
            </div>
          ))}
        </Card>
      </div>

      <SectionHeader title="CONSUMPTION · 14 DAYS"/>
      <div style={{ padding: '0 16px' }}>
        <Card pad={14}>
          <MiniBars data={item.consumption} color={T.ac}/>
          <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 10 }}>
            <Stat label="Avg/day" value={`${item.velocity} ${item.unit}`} size="sm"/>
            <Stat label="Peak" value={`${Math.max(...item.consumption)} ${item.unit}`} size="sm"/>
            <Stat label="Trend" value="+18%" deltaTone="red" size="sm"/>
          </div>
        </Card>
      </div>

      <SectionHeader title="RECOMMENDED ORDER"/>
      <div style={{ padding: '0 16px' }}>
        <Card tone="accent" pad={14}>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
            <div style={{
              fontFamily: T.numeric, fontSize: 38, fontWeight: 700, color: T.text,
              letterSpacing: -1.4, fontVariantNumeric: 'tabular-nums',
            }}>
              {Math.round(item.par * 2)}
            </div>
            <div style={{ fontSize: 13, color: T.sec }}>{item.unit} · covers 7 days</div>
          </div>
          <div style={{ fontSize: 12.5, color: T.sec, marginTop: 6 }}>
            @ ${item.unitCost}/unit · ${(item.par * 2 * item.unitCost).toFixed(2)} total from {item.supplier}
          </div>
          <div style={{ display: 'flex', gap: 8, marginTop: 12 }}>
            <Button kind="primary" size="sm" onClick={() => goto('po')}>Add to draft PO</Button>
            <Button kind="secondary" size="sm" style={{ width: 'auto', padding: '0 14px' }}>Adjust</Button>
          </div>
        </Card>
      </div>
    </div>
  );
}

function LegendDot({ color, label, dashed }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
      <div style={{
        width: 16, height: 2, background: dashed ? 'transparent' : color,
        border: dashed ? `1px dashed ${color}` : 'none',
      }}/>
      <div style={{ fontSize: 11, color: T.sec }}>{label}</div>
    </div>
  );
}

function MiniBars({ data, color }) {
  const max = Math.max(...data);
  const w = 326, h = 70;
  const bw = (w - data.length * 2) / data.length;
  return (
    <svg width={w} height={h}>
      {data.map((v, i) => {
        const bh = Math.max(2, (v / max) * (h - 4));
        return <rect key={i} x={i * (bw + 2)} y={h - bh - 2} width={bw} height={bh} rx={1.5} fill={color} opacity={0.6 + 0.4 * (v / max)}/>;
      })}
    </svg>
  );
}

// ═══════════════ PO Review ═══════════════════════════════════════════════════
function ScreenPOReview({ goto, onApprove }) {
  const po = DATA.draftPO;
  const [items, setItems] = React.useState(po.items);
  const subtotal = items.reduce((s, i) => s + i.qty * i.cost, 0);
  const hst = subtotal * 0.13;
  const total = subtotal + hst;

  const bump = (i, dq) => {
    const next = [...items];
    next[i] = { ...next[i], qty: Math.max(0, next[i].qty + dq) };
    setItems(next);
  };

  return (
    <div style={{ paddingBottom: 110 }}>
      <LargeTitle eyebrow={`DRAFT · ${po.number}`} title="Review order"/>

      {/* Supplier */}
      <div style={{ padding: '0 16px' }}>
        <Card pad={14}>
          <div style={{ display: 'flex', gap: 12 }}>
            <div style={{
              width: 44, height: 44, borderRadius: 10, background: T.acSoft,
              display: 'flex', alignItems: 'center', justifyContent: 'center',
            }}>
              <Icon name="truck" size={22} color={T.ac}/>
            </div>
            <div>
              <div style={{ fontSize: 15, fontWeight: 700, color: T.text }}>{po.supplier}</div>
              <div style={{ fontSize: 12, color: T.sec, marginTop: 1 }}>{po.rep} · deliver {po.deliverDate} {po.deliverWindow}</div>
            </div>
          </div>
        </Card>
      </div>

      <SectionHeader title={`ITEMS · ${items.length}`} action={
        <button style={{ background: 'none', border: 'none', color: T.ac, fontSize: 13, fontWeight: 600, cursor: 'pointer' }}>
          + Add
        </button>
      }/>
      <div style={{ padding: '0 16px' }}>
        <Card pad={0}>
          {items.map((it, i) => (
            <div key={i} style={{
              padding: 14, borderBottom: i < items.length - 1 ? `0.5px solid ${T.sep}` : 'none',
            }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <div style={{ flex: 1, minWidth: 0, paddingRight: 8 }}>
                  <div style={{ fontSize: 14, fontWeight: 600, color: T.text, letterSpacing: -0.2 }}>{it.name}</div>
                  <div style={{ fontSize: 11.5, color: T.sec, marginTop: 2 }}>
                    ${it.cost.toFixed(2)} per {it.unit} · line ${(it.qty * it.cost).toFixed(2)}
                  </div>
                </div>
                <div style={{
                  display: 'flex', alignItems: 'center', background: T.elev2, borderRadius: 8, padding: 2,
                }}>
                  <button onClick={() => bump(i, -1)} style={{
                    width: 28, height: 28, border: 'none', background: 'transparent', color: T.label,
                    borderRadius: 6, cursor: 'pointer',
                  }}>
                    <Icon name="minus" size={14} color={T.label}/>
                  </button>
                  <div style={{
                    minWidth: 38, textAlign: 'center', fontSize: 14, fontWeight: 700,
                    fontFamily: T.numeric, color: T.text, fontVariantNumeric: 'tabular-nums',
                  }}>{it.qty}</div>
                  <button onClick={() => bump(i, 1)} style={{
                    width: 28, height: 28, border: 'none', background: 'transparent', color: T.label,
                    borderRadius: 6, cursor: 'pointer',
                  }}>
                    <Icon name="plus" size={14} color={T.label}/>
                  </button>
                </div>
              </div>
            </div>
          ))}
        </Card>
      </div>

      {/* Totals */}
      <div style={{ padding: '16px' }}>
        <Card pad={14}>
          <TotalRow label="Subtotal" value={`$${subtotal.toFixed(2)}`}/>
          <TotalRow label="HST 13%" value={`$${hst.toFixed(2)}`}/>
          <div style={{ height: 0.5, background: T.sep, margin: '8px 0' }}/>
          <TotalRow label="Total" value={`$${total.toFixed(2)}`} big/>
        </Card>
      </div>

      {/* Approve CTA — fixed bottom-like */}
      <div style={{ padding: '0 16px' }}>
        <Button kind="primary" size="lg" icon="lock" onClick={onApprove}>
          Approve with PIN · ${total.toFixed(2)}
        </Button>
        <div style={{ fontSize: 11, color: T.ter, textAlign: 'center', marginTop: 10, lineHeight: 1.4 }}>
          POs &gt; $500 require PIN · Signed audit trail
        </div>
      </div>
    </div>
  );
}

function TotalRow({ label, value, big }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', padding: '4px 0' }}>
      <div style={{ fontSize: big ? 15 : 13, color: big ? T.text : T.sec, fontWeight: big ? 700 : 500 }}>{label}</div>
      <div style={{
        fontSize: big ? 20 : 14, fontWeight: big ? 800 : 600, color: T.text,
        fontFamily: T.numeric, fontVariantNumeric: 'tabular-nums',
      }}>{value}</div>
    </div>
  );
}

// ═══════════════ PIN Sheet ═══════════════════════════════════════════════════
function PINSheet({ open, onClose, onSuccess }) {
  const [pin, setPin] = React.useState('');
  const [err, setErr] = React.useState(false);
  const CORRECT = '0428';

  React.useEffect(() => {
    if (pin.length === 4) {
      if (pin === CORRECT) {
        setTimeout(() => { onSuccess(); setPin(''); }, 300);
      } else {
        setErr(true);
        setTimeout(() => { setErr(false); setPin(''); }, 600);
      }
    }
  }, [pin]);

  const tap = (d) => { if (pin.length < 4) setPin(pin + d); };
  const del = () => setPin(pin.slice(0, -1));

  return (
    <Sheet open={open} onClose={onClose} height="66%">
      <div style={{ padding: '12px 20px 30px', display: 'flex', flexDirection: 'column', alignItems: 'center' }}>
        <div style={{ fontSize: 17, fontWeight: 700, color: T.text, marginBottom: 2 }}>Approve purchase order</div>
        <div style={{ fontSize: 13, color: T.sec, marginBottom: 30 }}>$341.28 to Gordon Food Service</div>

        {/* Dots */}
        <div style={{ display: 'flex', gap: 14, marginBottom: 30,
          animation: err ? 'shake 0.5s' : '' }}>
          {[0,1,2,3].map(i => (
            <div key={i} style={{
              width: 16, height: 16, borderRadius: 8,
              background: err ? T.red : i < pin.length ? T.text : 'transparent',
              border: `1.5px solid ${err ? T.red : i < pin.length ? T.text : T.ter}`,
              transition: 'background 0.15s',
            }}/>
          ))}
        </div>

        {/* Hint */}
        <div style={{ fontSize: 11, color: T.ter, marginBottom: 20 }}>Try PIN: 0428</div>

        {/* Keypad */}
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 74px)', gap: 14, justifyContent: 'center' }}>
          {['1','2','3','4','5','6','7','8','9'].map(d => (
            <PinKey key={d} onClick={() => tap(d)}>{d}</PinKey>
          ))}
          <div/>
          <PinKey onClick={() => tap('0')}>0</PinKey>
          <PinKey onClick={del} icon>
            <Icon name="chevron-left" size={20} color={T.label}/>
          </PinKey>
        </div>
      </div>
    </Sheet>
  );
}

function PinKey({ children, onClick, icon }) {
  return (
    <button onClick={onClick} style={{
      width: 74, height: 74, borderRadius: 37, background: T.elev2, border: 'none',
      fontFamily: T.numeric, fontSize: icon ? 16 : 28, fontWeight: 400,
      color: T.text, cursor: 'pointer', letterSpacing: -0.5,
    }}>{children}</button>
  );
}

// ═══════════════ Send PO Screen ══════════════════════════════════════════════
function ScreenSendPO({ goto }) {
  return (
    <div style={{ paddingBottom: 100 }}>
      <LargeTitle eyebrow="APPROVED · PO-2026-0042" title="Send it"/>

      <div style={{ padding: '0 16px' }}>
        <Card tone="green" pad={16} style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <div style={{
            width: 44, height: 44, borderRadius: 22, background: T.green,
            display: 'flex', alignItems: 'center', justifyContent: 'center',
          }}>
            <Icon name="check" size={24} color="#04110B" strokeWidth={3}/>
          </div>
          <div>
            <div style={{ fontSize: 15, fontWeight: 700, color: T.text }}>Approved by Mei Chen</div>
            <div style={{ fontSize: 12.5, color: T.sec, marginTop: 1 }}>Tue Apr 22 · 7:42 AM · PIN-signed</div>
          </div>
        </Card>
      </div>

      <SectionHeader title="SEND VIA"/>
      <div style={{ padding: '0 16px' }}>
        <List>
          <ListRow icon="send" iconColor={T.blue} title="Email to rep" subtitle="dave.chan@gfs.ca" onClick={() => {}}/>
          <ListRow icon="message" iconColor={T.green} title="SMS Dave" subtitle="+1 416-555-0184 · usually replies 7 AM"/>
          <ListRow icon="truck" iconColor={T.ac} title="GFS Marketplace" subtitle="Direct API · instant confirm"/>
          <ListRow icon="printer" iconColor={T.purple} title="Print PDF" subtitle="AirPrint" isLast/>
        </List>
      </div>

      <SectionHeader title="SMS PREVIEW"/>
      <div style={{ padding: '0 16px' }}>
        <Card pad={14} style={{ fontFamily: T.mono, fontSize: 12, color: T.label, lineHeight: 1.7 }}>
          <span style={{ color: T.ac }}>Bluebird Café — PO-2026-0042</span><br/>
          Deliver Fri Apr 25, 7–9 AM to 482 Queen W<br/><br/>
          48 × Avocados (Hass) @ $2.14<br/>
          28 × Chicken thighs @ $11.80/kg<br/>
          12 × Heavy cream 35% @ $8.90/L<br/>
          6 × Butter unsalted @ $14.20/kg<br/>
          2 × AP flour 25kg @ $34.50<br/><br/>
          Subtotal $302.02 · HST $39.26<br/>
          <span style={{ color: T.text, fontWeight: 700 }}>Total $341.28</span><br/>
          <span style={{ color: T.ter }}>Signed: Mei Chen · 22 Apr 07:42 EDT</span>
        </Card>
      </div>

      <div style={{ padding: '16px' }}>
        <Button kind="primary" size="lg" icon="send" onClick={() => goto('orders')}>
          Send PO now
        </Button>
      </div>
    </div>
  );
}

Object.assign(window, { ScreenOrders, ScreenItemDetail, ScreenPOReview, ScreenSendPO, PINSheet });
