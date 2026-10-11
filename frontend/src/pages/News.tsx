import { useCallback, useEffect, useMemo, useState } from 'react';
import { marketDataApi } from '../api/client';
import type { Announcement, MarketSentiment, NewsArticle, StockSentiment, StockUniverse } from '../types';

function timeAgo(value?: string | null) {
  if (!value) return '-';
  const minutes = Math.max(0, Math.floor((Date.now() - new Date(value).getTime()) / 60000));
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes}m ago`;
  return `${Math.floor(minutes / 60)}h ago`;
}

function sentimentClass(score?: number | null) {
  if ((score || 0) > 0.3) return 'positive-border';
  if ((score || 0) < -0.3) return 'negative-border';
  return 'neutral-border';
}

export default function News() {
  const [tab, setTab] = useState<'all' | 'announcements' | 'impact'>('all');
  const [query, setQuery] = useState('');
  const [sector, setSector] = useState('');
  const [sentiment, setSentiment] = useState('all');
  const [selectedSymbol, setSelectedSymbol] = useState('');
  const [news, setNews] = useState<NewsArticle[]>([]);
  const [announcements, setAnnouncements] = useState<Announcement[]>([]);
  const [stocks, setStocks] = useState<StockUniverse[]>([]);
  const [stockSentiment, setStockSentiment] = useState<Record<string, StockSentiment>>({});
  const [market, setMarket] = useState<MarketSentiment | null>(null);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const load = useCallback(async (refresh = false) => {
    setLoading(true);
    setError('');
    if (refresh) await marketDataApi.refreshNews();

    const [latestResult, announcementsResult, universeResult, marketResult] = await Promise.allSettled([
      marketDataApi.latestNews({ limit: 100 }),
      marketDataApi.announcements(50),
      marketDataApi.universeStocks({ limit: 300 }),
      marketDataApi.marketSentiment(),
    ]);
    const latest = latestResult.status === 'fulfilled' ? latestResult.value : [];
    const official = announcementsResult.status === 'fulfilled' ? announcementsResult.value : [];
    const universe = universeResult.status === 'fulfilled' ? universeResult.value : { items: [] };
    setNews(latest);
    setAnnouncements(official);
    setStocks(universe.items);
    setMarket(marketResult.status === 'fulfilled' ? marketResult.value : null);
    if (latestResult.status === 'rejected') {
      setError(latestResult.reason instanceof Error ? latestResult.reason.message : 'Unable to load the news feed.');
    }
    const symbols = universe.items.slice(0, 80).map((stock) => stock.symbol);
    const results = await Promise.allSettled(symbols.map(async (symbol) => [symbol, await marketDataApi.stockSentiment(symbol)] as const));
    setStockSentiment(Object.fromEntries(results.flatMap((result) => result.status === 'fulfilled' ? [result.value] : [])));
    setLastUpdated(new Date());
    setLoading(false);
  }, []);

  useEffect(() => {
    load().catch((reason) => {
      setError(reason instanceof Error ? reason.message : 'Unable to load the news feed.');
      setLoading(false);
    });
    const interval = window.setInterval(() => load().catch(() => setLoading(false)), 15 * 60_000);
    return () => window.clearInterval(interval);
  }, [load]);

  const sectors = useMemo(() => Array.from(new Set(stocks.map((stock) => stock.sector || 'Unknown'))).slice(0, 12), [stocks]);

  const filteredNews = news.filter((item) => {
    const text = `${item.symbol || ''} ${item.headline || ''}`.toLowerCase();
    const score = item.sentiment_score || 0;
    return (!selectedSymbol || item.symbol === selectedSymbol || String(item.stocks_mentioned || '').includes(selectedSymbol))
      && (!query || text.includes(query.toLowerCase()))
      && (sentiment === 'all' || (sentiment === 'positive' && score > 0.2) || (sentiment === 'negative' && score < -0.2))
      && (tab !== 'impact' || (Math.abs(score) > 0.6 && item.urgency === 'HIGH'));
  });

  const filteredAnnouncements = announcements.filter((item) => {
    const text = `${item.symbol || ''} ${item.headline || ''}`.toLowerCase();
    return (!selectedSymbol || item.symbol === selectedSymbol) && (!query || text.includes(query.toLowerCase()));
  });

  const visibleStocks = stocks.filter((stock) => !sector || (stock.sector || 'Unknown') === sector);
  const feed = tab === 'announcements' ? filteredAnnouncements : filteredNews;

  return (
    <section className="news-layout">
      <aside className="news-left">
        <button className={!selectedSymbol ? 'active' : ''} onClick={() => setSelectedSymbol('')}>All News</button>
        <input placeholder="Search symbol or keyword" value={query} onChange={(event) => setQuery(event.target.value)} />
        <div className="chip-list">
          {sectors.map((item) => <button key={item} className={sector === item ? 'active' : ''} onClick={() => setSector(sector === item ? '' : item)}>{item}</button>)}
        </div>
        <select value={sentiment} onChange={(event) => setSentiment(event.target.value)}>
          <option value="all">All sentiment</option>
          <option value="positive">Positive</option>
          <option value="negative">Negative</option>
        </select>
        <div className="stock-list">
          {visibleStocks.slice(0, 120).map((stock) => {
            const score = stockSentiment[stock.symbol]?.overall_sentiment || 0;
            return (
              <button key={stock.symbol} className={selectedSymbol === stock.symbol ? 'active stock-button' : 'stock-button'} onClick={() => setSelectedSymbol(stock.symbol)}>
                <i className={score > 0.2 ? 'dot green' : score < -0.2 ? 'dot red' : 'dot grey'} /> {stock.symbol}
              </button>
            );
          })}
        </div>
      </aside>

      <div className="news-main">
        <div className="section-heading">
          <div className="segmented compact-tabs">
            <button className={tab === 'all' ? 'active' : ''} onClick={() => setTab('all')}>All News</button>
            <button className={tab === 'announcements' ? 'active' : ''} onClick={() => setTab('announcements')}>Announcements</button>
            <button className={tab === 'impact' ? 'active' : ''} onClick={() => setTab('impact')}>High Impact</button>
          </div>
          <div className="news-actions">
            <p className="muted">Last updated {lastUpdated ? timeAgo(lastUpdated.toISOString()) : '-'}</p>
            <button disabled={loading} onClick={() => load(true)}>{loading ? 'Refreshing...' : 'Refresh news'}</button>
          </div>
        </div>
        {loading ? <div className="empty">Loading news...</div> : null}
        {!loading && error ? <div className="empty">{error}</div> : null}
        {!loading && feed.length === 0 ? (
          <div className="empty">{tab === 'announcements' ? 'No announcements today.' : 'No news available yet. Use Refresh news to fetch the latest RSS headlines.'}</div>
        ) : null}
        <div className="news-feed">
          {feed.map((item: any) => {
            const score = Number(item.sentiment_score || 0);
            const isAnnouncement = tab === 'announcements' || item.announcement_type;
            return (
              <article className={`news-card ${sentimentClass(score)}`} key={`${isAnnouncement ? 'a' : 'n'}-${item.id}`}>
                <div className="news-card-top">
                  <span className="symbol-badge"><i className={score > 0.2 ? 'dot green' : score < -0.2 ? 'dot red' : 'dot grey'} /> {item.symbol || 'MARKET'}</span>
                  <span className="muted">{timeAgo(item.published_at)}</span>
                </div>
                <h3>{item.headline || 'Untitled announcement'}</h3>
                {isAnnouncement ? <span className="announcement-badge">{item.announcement_type || 'Announcement'}</span> : null}
                <p className="muted">{item.source || item.exchange || 'Exchange'} | Sentiment: {score.toFixed(2)}</p>
                <p>Signal Impact: {Math.round(score * 25)} score boost</p>
                {item.url ? <a href={item.url} target="_blank" rel="noreferrer">Read more</a> : null}
              </article>
            );
          })}
        </div>
      </div>

      <aside className="news-right">
        <div className="card">
          <span className="label">Market Sentiment</span>
          <strong>{market?.overall_score?.toFixed(2) || '0.00'}</strong>
          <div className="sentiment-bar"><span style={{ width: `${Math.min(100, Math.max(0, ((market?.overall_score || 0) + 1) * 50))}%` }} /></div>
        </div>
        <div className="card">
          <span className="label">Sector Sentiment</span>
          <div className="sector-bars">
            {Object.entries(market?.sector_sentiment || {}).slice(0, 8).map(([name, score]) => (
              <span key={name}>{name} <b className={score >= 0 ? 'positive' : 'negative'}>{score.toFixed(2)}</b></span>
            ))}
          </div>
        </div>
        <div className="card">
          <span className="label">Most Discussed</span>
          {(market?.most_discussed || []).map((item) => <p key={item.symbol}>{item.symbol}: {item.article_count} articles</p>)}
          <p>{market?.recent_announcements_count || 0} announcements in last hour</p>
        </div>
      </aside>
    </section>
  );
}
