import { useCallback, useEffect, useMemo, useState } from 'react';
import { Bar, BarChart, CartesianGrid, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import api from '../api/client';
import type { SignalScore, StrategyHistoryRow } from '../types';

interface BacktestTrade {
  id?: number;
  symbol: string;
  direction: string;
  entry_price: number;
  entry_time: string;
  exit_price: number;
  exit_time: string;
  exit_reason: string;
  quantity: number;
  pnl: number;
  pnl_pct: number;
  outcome: string;
  confidence_score: number;
}

interface MonthResult { pnl: number; trades: number; wins: number; win_rate: number }
interface BacktestResult {
  run_id: string;
  date_from: string;
  date_to: string;
  symbols_tested: string[];
  strategy_version: number;
  total_trades: number;
  win_rate: number;
  total_pnl: number;
  average_win: number;
  average_loss: number;
  profit_factor: number;
  max_drawdown: number;
  sharpe_ratio: number;
  monthly: Record<string, MonthResult>;
  trades: BacktestTrade[];
}

interface BacktestRun {
  run_id: string;
  created_at: string;
  date_from: string;
  date_to: string;
  symbols_tested: number;
  strategy_version: number;
  total_trades: number;
  win_rate: number;
  total_pnl: number;
  max_drawdown: number;
  sharpe_ratio: number;
  profit_factor: number;
}

function Metric({ label, value }: { label: string; value: string }) {
  return <div className="card"><span className="label">{label}</span><strong>{value}</strong></div>;
}

export default function Backtest() {
  const [dateFrom, setDateFrom] = useState('2026-04-29');
  const [dateTo, setDateTo] = useState('2026-07-01');
  const [symbol, setSymbol] = useState('');
  const [version, setVersion] = useState('');
  const [symbols, setSymbols] = useState<string[]>([]);
  const [versions, setVersions] = useState<StrategyHistoryRow[]>([]);
  const [history, setHistory] = useState<BacktestRun[]>([]);
  const [result, setResult] = useState<BacktestResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadReferenceData = useCallback(async () => {
    const [signals, strategies, runs] = await Promise.all([
      api.get<SignalScore[]>('/api/signals'),
      api.get<StrategyHistoryRow[]>('/api/strategy/history'),
      api.get<BacktestRun[]>('/api/backtest/history'),
    ]);
    setSymbols([...new Set(signals.data.map((item) => item.symbol))].sort());
    setVersions(strategies.data);
    setHistory(runs.data);
  }, []);

  useEffect(() => {
    loadReferenceData().catch((reason) => setError(reason instanceof Error ? reason.message : 'Could not load backtest data'));
  }, [loadReferenceData]);

  const runBacktest = async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await api.post<BacktestResult>('/api/backtest/run', {
        date_from: dateFrom,
        date_to: dateTo,
        symbol: symbol || null,
        strategy_version: version ? Number(version) : null,
      });
      setResult(response.data);
      await loadReferenceData();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Backtest failed');
    } finally {
      setLoading(false);
    }
  };

  const chartData = useMemo(() => result ? Object.entries(result.monthly).map(([month, data]) => ({ month, pnl: data.pnl })) : [], [result]);

  return (
    <section>
      <div className="backtest-form card">
        <label>From<input type="date" value={dateFrom} onChange={(event) => setDateFrom(event.target.value)} /></label>
        <label>To<input type="date" value={dateTo} onChange={(event) => setDateTo(event.target.value)} /></label>
        <label>Symbol<select value={symbol} onChange={(event) => setSymbol(event.target.value)}><option value="">All active symbols</option>{symbols.map((item) => <option key={item}>{item}</option>)}</select></label>
        <label>Strategy<select value={version} onChange={(event) => setVersion(event.target.value)}><option value="">Current active</option>{versions.map((item) => <option value={item.version} key={item.version}>v{item.version}{item.is_active ? ' (active)' : ''}</option>)}</select></label>
        <button onClick={runBacktest} disabled={loading || !dateFrom || !dateTo}>{loading ? 'Replaying candles...' : 'Run Backtest'}</button>
      </div>
      {error ? <div className="banner"><span>{error}</span></div> : null}
      {loading ? <div className="backtest-loading">Backtesting without lookahead bias. This may take 10–30 seconds…</div> : null}

      {result ? <>
        <div className="section-heading"><div><h2>Results</h2><p className="muted">Run {result.run_id.slice(0, 8)} · strategy v{result.strategy_version}</p></div></div>
        <div className="grid">
          <Metric label="Total Trades" value={String(result.total_trades)} />
          <Metric label="Win Rate" value={`${result.win_rate.toFixed(1)}%`} />
          <Metric label="Total PnL" value={`Rs ${result.total_pnl.toLocaleString(undefined, { maximumFractionDigits: 0 })}`} />
          <Metric label="Profit Factor" value={result.profit_factor.toFixed(2)} />
          <Metric label="Max Drawdown" value={`${result.max_drawdown.toFixed(1)}%`} />
          <Metric label="Sharpe Ratio" value={result.sharpe_ratio.toFixed(2)} />
          <Metric label="Average Win" value={`Rs ${result.average_win.toFixed(0)}`} />
          <Metric label="Average Loss" value={`Rs ${result.average_loss.toFixed(0)}`} />
        </div>
        <div className="card chart-card">
          <span className="label">Monthly PnL</span>
          {chartData.length ? <ResponsiveContainer width="100%" height={260}>
            <BarChart data={chartData}><CartesianGrid stroke="#2c3444" strokeDasharray="3 3" /><XAxis dataKey="month" stroke="#9aa6b7" /><YAxis stroke="#9aa6b7" /><Tooltip contentStyle={{ background: '#1a1d27', border: '1px solid #2c3444' }} /><Bar dataKey="pnl">{chartData.map((item) => <Cell key={item.month} fill={item.pnl >= 0 ? '#00c896' : '#ff4d6d'} />)}</Bar></BarChart>
          </ResponsiveContainer> : <div className="empty compact">No completed trades to chart.</div>}
        </div>
        <h2>Simulated Trades</h2>
        {result.trades.length ? <div className="table backtest-trades">
          <div className="row table-header"><span>Symbol</span><span>Side</span><span>Entry</span><span>Exit</span><span>Reason</span><span>PnL</span></div>
          {result.trades.map((trade, index) => <div className="row" key={`${trade.symbol}-${trade.entry_time}-${index}`}><strong>{trade.symbol}</strong><span>{trade.direction}</span><span>{trade.entry_price.toFixed(2)}</span><span>{trade.exit_price.toFixed(2)}</span><span>{trade.exit_reason}</span><span className={trade.pnl >= 0 ? 'positive' : 'negative'}>{trade.pnl.toFixed(0)}</span></div>)}
        </div> : <div className="empty">No trades crossed the selected strategy threshold.</div>}
      </> : null}

      <div className="section-heading"><div><h2>Backtest History</h2><p className="muted">Previous immutable simulation runs.</p></div></div>
      {history.length ? <div className="table backtest-history">
        <div className="row table-header"><span>Date</span><span>Period</span><span>Strategy</span><span>Trades</span><span>Win rate</span><span>PnL</span></div>
        {history.map((run) => <div className="row" key={run.run_id}><span>{new Date(run.created_at).toLocaleString()}</span><span>{run.date_from} → {run.date_to}</span><strong>v{run.strategy_version}</strong><span>{run.total_trades}</span><span>{run.win_rate.toFixed(1)}%</span><span className={run.total_pnl >= 0 ? 'positive' : 'negative'}>{run.total_pnl.toFixed(0)}</span></div>)}
      </div> : <div className="empty compact">No backtests have been saved yet.</div>}
    </section>
  );
}
