import { useCallback } from 'react';
import api from '../api/client';
import { useStore } from '../store/store';
import type { AccountSummary, PaperTrade, SignalScore, StrategyRule } from '../types';
import { usePollingResource } from './usePollingResource';

export function useAccount() {
  const { dispatch } = useStore();
  return usePollingResource(
    useCallback(async () => (await api.get<AccountSummary>('/api/account/summary')).data, []),
    useCallback((data) => dispatch({ type: 'SET_ACCOUNT', payload: data }), [dispatch]),
    30000,
  );
}

export function useSignals(skipPolling: boolean) {
  const { dispatch } = useStore();
  return usePollingResource(
    useCallback(async () => (await api.get<SignalScore[]>('/api/signals')).data, []),
    useCallback(
      (data) => {
        dispatch({ type: 'SET_SIGNALS', payload: data });
        dispatch({ type: 'SET_TOP_PICKS', payload: data.filter((s) => s.direction !== 'HOLD').slice(0, 3) });
      },
      [dispatch],
    ),
    15000,
    !skipPolling,
  );
}

export function useTrades(skipPolling: boolean) {
  const { dispatch } = useStore();
  return usePollingResource(
    useCallback(async () => (await api.get<PaperTrade[]>('/api/trades/open')).data, []),
    useCallback((data) => dispatch({ type: 'SET_OPEN_TRADES', payload: data }), [dispatch]),
    15000,
    !skipPolling,
  );
}

export function useTradeHistory() {
  const { dispatch } = useStore();
  return usePollingResource(
    useCallback(async () => (await api.get<PaperTrade[]>('/api/trades/history')).data, []),
    useCallback((data) => dispatch({ type: 'SET_TRADE_HISTORY', payload: data }), [dispatch]),
    60000,
  );
}

export function useStrategy() {
  const { dispatch } = useStore();
  return usePollingResource(
    useCallback(async () => (await api.get<StrategyRule | null>('/api/strategy')).data, []),
    useCallback((data) => dispatch({ type: 'SET_STRATEGY', payload: data }), [dispatch]),
    300000,
  );
}
