import axios, { AxiosError } from 'axios';
import type {
  ExecuteTradeResponse,
  FullCycleResponse,
  IndicatorRunResponse,
  MonitorTradesResponse,
  PipelineErrorResponse,
  PipelineStatus,
  ScanRunResponse,
} from '../types';

const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000',
  headers: {
    'Content-Type': 'application/json',
  },
});

api.interceptors.request.use((config) => {
  if (import.meta.env.DEV) {
    const fullUrl = `${config.baseURL || ''}${config.url || ''}`;
    console.log(`[api] ${config.method?.toUpperCase() || 'GET'} ${fullUrl}`);
  }
  return config;
});

api.interceptors.response.use(
  (response) => response,
  (error: AxiosError) => {
    if (!error.response) {
      throw new Error('Cannot connect to backend. Is the server running?');
    }
    if (error.response.status === 404) {
      throw new Error('Data not found');
    }
    if (error.response.status === 500) {
      const payload = error.response.data as Partial<PipelineErrorResponse> | undefined;
      throw new Error(payload?.message || 'Server error - check backend logs');
    }
    throw error;
  },
);

export const pipelineApi = {
  status: async () => (await api.get<PipelineStatus>('/api/pipeline/status')).data,
  runIndicators: async () => (
    await api.post<IndicatorRunResponse>('/api/pipeline/run-indicators')
  ).data,
  runScan: async () => (await api.post<ScanRunResponse>('/api/pipeline/run-scan')).data,
  executeTrade: async (symbol: string) => (
    await api.post<ExecuteTradeResponse>('/api/pipeline/execute-trade', { symbol })
  ).data,
  monitorTrades: async () => (
    await api.post<MonitorTradesResponse>('/api/pipeline/monitor-trades')
  ).data,
  runFullCycle: async () => (
    await api.post<FullCycleResponse>('/api/pipeline/run-full-cycle')
  ).data,
};

export default api;
