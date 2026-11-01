# Scaling to 5–15 Million Devices

## Challenges with the Existing Setup

| At 1,000 devices | At 10 million devices |
|---|---|
| One controller process | Cannot handle the high volume of device check-ins and traffic spikes. |
| SQLite with one writer | Writes happen one at a time. Scanning millions of records is too slow. |
| Scheduler sends nudges one by one | Needs parallel processing with rate limits to start thousands of upgrades per minute. |
| API counts campaign records on every request | Counting millions of records for every request is too slow. |
| One campaign for all devices | A faulty firmware image could affect millions of homes before the auto-halt kicks in. |

## Multiple Controller Instances

- **Stateless controllers behind a load balancer:** Run multiple controller instances. Any instance can handle a device check-in because all instances share the same database.

- **Use partitioning & sharding:** Replace SQLite with PostgreSQL or another database that supports row-level locking. Use DB indexes, partition by campaign and, if needed, distribute data by device ID across multiple databases.

- **Distribute scheduler jobs:** Divide jobs across controller instances to avoid processing the same device twice. Use a shared database or Redis counter to enforce the overall limit on simultaneous firmware upgrades.

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
- **Firmware delivery:** Use a CDN to distribute the firmware images across different regions

