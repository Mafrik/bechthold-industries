# Bechthold Industries

Bechthold Industries is the product name for this existing AI Market Platform project.

> A local-first market research and paper-trading platform built around explicit capital protection and repeatable strategy analysis. See the [project case study](docs/CASE_STUDY.md) for the architecture, product decisions and honest scope.

PAPER/DEVELOPMENT ONLY. No broker and no real-money execution.

Capital model: €200 BOOTSTRAP; until €350 all equity is trading capital with €50 base max position. At €350+, €200 is immediately reserved and excluded from trading capital while the API waits in BOOTSTRAP for manual `POST /api/capital/phase/confirm` with `{"confirm": true}` and the `X-Phase-Confirmation-Token` header. The pending reservation remains if equity later dips before confirmation. If total equity falls below €200, the protected portion is capped at available equity and trading capital becomes zero. Set `PHASE_CONFIRMATION_TOKEN` in the local environment before confirming; without it the API fails closed. Never put this token in the n8n workflow. After confirmation, only equity above the protected amount is trading capital. At €1000+, risk profile becomes CONSERVATIVE. Risk checks reject zero and negative position values.

The n8n PAPER workflow reads the API's current trading capital and position limit before sizing a candidate. Missing, invalid, or sub-threshold daily changes produce `HOLD`; only finite changes at or above +1% produce a buy candidate. Its final `Edit Fields1` node returns only the configured result fields.

Capital state is stored in PostgreSQL and restored when the API starts. PostgreSQL health is checked before the API container starts; `/health` reports API liveness and `/health/ready` checks database connectivity, which Compose uses for API readiness. Non-finite or non-positive quote and candle prices are rejected before reaching analytics.

Start the platform in PowerShell from the project folder:
`./scripts/start-platform.ps1`

The start script builds and starts the services, waits for API and n8n health checks, then prints this computer's local dashboard URL and any private Wi-Fi/Ethernet IPv4 URLs for the iPhone. Stop services without deleting saved data with `./scripts/stop-platform.ps1`.

For a fresh clone, copy `.env.example` to `.env` and enter your own Alpha Vantage API key before starting. The real `.env` is ignored by Git. `PHASE_CONFIRMATION_TOKEN` is optional until you use the token-protected PAPER capital simulation or manual phase confirmation endpoints.

Open http://localhost:8000 and http://localhost:5678
The API root serves a responsive dashboard for desktop and mobile. The dashboard is read-only except for risk checks and token-gated capital simulation and manual phase confirmation; it does not place orders. Both capital-changing POST endpoints require `X-Phase-Confirmation-Token` and fail closed if the token is missing or invalid.

### Install on your computer and iPhone (local network only)

The dashboard can be installed as a web app; it is not an App Store/IPA application and is not published to the internet. On Windows, open `http://localhost:8000` in Chrome or Edge and use the browser's Install option. To open it on an iPhone, keep Docker running on the computer, connect both devices to the same Wi-Fi, find the computer's local IPv4 address with `Get-NetIPConfiguration`, then open `http://COMPUTER-IP:8000` in iPhone Safari. Use Share → Add to Home Screen → Open as Web App. Allow port 8000 only on the Windows Private network profile and do not configure router port forwarding. The app cannot reach the computer while it is off or disconnected. Market quotes still need outbound internet access to Alpha Vantage; the dashboard itself is not hosted publicly.

The dashboard also includes a PAPER SMA crossover backtest. `GET /api/backtest/run?symbol=IBM&currency=USD&fast=5&slow=20` uses Alpha Vantage daily history, starts with the API's trading capital (never protected capital), limits each entry to the current max position, and estimates a 0.1% fee on each side. A signal observed at a daily close is executed at the next day's open. Open positions in the equity curve are valued at the latest close minus the estimated exit fee, so reported equity and the buy-and-hold comparison both account for round-trip costs. Results include an equity curve, drawdown, win rate, annualized return/volatility (252 trading-day convention), a Sharpe ratio with 0% risk-free rate, and a buy-and-hold comparison using the same max-position exposure and estimated round-trip fees. Capital and results are in EUR. For non-EUR listings, daily prices are converted using the FX open for stock opens and FX close for stock closes; symbols and their listing currency must be entered correctly. Supported currencies are EUR, USD, GBP, CHF, JPY, CAD and AUD. The endpoint is analytical only and does not update account capital or submit orders. Alpha Vantage's `compact` daily response is limited to the latest 100 data points; API plan limits may restrict availability.

The dashboard's Market Watch selector contains NVDA, TSLA, AAPL, MSFT and AMD. Selecting a ticker displays an interactive daily closing-price chart with 1M, 3M and maximum available ranges. The market-history endpoint returns up to 100 daily closes in the listing currency and reuses the one-hour PostgreSQL provider cache; changing chart range makes no new provider request. This is daily historical data, not a real-time quote, and the source's raw series is not adjusted for dividends or splits. The five symbols are a curated watchlist, not a live ranking by traded volume.

The comparison action checks a fixed nine-combination SMA grid on the first 78% (approximately) of history, selects the best training return with drawdown as a tie-breaker, then reports that parameter pair on a later holdout segment. Training and holdout runs start independently with the same trading capital; prior training prices warm up the holdout indicators, but no training-period trades or equity are included in holdout results. This is a basic out-of-sample check, not a guarantee of future returns. Non-EUR backtests require an additional FX history request and can therefore hit provider request limits sooner.

Backtest and comparison summaries are saved in PostgreSQL and the dashboard lists the latest eight runs. History contains PAPER inputs and results only and is separate from the capital-state table. Market quote responses are cached for five minutes and daily equity/FX history for one hour to reduce repeated provider requests. The current daily equity endpoint is raw and unadjusted for splits and dividends, so results can be distorted by corporate actions; adjusted daily history requires a provider entitlement.

Run API regression tests from the PyCharm terminal:
`docker compose exec -T api python -m unittest discover -s tests -v`

To configure manual phase confirmation without editing `.env`, run these commands in the same PyCharm PowerShell terminal and keep that terminal session open:

```powershell
$rng = [Security.Cryptography.RandomNumberGenerator]::Create()
$bytes = New-Object byte[] 32
$rng.GetBytes($bytes)
$env:PHASE_CONFIRMATION_TOKEN = [Convert]::ToBase64String($bytes)
$rng.Dispose()
docker compose up -d --build api
```

Use the same token header for manual PAPER capital simulations. When `/api/capital` reports `phase_transition_pending: true`, confirm the BOOTSTRAP to PROFIT_ONLY transition from that same terminal:

```powershell
$headers = @{ 'X-Phase-Confirmation-Token' = $env:PHASE_CONFIRMATION_TOKEN }
Invoke-RestMethod -Method Post -Uri 'http://localhost:8000/api/capital/phase/confirm' -ContentType 'application/json' -Headers $headers -Body '{"confirm":true}'
```

Example capital simulation (the shared token is required because this endpoint changes saved capital state):

```powershell
$headers = @{ 'X-Phase-Confirmation-Token' = $env:PHASE_CONFIRMATION_TOKEN }
Invoke-RestMethod -Method Post -Uri 'http://localhost:8000/api/capital/simulate' -ContentType 'application/json' -Headers $headers -Body '{"equity":350}'
```

## PostgreSQL backup and restore

Create and validate a compressed PostgreSQL backup in your Windows temporary directory:

```powershell
.\scripts\backup-postgres.ps1
```

Restore a backup for inspection into a **new database** (the script refuses to overwrite an existing database and never targets the active `trading` database):

```powershell
.\scripts\restore-postgres-to-new-database.ps1 -BackupPath "$env:TEMP\ai-market-platform-backups\YOUR-BACKUP.dump" -DatabaseName aimp_restore_check
```

The backup contains your local PAPER application data. Store it securely and remove it when it is no longer needed. Keep Docker Desktop running before using either script.
