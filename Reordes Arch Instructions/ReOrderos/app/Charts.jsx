// ReorderOS charts — clean SVG, tabular, tap-interactive where useful.
// All charts are responsive to width prop or parent.

// ─── Helpers ────────────────────────────────────────────────────────────────
function niceMax(max) {
  const mag = Math.pow(10, Math.floor(Math.log10(max)));
  const norm = max / mag;
  const nice = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10;
  return nice * mag;
}
function buildPath(points) {
  if (!points.length) return '';
  return 'M' + points.map((p, i) => (i === 0 ? `${p[0]},${p[1]}` : `L${p[0]},${p[1]}`)).join(' ');
}
function smoothPath(points) {
  if (points.length < 2) return '';
  let d = `M${points[0][0]},${points[0][1]}`;
  for (let i = 1; i < points.length; i++) {
    const p0 = points[i - 1], p1 = points[i];
    const cx = (p0[0] + p1[0]) / 2;
    d += ` Q${cx},${p0[1]} ${cx},${(p0[1] + p1[1]) / 2} T${p1[0]},${p1[1]}`;
  }
  return d;
}

// ─── Revenue Line + Forecast Band ───────────────────────────────────────────
// data: [{ label, actual, forecast? }]  — forecast indexes plot as dashed band
function RevenueLineChart({ data, width = 358, height = 180, stroke = T.ac, showForecast = true }) {
  const padL = 8, padR = 8, padT = 12, padB = 22;
  const w = width - padL - padR;
  const h = height - padT - padB;

  const all = data.flatMap(d => [d.actual, d.forecast, d.fLow, d.fHigh].filter(v => v != null));
  const rawMax = Math.max(...all);
  const rawMin = Math.min(...all);
  const max = niceMax(rawMax * 1.08);
  const min = Math.max(0, rawMin * 0.82);
  const x = (i) => padL + (i / (data.length - 1)) * w;
  const y = (v) => padT + h - ((v - min) / (max - min)) * h;

  const actualPts = data.map((d, i) => d.actual != null ? [x(i), y(d.actual)] : null).filter(Boolean);
  const fcPts = data.map((d, i) => d.forecast != null ? [x(i), y(d.forecast)] : null).filter(Boolean);
  const bandHigh = data.map((d, i) => d.fHigh != null ? [x(i), y(d.fHigh)] : null).filter(Boolean);
  const bandLow = data.map((d, i) => d.fLow != null ? [x(i), y(d.fLow)] : null).filter(Boolean);

  const actualPath = smoothPath(actualPts);
  const fcPath = smoothPath(fcPts);
  const bandPath = smoothPath(bandHigh) + ' L' + bandLow.reverse().map(p => p.join(',')).join(' L') + ' Z';

  const areaPath = actualPath + ` L${actualPts[actualPts.length-1][0]},${padT + h} L${actualPts[0][0]},${padT + h} Z`;

  const gid = 'g_' + Math.random().toString(36).slice(2, 8);
  const lastActualIdx = data.map(d => d.actual != null).lastIndexOf(true);
  const forecastStart = data.findIndex(d => d.forecast != null && d.actual == null);

  return (
    <svg width={width} height={height} style={{ display: 'block' }}>
      <defs>
        <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={stroke} stopOpacity="0.25"/>
          <stop offset="100%" stopColor={stroke} stopOpacity="0"/>
        </linearGradient>
      </defs>
      {/* baseline grid — two faint lines */}
      {[0.33, 0.66].map(p => (
        <line key={p} x1={padL} x2={padL + w} y1={padT + h * p} y2={padT + h * p}
          stroke={T.sep} strokeWidth="0.5" strokeDasharray="2 3"/>
      ))}
      {/* forecast band */}
      {showForecast && bandHigh.length > 1 && (
        <path d={bandPath} fill={stroke} fillOpacity="0.08" stroke="none"/>
      )}
      {/* area */}
      <path d={areaPath} fill={`url(#${gid})`}/>
      {/* actual line */}
      <path d={actualPath} fill="none" stroke={stroke} strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"/>
      {/* forecast line dashed */}
      {showForecast && fcPts.length > 1 && (
        <path d={fcPath} fill="none" stroke={stroke} strokeWidth="2" strokeDasharray="4 4"
          strokeLinecap="round" opacity="0.7"/>
      )}
      {/* end dot */}
      {actualPts.length > 0 && (
        <>
          <circle cx={actualPts[actualPts.length-1][0]} cy={actualPts[actualPts.length-1][1]} r="5" fill={stroke}/>
          <circle cx={actualPts[actualPts.length-1][0]} cy={actualPts[actualPts.length-1][1]} r="2" fill="#000"/>
        </>
      )}
      {/* x-axis labels (sparse) */}
      {data.map((d, i) => {
        if (data.length > 10 && i % Math.ceil(data.length / 6) !== 0 && i !== data.length - 1) return null;
        if (!d.label) return null;
        return (
          <text key={i} x={x(i)} y={height - 6} fontSize="10" fill={T.ter}
            textAnchor="middle" fontFamily={T.numeric}>{d.label}</text>
        );
      })}
    </svg>
  );
}

// ─── Sparkline ──────────────────────────────────────────────────────────────
function Sparkline({ data, width = 100, height = 32, stroke = T.ac, strokeWidth = 1.8 }) {
  const max = Math.max(...data), min = Math.min(...data);
  const pts = data.map((v, i) => [
    (i / (data.length - 1)) * (width - 2) + 1,
    height - 2 - ((v - min) / (max - min || 1)) * (height - 4),
  ]);
  return (
    <svg width={width} height={height} style={{ display: 'block' }}>
      <path d={smoothPath(pts)} fill="none" stroke={stroke} strokeWidth={strokeWidth}
        strokeLinecap="round" strokeLinejoin="round"/>
    </svg>
  );
}

// ─── Dayparting Heatmap ──────────────────────────────────────────────────────
// data: [ [v,v,v,...] per day, 7 rows × hours cols ], plus hot/cold thresholds
function Heatmap({ data, width = 358, days = ['M','T','W','T','F','S','S'],
                  hourLabels = ['6a','9a','12p','3p','6p','9p'],
                  hourLabelIndices = [0,3,6,9,12,15],
                  onCell, selected }) {
  const rows = data.length, cols = data[0].length;
  const labelW = 22, labelH = 16;
  const w = width - labelW;
  const cellW = w / cols;
  const cellH = 22;
  const height = rows * cellH + labelH + 6;

  const allValues = data.flat();
  const max = Math.max(...allValues);
  const min = Math.min(...allValues);
  const norm = (v) => (v - min) / (max - min || 1);

  return (
    <svg width={width} height={height} style={{ display: 'block' }}>
      {/* hour labels */}
      {hourLabelIndices.map((h, idx) => (
        <text key={h} x={labelW + (h + 0.5) * cellW} y={labelH - 4}
          fontSize="10" fill={T.ter} textAnchor="middle" fontFamily={T.numeric}>
          {hourLabels[idx]}
        </text>
      ))}
      {/* day labels */}
      {days.map((d, i) => (
        <text key={i} x={labelW - 8} y={labelH + i * cellH + cellH / 2 + 3}
          fontSize="10" fill={T.sec} textAnchor="end" fontFamily={T.numeric}>{d}</text>
      ))}
      {/* cells */}
      {data.map((row, di) =>
        row.map((v, hi) => {
          const n = norm(v);
          const isSel = selected && selected.day === di && selected.hour === hi;
          // color scale — near-black -> deep emerald -> bright emerald
          let fill;
          if (n < 0.2) fill = 'rgba(52,211,153,0.05)';
          else if (n < 0.4) fill = 'rgba(52,211,153,0.18)';
          else if (n < 0.6) fill = 'rgba(52,211,153,0.36)';
          else if (n < 0.8) fill = 'rgba(52,211,153,0.6)';
          else fill = 'rgba(52,211,153,0.92)';
          // hot-spot = amber if very high AND peak hours
          if (n > 0.85) fill = 'rgba(255,159,10,0.75)';
          // cold-spot visual pull — red outline where low in peak hours
          return (
            <g key={`${di}-${hi}`}>
              <rect
                x={labelW + hi * cellW + 1} y={labelH + di * cellH + 1}
                width={cellW - 2} height={cellH - 2} rx={3} fill={fill}
                stroke={isSel ? T.ac : 'transparent'} strokeWidth="1.5"
                style={{ cursor: onCell ? 'pointer' : 'default' }}
                onClick={onCell ? () => onCell(di, hi, v) : undefined}
              />
            </g>
          );
        })
      )}
    </svg>
  );
}

// ─── Menu Engineering 2×2 Quadrant ──────────────────────────────────────────
// items: [{ name, popularity (0..1), margin (0..1), category, color }]
function QuadrantChart({ items, width = 358, height = 280, selected, onSelect }) {
  const padL = 38, padR = 16, padT = 22, padB = 36;
  const w = width - padL - padR;
  const h = height - padT - padB;
  const cx = padL + w / 2;
  const cy = padT + h / 2;

  const labels = [
    { x: padL + w * 0.25, y: padT + 14, t: 'PUZZLES', c: T.amber },
    { x: padL + w * 0.75, y: padT + 14, t: 'STARS', c: T.green },
    { x: padL + w * 0.25, y: padT + h - 4, t: 'DOGS', c: T.red },
    { x: padL + w * 0.75, y: padT + h - 4, t: 'PLOWHORSES', c: T.blue },
  ];

  return (
    <svg width={width} height={height} style={{ display: 'block' }}>
      {/* quadrant fill tints */}
      <rect x={padL} y={padT} width={w/2} height={h/2} fill={T.amberSoft}/>
      <rect x={padL + w/2} y={padT} width={w/2} height={h/2} fill={T.greenSoft}/>
      <rect x={padL} y={padT + h/2} width={w/2} height={h/2} fill={T.redSoft}/>
      <rect x={padL + w/2} y={padT + h/2} width={w/2} height={h/2} fill={T.blueSoft}/>
      {/* axes */}
      <line x1={padL} y1={cy} x2={padL + w} y2={cy} stroke={T.sep} strokeWidth="1" strokeDasharray="3 3"/>
      <line x1={cx} y1={padT} x2={cx} y2={padT + h} stroke={T.sep} strokeWidth="1" strokeDasharray="3 3"/>
      {/* quadrant labels */}
      {labels.map((l, i) => (
        <text key={i} x={l.x} y={l.y} fontSize="10" fontWeight="700"
          fill={l.c} textAnchor="middle" letterSpacing="0.6">{l.t}</text>
      ))}
      {/* axis labels */}
      <text x={padL - 6} y={padT + h/2} fontSize="10" fill={T.sec}
        textAnchor="end" transform={`rotate(-90 ${padL - 6} ${padT + h/2})`}>MARGIN →</text>
      <text x={padL + w/2} y={height - 12} fontSize="10" fill={T.sec} textAnchor="middle">POPULARITY →</text>

      {/* points */}
      {items.map((it, i) => {
        const px = padL + it.popularity * w;
        const py = padT + (1 - it.margin) * h;
        const r = 10 + (it.size || 0) * 6;
        const isSel = selected && selected.name === it.name;
        const col = it.color || T.ac;
        return (
          <g key={i} style={{ cursor: onSelect ? 'pointer' : 'default' }}
             onClick={onSelect ? () => onSelect(it) : undefined}>
            <circle cx={px} cy={py} r={r} fill={col} fillOpacity={isSel ? 0.45 : 0.25}
              stroke={col} strokeWidth={isSel ? 2.5 : 1.5}/>
            {isSel && (
              <text x={px} y={py + 4} fontSize="10" fontWeight="700" fill="#fff" textAnchor="middle">
                {it.name.split(' ')[0]}
              </text>
            )}
          </g>
        );
      })}
    </svg>
  );
}

// ─── Variance Bridge (waterfall) ─────────────────────────────────────────────
// steps: [{ label, value, type: 'start'|'pos'|'neg'|'end' }]
function BridgeChart({ steps, width = 358, height = 200 }) {
  const padL = 10, padR = 10, padT = 14, padB = 48;
  const w = width - padL - padR;
  const h = height - padT - padB;
  const colW = w / steps.length;
  const barW = Math.min(46, colW - 10);

  // Compute stacked y positions
  let running = 0;
  const bars = steps.map((s, i) => {
    if (s.type === 'start' || s.type === 'end') {
      const val = Math.abs(s.value);
      const b = { ...s, base: 0, top: val, i };
      running = s.type === 'start' ? s.value : running;
      return b;
    }
    const base = running;
    const top = running + s.value;
    running = top;
    return { ...s, base: Math.min(base, top), top: Math.max(base, top), i };
  });

  const allVals = bars.flatMap(b => [b.base, b.top]);
  const max = Math.max(...allVals) * 1.05;
  const min = 0;
  const y = v => padT + h - ((v - min) / (max - min)) * h;

  return (
    <svg width={width} height={height} style={{ display: 'block' }}>
      {bars.map((b, i) => {
        const colorMap = {
          start: T.blue,
          pos: T.green,
          neg: T.red,
          end: T.ac,
        };
        const color = colorMap[b.type];
        const soft = b.type === 'start' ? T.blueSoft : b.type === 'pos' ? T.greenSoft
                    : b.type === 'neg' ? T.redSoft : T.acSoft;
        const barY = y(b.top);
        const barH = y(b.base) - y(b.top);
        const cx = padL + i * colW + colW / 2;
        const nextY = b.type !== 'end' && bars[i+1]
          ? y(bars[i+1].type === 'start' ? Math.abs(bars[i+1].value)
             : Math.max(bars[i+1].base, bars[i+1].top))
          : null;
        return (
          <g key={i}>
            <rect x={cx - barW/2} y={barY} width={barW} height={Math.max(2, barH)}
              rx={3} fill={soft} stroke={color} strokeWidth="1.5"/>
            {/* connector line to next bar */}
            {b.type !== 'end' && bars[i+1] && bars[i+1].type !== 'end' && (
              <line x1={cx + barW/2} y1={y(b.top)} x2={cx + colW - barW/2}
                y2={y(b.top)} stroke={T.sep} strokeWidth="1" strokeDasharray="2 2"/>
            )}
            {/* value on bar */}
            <text x={cx} y={barY - 5} fontSize="11" fill={color} fontWeight="700"
              textAnchor="middle" fontFamily={T.numeric}>
              {b.type === 'start' || b.type === 'end'
                ? `$${b.value.toLocaleString()}`
                : (b.value >= 0 ? '+' : '') + `$${Math.abs(b.value)}`}
            </text>
            {/* label */}
            <text x={cx} y={height - 24} fontSize="10" fill={T.sec}
              textAnchor="middle" fontFamily={T.ui}>
              <tspan x={cx} dy="0">{b.label.split(' ')[0]}</tspan>
              {b.label.split(' ')[1] && <tspan x={cx} dy="11">{b.label.split(' ').slice(1).join(' ')}</tspan>}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

// ─── Burndown chart — inventory level vs par over N days ─────────────────────
// data: [{label, stock, par, forecast?}]; stockoutIdx = day we hit 0
function BurndownChart({ data, width = 358, height = 140, stockoutDay }) {
  const padL = 8, padR = 8, padT = 10, padB = 20;
  const w = width - padL - padR;
  const h = height - padT - padB;
  const allV = data.flatMap(d => [d.stock, d.par, d.forecast].filter(v => v != null));
  const max = niceMax(Math.max(...allV) * 1.1);
  const x = i => padL + (i / (data.length - 1)) * w;
  const y = v => padT + h - (v / max) * h;

  const actualPts = data.filter(d => d.stock != null).map((d, i) => [x(i), y(d.stock)]);
  const fcPts = data.map((d, i) => d.forecast != null ? [x(i), y(d.forecast)] : null).filter(Boolean);

  const parY = y(data[0].par);
  return (
    <svg width={width} height={height} style={{ display: 'block' }}>
      {/* par line */}
      <line x1={padL} y1={parY} x2={padL + w} y2={parY} stroke={T.amber}
        strokeWidth="1.5" strokeDasharray="4 3" opacity="0.6"/>
      <text x={padL + w - 4} y={parY - 4} fontSize="9" fill={T.amber}
        textAnchor="end" fontWeight="600">PAR {data[0].par}</text>
      {/* forecast stock */}
      {fcPts.length > 1 && (
        <path d={smoothPath(fcPts)} fill="none" stroke={T.red} strokeWidth="2"
          strokeDasharray="5 3" opacity="0.75"/>
      )}
      {/* actual stock */}
      <path d={smoothPath(actualPts) + ` L${actualPts[actualPts.length-1][0]},${padT+h} L${actualPts[0][0]},${padT+h} Z`}
        fill={T.ac} fillOpacity="0.18"/>
      <path d={smoothPath(actualPts)} fill="none" stroke={T.ac} strokeWidth="2.2" strokeLinecap="round"/>
      {/* stockout marker */}
      {stockoutDay != null && (
        <>
          <line x1={x(stockoutDay)} y1={padT} x2={x(stockoutDay)} y2={padT + h}
            stroke={T.red} strokeWidth="1" strokeDasharray="3 2"/>
          <circle cx={x(stockoutDay)} cy={y(0)} r="5" fill={T.red}/>
          <text x={x(stockoutDay)} y={padT + 10} fontSize="10" fontWeight="700"
            fill={T.red} textAnchor="middle">STOCK-OUT</text>
        </>
      )}
      {/* x labels */}
      {data.map((d, i) => {
        if (!d.label || (i % Math.ceil(data.length / 5) !== 0 && i !== data.length - 1)) return null;
        return (
          <text key={i} x={x(i)} y={height - 4} fontSize="9" fill={T.ter}
            textAnchor="middle" fontFamily={T.numeric}>{d.label}</text>
        );
      })}
    </svg>
  );
}

// ─── Percentile Ring (peer benchmark) ───────────────────────────────────────
function PercentileRing({ percentile, size = 100, label, sub, color }) {
  const stroke = 9;
  const r = (size - stroke) / 2;
  const cx = size / 2, cy = size / 2;
  const circ = 2 * Math.PI * r;
  const offset = circ * (1 - percentile / 100);
  const c = color || (percentile >= 75 ? T.green : percentile >= 50 ? T.ac : percentile >= 25 ? T.amber : T.red);
  return (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 6 }}>
      <div style={{ position: 'relative', width: size, height: size }}>
        <svg width={size} height={size} style={{ transform: 'rotate(-90deg)' }}>
          <circle cx={cx} cy={cy} r={r} fill="none" stroke={T.elev2} strokeWidth={stroke}/>
          <circle cx={cx} cy={cy} r={r} fill="none" stroke={c} strokeWidth={stroke}
            strokeLinecap="round" strokeDasharray={circ} strokeDashoffset={offset}
            style={{ transition: 'stroke-dashoffset 1.2s cubic-bezier(0.22,1,0.36,1)' }}/>
        </svg>
        <div style={{
          position: 'absolute', inset: 0, display: 'flex', flexDirection: 'column',
          alignItems: 'center', justifyContent: 'center',
        }}>
          <div style={{
            fontFamily: T.numeric, fontSize: size * 0.32, fontWeight: 800,
            color: T.text, lineHeight: 1, letterSpacing: -0.04 * (size * 0.32),
          }}>{percentile}</div>
          <div style={{ fontSize: 9, color: T.sec, fontWeight: 600, letterSpacing: 0.4, textTransform: 'uppercase' }}>
            %ile
          </div>
        </div>
      </div>
      {label && <div style={{ fontSize: 12, fontWeight: 600, color: T.label, textAlign: 'center' }}>{label}</div>}
      {sub && <div style={{ fontSize: 10, color: T.sec, textAlign: 'center' }}>{sub}</div>}
    </div>
  );
}

// ─── Mini bar chart (horizontal) ────────────────────────────────────────────
function BarList({ data, width = '100%', max, colorFn }) {
  const m = max || Math.max(...data.map(d => d.value));
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8, width }}>
      {data.map((d, i) => {
        const pct = d.value / m;
        const color = (colorFn && colorFn(d, i)) || T.ac;
        return (
          <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <div style={{ flex: '0 0 96px', fontSize: 13, color: T.label, fontWeight: 500,
              overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {d.label}
            </div>
            <div style={{ flex: 1, height: 8, background: T.elev2, borderRadius: 4, overflow: 'hidden' }}>
              <div style={{ width: `${pct * 100}%`, height: '100%', background: color,
                borderRadius: 4, transition: 'width 1s cubic-bezier(0.22,1,0.36,1)' }}/>
            </div>
            <div style={{
              flex: '0 0 auto', fontSize: 12, color: T.sec, fontWeight: 600,
              fontFamily: T.numeric, fontVariantNumeric: 'tabular-nums', minWidth: 48, textAlign: 'right',
            }}>{d.display || d.value}</div>
          </div>
        );
      })}
    </div>
  );
}

// ─── Donut chart ────────────────────────────────────────────────────────────
function Donut({ data, size = 160, thickness = 18, center }) {
  const r = (size - thickness) / 2;
  const cx = size / 2, cy = size / 2;
  const total = data.reduce((s, d) => s + d.value, 0);
  const circ = 2 * Math.PI * r;
  let acc = 0;
  return (
    <svg width={size} height={size} style={{ transform: 'rotate(-90deg)' }}>
      {data.map((d, i) => {
        const frac = d.value / total;
        const dash = frac * circ;
        const gap = circ - dash;
        const offset = -acc * circ;
        acc += frac;
        return (
          <circle key={i} cx={cx} cy={cy} r={r} fill="none" stroke={d.color}
            strokeWidth={thickness} strokeDasharray={`${dash} ${gap}`}
            strokeDashoffset={offset}/>
        );
      })}
      {center && (
        <g transform={`rotate(90 ${cx} ${cy})`}>
          {center}
        </g>
      )}
    </svg>
  );
}

// ─── Price elasticity curve ──────────────────────────────────────────────────
function ElasticityChart({ currentPrice, targetPrice, data, width = 358, height = 160 }) {
  const padL = 26, padR = 12, padT = 12, padB = 30;
  const w = width - padL - padR;
  const h = height - padT - padB;
  const prices = data.map(d => d.price);
  const demands = data.map(d => d.demand);
  const pMin = Math.min(...prices), pMax = Math.max(...prices);
  const dMax = Math.max(...demands) * 1.1;
  const x = p => padL + ((p - pMin) / (pMax - pMin)) * w;
  const y = d => padT + h - (d / dMax) * h;
  const pts = data.map(d => [x(d.price), y(d.demand)]);

  return (
    <svg width={width} height={height} style={{ display: 'block' }}>
      <path d={smoothPath(pts)} fill="none" stroke={T.ac} strokeWidth="2.5" strokeLinecap="round"/>
      {/* current price marker */}
      <line x1={x(currentPrice)} y1={padT} x2={x(currentPrice)} y2={padT + h}
        stroke={T.sec} strokeWidth="1" strokeDasharray="2 2"/>
      <text x={x(currentPrice)} y={padT + 10} fontSize="10" fill={T.sec}
        textAnchor="middle" fontWeight="600">NOW ${currentPrice}</text>
      {/* target price marker */}
      <line x1={x(targetPrice)} y1={padT} x2={x(targetPrice)} y2={padT + h}
        stroke={T.ac} strokeWidth="1.5" strokeDasharray="4 3"/>
      <text x={x(targetPrice)} y={padT + 10} fontSize="10" fill={T.ac}
        textAnchor="middle" fontWeight="700">→ ${targetPrice}</text>
      {/* axis labels */}
      <text x={padL} y={height - 8} fontSize="9" fill={T.ter}>${pMin}</text>
      <text x={padL + w} y={height - 8} fontSize="9" fill={T.ter} textAnchor="end">${pMax}</text>
      <text x={padL - 4} y={padT + 8} fontSize="9" fill={T.ter} textAnchor="end">DEMAND</text>
    </svg>
  );
}

Object.assign(window, {
  RevenueLineChart, Sparkline, Heatmap, QuadrantChart, BridgeChart,
  BurndownChart, PercentileRing, BarList, Donut, ElasticityChart,
});
