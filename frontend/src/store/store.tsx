import { createContext, ReactNode, useContext, useMemo, useReducer } from 'react';
import type { AccountSummary, PaperTrade, SignalScore, StrategyRule } from '../types';

export interface AppState {
  account: AccountSummary | null;
  signals: SignalScore[];
  topPicks: SignalScore[];
  openTrades: PaperTrade[];
  tradeHistory: PaperTrade[];
  activeStrategy: StrategyRule | null;
  wsConnected: boolean;
  lastWsUpdate: string | null;
  globalError: string | null;
}

type Action =
  | { type: 'SET_ACCOUNT'; payload: AccountSummary | null }
  | { type: 'SET_SIGNALS'; payload: SignalScore[] }
  | { type: 'SET_TOP_PICKS'; payload: SignalScore[] }
  | { type: 'SET_OPEN_TRADES'; payload: PaperTrade[] }
  | { type: 'SET_TRADE_HISTORY'; payload: PaperTrade[] }
  | { type: 'SET_STRATEGY'; payload: StrategyRule | null }
  | { type: 'SET_WS_CONNECTED'; payload: boolean }
  | { type: 'SET_WS_UPDATE'; payload: string | null }
  | { type: 'SET_ERROR'; payload: string }
  | { type: 'CLEAR_ERROR' };

const initialState: AppState = {
  account: null,
  signals: [],
  topPicks: [],
  openTrades: [],
  tradeHistory: [],
  activeStrategy: null,
  wsConnected: false,
  lastWsUpdate: null,
  globalError: null,
};

function reducer(state: AppState, action: Action): AppState {
  switch (action.type) {
    case 'SET_ACCOUNT':
      return { ...state, account: action.payload };
    case 'SET_SIGNALS':
      return { ...state, signals: action.payload };
    case 'SET_TOP_PICKS':
      return { ...state, topPicks: action.payload };
    case 'SET_OPEN_TRADES':
      return { ...state, openTrades: action.payload };
    case 'SET_TRADE_HISTORY':
      return { ...state, tradeHistory: action.payload };
    case 'SET_STRATEGY':
      return { ...state, activeStrategy: action.payload };
    case 'SET_WS_CONNECTED':
      return { ...state, wsConnected: action.payload };
    case 'SET_WS_UPDATE':
      return { ...state, lastWsUpdate: action.payload };
    case 'SET_ERROR':
      return { ...state, globalError: action.payload };
    case 'CLEAR_ERROR':
      return { ...state, globalError: null };
    default:
      return state;
  }
}

const StoreContext = createContext<
  { state: AppState; dispatch: React.Dispatch<Action> } | undefined
>(undefined);

export function StoreProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(reducer, initialState);
  const value = useMemo(() => ({ state, dispatch }), [state]);
  return <StoreContext.Provider value={value}>{children}</StoreContext.Provider>;
}

export function useStore() {
  const context = useContext(StoreContext);
  if (!context) {
    throw new Error('useStore must be used inside StoreProvider');
  }
  return context;
}
