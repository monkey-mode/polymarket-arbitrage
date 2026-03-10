# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Polymarket autonomous arbitrage bot that discovers Bitcoin 5-minute prediction markets, streams real-time prices via WebSocket, evaluates Dutch-Book arbitrage opportunities (YES_ask + NO_ask < $1.00 - fees), and executes profitable FOK trades on both sides simultaneously.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run tests
pytest tests/ -v

# Run single test file
pytest tests/test_strategy.py -v

# Paper trading (dry-run, no real orders)
python src/v2/main.py --dry-run

# Paper trading, stop after first trade
python src/v2/main.py --dry-run --one-shot

# Live trading
python src/v2/main.py

# Start Redis + Subscriber service
docker-compose up -d
```

## Architecture

The codebase has two implementations:
- **`src/v1/`** — Legacy flat structure (reference only)
- **`src/v2/`** — Active layered architecture (use this)

### V2 Module Layout

| Layer | Path | Responsibility |
|-------|------|----------------|
| Chain | `src/v2/chain/blockchain.py` | Web3, gas, nonce, approvals, Polygon txns |
| Exchange | `src/v2/exchange/` | CLOB client, Gamma API discovery, WebSocket prices |
| Trading | `src/v2/trading/` | Evaluator, executor, merger, strategy orchestrator |
| Infra | `src/v2/infra/redis_logger.py` | Async Redis pub/sub + rotating file logs |

### Core Trading Flow

```
main.py
  └─ DiscoveryManager → Gamma API → next BTC 5m market (YES/NO token IDs)
  └─ WebSocketStreamer → wss://ws-subscriptions-clob.polymarket.com/ws/market
      └─ on best_bid_ask → ArbitrageStrategy
          ├─ MarketEvaluator  → pure math: is YES_ask + NO_ask + fees < $1.00?
          ├─ OrderExecutor    → FOK orders both sides; handles legging risk
          └─ PositionMerger   → CTF.mergePositions() → USDC
```

### Key Design Decisions

**Arbitrage logic** (`evaluator.py`): Fee formula is `amount × rate × (1 - (2 × |price - 0.5|)^exponent)`. Crypto markets: rate=0.0075, exponent=2. Profit threshold is strictly positive.

**Legging safety** (`executor.py`): If only one side of a FOK fills (partial execution), the executor cancels the other and issues emergency market-sell on the orphan position to avoid holding unhedged exposure.

**Gas management** (`blockchain.py`): Dynamic gas = `base_fee × 1.5 + 150 Gwei priority`. Retry loop bumps gas on underpriced failures; max 3 attempts, 120s timeout.

**Market discovery** (`discovery.py`): Predicts BTC 5-minute market slugs as `btc-updown-5m-{UNIX_TIMESTAMP}` and probes Gamma API for 3 time windows (current + 2 future). Filters only markets with active order books.

**Position merging** (`merger.py`): Calls `mergePositions()` on Polygon CTF contract (`0x4D97DCd97eC945f40cF65F87097ACe5EA0476045`) with partition `[1, 2]` (YES=1, NO=2) to convert token pairs back to USDC.

## Configuration

Copy `.env.example` to `.env`. Key variables:

```
WALLET_PRIVATE_KEY      # Transaction signing key
WALLET_ADDRESS          # EOA address
FUNDER_ADDRESS          # Proxy wallet (holds funds)
CHAIN_ID=137            # Polygon mainnet
WEB3_PROVIDER_URI       # Polygon RPC (e.g. https://polygon-rpc.com)
HOST=https://clob.polymarket.com
SIGNATURE_TYPE          # 0=EOA, 1=Proxy, 2=Gnosis Safe
REDIS_HOST/REDIS_PORT   # Redis connection
```

Fee config and contract addresses are hardcoded in `src/v2/config.py`.

## Agent Skills Reference

`agent-skills/` contains Polymarket integration documentation for AI-assisted development:
- `SKILL.md` — Entry point (~200 lines): client setup, order types, key APIs
- Deeper files (`authentication.md`, `order-patterns.md`, `websocket.md`, `ctf-operations.md`) — load on demand for specific topics

## External Dependencies

- **py-clob-client** — Polymarket CLOB SDK (order placement, book reads)
- **web3** — Polygon transaction signing and contract calls
- **redis** (async) — Logging pub/sub channel `polymarket_bot_logs`
- **websockets** — Price streaming from Polymarket WebSocket

## Logging

- Paper trades: `paper_trades.json`
- Rotating logs: `logs/market_evaluations_*.log` (1MB, 5 backups)
- Redis channel: `polymarket_bot_logs` (consumed by `src/v2/subscriber.py` in Docker)
