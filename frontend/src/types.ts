export type Direction = 'BUY' | 'SELL' | 'HOLD';

export interface AccountSummary {
  id?: number;
  balance?: number;
  current_balance?: number;
  equity?: number;
  realized_pnl?: number;
  unrealized_pnl?: number;
  open_trades?: number;
  closed_trades?: number;
  initial_balance?: number;
  reserved_amount?: number;
  total_pnl?: number;
  daily_pnl?: number;
  total_trades?: number;
  winning_trades?: number;
  losing_trades?: number;
  win_rate?: number;
  max_drawdown?: number;
  peak_balance?: number;
  open_positions_count?: number;
  timestamp?: string;
  created_at?: string;
  updated_at?: string;
  last_updated?: string;
}

export interface SignalScore {
  id: number | null;
  symbol: string;
  scan_cycle: string | null;
  confidence_score: number;
  direction: Direction;
  market_condition: string;
  contrib_rsi: number;
  contrib_macd: number;
  contrib_volume: number;
  contrib_vwap: number;
  explanation: string;
  created_at: string | null;
}

export interface PaperTrade {
  id: number;
  symbol: string;
  direction: 'BUY' | 'SELL';
  quantity: number;
  entry_price: number;
  entry_time: string;
  entry_reason?: string | null;
  target_price?: number | null;
  stop_loss_price?: number | null;
  exit_price?: number | null;
  exit_time?: string | null;
  exit_reason?: string | null;
  pnl?: number | null;
  outcome?: string | null;
  status: 'OPEN' | 'CLOSED';
  current_price?: number;
  unrealized_pnl?: number;
}

export interface StrategyRule {
  id: number;
  version: number;
  is_active: number;
  weight_rsi: number;
  weight_macd: number;
  weight_volume: number;
  weight_vwap: number;
  min_score_to_trade: number;
  max_open_trades: number;
  risk_per_trade_pct: number;
  best_entry_window: string | null;
  trade_in_sideways?: number;
  notes?: string | null;
  created_at: string;
  updated_at: string;
}

export interface StrategyHistoryRow extends StrategyRule {
  learning_win_rate?: number | null;
  trades_analyzed?: number | null;
}

export interface LearningInsight {
  id: number;
  strategy_version?: number | null;
  trades_analyzed?: number | null;
  total_trades?: number | null;
  win_rate?: number | null;
  best_time_window?: string | null;
  best_market_condition?: string | null;
  worst_signal?: string | null;
  summary?: string | null;
  created_at: string;
}

export interface LearningRunResponse {
  skipped: boolean;
  reason?: string;
  new_strategy_version?: number;
  what_changed?: string;
  findings?: Record<string, unknown>;
  report?: string;
}

export interface PingResponse {
  status: string;
  timestamp: string;
  database: string;
  total_candles: number;
  total_trades: number;
  market_open: boolean;
  orchestrator_alive: boolean;
  last_run: string | null;
  next_run: string | null;
  cycle_interval_minutes: number;
  cycle_number: number;
  consecutive_failures: Record<string, number>;
}

export interface PipelineStatus {
  status: string;
  indicators: string | null;
  scan: string | null;
  trade_execution: string | null;
  monitoring: string | null;
}

export interface PipelineErrorResponse {
  status: 'error';
  stage: string;
  message: string;
  traceback?: string;
}

export interface IndicatorRunResponse {
  status: string;
  symbols_processed: number;
  indicators_computed: number;
  errors: { symbol?: string; error: string }[];
}

export interface TopPick {
  symbol: string;
  direction: Direction;
  weighted_score: number;
  market_condition: string;
  explanation?: string;
}

export interface ScanRunResponse {
  status: string;
  stocks_scanned: number;
  signals_above_threshold: number;
  top_picks: TopPick[];
  errors?: { symbol?: string; error: string }[];
}

export interface ValidationResult {
  approved: boolean;
  reason?: string;
  failed_check?: string;
}

export interface PositionResult {
  symbol: string;
  direction: 'BUY' | 'SELL';
  entry_price: number;
  stop_loss_price: number;
  target_price: number;
  quantity: number;
  risk_per_share: number;
  max_loss_amount: number;
  capital_required: number;
  capital_constrained: boolean;
}

export interface TradeResult {
  success: boolean;
  trade_id?: number;
  reason?: string;
  failed_check?: string;
}

export interface ExecuteTradeResponse {
  validation: ValidationResult;
  position: PositionResult | null;
  trade: TradeResult | null;
}

export interface MonitorTradeDetail {
  trade_id: number;
  symbol: string;
  status: string;
  exit_reason?: string | null;
  outcome?: string | null;
  exit_price?: number;
  pnl?: number;
  pnl_pct?: number;
}

export interface MonitorTradesResponse {
  status: string;
  trades_closed: number;
  details: MonitorTradeDetail[];
  trades_checked?: number;
}

export interface FullCycleResponse {
  status: string;
  started_at?: string;
  indicators?: IndicatorRunResponse;
  scan?: ScanRunResponse;
  trade_executions?: { symbol: string; result: ExecuteTradeResponse | PipelineErrorResponse }[];
  monitoring?: MonitorTradesResponse;
  finished_at?: string;
  [key: string]: unknown;
}
