// Small charts for Settings. No library: one hue, thin bars, a readout on hover or focus, and the numbers as a table.

import { useState } from "react";

export type Point = { label: string; value: number };

/** How much of a limit is used. `tick` (0..1) marks where something changes, e.g. where background work pauses. */
export function Meter({ value, max, tick, tone, label }: { value: number; max: number; tick?: number; tone?: "ok" | "warn" | "error"; label: string }) {
  const pct = max > 0 ? Math.min(100, (value / max) * 100) : 0;
  return (
    <div className="meter" role="meter" aria-valuemin={0} aria-valuemax={max} aria-valuenow={value} aria-label={label}>
      <i className={`meter-fill ${tone ?? ""}`} style={{ width: `${pct}%` }} />
      {tick != null && <b className="meter-tick" style={{ left: `${tick * 100}%` }} />}
    </div>
  );
}

/** One series over time. Bars are anchored to the baseline with a rounded top; hover, touch or focus shows the value. */
export function BarChart({ data, format, title }: { data: Point[]; format: (n: number) => string; title: string }) {
  const [at, setAt] = useState<number | null>(null);
  const W = 600;
  const H = 120;
  const max = Math.max(...data.map((d) => d.value), 0) || 1;
  const slot = W / data.length;
  const bw = Math.min(20, slot * 0.6);
  const bar = (i: number, v: number) => {
    const h = v <= 0 ? 0 : Math.max(2, (v / max) * (H - 10));
    const x = i * slot + (slot - bw) / 2;
    const r = Math.min(4, bw / 2, h);
    return h === 0 ? "" : `M${x},${H} V${H - h + r} a${r},${r} 0 0 1 ${r},${-r} h${bw - 2 * r} a${r},${r} 0 0 1 ${r},${r} V${H} Z`;
  };
  return (
    <figure className="chart" aria-label={title}>
      <div className="chart-plot" onMouseLeave={() => setAt(null)}>
        <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img" aria-label={title}>
          <line className="chart-grid" x1="0" x2={W} y1="1" y2="1" />
          <line className="chart-base" x1="0" x2={W} y1={H} y2={H} />
          {data.map((d, i) => (
            <g key={d.label}>
              <path className={`chart-bar ${at === i ? "on" : ""}`} d={bar(i, d.value)} />
              <rect
                className="chart-hit"
                x={i * slot}
                y="0"
                width={slot}
                height={H}
                tabIndex={0}
                aria-label={`${d.label}: ${format(d.value)}`}
                onMouseEnter={() => setAt(i)}
                onFocus={() => setAt(i)}
                onBlur={() => setAt(null)}
                onTouchStart={() => setAt(i)}
              />
            </g>
          ))}
        </svg>
        <span className="chart-max">{format(max)}</span>
        {at != null && (
          <span className="chart-tip" style={{ left: `${((at + 0.5) / data.length) * 100}%` }}>
            <b>{format(data[at].value)}</b> {data[at].label}
          </span>
        )}
      </div>
      <div className="chart-axis">
        <span>{data[0].label}</span>
        <span>{data[data.length - 1].label}</span>
      </div>
      <details className="chart-table">
        <summary>Show the numbers</summary>
        <table>
          <tbody>
            {data.map((d) => (
              <tr key={d.label}>
                <th scope="row">{d.label}</th>
                <td>{format(d.value)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </figure>
  );
}

/** Where it went: one bar per part, each against the largest part. */
export function Split({ rows, format }: { rows: { label: string; value: number }[]; format: (n: number) => string }) {
  const max = Math.max(...rows.map((r) => r.value), 0) || 1;
  return (
    <ul className="split">
      {rows.map((r) => (
        <li key={r.label}>
          <span className="split-label">{r.label}</span>
          <span className="split-bar">
            <i style={{ width: `${Math.max(2, (r.value / max) * 100)}%` }} />
          </span>
          <span className="split-value">{format(r.value)}</span>
        </li>
      ))}
    </ul>
  );
}
