// ReorderOS atoms — iOS-native patterns.
// Depends on T (tokens), Icon.

// Large title (iOS standard — 34/41 semibold). Shrinks on scroll elsewhere.
function LargeTitle({ title, eyebrow, action, style }) {
  return (
    <div style={{ padding: '6px 20px 10px', ...style }}>
      {eyebrow && (
        <div style={{
          fontSize: 12, fontWeight: 600, color: T.ac,
          letterSpacing: 0.6, textTransform: 'uppercase', marginBottom: 6,
        }}>{eyebrow}</div>
      )}
      <div style={{ display: 'flex', alignItems: 'flex-end', justifyContent: 'space-between', gap: 12 }}>
        <h1 style={{
          margin: 0, fontFamily: T.display,
          fontSize: 34, fontWeight: 700, lineHeight: '41px',
          letterSpacing: 0.37, color: T.text,
        }}>{title}</h1>
        {action}
      </div>
    </div>
  );
}

// Secondary section header (iOS grouped list header style)
function SectionHeader({ title, action, style }) {
  return (
    <div style={{
      display: 'flex', alignItems: 'center', justifyContent: 'space-between',
      padding: '18px 20px 8px', ...style,
    }}>
      <div style={{
        fontSize: 13, fontWeight: 600, color: T.sec,
        letterSpacing: -0.08,
      }}>{title}</div>
      {action}
    </div>
  );
}

// Card (elev1, rounded, optional pad)
function Card({ children, tone, pad = 16, style, onClick }) {
  const toneMap = {
    accent: { bg: T.acSoft, border: `1px solid ${T.acBorder}` },
    red:    { bg: T.redSoft, border: `1px solid rgba(255,69,58,0.22)` },
    green:  { bg: T.greenSoft, border: `1px solid rgba(48,209,88,0.22)` },
    amber:  { bg: T.amberSoft, border: `1px solid rgba(255,159,10,0.22)` },
    blue:   { bg: T.blueSoft, border: `1px solid rgba(10,132,255,0.22)` },
    purple: { bg: T.purpleSoft, border: `1px solid rgba(191,90,242,0.22)` },
  };
  const t = toneMap[tone] || { bg: T.elev1, border: 'none' };
  return (
    <div onClick={onClick} style={{
      background: t.bg, border: t.border, borderRadius: T.radius,
      padding: pad,
      cursor: onClick ? 'pointer' : 'default',
      ...style,
    }}>{children}</div>
  );
}

// Pill / badge
function Pill({ children, tone = 'neutral', style }) {
  const map = {
    green:  { bg: T.greenSoft, fg: T.green },
    red:    { bg: T.redSoft, fg: T.red },
    amber:  { bg: T.amberSoft, fg: T.amber },
    blue:   { bg: T.blueSoft, fg: T.blue },
    purple: { bg: T.purpleSoft, fg: T.purple },
    accent: { bg: T.acSoft, fg: T.ac },
    neutral:{ bg: 'rgba(120,120,128,0.24)', fg: T.label },
  };
  const m = map[tone] || map.neutral;
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: 4,
      fontSize: 11, fontWeight: 600, letterSpacing: 0.2,
      color: m.fg, background: m.bg,
      padding: '3px 8px', borderRadius: 6, ...style,
    }}>{children}</span>
  );
}

// Button — iOS native 3 variants
function Button({ children, kind = 'primary', size = 'md', onClick, style, disabled, icon }) {
  const base = {
    display: 'inline-flex', alignItems: 'center', justifyContent: 'center', gap: 6,
    fontFamily: T.ui, fontWeight: 600, border: 'none', cursor: disabled ? 'default' : 'pointer',
    letterSpacing: -0.1, outline: 'none', width: '100%',
    opacity: disabled ? 0.4 : 1,
  };
  const sizes = {
    sm: { height: 34, fontSize: 14, borderRadius: 10, padding: '0 14px' },
    md: { height: 44, fontSize: 15, borderRadius: 12, padding: '0 18px' },
    lg: { height: 52, fontSize: 17, borderRadius: 14, padding: '0 20px' },
  };
  const kinds = {
    primary: { background: T.ac, color: '#04110B' },
    secondary: { background: T.elev2, color: T.text },
    ghost: { background: 'transparent', color: T.ac },
    destructive: { background: 'rgba(255,69,58,0.18)', color: T.red },
  };
  return (
    <button onClick={disabled ? undefined : onClick}
      style={{ ...base, ...sizes[size], ...kinds[kind], ...style }}>
      {icon && <Icon name={icon} size={16} color="currentColor"/>}
      {children}
    </button>
  );
}

// Stat — big tabular number + label + optional delta
function Stat({ value, label, delta, deltaTone = 'green', size = 'md', style }) {
  const sizes = {
    sm: { num: 22, lbl: 11 },
    md: { num: 30, lbl: 12 },
    lg: { num: 44, lbl: 13 },
    xl: { num: 56, lbl: 14 },
  };
  const s = sizes[size];
  const dc = deltaTone === 'red' ? T.red : deltaTone === 'amber' ? T.amber : T.green;
  return (
    <div style={style}>
      {label && (
        <div style={{
          fontSize: s.lbl, fontWeight: 500, color: T.sec,
          letterSpacing: -0.1, marginBottom: 4,
        }}>{label}</div>
      )}
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
        <div style={{
          fontFamily: T.numeric, fontSize: s.num, fontWeight: 700,
          color: T.text, letterSpacing: -0.04 * s.num,
          lineHeight: 1.02, fontVariantNumeric: 'tabular-nums',
        }}>{value}</div>
        {delta && (
          <div style={{
            fontSize: Math.max(11, s.num * 0.33), fontWeight: 600,
            color: dc, letterSpacing: -0.1, lineHeight: 1,
          }}>{delta}</div>
        )}
      </div>
    </div>
  );
}

// Grouped list (iOS inset style) — used for settings-like rows
function List({ header, footer, children, style }) {
  return (
    <div style={{ margin: '0 16px', ...style }}>
      {header && (
        <div style={{
          fontSize: 13, fontWeight: 400, color: T.sec,
          padding: '6px 4px 6px', letterSpacing: -0.08,
          textTransform: 'uppercase',
        }}>{header}</div>
      )}
      <div style={{
        background: T.elev1, borderRadius: T.radius, overflow: 'hidden',
      }}>{children}</div>
      {footer && (
        <div style={{
          fontSize: 12, fontWeight: 400, color: T.ter,
          padding: '6px 4px', letterSpacing: -0.08,
        }}>{footer}</div>
      )}
    </div>
  );
}

function ListRow({ icon, iconColor, title, subtitle, detail, chevron = true, onClick, danger, isLast, trailing }) {
  return (
    <div onClick={onClick} style={{
      display: 'flex', alignItems: 'center', minHeight: 52, padding: '10px 14px',
      cursor: onClick ? 'pointer' : 'default',
      borderBottom: isLast ? 'none' : `0.5px solid ${T.sep}`,
    }}>
      {icon && (
        <div style={{
          width: 30, height: 30, borderRadius: 7,
          background: iconColor || T.elev2,
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          marginRight: 12, flexShrink: 0,
        }}>
          <Icon name={icon} size={17} color={iconColor ? '#fff' : T.label}/>
        </div>
      )}
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{
          fontSize: 16, fontWeight: 500, color: danger ? T.red : T.text,
          letterSpacing: -0.32, lineHeight: 1.2,
        }}>{title}</div>
        {subtitle && (
          <div style={{
            fontSize: 13, color: T.sec, marginTop: 2,
            letterSpacing: -0.08, lineHeight: 1.3,
          }}>{subtitle}</div>
        )}
      </div>
      {trailing}
      {detail && (
        <div style={{
          fontSize: 15, color: T.sec, marginLeft: 8,
          fontVariantNumeric: 'tabular-nums',
        }}>{detail}</div>
      )}
      {chevron && (
        <Icon name="chevron-right" size={14} color={T.ter} style={{ marginLeft: 6 }}/>
      )}
    </div>
  );
}

// Segmented control (iOS)
function Segmented({ options, value, onChange, style }) {
  return (
    <div style={{
      display: 'flex', padding: 2, borderRadius: 9,
      background: 'rgba(120,120,128,0.24)', gap: 0,
      ...style,
    }}>
      {options.map(opt => {
        const v = typeof opt === 'string' ? opt : opt.value;
        const lbl = typeof opt === 'string' ? opt : opt.label;
        const active = v === value;
        return (
          <button key={v} onClick={() => onChange(v)} style={{
            flex: 1, height: 28, border: 'none',
            background: active ? T.elev2 : 'transparent',
            color: active ? T.text : T.label,
            fontFamily: T.ui, fontSize: 13, fontWeight: active ? 600 : 500,
            borderRadius: 7, cursor: 'pointer',
            letterSpacing: -0.08,
            boxShadow: active ? '0 3px 8px rgba(0,0,0,0.12), 0 3px 1px rgba(0,0,0,0.04)' : 'none',
            transition: 'all 0.15s',
          }}>{lbl}</button>
        );
      })}
    </div>
  );
}

// Tab bar (bottom) — 4 tabs + optional center CTA
function TabBar({ active, onChange, items }) {
  return (
    <div style={{
      position: 'absolute', bottom: 0, left: 0, right: 0, zIndex: 40,
      height: 84, paddingBottom: 28,
      background: 'rgba(20,20,22,0.82)',
      backdropFilter: 'blur(30px) saturate(180%)',
      WebkitBackdropFilter: 'blur(30px) saturate(180%)',
      borderTop: `0.5px solid ${T.sep}`,
      display: 'flex', alignItems: 'stretch',
    }}>
      {items.map(item => {
        const isActive = item.id === active;
        return (
          <button key={item.id} onClick={() => onChange(item.id)} style={{
            flex: 1, background: 'transparent', border: 'none', cursor: 'pointer',
            display: 'flex', flexDirection: 'column', alignItems: 'center',
            justifyContent: 'center', gap: 3, padding: '8px 4px',
            color: isActive ? T.ac : T.sec,
          }}>
            <Icon name={item.icon} size={24} color="currentColor" strokeWidth={isActive ? 2.2 : 1.8}/>
            <div style={{
              fontSize: 10, fontWeight: 500, letterSpacing: 0.1,
            }}>{item.label}</div>
          </button>
        );
      })}
    </div>
  );
}

// Sheet (iOS bottom sheet)
function Sheet({ open, onClose, children, height }) {
  if (!open) return null;
  return (
    <>
      <div onClick={onClose} style={{
        position: 'absolute', inset: 0, background: 'rgba(0,0,0,0.5)',
        zIndex: 50, animation: 'fadein 0.25s',
      }}/>
      <div style={{
        position: 'absolute', left: 0, right: 0, bottom: 0, zIndex: 51,
        background: T.elev1, borderRadius: '14px 14px 0 0',
        maxHeight: height || '85%', overflow: 'hidden',
        display: 'flex', flexDirection: 'column',
        animation: 'slideup 0.32s cubic-bezier(0.22,1,0.36,1)',
      }}>
        <div style={{ display: 'flex', justifyContent: 'center', padding: '8px 0 4px' }}>
          <div style={{ width: 36, height: 5, borderRadius: 3, background: T.ter }}/>
        </div>
        <div style={{ flex: 1, overflow: 'auto' }}>{children}</div>
      </div>
    </>
  );
}

// Loading shimmer cell
function Skeleton({ w, h = 14, style }) {
  return (
    <div style={{
      width: w, height: h, borderRadius: 6,
      background: T.elev2, ...style,
    }}/>
  );
}

Object.assign(window, {
  LargeTitle, SectionHeader, Card, Pill, Button, Stat,
  List, ListRow, Segmented, TabBar, Sheet, Skeleton,
});
