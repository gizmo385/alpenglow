type SparklineProps = {
  /** Y-values to plot; X is evenly spaced across the width. */
  values: number[];
  stroke?: string;
  height?: number;
  /** Viewbox width; the stroke is non-scaling so this is arbitrary. */
  width?: number;
};

/** SVG polyline sparkline. Non-scaling stroke, auto-normalized to the data. */
export function Sparkline({
  values,
  stroke = "var(--color-accent-400)",
  height = 60,
  width = 240,
}: SparklineProps) {
  if (values.length === 0) {
    return <svg className="sparkline" style={{ height }} viewBox={`0 0 ${width} ${height}`} />;
  }
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const pad = 3;
  const usable = height - pad * 2;
  const step = values.length > 1 ? width / (values.length - 1) : 0;
  const points = values
    .map((v, i) => {
      const x = (i * step).toFixed(1);
      const y = (pad + (1 - (v - min) / span) * usable).toFixed(1);
      return `${x},${y}`;
    })
    .join(" ");
  return (
    <svg
      className="sparkline"
      style={{ height }}
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="none"
    >
      <polyline
        points={points}
        fill="none"
        stroke={stroke}
        strokeWidth={2}
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  );
}
