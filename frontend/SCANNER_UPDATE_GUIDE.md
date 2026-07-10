# Frontend Scanner Component Update Guide

## Overview

This guide shows how to update the Scanner page to display two-stage scanning results.

---

## Current vs. New UI

### Before (Single-Stage)
```
┌─────────────────────────────────────────┐
│  Scanner                                │
├─────────────────────────────────────────┤
│                                         │
│  [Table showing 15 stocks with scores] │
│                                         │
└─────────────────────────────────────────┘
```

### After (Two-Stage)
```
┌─────────────────────────────────────────┐
│  Scanner                                │
├─────────────────────────────────────────┤
│  Stage 1: Broad Market Scan             │
│  ✓ 2,357 stocks → 211 flagged          │
│  Reasons: 210 F&O, 45 price, 38 volume │
├─────────────────────────────────────────┤
│  Stage 2: Deep Technical Analysis       │
│  ✓ 211 analyzed → 8 candidates         │
│  Threshold: 60 points                   │
├─────────────────────────────────────────┤
│  [Table showing top 8 candidates]       │
│                                         │
└─────────────────────────────────────────┘
```

---

## Implementation Steps

### Step 1: Add New API Hooks

Create or update `src/hooks/useDataHooks.ts`:

```typescript
// Add to existing file

export function useStage1Results() {
  return usePollingResource<Stage1Results>('/api/scan/stage1/latest', 30000);
}

export function useStage2Results() {
  return usePollingResource<Stage2Results>('/api/scan/stage2/latest', 30000);
}

export function useScanHistory() {
  return usePollingResource<ScanHistory[]>('/api/scan/history', 60000);
}

// Type definitions
interface Stage1Results {
  scan_time: string;
  total_scanned: number;
  total_flagged: number;
  flagged_stocks: {
    symbol: string;
    stage1_price_change: number;
    stage1_volume_ratio: number;
    stage1_reason: string;
  }[];
  reason_summary: Record<string, number>;
}

interface Stage2Results {
  scan_time: string;
  total_analyzed: number;
  candidates_above_threshold: number;
  candidates: {
    symbol: string;
    stage2_score: number;
    stage2_direction: string;
    stage2_pattern: string | null;
    final_selected: number;
    stage1_price_change: number;
    stage1_volume_ratio: number;
  }[];
}

interface ScanHistory {
  scan_time: string;
  stage1_count: number;
  stage2_analyzed: number;
  candidates: number;
}
```

### Step 2: Create Summary Card Components

Create `src/components/ScanSummaryCard.tsx`:

```typescript
import React from 'react';

interface ScanSummaryCardProps {
  title: string;
  subtitle: string;
  stats: { label: string; value: string | number }[];
  icon?: React.ReactNode;
}

export function ScanSummaryCard({ title, subtitle, stats, icon }: ScanSummaryCardProps) {
  return (
    <div className="scan-summary-card">
      <div className="card-header">
        {icon && <div className="icon">{icon}</div>}
        <div>
          <h3>{title}</h3>
          <p className="subtitle">{subtitle}</p>
        </div>
      </div>
      
      <div className="stats-grid">
        {stats.map((stat, idx) => (
          <div key={idx} className="stat">
            <span className="label">{stat.label}</span>
            <span className="value">{stat.value}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
```

Add styles to `src/styles.css`:

```css
.scan-summary-card {
  background: white;
  border-radius: 8px;
  padding: 20px;
  box-shadow: 0 2px 4px rgba(0,0,0,0.1);
  margin-bottom: 16px;
}

.scan-summary-card .card-header {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
}

.scan-summary-card .icon {
  font-size: 24px;
  color: #4CAF50;
}

.scan-summary-card h3 {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
  color: #333;
}

.scan-summary-card .subtitle {
  margin: 4px 0 0 0;
  font-size: 14px;
  color: #666;
}

.scan-summary-card .stats-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
  gap: 16px;
}

.scan-summary-card .stat {
  display: flex;
  flex-direction: column;
}

.scan-summary-card .stat .label {
  font-size: 12px;
  color: #999;
  text-transform: uppercase;
  margin-bottom: 4px;
}

.scan-summary-card .stat .value {
  font-size: 20px;
  font-weight: 600;
  color: #333;
}
```

### Step 3: Update Scanner Page

Update `src/pages/Scanner.tsx`:

```typescript
import React from 'react';
import { useStage1Results, useStage2Results } from '../hooks/useDataHooks';
import { ScanSummaryCard } from '../components/ScanSummaryCard';

export function Scanner() {
  const stage1 = useStage1Results();
  const stage2 = useStage2Results();
  
  // Format time
  const formatTime = (timestamp: string) => {
    if (!timestamp) return 'N/A';
    return new Date(timestamp).toLocaleTimeString('en-US', {
      hour: '2-digit',
      minute: '2-digit'
    });
  };
  
  // Calculate stats
  const stage1Stats = stage1.data ? [
    { label: 'Scanned', value: stage1.data.total_scanned.toLocaleString() },
    { label: 'Flagged', value: stage1.data.total_flagged },
    { label: 'Last Scan', value: formatTime(stage1.data.scan_time) },
  ] : [];
  
  const stage2Stats = stage2.data ? [
    { label: 'Analyzed', value: stage2.data.total_analyzed },
    { label: 'Candidates', value: stage2.data.candidates_above_threshold },
    { label: 'Completed', value: formatTime(stage2.data.scan_time) },
  ] : [];
  
  return (
    <div className="scanner-page">
      <h1>Market Scanner</h1>
      
      {/* Stage 1 Summary */}
      <ScanSummaryCard
        title="Stage 1: Broad Market Scan"
        subtitle="Fast scan of all NSE stocks"
        stats={stage1Stats}
        icon="🔍"
      />
      
      {/* Stage 2 Summary */}
      <ScanSummaryCard
        title="Stage 2: Deep Technical Analysis"
        subtitle="Detailed analysis of flagged stocks"
        stats={stage2Stats}
        icon="📊"
      />
      
      {/* Candidates Table */}
      <div className="candidates-section">
        <h2>Top Candidates</h2>
        
        {stage2.data?.candidates.length === 0 && (
          <p className="no-data">No candidates above threshold</p>
        )}
        
        {stage2.data && stage2.data.candidates.length > 0 && (
          <table className="candidates-table">
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Direction</th>
                <th>Score</th>
                <th>Price Change</th>
                <th>Volume</th>
              </tr>
            </thead>
            <tbody>
              {stage2.data.candidates.map((candidate) => (
                <tr key={candidate.symbol}>
                  <td className="symbol">{candidate.symbol}</td>
                  <td>
                    <span className={`direction ${candidate.stage2_direction.toLowerCase()}`}>
                      {candidate.stage2_direction}
                    </span>
                  </td>
                  <td className="score">{candidate.stage2_score.toFixed(1)}</td>
                  <td className={candidate.stage1_price_change >= 0 ? 'positive' : 'negative'}>
                    {candidate.stage1_price_change >= 0 ? '+' : ''}
                    {candidate.stage1_price_change.toFixed(2)}%
                  </td>
                  <td>{candidate.stage1_volume_ratio.toFixed(1)}x</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      
      {/* Loading/Error States */}
      {stage1.loading && <p>Loading Stage 1 data...</p>}
      {stage2.loading && <p>Loading Stage 2 data...</p>}
      {stage1.error && <p className="error">Stage 1 error: {stage1.error}</p>}
      {stage2.error && <p className="error">Stage 2 error: {stage2.error}</p>}
    </div>
  );
}
```

Add table styles to `src/styles.css`:

```css
.scanner-page {
  padding: 20px;
  max-width: 1200px;
  margin: 0 auto;
}

.scanner-page h1 {
  font-size: 24px;
  font-weight: 600;
  margin-bottom: 24px;
  color: #333;
}

.candidates-section {
  background: white;
  border-radius: 8px;
  padding: 20px;
  box-shadow: 0 2px 4px rgba(0,0,0,0.1);
}

.candidates-section h2 {
  font-size: 18px;
  font-weight: 600;
  margin: 0 0 16px 0;
  color: #333;
}

.candidates-table {
  width: 100%;
  border-collapse: collapse;
}

.candidates-table th {
  text-align: left;
  padding: 12px;
  border-bottom: 2px solid #e0e0e0;
  font-size: 12px;
  font-weight: 600;
  color: #666;
  text-transform: uppercase;
}

.candidates-table td {
  padding: 12px;
  border-bottom: 1px solid #f0f0f0;
}

.candidates-table .symbol {
  font-weight: 600;
  color: #333;
}

.candidates-table .direction {
  display: inline-block;
  padding: 4px 8px;
  border-radius: 4px;
  font-size: 12px;
  font-weight: 600;
  text-transform: uppercase;
}

.candidates-table .direction.buy {
  background: #E8F5E9;
  color: #2E7D32;
}

.candidates-table .direction.sell {
  background: #FFEBEE;
  color: #C62828;
}

.candidates-table .direction.hold {
  background: #FFF3E0;
  color: #E65100;
}

.candidates-table .score {
  font-weight: 600;
  color: #333;
}

.candidates-table .positive {
  color: #2E7D32;
}

.candidates-table .negative {
  color: #C62828;
}

.no-data {
  text-align: center;
  color: #999;
  padding: 40px;
  font-size: 14px;
}

.error {
  color: #C62828;
  background: #FFEBEE;
  padding: 12px;
  border-radius: 4px;
  margin-top: 16px;
}
```

---

## Optional: Add Scan History Chart

If you want to visualize scan trends:

```typescript
import React from 'react';
import { useScanHistory } from '../hooks/useDataHooks';

export function ScanHistoryChart() {
  const history = useScanHistory();
  
  if (!history.data || history.data.length === 0) {
    return null;
  }
  
  return (
    <div className="scan-history-chart">
      <h3>Scan History (Last 24 Hours)</h3>
      <div className="chart-container">
        {history.data.map((scan, idx) => (
          <div key={idx} className="bar-group">
            <div className="bar stage1" style={{ height: `${scan.stage1_count / 3}px` }}>
              <span className="value">{scan.stage1_count}</span>
            </div>
            <div className="bar stage2" style={{ height: `${scan.candidates * 10}px` }}>
              <span className="value">{scan.candidates}</span>
            </div>
            <div className="time-label">
              {new Date(scan.scan_time).toLocaleTimeString('en-US', {
                hour: '2-digit',
                minute: '2-digit'
              })}
            </div>
          </div>
        ))}
      </div>
      <div className="legend">
        <span><div className="color stage1"></div> Stage 1 Flagged</span>
        <span><div className="color stage2"></div> Candidates</span>
      </div>
    </div>
  );
}
```

---

## Testing

1. **Start backend:**
```bash
cd backend
uvicorn main:app --reload --port 8000
```

2. **Start frontend:**
```bash
cd frontend
npm run dev
```

3. **Run a scan:**
```bash
python analysis/stage1_scanner.py
python analysis/stage2_scanner.py  # Use symbols from Stage 1
```

4. **Open browser:**
```
http://localhost:5173/scanner
```

You should see:
- Stage 1 summary card with scan metrics
- Stage 2 summary card with analysis results
- Table showing top candidates with scores

---

## Key Points

1. **Polling Frequency:**
   - Stage 1/2 results: Every 30 seconds
   - Scan history: Every 60 seconds

2. **Data Freshness:**
   - Orchestrator updates scan results every 5 minutes during market hours
   - Frontend shows latest completed scan

3. **Error Handling:**
   - Display loading states while fetching
   - Show error messages if API calls fail
   - Fallback to "No data" when scans haven't run yet

4. **Performance:**
   - Use `usePollingResource` hook for automatic updates
   - Limit table rows to top 20 candidates
   - Cache API responses for 30 seconds

---

## Troubleshooting

**Issue:** No data showing
- **Solution:** Run `python test_two_stage_pipeline.py` to populate data

**Issue:** API errors (404)
- **Solution:** Ensure backend is running on port 8000

**Issue:** Stale data
- **Solution:** Check orchestrator is running: `ps aux | grep orchestrator`

**Issue:** Empty candidates
- **Solution:** Normal if threshold is high or market is quiet

---

## Next Steps

After basic implementation works:

1. Add real-time WebSocket updates
2. Add filtering/sorting to candidates table
3. Add "Execute Trade" buttons for each candidate
4. Add detailed view modal with full signal explanation
5. Add historical performance tracking per symbol

The two-stage UI provides clear visibility into the scanning process and helps users understand why specific stocks are selected for trading.
