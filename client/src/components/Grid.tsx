import type { ReactNode } from "react";

interface GridProps {
  columns: number;
  children: ReactNode;
  label?: string;
  scrollTarget?: string;
}

export function Grid({ columns, children, label, scrollTarget }: GridProps) {
  return (
    <section className="grid-section" data-scroll-target={scrollTarget}>
      {label && <h2 className="grid-label">{label}</h2>}
      <div
        className="grid"
        style={{ gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))` }}
      >
        {children}
      </div>
    </section>
  );
}

interface GridCellProps {
  title: string;
  subtitle?: string;
  imageUrl?: string;
  row: number;
  col: number;
  onClick: () => void;
}

export function GridCell({
  title,
  subtitle,
  imageUrl,
  row,
  col,
  onClick,
}: GridCellProps) {
  return (
    <button
      className="grid-cell"
      data-row={row}
      data-col={col}
      onClick={onClick}
    >
      {imageUrl && <img src={imageUrl} alt="" className="grid-cell-image" />}
      <div className="grid-cell-body">
        <div className="grid-cell-title">{title}</div>
        {subtitle && <div className="grid-cell-subtitle">{subtitle}</div>}
      </div>
    </button>
  );
}
