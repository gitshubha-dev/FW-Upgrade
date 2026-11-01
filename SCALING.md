# Scaling to 5–15 million devices

The core design carries over unchanged: a per-device state machine stored in a database, a
scheduler that admits work up to a limit, timeouts on every waiting state, and compare-and-set
state changes. What changes at this scale is the infrastructure around it.

## Challenges with the existing setup

| At 1,000 devices | At 10 million devices |
|---|---|
| One controller process | One process can't hold the inform rate: 10 M devices checking in daily is about 115/s on average, with large spikes after outages |
| SQLite, one writer | Writes are serialized; a full table scan per tick is impossible |
| Scheduler nudges one device at a time | Starting thousands of upgrades per minute needs parallel, rate-limited dispatch |
| `GET /campaigns/{id}` counts rows | Counting 10 M rows per request is too slow |
| One campaign over the whole fleet | A bad image could take down millions of homes before auto-halt reacts |

## Multiple controller instances

- **Stateless controllers behind a load balancer.** Any instance can handle any device's inform,
  because all state is in the database.
- **Postgres (or another shared, row-locking database)** in place of SQLite, with the existing
  indexes on `(device_id, campaign_id)` and `(state, updated_at)`. At 15 M rows per campaign,
  partition `device_upgrades` by campaign, and shard by device ID if one primary isn't enough.
- **Divide scheduler jobs across instances to avoid duplicate processing** of devices and timeouts. Use a shared database/Redis counter to enforce the global concurrent firmware upgrade limit across all instances.

## Rollout in waves, not one campaign

Upgrading 10 M devices at once is the real risk. A campaign becomes a sequence of waves:

1. **Canary:** Start with around 1,000 devices covering different models and regions. Wait for all upgrades to complete before proceeding.
2. **Gradual rollout:** Increase the rollout in stages: 1% → 10% → 50% → 100%. Check the failure rate after each stage.
3. **Stop when issues occur:** Monitor failures by device model, region and firmware version. Stop the rollout for the affected group if problems occur, without stopping the entire rollout. Ensure enough devices have completed the upgrade before evaluating failures.
4. **Consider customers:** Schedule upgrades during night-time in each region. Limit the number of devices rebooting at the same time in a neighbourhood or on the same network element.

## Nudge REST API or something else?

The nudge (connection request) doesn't scale well: many gateways sit behind NAT or carrier-grade
NAT and can't be reached, and nudging millions individually is slow. I would use three paths:

- **Persistent connections (TR-369 / USP over MQTT or WebSocket)** for devices that support it.
  The device keeps a connection to a broker cluster, so the controller pushes commands instantly
  with no nudge. Each broker holds hundreds of thousands of idle connections, so 10–15 M devices
  need tens of brokers. Devices reconnect with random jitter to avoid storms after outages.
- **Wait for the periodic inform** for non-urgent upgrades on legacy TR-069 devices: mark them
  PENDING and hand out the order on their next scheduled check-in. It costs nothing, but takes up
  to one inform interval.
- **Rate-limited nudges** only for urgent cases (security fixes), dispatched from a queue by a
  worker pool with a global rate limit, rather than from a scheduler loop.

The firmware image itself is served from a **CDN**, never from the controller: 10 M × 50 MB is
500 TB of transfer.

## Tracking, recording and reporting

- **Every state transition is published as an event** (device, campaign, from, to, reason,
  timestamp) to a stream such as Kafka. The controller already logs exactly these transitions;
  at scale they become events. The database stays the source of truth for each device's current
  state; the stream is for everything downstream.
- **During the campaign:** a consumer keeps running counts per campaign, wave, state, model and
  region, so dashboards and the auto-halt check read pre-computed totals instead of counting rows.
  Prometheus keeps the operational metrics (request rates, errors, latency, nudge and timeout
  rates); per-device detail stays out of metric labels.
- **After the campaign:** the event stream lands in a data warehouse for reporting: failure rate
  by model, region and fault code; time to upgrade; devices that never reported; reverts. It also
  gives a per-device history, so support staff can answer "what happened to this customer's
  gateway?"
- **Reconciliation:** devices left TIMED_OUT are re-checked later against the version they report
  in subsequent informs, so the final report reflects what devices actually run, not just what the
  campaign observed.

## Summary

Many stateless controllers sharing a partitioned database, compare-and-set transitions, rollout
in canary-first waves with per-segment halting, persistent device connections in place of nudges
where possible, images from a CDN, and progress tracked through an event stream rather than by
counting rows.
