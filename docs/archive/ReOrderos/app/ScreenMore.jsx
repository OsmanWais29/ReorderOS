// More — profile, chat, benchmark, invoices, recipes, settings.

function ScreenMore({ goto }) {
  return (
    <div style={{ paddingBottom: 100 }}>
      <LargeTitle title="More"/>

      {/* Profile card */}
      <div style={{ padding: '0 16px' }}>
        <Card pad={16} onClick={() => goto('profile')}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
            <div style={{
              width: 56, height: 56, borderRadius: 28, background: `linear-gradient(135deg, ${T.ac}, ${T.purple})`,
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              fontSize: 22, fontWeight: 700, color: '#fff', letterSpacing: -0.4,
            }}>MC</div>
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: 17, fontWeight: 700, color: T.text }}>Mei Chen</div>
              <div style={{ fontSize: 13, color: T.sec, marginTop: 1 }}>Owner · Bluebird Café</div>
              <div style={{ fontSize: 11, color: T.ter, marginTop: 2 }}>482 Queen St W · Toronto</div>
            </div>
            <Icon name="chevron-right" size={16} color={T.ter}/>
          </div>
        </Card>
      </div>

      <SectionHeader title="INSIGHTS"/>
      <div style={{ padding: '0 16px' }}>
        <List>
          <ListRow icon="message" iconColor={T.ac} title="Ask Claude"
            subtitle="Ask anything about your numbers" onClick={() => goto('chat')}/>
          <ListRow icon="users" iconColor={T.blue} title="Benchmark"
            subtitle="Compare to 28 GTA peers" onClick={() => goto('benchmark')}/>
          <ListRow icon="book" iconColor={T.purple} title="Recipes"
            subtitle="18 items · sync to POS"/>
          <ListRow icon="scan" iconColor={T.green} title="Scan invoice"
            subtitle="OCR to 3-way match" onClick={() => goto('invoice')} isLast/>
        </List>
      </div>

      <SectionHeader title="OPERATIONS"/>
      <div style={{ padding: '0 16px' }}>
        <List>
          <ListRow icon="users" iconColor={T.amber} title="Team" subtitle="4 members"/>
          <ListRow icon="bell" iconColor={T.red} title="Alerts & rules" subtitle="7 active rules"/>
          <ListRow icon="shield" iconColor={T.green} title="Audit trail" subtitle="All changes signed"/>
          <ListRow icon="archive" iconColor={T.blue} title="Export data" subtitle="CSV · accountant" isLast/>
        </List>
      </div>

      <SectionHeader title="SETTINGS"/>
      <div style={{ padding: '0 16px' }}>
        <List>
          <ListRow icon="settings" iconColor={T.sec} title="Preferences"/>
          <ListRow icon="help" iconColor={T.sec} title="Help & feedback"/>
          <ListRow icon="info" iconColor={T.sec} title="About ReOrderOS" subtitle="v2.4.1" isLast/>
        </List>
      </div>

      <div style={{ textAlign: 'center', fontSize: 11, color: T.ter, marginTop: 20, letterSpacing: 0.3 }}>
        ReOrderOS · Built in Toronto
      </div>
    </div>
  );
}

// ═══════════════ Claude Chat ═════════════════════════════════════════════════
function ScreenChat({ goto }) {
  const [messages, setMessages] = React.useState([
    { role: 'bot', text: "Good morning Mei. What's on your mind today?", suggestions: [
      "Why did food cost spike?",
      "Build a Thursday night promo",
      "Which items should I 86?",
    ]},
  ]);
  const [input, setInput] = React.useState('');
  const [thinking, setThinking] = React.useState(false);

  const send = (text) => {
    if (!text.trim()) return;
    setMessages(m => [...m, { role: 'user', text }]);
    setInput('');
    setThinking(true);
    setTimeout(() => {
      setThinking(false);
      setMessages(m => [...m, {
        role: 'bot',
        text: "Food cost jumped 3.4% mostly from chicken thigh portioning — avg 182g vs 160g spec — that's $412 over period.",
        cards: [{ title: 'Variance bridge', subtitle: 'See waterfall breakdown', go: 'stock' }],
        suggestions: ["Print portion guide", "Show me waste log", "Who's on line tonight?"],
      }]);
    }, 1200);
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', paddingBottom: 0 }}>
      <LargeTitle eyebrow="CLAUDE · RESTAURANT CONTEXT" title="Ask anything"/>
      <div style={{ flex: 1, overflow: 'auto', padding: '0 16px 120px' }}>
        {messages.map((m, i) => <ChatBubble key={i} msg={m} onSuggest={send} goto={goto}/>)}
        {thinking && (
          <div style={{ display: 'flex', gap: 6, padding: '8px 14px' }}>
            {[0,1,2].map(i => (
              <div key={i} style={{
                width: 7, height: 7, borderRadius: 4, background: T.ac,
                animation: `bounce 1.2s ${i*0.15}s infinite`,
              }}/>
            ))}
          </div>
        )}
      </div>
      <div style={{
        position: 'absolute', bottom: 68, left: 0, right: 0, padding: 12,
        background: `linear-gradient(to top, ${T.bg}, ${T.bg}cc)`,
      }}>
        <div style={{
          display: 'flex', gap: 8, alignItems: 'center',
          background: T.elev2, borderRadius: 22, padding: '4px 6px 4px 16px',
        }}>
          <input value={input} onChange={e => setInput(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && send(input)}
            placeholder="Ask about covers, variance, menu…"
            style={{
              flex: 1, background: 'transparent', border: 'none', color: T.text,
              fontSize: 15, outline: 'none', padding: '10px 0',
            }}/>
          <button onClick={() => send(input)} style={{
            width: 36, height: 36, borderRadius: 18, border: 'none',
            background: input ? T.ac : T.ter, cursor: 'pointer',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
          }}>
            <Icon name="arrow-up" size={18} color="#fff" strokeWidth={2.5}/>
          </button>
        </div>
      </div>
    </div>
  );
}

function ChatBubble({ msg, onSuggest, goto }) {
  const isBot = msg.role === 'bot';
  return (
    <div style={{ margin: '14px 0' }}>
      <div style={{
        display: 'flex', justifyContent: isBot ? 'flex-start' : 'flex-end',
      }}>
        <div style={{
          maxWidth: '85%', padding: '10px 14px',
          background: isBot ? T.elev1 : T.ac,
          color: isBot ? T.text : '#fff',
          borderRadius: isBot ? '16px 16px 16px 4px' : '16px 16px 4px 16px',
          fontSize: 14.5, lineHeight: 1.45,
        }}>
          {msg.text}
        </div>
      </div>
      {msg.cards && msg.cards.map((c, i) => (
        <div key={i} onClick={() => goto(c.go)} style={{
          marginTop: 8, padding: 12, background: T.elev1, borderRadius: 12,
          display: 'flex', alignItems: 'center', gap: 10, cursor: 'pointer',
          border: `1px solid ${T.acSoft}`,
        }}>
          <div style={{ width: 32, height: 32, borderRadius: 8, background: T.acSoft,
            display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
            <Icon name="chart" size={16} color={T.ac}/>
          </div>
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 14, fontWeight: 600, color: T.text }}>{c.title}</div>
            <div style={{ fontSize: 12, color: T.sec, marginTop: 1 }}>{c.subtitle}</div>
          </div>
          <Icon name="chevron-right" size={14} color={T.ter}/>
        </div>
      ))}
      {msg.suggestions && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 10 }}>
          {msg.suggestions.map((s, i) => (
            <button key={i} onClick={() => onSuggest(s)} style={{
              padding: '8px 12px', background: T.elev1, border: `1px solid ${T.sep}`,
              borderRadius: 16, color: T.label, fontSize: 13, fontWeight: 500,
              cursor: 'pointer', whiteSpace: 'nowrap',
            }}>{s}</button>
          ))}
        </div>
      )}
    </div>
  );
}

// ═══════════════ Benchmark ═══════════════════════════════════════════════════
function ScreenBenchmark() {
  const b = DATA.benchmark;
  return (
    <div style={{ paddingBottom: 100 }}>
      <LargeTitle eyebrow="PEER COHORT" title="Benchmark"/>

      <div style={{ padding: '0 16px' }}>
        <Card tone="accent" pad={14}>
          <div style={{ fontSize: 12, color: T.ac, fontWeight: 600, letterSpacing: 0.3 }}>
            <Icon name="users" size={13} color={T.ac} style={{ verticalAlign: -2, marginRight: 6 }}/>
            {b.cohort}
          </div>
          <div style={{ fontSize: 12, color: T.sec, marginTop: 8, lineHeight: 1.5 }}>
            K-anonymized. No single-peer identification. Updated weekly.
          </div>
        </Card>
      </div>

      <SectionHeader title="METRICS"/>
      <div style={{ padding: '0 16px' }}>
        {b.metrics.map((m, i) => (
          <BenchmarkRow key={i} m={m}/>
        ))}
      </div>
    </div>
  );
}

function BenchmarkRow({ m }) {
  const isBetter = m.dir === 'lower-is-better' ? m.percentile < 50 :
                   m.dir === 'higher-is-better' ? m.percentile > 50 : true;
  const tone = m.dir === 'contextual' ? T.sec : isBetter ? T.green : T.red;
  return (
    <Card pad={14} style={{ marginBottom: 10 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
        <div style={{ fontSize: 13, color: T.sec, fontWeight: 500 }}>{m.label}</div>
        <Pill tone={isBetter ? 'green' : 'red'}>{m.percentile}th %ile</Pill>
      </div>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, marginTop: 6 }}>
        <div style={{
          fontFamily: T.numeric, fontSize: 28, fontWeight: 700, color: T.text,
          letterSpacing: -0.9, fontVariantNumeric: 'tabular-nums',
        }}>{m.value}</div>
        <div style={{ fontSize: 12, color: T.sec }}>you · peer median {m.benchmark}</div>
      </div>
      {/* Percentile bar */}
      <div style={{ marginTop: 10, height: 6, background: T.elev2, borderRadius: 3, position: 'relative', overflow: 'visible' }}>
        <div style={{
          position: 'absolute', left: '50%', top: -2, bottom: -2, width: 1, background: T.ter,
        }}/>
        <div style={{
          position: 'absolute', left: `${m.percentile}%`, top: -5, transform: 'translateX(-50%)',
          width: 14, height: 14, borderRadius: 7, background: tone,
          border: `2px solid ${T.bg}`,
        }}/>
      </div>
      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 10, color: T.ter, marginTop: 4 }}>
        <span>P0</span><span>P50</span><span>P100</span>
      </div>
    </Card>
  );
}

// ═══════════════ Invoice Scan ═════════════════════════════════════════════════
function ScreenInvoice({ goto }) {
  const [step, setStep] = React.useState('scan'); // scan → match → done
  return (
    <div style={{ paddingBottom: 100 }}>
      {step === 'scan' && <InvoiceScan onNext={() => setStep('match')} goto={goto}/>}
      {step === 'match' && <InvoiceMatch onBack={() => setStep('scan')} onDone={() => goto('stock')}/>}
    </div>
  );
}

function InvoiceScan({ onNext, goto }) {
  return (
    <>
      <LargeTitle eyebrow="CAMERA · OCR" title="Scan invoice"/>
      <div style={{ padding: '0 16px' }}>
        <div style={{
          aspectRatio: '3 / 4', background: T.elev1, borderRadius: 14,
          border: `2px dashed ${T.ter}`,
          display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
          position: 'relative', overflow: 'hidden',
        }}>
          {/* Corner brackets */}
          {[[8,8,true,true],[8,8,true,false],[8,8,false,true],[8,8,false,false]].map(([t,l,isTop,isLeft],i) => (
            <div key={i} style={{
              position: 'absolute', width: 28, height: 28,
              [isTop ? 'top' : 'bottom']: 14, [isLeft ? 'left' : 'right']: 14,
              borderTop: isTop ? `3px solid ${T.ac}` : 'none',
              borderBottom: !isTop ? `3px solid ${T.ac}` : 'none',
              borderLeft: isLeft ? `3px solid ${T.ac}` : 'none',
              borderRight: !isLeft ? `3px solid ${T.ac}` : 'none',
              borderRadius: 4,
            }}/>
          ))}
          <Icon name="scan" size={64} color={T.label} style={{ opacity: 0.4 }}/>
          <div style={{ fontSize: 15, color: T.label, marginTop: 16, fontWeight: 500 }}>
            Align invoice in frame
          </div>
          <div style={{ fontSize: 11.5, color: T.sec, marginTop: 4, textAlign: 'center', padding: '0 20px', lineHeight: 1.4 }}>
            OCR reads line items, matches to SKUs, and 3-way-checks against PO + receiving.
          </div>
        </div>
      </div>
      <div style={{ padding: 16 }}>
        <Button kind="primary" size="lg" icon="scan" onClick={onNext}>Capture</Button>
        <div style={{ display: 'flex', justifyContent: 'center', marginTop: 12, gap: 20 }}>
          <TextBtn icon="image" label="From photos"/>
          <TextBtn icon="file" label="From email"/>
        </div>
      </div>
    </>
  );
}

function TextBtn({ icon, label }) {
  return (
    <button style={{
      display: 'flex', alignItems: 'center', gap: 6,
      background: 'none', border: 'none', color: T.ac, fontSize: 14, fontWeight: 600, cursor: 'pointer',
    }}>
      <Icon name={icon} size={14} color={T.ac}/>
      {label}
    </button>
  );
}

function InvoiceMatch({ onBack, onDone }) {
  const lines = [
    { item: 'Avocados (Hass)', po: 48, received: 48, invoiced: 48, price: 2.14, match: 'ok' },
    { item: 'Chicken thighs',  po: 28, received: 28, invoiced: 28, price: 11.80, match: 'ok' },
    { item: 'Heavy cream',     po: 12, received: 11, invoiced: 12, price: 8.90, match: 'short' },
    { item: 'Butter',          po: 6,  received: 6,  invoiced: 6,  price: 14.20, match: 'ok' },
    { item: 'AP flour',        po: 2,  received: 2,  invoiced: 2,  price: 36.00, match: 'price' },
  ];
  return (
    <>
      <LargeTitle eyebrow="PO-2026-0041 · GFS" title="3-way match"/>
      <div style={{ padding: '0 16px' }}>
        <Card tone="amber" pad={14}>
          <div style={{ fontSize: 13, fontWeight: 700, color: T.amber }}>
            <Icon name="alert" size={14} color={T.amber} style={{ verticalAlign: -2, marginRight: 4 }}/>
            2 discrepancies found
          </div>
          <div style={{ fontSize: 12, color: T.label, marginTop: 4, lineHeight: 1.4 }}>
            1 short receipt · 1 price variance. Review before approving for payment.
          </div>
        </Card>
      </div>
      <SectionHeader title="LINE ITEMS"/>
      <div style={{ padding: '0 16px' }}>
        <Card pad={0}>
          {lines.map((l, i) => (
            <MatchRow key={i} l={l} last={i === lines.length - 1}/>
          ))}
        </Card>
      </div>
      <div style={{ padding: 16, display: 'flex', gap: 10 }}>
        <Button kind="secondary" size="md" style={{ flex: 1 }} onClick={onBack}>Re-scan</Button>
        <Button kind="primary" size="md" style={{ flex: 1 }} onClick={onDone}>Approve $298.62</Button>
      </div>
    </>
  );
}

function MatchRow({ l, last }) {
  const color = l.match === 'ok' ? T.green : l.match === 'short' ? T.red : T.amber;
  const badge = l.match === 'ok' ? 'MATCH' : l.match === 'short' ? 'SHORT' : 'PRICE +';
  return (
    <div style={{
      padding: 14, borderBottom: last ? 'none' : `0.5px solid ${T.sep}`,
    }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div style={{ fontSize: 14, fontWeight: 600, color: T.text, letterSpacing: -0.2 }}>{l.item}</div>
        <Pill tone={l.match === 'ok' ? 'green' : l.match === 'short' ? 'red' : 'amber'}>{badge}</Pill>
      </div>
      <div style={{ display: 'flex', marginTop: 8, gap: 12 }}>
        <MiniStat label="PO" value={l.po}/>
        <MiniStat label="Recv" value={l.received}/>
        <MiniStat label="Invc" value={l.invoiced}/>
        <MiniStat label="Price" value={`$${l.price}`}/>
      </div>
    </div>
  );
}

// ═══════════════ Notifications ═══════════════════════════════════════════════
function ScreenNotifications({ goto }) {
  const items = [
    { id: 1, icon: 'alert', color: T.red, title: 'Avocados → out in 48h',
      sub: 'Order before 4 PM cutoff', time: '7:42 AM', unread: true, go: 'orders' },
    { id: 2, icon: 'trend-up', color: T.green, title: 'Shawarma Poutine up 48% WoW',
      sub: 'Consider +$1 price bump', time: '6:30 AM', unread: true, go: 'sales' },
    { id: 3, icon: 'drop', color: T.blue, title: 'Rain forecast Thu & Sat',
      sub: 'Delivery push opportunity', time: '6:30 AM', unread: false, go: 'sales' },
    { id: 4, icon: 'check-circle', color: T.green, title: 'PO-2026-0040 delivered in full',
      sub: 'OFT · 18 lines matched', time: 'Yesterday', unread: false },
    { id: 5, icon: 'alert', color: T.amber, title: 'Chicken portioning 14% over',
      sub: 'Retrain line cooks on 160g spec', time: 'Yesterday', unread: false, go: 'stock' },
    { id: 6, icon: 'dollar', color: T.red, title: 'OFT price on Roma ↑ 12%',
      sub: 'GFS alternative saves $86/mo', time: 'Mon', unread: false, go: 'stock' },
  ];
  return (
    <div style={{ paddingBottom: 100 }}>
      <LargeTitle title="Notifications" eyebrow={`${items.filter(i => i.unread).length} unread`}/>
      <div style={{ padding: '0 16px' }}>
        <List>
          {items.map((n, i) => (
            <div key={n.id} onClick={() => n.go && goto(n.go)} style={{
              padding: '14px 16px', cursor: n.go ? 'pointer' : 'default',
              background: n.unread ? T.acSoft : 'transparent',
              display: 'flex', gap: 12, alignItems: 'flex-start',
              borderBottom: i < items.length - 1 ? `0.5px solid ${T.sep}` : 'none',
            }}>
              <div style={{
                width: 36, height: 36, borderRadius: 9, background: `${n.color}26`,
                display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0,
              }}>
                <Icon name={n.icon} size={16} color={n.color}/>
              </div>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8 }}>
                  <div style={{ fontSize: 14, fontWeight: 600, color: T.text, letterSpacing: -0.2 }}>{n.title}</div>
                  <div style={{ fontSize: 11, color: T.ter, flexShrink: 0 }}>{n.time}</div>
                </div>
                <div style={{ fontSize: 12.5, color: T.sec, marginTop: 2, lineHeight: 1.4 }}>{n.sub}</div>
              </div>
              {n.unread && <div style={{ width: 8, height: 8, borderRadius: 4, background: T.ac, marginTop: 8, flexShrink: 0 }}/>}
            </div>
          ))}
        </List>
      </div>
    </div>
  );
}

// ═══════════════ Search ═══════════════════════════════════════════════════════
function ScreenSearch({ goto }) {
  const [q, setQ] = React.useState('');
  const suggestions = [
    { cat: 'JUMP TO', items: [
      { icon: 'chart', label: 'Sales overview', go: 'sales' },
      { icon: 'alert', label: 'At-risk items', go: 'orders' },
      { icon: 'trash', label: 'Log waste', go: 'waste' },
      { icon: 'scan',  label: 'Scan invoice', go: 'invoice' },
    ]},
    { cat: 'ASK CLAUDE', items: [
      { icon: 'message', label: 'Why did food cost spike this week?', go: 'chat' },
      { icon: 'message', label: 'What should I promote Thursday?', go: 'chat' },
      { icon: 'message', label: 'Which items to 86?', go: 'chat' },
    ]},
  ];
  return (
    <div style={{ paddingBottom: 100 }}>
      <div style={{ padding: '54px 16px 0' }}>
        <div style={{
          display: 'flex', alignItems: 'center', gap: 10,
          background: T.elev2, borderRadius: 12, padding: '10px 14px',
        }}>
          <Icon name="search" size={18} color={T.sec}/>
          <input autoFocus value={q} onChange={e => setQ(e.target.value)}
            placeholder="Search menu, suppliers, orders…"
            style={{
              flex: 1, background: 'transparent', border: 'none', color: T.text,
              fontSize: 16, outline: 'none',
            }}/>
          <button onClick={() => goto('home')} style={{ background: 'none', border: 'none', color: T.ac, fontSize: 15, cursor: 'pointer' }}>
            Cancel
          </button>
        </div>
      </div>
      {suggestions.map(s => (
        <div key={s.cat}>
          <SectionHeader title={s.cat}/>
          <div style={{ padding: '0 16px' }}>
            <List>
              {s.items.map((it, i, arr) => (
                <ListRow key={i} icon={it.icon} title={it.label} onClick={() => goto(it.go)}
                  isLast={i === arr.length - 1}/>
              ))}
            </List>
          </div>
        </div>
      ))}
    </div>
  );
}

// Profile stub
function ScreenProfile({ goto }) {
  return (
    <div style={{ paddingBottom: 100 }}>
      <LargeTitle eyebrow="OWNER" title="Mei Chen"/>
      <div style={{ padding: '0 16px' }}>
        <Card pad={16}>
          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center' }}>
            <div style={{
              width: 80, height: 80, borderRadius: 40, background: `linear-gradient(135deg, ${T.ac}, ${T.purple})`,
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              fontSize: 30, fontWeight: 700, color: '#fff', letterSpacing: -0.4,
            }}>MC</div>
            <div style={{ fontSize: 20, fontWeight: 700, color: T.text, marginTop: 12 }}>Mei Chen</div>
            <div style={{ fontSize: 13, color: T.sec }}>Owner since Mar 2021</div>
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 0, marginTop: 16,
            paddingTop: 16, borderTop: `0.5px solid ${T.sep}` }}>
            <Stat label="Team" value="4" size="sm" style={{ textAlign: 'center' }}/>
            <Stat label="POs sent" value="164" size="sm" style={{ textAlign: 'center', borderLeft: `0.5px solid ${T.sep}`, borderRight: `0.5px solid ${T.sep}` }}/>
            <Stat label="Month" value="$78k" size="sm" style={{ textAlign: 'center' }}/>
          </div>
        </Card>
      </div>
    </div>
  );
}

Object.assign(window, { ScreenMore, ScreenChat, ScreenBenchmark, ScreenInvoice, ScreenNotifications, ScreenSearch, ScreenProfile });
