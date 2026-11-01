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
5. **Event tracking:** Record every device state change (device, campaign, old state, new state, reason and timestamp) in Kafka. The database remains the source of truth for the current state. Kafka provides events

## Device Communication & Firmware Delivery

Sending individual nudges to millions of devices is slow, and many gateways behind NAT cannot be reached directly. I would use three approaches:

- **Persistent connections:** Use MQTT or WebSocket connections to push commands instantly. A few dozen brokers can handle 10–15 million devices. Use random reconnect intervals to avoid traffic spikes after outages.
- **Periodic Inform:** For non-urgent upgrades, mark devices as PENDING and send instructions during their next scheduled check-in.
- **Rate-limited nudges:** Use a queue and worker pool for urgent updates, such as security fixes, with a global request limit.
- **Firmware delivery:** Use a CDN instead of the controller. Upgrading 10 million devices with a 50 MB image requires 500 TB of data transfer.

## Summary

Many stateless controllers sharing a partitioned database, compare-and-set transitions, rollout
in canary-first waves with per-segment halting, persistent device connections in place of nudges
where possible, images from a CDN, and progress tracked through an event stream rather than by
counting rows.
