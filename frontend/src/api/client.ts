import axios, { AxiosError } from 'axios';
import type {
  ExecuteTradeResponse,
  FullCycleResponse,
  IndicatorRunResponse,
  MonitorTradesResponse,
  PipelineErrorResponse,
  PipelineStatus,
  ScanRunResponse,
  Announcement,
  MarketSentiment,
  NewsArticle,
  ScanSummary,
  StockSentiment,
  StockUniverse,
  TimeframeConfluence,
  UniverseSummary,
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

export const marketDataApi = {
  universeSummary: async () => (await api.get<UniverseSummary>('/api/universe/summary')).data,
  universeStocks: async (params?: { sector?: string; is_fo?: number; limit?: number; offset?: number }) => (
    await api.get<{ items: StockUniverse[]; total: number; limit: number; offset: number }>('/api/universe/stocks', { params })
  ).data,
  scanLatest: async () => (await api.get<ScanSummary>('/api/scan/latest')).data,
  stage1Flagged: async () => (await api.get<any[]>('/api/scan/stage1/flagged')).data,
  stage2Results: async () => (await api.get<any[]>('/api/scan/stage2/results')).data,
  latestNews: async (params?: { symbol?: string; sentiment?: string; urgency?: string; since?: string; limit?: number }) => (
    await api.get<NewsArticle[]>('/api/news/latest', { params })
  ).data,
  refreshNews: async () => (await api.post<{ total_articles: number; new_articles: number }>('/api/news/refresh')).data,
  stockNews: async (symbol: string) => (await api.get<Array<NewsArticle | Announcement>>(`/api/news/stock/${symbol}`)).data,
  announcements: async (limit = 20) => (await api.get<Announcement[]>('/api/news/announcements', { params: { limit } })).data,
  marketSentiment: async () => (await api.get<MarketSentiment>('/api/news/market-sentiment')).data,
  stockSentiment: async (symbol: string) => (await api.get<StockSentiment>(`/api/news/sentiment/${symbol}`)).data,
  confluence: async (symbol: string) => (await api.get<TimeframeConfluence>(`/api/signals/confluence/${symbol}`)).data,
};

export default api;
