import { useCallback, useEffect, useState } from 'react';
import api from './api/client';
import {
  useAccount,
  useSignals,
  useStrategy,
  useTradeHistory,
  useTrades,
} from './hooks/useDataHooks';
import PipelineControl from './pages/PipelineControl';
import Backtest from './pages/Backtest';
import { useWebSocket } from './hooks/useWebSocket';
import { StoreProvider, useStore } from './store/store';
import type { LearningInsight, LearningRunResponse, PingResponse, StrategyHistoryRow } from './types';

type Page = 'overview' | 'scanner' | 'openTrades' | 'tradeHistory' | 'strategy' | 'backtest' | 'pipeline';

function minutesAgo(value: string | null) {
  if (!value) return 'Never updated';
  const minutes = Math.max(0, Math.floor((Date.now() - new Date(value).getTime()) / 60000));
  if (minutes === 0) return 'Last updated just now';
  if (minutes === 1) return 'Last updated 1 minute ago';
  return `Last updated ${minutes} minutes ago`;
}

function ErrorBanner({ message, onRetry }: { message: string | null; onRetry: () => void }) {
  if (!message) return null;
  return (
    <div className="banner">
      <span>{message}</span>
      <button onClick={onRetry}>Retry</button>
    </div>
  );
}

function SkeletonGrid({ count = 3 }: { count?: number }) {
  return (
    <div className="grid">
      {Array.from({ length: count }).map((_, index) => (
        <div className="card skeleton animate-pulse" key={index}>
          <div className="sk-line wide" />
          <div className="sk-line" />
          <div className="sk-line short" />
        </div>
      ))}
    </div>
  );
}

function OverviewPage() {
  const { state } = useStore();
  const resource = useAccount();
  const account = state.account;
  const balance = account?.balance ?? account?.current_balance ?? account?.equity;
  const realized = account?.total_pnl ?? account?.realized_pnl ?? 0;
  const openTrades = account?.open_positions_count ?? account?.open_trades ?? 0;
  return (
    <section>
      <ErrorBanner message={resource.error} onRetry={resource.refresh} />
      <p className="muted">{minutesAgo(resource.lastUpdated)}</p>
      {resource.loading && !state.account ? (
        <SkeletonGrid />
      ) : (
        <div className="grid">
          <Metric label="Equity" value={`Rs ${balance?.toLocaleString() ?? '-'}`} />
          <Metric label="Realized PnL" value={`Rs ${realized.toLocaleString()}`} />
          <Metric label="Unrealized PnL" value={`Rs ${(account?.unrealized_pnl ?? 0).toLocaleString()}`} />
          <Metric label="Open Trades" value={String(openTrades)} />
          <Metric label="Win Rate" value={account?.win_rate != null ? `${account.win_rate.toFixed(1)}%` : '-'} />
          <Metric label="Max Drawdown" value={account?.max_drawdown != null ? `Rs ${account.max_drawdown.toLocaleString()}` : '-'} />
        </div>
      )}
    </section>
  );
}

function ScannerPage({ wsConnected }: { wsConnected: boolean }) {
  const { state } = useStore();
  const resource = useSignals(wsConnected);
  return (
    <section>
      <ErrorBanner message={resource.error} onRetry={resource.refresh} />
      <p className="muted">{state.lastWsUpdate ? minutesAgo(state.lastWsUpdate) : minutesAgo(resource.lastUpdated)}</p>
      {resource.loading && state.signals.length === 0 ? <SkeletonGrid count={6} /> : null}
      {state.signals.length === 0 && !resource.loading ? (
        <EmptyState text="No signals yet. Run the scanner to see results." />
      ) : (
        <div className="table">
          {state.signals.map((signal) => (
            <div className="row" key={signal.symbol}>
              <strong>{signal.symbol}</strong>
              <span className={`pill ${signal.direction.toLowerCase()}`}>{signal.direction}</span>
              <span>{signal.confidence_score.toFixed(1)}</span>
              <span>{signal.market_condition}</span>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function OpenTradesPage({ wsConnected }: { wsConnected: boolean }) {
  const { state } = useStore();
  const resource = useTrades(wsConnected);
  return (
    <section>
      <ErrorBanner message={resource.error} onRetry={resource.refresh} />
      <p className="muted">{state.lastWsUpdate ? minutesAgo(state.lastWsUpdate) : minutesAgo(resource.lastUpdated)}</p>
      {resource.loading && state.openTrades.length === 0 ? <SkeletonGrid /> : null}
      {state.openTrades.length === 0 && !resource.loading ? (
        <EmptyState text="No open positions. The scanner will pick up the next opportunity." />
      ) : (
        <div className="table">
          {state.openTrades.map((trade) => (
            <div className="row" key={trade.id}>
              <strong>{trade.symbol}</strong>
              <span>{trade.direction}</span>
              <span>Entry {trade.entry_price}</span>
              <span>Now {trade.current_price ?? '-'}</span>
              <span className={(trade.unrealized_pnl ?? 0) >= 0 ? 'positive' : 'negative'}>
                {trade.unrealized_pnl ?? 0}
              </span>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function TradeHistoryPage() {
  const { state } = useStore();
  const [filter, setFilter] = useState('ALL');
  const resource = useTradeHistory();
  const rows = state.tradeHistory.filter((trade) => filter === 'ALL' || trade.outcome === filter);

  return (
    <section>
      <ErrorBanner message={resource.error} onRetry={resource.refresh} />
      <p className="muted">{minutesAgo(resource.lastUpdated)}</p>
      <div className="segmented">
        {['ALL', 'WIN', 'LOSS', 'BREAKEVEN'].map((item) => (
          <button className={filter === item ? 'active' : ''} onClick={() => setFilter(item)} key={item}>
            {item}
          </button>
        ))}
      </div>
      {resource.loading && state.tradeHistory.length === 0 ? <SkeletonGrid /> : null}
      {rows.length === 0 && !resource.loading ? (
        <EmptyState text="No completed trades yet. Trades will appear here after they close." />
      ) : (
        <div className="table">
          {rows.map((trade) => (
            <div className="row" key={trade.id}>
              <strong>{trade.symbol}</strong>
              <span>{trade.direction}</span>
              <span>{trade.outcome ?? '-'}</span>
              <span className={(trade.pnl ?? 0) >= 0 ? 'positive' : 'negative'}>{trade.pnl ?? 0}</span>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function StrategyPage() {
  const { state } = useStore();
  const resource = useStrategy();
  const strategy = state.activeStrategy;
  const [history, setHistory] = useState<StrategyHistoryRow[]>([]);
  const [latestInsight, setLatestInsight] = useState<LearningInsight | null>(null);
  const [learningReport, setLearningReport] = useState<string | null>(null);
  const [learningBusy, setLearningBusy] = useState(false);
  const [learningError, setLearningError] = useState<string | null>(null);

  const loadLearningData = useCallback(async () => {
    const [historyResponse, insightResponse] = await Promise.all([
      api.get<StrategyHistoryRow[]>('/api/strategy/history'),
      api.get<LearningInsight | null>('/api/learning/latest'),
    ]);
    setHistory(historyResponse.data);
    setLatestInsight(insightResponse.data);
  }, []);

  useEffect(() => {
    loadLearningData().catch((error) => {
      setLearningError(error instanceof Error ? error.message : 'Could not load learning history');
    });
  }, [loadLearningData]);

  const runLearning = async () => {
    setLearningBusy(true);
    setLearningError(null);
    try {
      const response = await api.post<LearningRunResponse>('/api/learning/run');
      setLearningReport(response.data.report || response.data.reason || response.data.what_changed || 'Learning cycle completed.');
      await Promise.all([resource.refresh(), loadLearningData()]);
    } catch (error) {
      setLearningError(error instanceof Error ? error.message : 'Learning cycle failed');
    } finally {
      setLearningBusy(false);
    }
  };
  return (
    <section>
      <ErrorBanner message={resource.error} onRetry={resource.refresh} />
      <ErrorBanner message={learningError} onRetry={loadLearningData} />
      <p className="muted">{minutesAgo(resource.lastUpdated)}</p>
      {resource.loading && !strategy ? <SkeletonGrid /> : null}
      {!strategy && !resource.loading ? (
        <EmptyState text="No strategy versions found. Run init_db.py to seed the default strategy." />
      ) : strategy ? (
        <div className="grid">
          <Metric label="RSI Weight" value={`${strategy.weight_rsi * 100}%`} />
          <Metric label="MACD Weight" value={`${strategy.weight_macd * 100}%`} />
          <Metric label="Volume Weight" value={`${strategy.weight_volume * 100}%`} />
          <Metric label="VWAP Weight" value={`${strategy.weight_vwap * 100}%`} />
          <Metric label="Minimum Score" value={String(strategy.min_score_to_trade)} />
          <Metric label="Max Open Trades" value={String(strategy.max_open_trades)} />
        </div>
      ) : null}

      <div className="section-heading">
        <div>
          <h2>Learning History</h2>
          <p className="muted">Strategy versions created from closed-trade performance.</p>
        </div>
        <button onClick={runLearning} disabled={learningBusy}>
          {learningBusy ? 'Running learning cycle...' : 'Run Learning Cycle'}
        </button>
      </div>

      {latestInsight ? (
        <div className="card insight-card">
          <span className="label">Latest Insights</span>
          <div className="insight-grid">
            <span>Best window <strong>{latestInsight.best_time_window || '-'}</strong></span>
            <span>Worst signal <strong>{latestInsight.worst_signal || '-'}</strong></span>
            <span>Win rate <strong>{latestInsight.win_rate != null ? `${latestInsight.win_rate.toFixed(1)}%` : '-'}</strong></span>
          </div>
        </div>
      ) : <div className="empty compact">No learning cycle has completed yet.</div>}

      {history.length ? (
        <div className="table learning-table">
          <div className="row table-header">
            <strong>Version</strong><span>Date</span><span>Win rate</span><span>What changed</span><span>Trades</span>
          </div>
          {history.map((item) => (
            <div className="row" key={item.version}>
              <strong>v{item.version}{item.is_active ? ' (active)' : ''}</strong>
              <span>{item.created_at ? new Date(item.created_at).toLocaleDateString() : '-'}</span>
              <span>{item.learning_win_rate != null ? `${item.learning_win_rate.toFixed(1)}%` : '-'}</span>
              <span className="notes-cell">{item.notes || 'Initial strategy'}</span>
              <span>{item.trades_analyzed ?? '-'}</span>
            </div>
          ))}
        </div>
      ) : null}

      {learningReport ? (
        <details className="learning-report" open>
          <summary>Learning cycle report</summary>
          <pre>{learningReport}</pre>
        </details>
      ) : null}
    </section>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="card">
      <span className="label">{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function EmptyState({ text }: { text: string }) {
  return <div className="empty">{text}</div>;
}

function Dashboard({ ping }: { ping: PingResponse }) {
  const [page, setPage] = useState<Page>('overview');
  const { state } = useStore();
  const ws = useWebSocket();
  const pages: { id: Page; label: string }[] = [
    { id: 'overview', label: 'Overview' },
    { id: 'scanner', label: 'Scanner' },
    { id: 'openTrades', label: 'Open Trades' },
    { id: 'tradeHistory', label: 'Trade History' },
    { id: 'strategy', label: 'Strategy' },
    { id: 'backtest', label: 'Backtest' },
    { id: 'pipeline', label: '▶ Pipeline' },
  ];

  return (
    <main>
      <header>
        <div>
          <h1>AI Trading Agent</h1>
          <p>{ping.database} database, {ping.total_candles.toLocaleString()} candles</p>
        </div>
        <div className="status-group">
          <span className={`status ${ping.orchestrator_alive ? 'online' : 'warning'}`}>
            {ping.orchestrator_alive ? 'Orchestrator: Live ✓' : 'Orchestrator: Stopped ⚠️'}
          </span>
          <span className={`status ${ws.connected ? 'online' : 'offline'}`}>
            {ws.connected ? 'Backend: Live' : 'Backend: Disconnected'}
          </span>
        </div>
      </header>

      <nav>
        {pages.map((item) => (
          <button className={page === item.id ? 'active' : ''} onClick={() => setPage(item.id)} key={item.id}>
            {item.label}
          </button>
        ))}
      </nav>

      <ErrorBanner message={state.globalError} onRetry={() => window.location.reload()} />
      {page === 'overview' && <OverviewPage />}
      {page === 'scanner' && <ScannerPage wsConnected={ws.connected} />}
      {page === 'openTrades' && <OpenTradesPage wsConnected={ws.connected} />}
      {page === 'tradeHistory' && <TradeHistoryPage />}
      {page === 'strategy' && <StrategyPage />}
      {page === 'backtest' && <Backtest />}
      {page === 'pipeline' && <PipelineControl />}
    </main>
  );
}

function StartupGate() {
  const [ping, setPing] = useState<PingResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const checkConnection = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await api.get<PingResponse>('/api/ping');
      setPing(response.data);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Cannot connect to backend');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    checkConnection();
    const id = window.setInterval(checkConnection, 60000);
    return () => window.clearInterval(id);
  }, [checkConnection]);

  if (loading && !ping) {
    return <div className="fullscreen"><SkeletonGrid /></div>;
  }

  if (error && !ping) {
    return (
      <div className="fullscreen error-page">
        <h1>Cannot connect to Trading Agent backend</h1>
        <p>Make sure the backend is running:</p>
        <pre>cd ai-trading-agent{'\n'}python -m uvicorn backend.main:app --reload --port 8000</pre>
        <button onClick={checkConnection}>Retry Connection</button>
      </div>
    );
  }

  return ping ? <Dashboard ping={ping} /> : null;
}

export default function App() {
  return (
    <StoreProvider>
      <StartupGate />
    </StoreProvider>
  );
}
