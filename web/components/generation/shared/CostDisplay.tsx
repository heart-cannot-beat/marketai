const CURRENCY_SYMBOLS: Record<string, string> = {
  USD: '$',
  EUR: '€',
  GBP: '£',
  CNY: '¥',
};

interface CostDisplayProps {
  amount: string | null;
  currency: string;
}

export function CostDisplay({ amount, currency }: CostDisplayProps) {
  if (amount === null) {
    return <span aria-label="No cost data">—</span>;
  }
  const symbol = CURRENCY_SYMBOLS[currency] ?? `${currency} `;
  const formatted = parseFloat(amount).toFixed(2);
  return (
    <span aria-label={`${formatted} ${currency}`}>
      {symbol}
      {formatted}
    </span>
  );
}
