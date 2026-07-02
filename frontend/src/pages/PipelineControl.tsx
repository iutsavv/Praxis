import { useCallback, useEffect, useMemo, useState } from 'react';
import api, { pipelineApi } from '../api/client';
import { useStore } from '../store/store';
import type {
  AccountSummary,
  ExecuteTradeResponse,
  FullCycleResponse,
  IndicatorRunResponse,
  MonitorTradesResponse,
  PaperTrade,
  PipelineStatus,
  ScanRunResponse,
  TopPick,
} from '../types';

type Stage = 'indicators' | 'scan' | 'execute' | 'monitor' | 'fullCycle' | 'status';
type LoadingState = Partial<Record<Stage, boolean>>;
type ErrorEntry = { id: number; timestamp: string; stage: string; message: string };
type FullCycleStep = 'Indicators' | 'Scan' | 'Execute' | 'Monitor';

const statusLabels: { key: keyof Omit<PipelineStatus, 'status'>; label: string }[] = [
  { key: 'indicators', label: 'Indicators' },
  { key: 'scan', label: 'Scan' },
  { key: 'trade_execution', label: 'Trade Execution' },
  { key: 'monitoring', label: 'Monitoring' },
];

const fullCycleSteps: FullCycleStep[] = ['Indicators', 'Scan', 'Execute', 'Monitor'];

function parseTimestamp(value: string | null) {
  if (!value) return null;
  const parsed = new Date(value.includes('T') ? value : value.replace(' ', 'T'));
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function timeAgo(value: string | null) {
  const date = parseTimestamp(value);
  if (!date) return 'never run';
  const minutes = Math.max(0, Math.floor((Date.now() - date.getTime()) / 60000));
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes} minute${minutes === 1 ? '' : 's'} ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} hour${hours === 1 ? '' : 's'} ago`;
  const days = Math.floor(hours / 24);
  return `${days} day${days === 1 ? '' : 's'} ago`;
}

function statusTone(value: string | null) {
  const date = parseTimestamp(value);
  if (!date) return 'stale';
  const minutes = Math.max(0, Math.floor((Date.now() - date.getTime()) / 60000));
  if (minutes <= 60) return 'fresh';
  if (minutes <= 1440) return 'aging';
  return 'stale';
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : 'Request failed';
}

function formatCurrency(value: number | null | undefined) {
  if (typeof value !== 'number') return '-';
  return `₹${value.toLocaleString('en-IN', { maximumFractionDigits: 2 })}`;
}

function formatNumber(value: number | null | undefined) {
  if (typeof value !== 'number') return '-';
  return value.toLocaleString('en-IN', { maximumFractionDigits: 2 });
}

function DirectionBadge({ direction }: { direction: string }) {
  return <span className={`pill ${direction.toLowerCase()}`}>{direction}</span>;
}

function Spinner() {
  return <span className="spinner" aria-hidden="true" />;
}

function ResultText({ children, tone = 'success' }: { children: React.ReactNode; tone?: 'success' | 'error' | 'muted' }) {
  return <div className={`pipeline-result ${tone}`}>{children}</div>;
}

export default function PipelineControl() {
  const { dispatch } = useStore();
  const [status, setStatus] = useState<PipelineStatus | null>(null);
  const [loading, setLoading] = useState<LoadingState>({});
  const [errors, setErrors] = useState<ErrorEntry[]>([]);
  const [lastResponse, setLastResponse] = useState<unknown>(null);
  const [indicatorResult, setIndicatorResult] = useState<IndicatorRunResponse | null>(null);
  const [scanResult, setScanResult] = useState<ScanRunResponse | null>(null);
  const [executeResult, setExecuteResult] = useState<ExecuteTradeResponse | null>(null);
  const [monitorResult, setMonitorResult] = useState<MonitorTradesResponse | null>(null);
  const [fullCycleResult, setFullCycleResult] = useState<FullCycleResponse | null>(null);
  const [selectedSymbol, setSelectedSymbol] = useState('');
  const [fullProgress, setFullProgress] = useState<Record<FullCycleStep, 'pending' | 'running' | 'done'>>({
    Indicators: 'pending',
    Scan: 'pending',
    Execute: 'pending',
    Monitor: 'pending',
  });

  const topPicks = scanResult?.top_picks ?? [];
  const indicatorsReady = Boolean(status?.indicators);
  const anyLoading = Object.values(loading).some(Boolean);

  const addError = useCallback((stage: string, message: string) => {
    setErrors((current) => [
      { id: Date.now(), timestamp: new Date().toLocaleTimeString(), stage, message },
      ...current,
    ].slice(0, 10));
  }, []);

  const refreshStatus = useCallback(async () => {
    setLoading((current) => ({ ...current, status: true }));
    try {
      const data = await pipelineApi.status();
      setStatus(data);
      setLastResponse(data);
    } catch (error) {
      addError('status', errorMessage(error));
    } finally {
      setLoading((current) => ({ ...current, status: false }));
    }
  }, []);

  const refreshOpenTrades = useCallback(async () => {
    const data = (await api.get<PaperTrade[]>('/api/trades/open')).data;
    dispatch({ type: 'SET_OPEN_TRADES', payload: data });
  }, [dispatch]);

  const refreshAccount = useCallback(async () => {
    const data = (await api.get<AccountSummary>('/api/account/summary')).data;
    dispatch({ type: 'SET_ACCOUNT', payload: data });
  }, [dispatch]);

  const runRequest = useCallback(
    async <T,>(stage: Stage, request: () => Promise<T>, onSuccess: (data: T) => Promise<void> | void) => {
      setLoading((current) => ({ ...current, [stage]: true }));
      try {
        const data = await request();
        setLastResponse(data);
        await onSuccess(data);
        await refreshStatus();
      } catch (error) {
        const message = errorMessage(error);
        addError(stage, message);
        setLastResponse({ status: 'error', stage, message });
      } finally {
        setLoading((current) => ({ ...current, [stage]: false }));
      }
    },
    [addError, refreshStatus],
  );

  useEffect(() => {
    refreshStatus();
  }, [refreshStatus]);

  useEffect(() => {
    if (!selectedSymbol && topPicks[0]) {
      setSelectedSymbol(topPicks[0].symbol);
    }
  }, [selectedSymbol, topPicks]);

  const monitorSummary = useMemo(() => {
    if (!monitorResult) return null;
    if (monitorResult.trades_closed === 0) return '✅ Open positions checked, 0 closed';
    const first = monitorResult.details[0];
    const pnl = typeof first?.pnl === 'number' ? `, ${formatCurrency(first.pnl)}` : '';
    return `✅ Open positions checked, ${monitorResult.trades_closed} closed (${first?.exit_reason ?? first?.status}${pnl})`;
  }, [monitorResult]);

  const startFullProgress = () => {
    setFullProgress({ Indicators: 'running', Scan: 'pending', Execute: 'pending', Monitor: 'pending' });
    window.setTimeout(() => setFullProgress((p) => ({ ...p, Indicators: 'done', Scan: 'running' })), 500);
    window.setTimeout(() => setFullProgress((p) => ({ ...p, Scan: 'done', Execute: 'running' })), 1000);
    window.setTimeout(() => setFullProgress((p) => ({ ...p, Execute: 'done', Monitor: 'running' })), 1500);
  };

  const completeFullProgress = () => {
    setFullProgress({ Indicators: 'done', Scan: 'done', Execute: 'done', Monitor: 'done' });
  };

  return (
    <section className="pipeline-page">
      <div className="developer-strip">
        <span className="developer-icon">⚙</span>
        <div>
          <span className="label">Developer / Testing Tools</span>
          <h2>Manual Pipeline Control</h2>
        </div>
        <button onClick={refreshStatus} disabled={loading.status || anyLoading}>
          {loading.status ? <Spinner /> : null}
          Refresh Status
        </button>
      </div>

      <section className="pipeline-section">
        <div className="section-heading">
          <h3>Pipeline Status</h3>
          <span className="muted">Use this as the top-to-bottom run order.</span>
        </div>
        <div className="status-grid">
          {statusLabels.map((item) => {
            const value = status?.[item.key] ?? null;
            return (
              <div className="card status-card" key={item.key}>
                <span className={`dot ${statusTone(value)}`} />
                <span className="label">{item.label}</span>
                <strong>{timeAgo(value)}</strong>
                <small>{value ?? 'No run recorded yet'}</small>
              </div>
            );
          })}
        </div>
      </section>

      <section className="pipeline-section">
        <div className="section-heading">
          <h3>Step-by-Step Controls</h3>
          <span className="muted">Run these manually while validating backend behavior.</span>
        </div>
        <div className="pipeline-controls">
          <div className="card control-card">
            <h4>Compute Indicators</h4>
            <button
              className="primary-action"
              disabled={Boolean(loading.indicators) || anyLoading}
              onClick={() =>
                runRequest('indicators', pipelineApi.runIndicators, (data) => {
                  setIndicatorResult(data);
                })
              }
            >
              {loading.indicators ? <Spinner /> : null}
              Run Indicator Engine
            </button>
            {indicatorResult ? (
              <ResultText>
                ✅ {indicatorResult.symbols_processed} symbols processed,{' '}
                {indicatorResult.indicators_computed.toLocaleString('en-IN')} indicators computed
              </ResultText>
            ) : (
              <ResultText tone="muted">Start here when the database has candle data.</ResultText>
            )}
          </div>

          <div className="card control-card">
            <h4>Run Signal Scan</h4>
            <button
              className="primary-action"
              disabled={!indicatorsReady || Boolean(loading.scan) || anyLoading}
              onClick={() =>
                runRequest('scan', pipelineApi.runScan, (data) => {
                  setScanResult(data);
                  setSelectedSymbol(data.top_picks[0]?.symbol ?? selectedSymbol);
                })
              }
            >
              {loading.scan ? <Spinner /> : null}
              Scan Market
            </button>
            {!indicatorsReady ? <ResultText tone="muted">Run indicators before scanning.</ResultText> : null}
            {scanResult ? (
              <ResultText>
                ✅ {scanResult.stocks_scanned} stocks scanned, {scanResult.signals_above_threshold} above threshold
              </ResultText>
            ) : null}
            {topPicks.length > 0 ? (
              <div className="mini-list">
                {topPicks.map((pick) => (
                  <button className="pick-row" key={pick.symbol} onClick={() => setSelectedSymbol(pick.symbol)}>
                    <strong>{pick.symbol}</strong>
                    <DirectionBadge direction={pick.direction} />
                    <span>{formatNumber(pick.weighted_score)}</span>
                  </button>
                ))}
              </div>
            ) : null}
          </div>

          <div className="card control-card">
            <h4>Execute Trade</h4>
            <label className="field-label" htmlFor="symbol-input">Symbol</label>
            <input
              id="symbol-input"
              list="top-pick-symbols"
              placeholder="RELIANCE"
              value={selectedSymbol}
              onChange={(event) => setSelectedSymbol(event.target.value.toUpperCase())}
            />
            <datalist id="top-pick-symbols">
              {topPicks.map((pick) => <option key={pick.symbol} value={pick.symbol} />)}
            </datalist>
            <button
              className="primary-action"
              disabled={!selectedSymbol.trim() || Boolean(loading.execute) || anyLoading}
              onClick={() =>
                runRequest('execute', () => pipelineApi.executeTrade(selectedSymbol), async (data) => {
                  setExecuteResult(data);
                  if (data.trade?.success) {
                    await refreshOpenTrades();
                  }
                })
              }
            >
              {loading.execute ? <Spinner /> : null}
              Validate & Execute
            </button>
            <ExecuteResult result={executeResult} />
          </div>

          <div className="card control-card">
            <h4>Monitor Open Trades</h4>
            <button
              className="primary-action"
              disabled={Boolean(loading.monitor) || anyLoading}
              onClick={() =>
                runRequest('monitor', pipelineApi.monitorTrades, async (data) => {
                  setMonitorResult(data);
                  if (data.trades_closed > 0) {
                    await refreshOpenTrades();
                    await refreshAccount();
                  }
                })
              }
            >
              {loading.monitor ? <Spinner /> : null}
              Check Open Positions
            </button>
            {monitorSummary ? <ResultText>{monitorSummary}</ResultText> : <ResultText tone="muted">No monitor run yet.</ResultText>}
            {monitorResult?.details.length ? (
              <div className="mini-list">
                {monitorResult.details.map((item) => (
                  <div className="closed-row" key={item.trade_id}>
                    <strong>{item.symbol}</strong>
                    <span>{item.exit_reason ?? item.status}</span>
                    <span className={(item.pnl ?? 0) >= 0 ? 'positive' : 'negative'}>{formatCurrency(item.pnl)}</span>
                  </div>
                ))}
              </div>
            ) : null}
          </div>
        </div>
      </section>

      <section className="pipeline-section">
        <div className="full-cycle-panel">
          <div>
            <span className="label">One-click test run</span>
            <h3>Run Full Cycle</h3>
          </div>
          <button
            className="big-action"
            disabled={Boolean(loading.fullCycle) || anyLoading}
            onClick={() => {
              startFullProgress();
              runRequest('fullCycle', pipelineApi.runFullCycle, async (data) => {
                setFullCycleResult(data);
                completeFullProgress();
                await Promise.all([refreshOpenTrades(), refreshAccount()]);
              });
            }}
          >
            {loading.fullCycle ? <Spinner /> : null}
            Run Full Pipeline Cycle
          </button>
        </div>
        <div className="progress-line">
          {fullCycleSteps.map((step) => (
            <div className={`progress-step ${fullProgress[step]}`} key={step}>
              <span>{fullProgress[step] === 'done' ? '✓' : fullProgress[step] === 'running' ? '•' : ''}</span>
              {step}
            </div>
          ))}
        </div>
        {fullCycleResult ? (
          <div className="card summary-card">
            <h4>Cycle Summary</h4>
            <div className="summary-grid">
              <Metric label="Indicators" value={`${fullCycleResult.indicators?.symbols_processed ?? 0} symbols`} />
              <Metric label="Scan" value={`${fullCycleResult.scan?.signals_above_threshold ?? 0} signals`} />
              <Metric label="Executions" value={String(fullCycleResult.trade_executions?.length ?? 0)} />
              <Metric label="Closed" value={String(fullCycleResult.monitoring?.trades_closed ?? 0)} />
            </div>
          </div>
        ) : null}
      </section>

      <section className="pipeline-section two-column">
        <div className="card">
          <div className="section-heading inline">
            <h3>Live Error Log</h3>
            <button onClick={() => setErrors([])} disabled={errors.length === 0}>Clear</button>
          </div>
          {errors.length === 0 ? (
            <div className="empty small">No pipeline errors in this session.</div>
          ) : (
            <div className="error-log">
              {errors.map((entry) => (
                <div className="error-entry" key={entry.id}>
                  <span>{entry.timestamp}</span>
                  <strong>{entry.stage}</strong>
                  <p>{entry.message}</p>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="card">
          <details className="raw-viewer">
            <summary>Raw Response Viewer</summary>
            <pre>{lastResponse ? JSON.stringify(lastResponse, null, 2) : 'No API response yet.'}</pre>
          </details>
        </div>
      </section>
    </section>
  );
}

function ExecuteResult({ result }: { result: ExecuteTradeResponse | null }) {
  if (!result) return <ResultText tone="muted">Pick a symbol after scanning, or type one manually.</ResultText>;
  const approved = result.validation.approved;
  return (
    <div className="execute-blocks">
      <div className={`visual-block ${approved ? 'approved' : 'rejected'}`}>
        <strong>{approved ? '✓ Validation approved' : '× Validation rejected'}</strong>
        <span>{result.validation.reason ?? result.validation.failed_check ?? 'Signal passed all checks.'}</span>
      </div>

      <div className={`visual-block ${approved && result.position ? '' : 'disabled'}`}>
        <strong>Position sizing</strong>
        {approved && result.position ? (
          <table>
            <tbody>
              <tr><td>Entry</td><td>{formatCurrency(result.position.entry_price)}</td></tr>
              <tr><td>Stop Loss</td><td>{formatCurrency(result.position.stop_loss_price)}</td></tr>
              <tr><td>Target</td><td>{formatCurrency(result.position.target_price)}</td></tr>
              <tr><td>Quantity</td><td>{result.position.quantity}</td></tr>
              <tr><td>Capital</td><td>{formatCurrency(result.position.capital_required)}</td></tr>
            </tbody>
          </table>
        ) : (
          <span>Skipped until validation approves.</span>
        )}
      </div>

      <div className={`visual-block ${result.trade?.success ? 'approved' : approved ? 'rejected' : 'disabled'}`}>
        <strong>Trade result</strong>
        {result.trade?.success ? (
          <span>Trade opened with ID {result.trade.trade_id}</span>
        ) : (
          <span>{result.trade?.reason ?? 'Skipped until validation approves.'}</span>
        )}
      </div>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="metric-tile">
      <span className="label">{label}</span>
      <strong>{value}</strong>
    </div>
  );
}
