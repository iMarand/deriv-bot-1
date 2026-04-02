# Risk Sizing Notes

## New CLI flag

Use `--disable-kelly` if you want stake sizing to ignore Kelly and use only:

- `base_stake`
- Martingale multiplier
- hard risk caps

Example:

```bash
python bot.py --token YOUR_TOKEN --account-mode demo --base-stake 1.0 --profit-target 50 --loss-limit -30 --score-threshold 0.50 --disable-kelly
```

## What changed

The bot previously allowed Kelly sizing to become active after enough trade history. That could raise the base stake sharply if recent results looked strong.

Example of old behavior:

- base stake = `1.0`
- after `15` settled trades, Kelly becomes eligible
- if Kelly estimates a strong edge, it can push base stake up toward `max_stake_usd`
- with `max_stake_usd = 50`, a later trade could jump from small stakes to `$50`

That is why you saw a sudden large stake even though earlier trades were around `$1`, `$2`, or `$4`.

## Current behavior

The bot now has an extra safety cap:

- a single new stake cannot be larger than the remaining loss room before `loss_limit`

Example with `--loss-limit -30`:

- current net P/L = `-24`
- remaining loss room = `6`
- even if Kelly or Martingale wants `$50`
- the bot caps the next trade to about `$6`

This prevents one trade from jumping far past the configured loss limit.

## How Kelly works now

When Kelly is enabled:

- it stays off until `min_trades_for_kelly` is reached
- then it estimates edge from recent trade history
- if the estimate is weak or negative, Kelly does nothing useful
- if the estimate is strong, it can increase the base stake
- Martingale is then applied on top of that base
- loss-limit protection still caps the final stake

Request logs now include the sizing source, for example:

```text
sizing=base
sizing=base+martingale(x2.00^1)
sizing=kelly(0.0412)
sizing=kelly(0.0412)+martingale(x2.00^1)
sizing=kelly(0.0412)+loss-cap
```

## What `--disable-kelly` changes

When disabled:

- Kelly never contributes to stake sizing
- the bot uses base stake and Martingale only
- loss-limit protection still applies

So with:

- `--base-stake 1.0`
- `--multiplier 2.0`
- `--disable-kelly`

the typical progression is:

- first trade: `$1`
- after 1 loss: `$2`
- after 2 losses: `$4`
- after 3 losses: `$8`

subject to:

- `max_stake_usd`
- bankroll cap
- circuit breaker
- remaining loss room

## Does changing `loss-limit` affect stake size?

Yes.

`loss-limit` now directly affects the maximum safe next stake.

Example:

- `--loss-limit -10` gives much less room, so large stakes are capped sooner
- `--loss-limit -100` gives more room, so larger Kelly or Martingale stakes are allowed

So reducing loss limit makes the bot more conservative.

## Does changing `profit-target` affect stake size?

Not directly.

`profit-target` only decides when the session stops after reaching enough profit.

It does not directly change the next stake calculation.

## Practical guidance

Use `--disable-kelly` if you want:

- more predictable stake progression
- easier debugging
- smaller sudden jumps

Keep Kelly enabled if you want:

- adaptive sizing based on observed results
- but more variable stake sizes

If you want both safety and simplicity, this is a reasonable command:

```bash
python bot.py --token YOUR_TOKEN --account-mode demo --base-stake 1.0 --profit-target 50 --loss-limit -30 --score-threshold 0.50 --disable-kelly --save-app-json
```

## Requiring known regime

Use `--require-known-regime` if you want the bot to block entries until the regime detector has moved past `UNKNOWN`.

Example:

```bash
python bot.py --token YOUR_TOKEN --account-mode demo --base-stake 1.0 --profit-target 50 --loss-limit -30 --score-threshold 0.50 --disable-kelly --require-known-regime
```

With that flag:

- a symbol must finish regime warmup before it can trade
- startup is slower
- early `UNKNOWN` trades are prevented
- status lines will show `waiting for regime X/200 returns`
