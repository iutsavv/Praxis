import { useEffect, useRef, useState } from 'react';
import { useStore } from '../store/store';
import type { PaperTrade, SignalScore } from '../types';

type LiveMessage =
  | { type: 'signal_update'; data: SignalScore[]; timestamp: string }
  | { type: 'trade_update'; data: PaperTrade[]; timestamp: string };

export function useWebSocket() {
  const { dispatch } = useStore();
  const [connected, setConnected] = useState(false);
  const [lastUpdate, setLastUpdate] = useState<string | null>(null);
  const retryRef = useRef(0);
  const socketRef = useRef<WebSocket | null>(null);
  const retryTimerRef = useRef<number | null>(null);
  const unmountedRef = useRef(false);

  useEffect(() => {
    const wsUrl = import.meta.env.VITE_WS_URL || 'ws://localhost:8000/ws/live';

    const connect = () => {
      if (unmountedRef.current) return;
      const socket = new WebSocket(wsUrl);
      socketRef.current = socket;

      socket.onopen = () => {
        retryRef.current = 0;
        setConnected(true);
        dispatch({ type: 'SET_WS_CONNECTED', payload: true });
        dispatch({ type: 'CLEAR_ERROR' });
      };

      socket.onmessage = (event) => {
        try {
          const message = JSON.parse(event.data) as LiveMessage;
          if (message.type === 'signal_update') {
            dispatch({ type: 'SET_SIGNALS', payload: message.data });
            dispatch({
              type: 'SET_TOP_PICKS',
              payload: message.data.filter((signal) => signal.direction !== 'HOLD').slice(0, 3),
            });
          }
          if (message.type === 'trade_update') {
            dispatch({ type: 'SET_OPEN_TRADES', payload: message.data });
          }
          setLastUpdate(message.timestamp);
          dispatch({ type: 'SET_WS_UPDATE', payload: message.timestamp });
        } catch {
          dispatch({ type: 'SET_ERROR', payload: 'Invalid live update received' });
        }
      };

      socket.onclose = () => {
        setConnected(false);
        dispatch({ type: 'SET_WS_CONNECTED', payload: false });
        if (unmountedRef.current) return;
        if (retryRef.current < 5) {
          retryRef.current += 1;
          retryTimerRef.current = window.setTimeout(connect, 3000);
        } else {
          dispatch({ type: 'SET_ERROR', payload: 'Connection lost' });
        }
      };

      socket.onerror = () => {
        socket.close();
      };
    };

    unmountedRef.current = false;
    connect();

    return () => {
      unmountedRef.current = true;
      if (retryTimerRef.current) {
        window.clearTimeout(retryTimerRef.current);
      }
      socketRef.current?.close();
      socketRef.current = null;
    };
  }, [dispatch]);

  return { connected, lastUpdate };
}
