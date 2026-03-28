# Token & Cost Tracking Feature Implementation

**Status**: In Progress
**Date**: 2026-03-28

## Overview

Implement token tracking and cost comparison features to help users compare local/queue-based LLM inference costs against paid services.

## Database Schema

### New Table: `request_usage`
```sql
CREATE TABLE IF NOT EXISTS request_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    virtual_model TEXT NOT NULL,
    endpoint_name TEXT,
    endpoint_id INTEGER,
    request_type TEXT DEFAULT 'chat',  -- 'chat', 'completion', or 'embedding'
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    cost_estimate REAL DEFAULT 0,
    response_time_ms INTEGER DEFAULT 0,
    created_at INTEGER DEFAULT (strftime('%s', 'now'))
);
```

### New Table: `embedding_usage`
```sql
CREATE TABLE IF NOT EXISTS embedding_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    virtual_model TEXT NOT NULL,
    endpoint_name TEXT,
    endpoint_id INTEGER,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    cost_estimate REAL DEFAULT 0,
    response_time_ms INTEGER DEFAULT 0,
    created_at INTEGER DEFAULT (strftime('%s', 'now'))
);
```

### New Field: `cost_per_1k_tokens` in `virtual_models`
- Type: REAL (default 0.0)
- Description: Cost in USD per 1,000 tokens (user-configurable, optional)

### Indexes
```sql
CREATE INDEX IF NOT EXISTS idx_request_usage_virtual_model ON request_usage(virtual_model);
CREATE INDEX IF NOT EXISTS idx_request_usage_created_at ON request_usage(created_at);
CREATE INDEX IF NOT EXISTS idx_embedding_usage_virtual_model ON embedding_usage(virtual_model);
CREATE INDEX IF NOT EXISTS idx_embedding_usage_created_at ON embedding_usage(created_at);
```

## Implementation Checklist

### Phase 1: Database Setup
- [ ] Add `init_database()` calls for new tables
- [ ] Add `cost_per_1k_tokens` field to virtual_models table (optional, nullable)
- [ ] Create indexes for performance

### Phase 2: Logging Functionality
- [ ] Create `log_chat_usage()` function:
  - Parameters: virtual_model, endpoint_name, endpoint_id, usage_dict, response_time_ms
  - Calculate cost: (total_tokens / 1000) * cost_per_1k_tokens (from virtual_models table)
  - Insert into request_usage table

- [ ] Create `log_completion_usage()` function:
  - Same structure as log_chat_usage

- [ ] Create `log_embedding_usage()` function:
  - Parameters: virtual_model, endpoint_name, endpoint_id, input_tokens, output_tokens, response_time_ms
  - Calculate cost using same formula
  - Insert into embedding_usage table

- [ ] Update `POST /v1/chat/completions` to log after response:
  - Capture token usage from backend response
  - Measure response time
  - Call `log_chat_usage()`

- [ ] Update `POST /v1/completions` to log after response:
  - Capture token usage from backend response
  - Measure response time
  - Call `log_completion_usage()`

- [ ] Update `POST /v1/embeddings` to log after response:
  - Capture token usage from backend response (if available)
  - Measure response time
  - Call `log_embedding_usage()`

### Phase 3: API Endpoints

#### GET /api/admin/usage
- [ ] Create endpoint with parameters:
  - `start_date`: ISO timestamp or None (last 24h default)
  - `end_date`: ISO timestamp or None (now default)
  - `virtual_model`: Filter by virtual model name or None
- [ ] Query aggregation:
  - Total prompt_tokens, completion_tokens, total_tokens
  - Total cost_estimate (sum)
  - Request count
  - Average response_time_ms
- [ ] Daily breakdown query:
  - Group by date (strftime('%Y-%m-%d', datetime(created_at, 'unixepoch')))
  - Count and sum tokens per day
- [ ] Return JSON response structure:
```json
{
  "summary": {
    "total_prompt_tokens": 0,
    "total_completion_tokens": 0,
    "total_tokens": 0,
    "total_cost": 0.0,
    "request_count": 0,
    "avg_response_time_ms": 0.0
  },
  "daily_breakdown": [
    {
      "date": "2026-03-28",
      "prompt_tokens": 0,
      "completion_tokens": 0,
      "requests": 0
    }
  ]
}
```

#### GET /api/admin/usage/by_model
- [ ] Create endpoint with parameters:
  - `start_date`: Optional
  - `end_date`: Optional
- [ ] Query:
  - GROUP BY virtual_model
  - SUM(prompt_tokens, completion_tokens, total_tokens, cost_estimate)
  - COUNT requests
- [ ] Return JSON:
```json
[
  {
    "virtual_model": "model_name",
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
    "cost_estimate": 0.0,
    "request_count": 0
  }
]
```

#### GET /api/admin/usage/by_endpoint
- [ ] Create endpoint with parameters:
  - `start_date`: Optional
  - `end_date`: Optional
- [ ] Query:
  - GROUP BY endpoint_name
  - SUM(prompt_tokens, completion_tokens, total_tokens)
  - COUNT requests
- [ ] Return JSON:
```json
[
  {
    "endpoint_name": "endpoint_name",
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
    "request_count": 0
  }
]
```

#### POST /api/admin/usage/export
- [ ] Create endpoint with parameters:
  - `start_date`: Optional
  - `end_date`: Optional
  - `virtual_model`: Optional filter
- [ ] Query request_usage data
- [ ] Generate CSV with columns:
  - Date, Virtual Model, Endpoint, Request Type, Prompt Tokens, Completion Tokens, Total Tokens, Cost ($), Response Time (ms)
- [ ] Return text/plain CSV content

#### POST /api/admin/virtual-models/<id>/update_cost
- [ ] Create PUT endpoint for updating cost_per_1k_tokens
- [ ] Parameters: cost_per_1k_tokens (float, nullable)
- [ ] Update virtual_models table
- [ ] Return updated model info

### Phase 4: Admin UI Updates

#### New Section: Usage & Cost Tab
- [ ] Add tab navigation button "Usage & Cost"
- [ ] Create tab container with date range picker
- [ ] Summary cards section (4 cards):
  - Total Requests
  - Total Tokens (In/Out)
  - Total Cost
  - Avg Response Time (ms)

#### Token Usage Chart
- [ ] Include Chart.js via CDN: `<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>`
- [ ] Line chart showing:
  - X-axis: Date
  - Y-axis: Tokens (double line - prompt vs completion)
  - Smooth curves
  - Legend visible
- [ ] Chart updates on date range change

#### Daily Breakdown Table
- [ ] Table showing:
  - Date, Prompt Tokens, Completion Tokens, Total Tokens, Requests
  - Sortable by date
  - Paginated if > 20 rows

#### Per-Model Breakdown Table
- [ ] Table showing:
  - Virtual Model, Prompt Tokens, Completion Tokens, Total Tokens, Cost, Requests
  - Sortable by cost or tokens

#### Export Button
- [ ] Button: "Export CSV"
- [ ] On click: POST to /api/admin/usage/export
- [ ] Triggers download with filename: `usage_report_YYYY-MM-DD.csv`

#### Cost Configuration Form
- [ ] In Virtual Models table: Add column "Cost/1K"
- [ ] Edit form in modal: Add input field for "Cost per 1K tokens (USD)"
- [ ] Description: "Optional: Set to compare costs against paid services. Leave blank for free/local."
- [ ] Save via PUT /virtual-models/<id>

### Phase 5: Testing
- [ ] Test database schema initialization
- [ ] Test token logging after chat completion
- [ ] Test token logging after completion request
- [ ] Test token logging after embeddings (when available)
- [ ] Test usage API with various date ranges
- [ ] Test by_model aggregation
- [ ] Test by_endpoint aggregation
- [ ] Test CSV export
- [ ] Test cost calculation with virtual model cost
- [ ] Test cost calculation without virtual model cost (should be $0)
- [ ] Test admin UI loading with empty usage data
- [ ] Test admin UI with real usage data
- [ ] Test date range filtering
- [ ] Test export filename generation

### Phase 6: Documentation
- [ ] Update README.md with new feature description
- [ ] Update CHANGELOG.md with new version entry
- [ ] Add usage API documentation section
- [ ] Add cost configuration guide

## Technical Notes

### Cost Calculation Logic
```python
# Get cost_per_1k_tokens from virtual_models table
# If NULL or 0, cost = $0
# Otherwise: cost = (total_tokens / 1000) * cost_per_1k_tokens
```

### Default Date Range
- If start_date not provided: 24 hours ago
- If end_date not provided: current time

### Embeddings Handling
- Embeddings may or may not have token usage (depends on backend)
- If no usage data, log with all token fields = 0
- Separate table to track embeddings separately

### Response Time Measurement
- Measure time from request start to response complete
- Store in milliseconds
- Average calculated as: SUM(response_time_ms) / COUNT(*)

## Migration Notes
- [ ] Database schema is backward compatible
- [ ] New tables are added, existing tables unchanged
- [ ] Existing functionality remains intact
- [ ] Token logging is optional (won't break if backend doesn't return usage)

## Files to Modify
- [ ] `simple_bridge.py`: Database, logging functions, API endpoints
- [ ] `admin_dashboard.html`: Usage & Cost tab UI
- [ ] `README.md`: Documentation
- [ ] `CHANGELOG.md`: Version notes

## Rollback Plan
If issues arise:
1. Remove new database tables
2. Revert code changes
3. Container restart required
4. No data loss (tables are additive)
