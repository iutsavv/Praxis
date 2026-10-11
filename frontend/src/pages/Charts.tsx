import { useCallback, useEffect, useMemo, useState } from 'react';
import api from '../api/client';

type Timeframe = '5m' | '15m' | '1h' | '1d';

interface ChartSymbol {
  symbol: string;
  name?: string | null;
  sector?: string | null;
  score?: number | null;
  direction?: string | null;
  pattern?: string | null;
}

interface ChartContext {
  symbol: string;
  interval: string;
  summary: {
    current_price: number | null;
    change_pct: number | null;
    volume: number | null;
    timestamp: string | null;
  };
  patterns: Array<{ pattern_name: string; direction: string; strength: number; timestamp: string }>;
  levels: Array<{ level_price: number; level_type: string; touches: number; strength_score: number }>;
  news: { score: number; headlines: string[] };
  trade: null | {
    direction: string;
    quantity: number;
    entry_price: number;
    target_price?: number | null;
    stop_loss_price?: number | null;
    current_price?: number | null;
    unrealized_pnl?: number | null;
  };
  signal: null | {
    score?: number | null;
    direction?: string | null;
    pattern?: string | null;
    setup?: string | null;
  };
  trade_plan: null | {
    entry_price?: number;
    target_price?: number;
    stop_loss_price?: number;
    setup_name?: string | null;
    confidence?: number | null;
    error?: string;
  };
}

const timeframes: Timeframe[] = ['5m', '15m', '1h', '1d'];

function formatMoney(value?: number | null) {
  return value == null ? '-' : `Rs ${value.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
}

function formatCompact(value?: number | null) {
  return value == null ? '-' : value.toLocaleString(undefined, { notation: 'compact' });
}

export default function Charts() {
  const [symbols, setSymbols] = useState<ChartSymbol[]>([]);
  const [selected, setSelected] = useState('');
  const [interval, setInterval] = useState<Timeframe>('15m');
  const [query, setQuery] = useState('');
  const [context, setContext] = useState<ChartContext | null>(null);
  const [loading, setLoading] = useState(true);
  const [chartLoading, setChartLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(Date.now());

  const loadSymbols = useCallback(async () => {
    const response = await api.get<ChartSymbol[]>('/api/charts/symbols');
    setSymbols(response.data);
    setSelected((current) => current || response.data[0]?.symbol || '');
  }, []);

  const loadContext = useCallback(async (symbol: string, activeInterval: Timeframe) => {
    if (!symbol) return;
    setLoading(true);
    setError(null);
    try {
      const response = await api.get<ChartContext>(`/api/charts/${symbol}/context`, {
        params: { interval: activeInterval },
      });
      setContext(response.data);
      setRefreshKey(Date.now());
      setChartLoading(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load chart context');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadSymbols().catch((err) => {
      setError(err instanceof Error ? err.message : 'Failed to load stocks');
      setLoading(false);
    });
  }, [loadSymbols]);

  useEffect(() => {
    loadContext(selected, interval);
  }, [selected, interval, loadContext]);

  useEffect(() => {
    const id = window.setInterval(() => {
      loadContext(selected, interval);
    }, 300_000);
    return () => window.clearInterval(id);
  }, [selected, interval, loadContext]);

  const filtered = useMemo(() => {
    const needle = query.trim().toUpperCase();
    if (!needle) return symbols;
    return symbols.filter((item) => (
      item.symbol.toUpperCase().includes(needle)
      || (item.name || '').toUpperCase().includes(needle)
      || (item.pattern || '').toUpperCase().includes(needle)
    ));
  }, [symbols, query]);

  const chartUrl = selected
    ? `${api.defaults.baseURL}/api/charts/${selected}?interval=${interval}&t=${refreshKey}`
    : '';

  const supports = (context?.levels || []).filter((level) => level.level_type === 'SUPPORT').slice(0, 4);
  const resistances = (context?.levels || []).filter((level) => level.level_type === 'RESISTANCE').slice(0, 4);
  const signal = context?.signal;

  return (
    <section className="charts-page">
      <aside className="chart-sidebar">
        <input
          aria-label="Search stocks"
          placeholder="Search stocks"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
        <div className="chart-stock-list">
          {filtered.map((stock) => (
            <button
              className={`chart-stock-row ${stock.symbol === selected ? 'active' : ''}`}
              key={stock.symbol}
              onClick={() => setSelected(stock.symbol)}
            >
              <span>
                <strong>{stock.symbol}</strong>
                <small>{stock.pattern || stock.sector || '-'}</small>
              </span>
              <span className="chart-badges">
                <b>{stock.score != null ? Math.round(stock.score) : '-'}</b>
                <i className={(stock.direction || 'HOLD').toLowerCase()}>{stock.direction || 'HOLD'}</i>
              </span>
            </button>
          ))}
        </div>
      </aside>

      <div className="chart-main">
        <div className="chart-toolbar">
          <div>
            <h2>{selected || 'Charts'}</h2>
            <p className="muted">{context?.summary.timestamp || 'Select a stock to view chart'}</p>
          </div>
          <div className="segmented chart-tabs">
            {timeframes.map((item) => (
              <button
                className={interval === item ? 'active' : ''}
                key={item}
                onClick={() => setInterval(item)}
              >
                {item}
              </button>
            ))}
          </div>
        </div>

        {error ? <div className="banner">{error}</div> : null}
        <div className="chart-image-wrap">
          {(loading || chartLoading) && <div className="chart-skeleton animate-pulse" />}
          {chartUrl ? (
            <img
              alt={`${selected} ${interval} candlestick chart`}
              src={chartUrl}
              onLoad={() => setChartLoading(false)}
              onError={() => {
                setChartLoading(false);
                setError('Chart image could not be generated for this symbol/timeframe.');
              }}
            />
          ) : (
            <div className="empty">No active stocks found.</div>
          )}
        </div>
      </div>

      <aside className="chart-right-panel">
        <div className="chart-panel-block">
          <span className="label">Stock Summary</span>
          <strong>{formatMoney(context?.summary.current_price)}</strong>
          <p className={(context?.summary.change_pct || 0) >= 0 ? 'positive' : 'negative'}>
            {context?.summary.change_pct != null ? `${context.summary.change_pct > 0 ? '+' : ''}${context.summary.change_pct}%` : '-'}
          </p>
          <p className="muted">Volume {formatCompact(context?.summary.volume)}</p>
        </div>

        <div className="chart-panel-block">
          <span className="label">Active Patterns</span>
          {context?.patterns.length ? context.patterns.map((pattern) => (
            <p key={`${pattern.timestamp}-${pattern.pattern_name}`}>
              {pattern.pattern_name} <span className="muted">({pattern.strength})</span>
            </p>
          )) : <p className="muted">No fresh pattern.</p>}
        </div>

        <div className="chart-panel-block">
          <span className="label">Key Levels</span>
          <table className="levels-table">
            <tbody>
              {[...supports, ...resistances].map((level) => (
                <tr key={`${level.level_type}-${level.level_price}`}>
                  <td className={level.level_type === 'SUPPORT' ? 'positive' : 'negative'}>{level.level_type === 'SUPPORT' ? 'S' : 'R'}</td>
                  <td>{formatMoney(level.level_price)}</td>
                  <td>{Math.round(level.strength_score || 0)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {!context?.levels.length && <p className="muted">No active levels.</p>}
        </div>

        <div className="chart-panel-block">
          <span className="label">News Sentiment</span>
          <strong className={(context?.news.score || 0) >= 0 ? 'positive' : 'negative'}>
            {context?.news.score?.toFixed(2) ?? '0.00'}
          </strong>
          {(context?.news.headlines || []).map((headline) => <p className="muted" key={headline}>{headline}</p>)}
        </div>

        {context?.trade ? (
          <div className="chart-panel-block trade-live">
            <span className="label">Open Trade</span>
            <p>{context.trade.direction} x {context.trade.quantity}</p>
            <p>Entry {formatMoney(context.trade.entry_price)}</p>
            <p>Target {formatMoney(context.trade.target_price)}</p>
            <p>SL {formatMoney(context.trade.stop_loss_price)}</p>
            <strong className={(context.trade.unrealized_pnl || 0) >= 0 ? 'positive' : 'negative'}>
              {formatMoney(context.trade.unrealized_pnl)}
            </strong>
          </div>
        ) : signal && signal.direction && signal.direction !== 'HOLD' ? (
          <div className="chart-panel-block trade-plan">
            <span className="label">Trade Plan</span>
            <p>{context?.trade_plan?.setup_name || signal.setup || signal.pattern || 'Active setup'}</p>
            <p>{signal.direction} / confidence {context?.trade_plan?.confidence != null ? Math.round(context.trade_plan.confidence) : signal.score != null ? Math.round(signal.score) : '-'}</p>
            <p>Entry {formatMoney(context?.trade_plan?.entry_price || context?.summary.current_price)}</p>
            <p>Target {formatMoney(context?.trade_plan?.target_price)}</p>
            <p>Stop {formatMoney(context?.trade_plan?.stop_loss_price)}</p>
          </div>
        ) : null}
      </aside>
    </section>
  );
}
