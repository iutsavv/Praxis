import { useCallback, useEffect, useRef, useState } from 'react';

export function usePollingResource<T>(
  fetcher: () => Promise<T>,
  onData: (data: T) => void,
  intervalMs: number,
  enabled = true,
) {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<string | null>(null);
  const mountedRef = useRef(true);

  const refresh = useCallback(async () => {
    try {
      setError(null);
      const data = await fetcher();
      if (!mountedRef.current) return;
      onData(data);
      setLastUpdated(new Date().toISOString());
    } catch (err) {
      if (!mountedRef.current) return;
      setError(err instanceof Error ? err.message : 'Request failed');
    } finally {
      if (mountedRef.current) setLoading(false);
    }
  }, [fetcher, onData]);

  useEffect(() => {
    mountedRef.current = true;
    if (!enabled) {
      setLoading(false);
      return;
    }

    refresh();
    const id = window.setInterval(refresh, intervalMs);
    return () => {
      mountedRef.current = false;
      window.clearInterval(id);
    };
  }, [enabled, intervalMs, refresh]);

  return { loading, error, lastUpdated, refresh };
}
