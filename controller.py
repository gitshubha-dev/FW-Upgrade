import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

import db
import metrics
from config import (MAX_NUDGES, MIN_SAMPLE_SIZE, NOTIFY_TIMEOUT_SEC,
                    RESULT_TIMEOUT_SEC, SIMULATOR_URL, TICK_SECONDS)


@asynccontextmanager
async def lifespan(app):
    """Runs once when the controller starts (before yield) and once when it stops (after yield)."""
    task = asyncio.create_task(scheduler_loop())   # start the loop in the background
    yield                                          # the controller serves requests while we're paused here
    task.cancel()                                  # on shutdown, stop the loop

app = FastAPI(title="Firmware Controller", lifespan=lifespan)

@app.middleware("http")
async def record_request_metrics(request: Request, call_next):
    """Count every request by endpoint, method and status code, and time it."""
    start = time.perf_counter()
    status = "500"                                   # used if the endpoint crashes
    try:
        response = await call_next(request)          # run the actual endpoint
        status = str(response.status_code)
        return response
    finally:                                         # runs whether it succeeded or crashed
        route = request.scope.get("route")
        endpoint = route.path if route else "unmatched"
        metrics.REQUESTS.labels(endpoint, request.method, status).inc()
        metrics.REQUEST_SECONDS.labels(endpoint).observe(time.perf_counter() - start)

FINAL_STATES = ("SUCCEEDED", "FAILED", "TIMED_OUT")            # a device in one of these is done
IN_FLIGHT = ("NOTIFIED", "DOWNLOAD_SENT", "UPGRADING")         # count toward maxConcurrent
AWAITING_RESULT = ("DOWNLOAD_SENT", "UPGRADING")               # order sent; a result may arrive
db.init_db() # create table if missing

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("controller")

def log(device_id, text):
    logger.info("%s  %s", device_id, text)

def move(campaign_id, device_id, new_state, from_state, reason):
    """Compare-and-set a device's state and log the transition. Returns True if it moved."""
    if db.set_state(campaign_id, device_id, new_state, from_state):
        log(device_id, f"{from_state} -> {new_state} ({reason})")
        return True
    return False

def failure_stats(counts):
    """Return (finished devices, failure rate %) from a campaign's state counts."""    
    failed = counts.get("FAILED", 0) + counts.get("TIMED_OUT", 0)
    finished = failed + counts.get("SUCCEEDED", 0)
    rate = round(100 * failed / finished, 1) if finished else 0.0
    return finished, rate

async def scheduler_loop():
    """Run a scheduler tick every TICK_SECONDS, forever."""
    async with httpx.AsyncClient(timeout=5) as client:      # one HTTP client, reused for every nudge
        while True:
            try:
                await scheduler_tick(client)
            except Exception as exc:                        # never let one bad tick kill the loop
                log("scheduler", f"tick failed: {exc!r}")
            await asyncio.sleep(TICK_SECONDS)               # wait without blocking the endpoints


async def scheduler_tick(client):
    """One round: handle timeouts, then work on each running campaign."""
    await handle_timeouts(client)

    for campaign in db.get_campaigns_by_status("RUNNING"):
        campaign_id = campaign["id"]
        counts = db.count_states(campaign_id)
        in_flight = sum(counts.get(state, 0) for state in IN_FLIGHT)
        pending = counts.get("PENDING", 0)

        finished, rate = failure_stats(counts)
        threshold = campaign["failure_threshold_percent"]
        if finished >= MIN_SAMPLE_SIZE and rate > threshold:
            db.set_campaign_status(campaign_id, "HALTED")
            db.reset_notified(campaign_id)
            log("scheduler", f"campaign {campaign_id} HALTED: failure rate {rate}% > {threshold}% "
                             f"after {finished} finished devices")
            continue

        if in_flight == 0 and pending == 0:
            db.set_campaign_status(campaign_id, "COMPLETED")
            log("scheduler", f"campaign {campaign_id} COMPLETED")
            continue

        free_slots = campaign["max_concurrent"] - in_flight
        if free_slots <= 0:
            continue

        for device_id in db.get_pending_devices(campaign_id, free_slots):
            if db.mark_nudged(campaign_id, device_id, "PENDING"):
                await nudge(client, device_id)


async def handle_timeouts(client):
    """Retry or give up on devices that have waited too long in a waiting state."""
    now = time.time()

    # NOTIFIED: nudging again is safe, so retry a few times before giving up.
    for row in db.get_stale_devices("NOTIFIED", now - NOTIFY_TIMEOUT_SEC):
        campaign_id, device_id = row["campaign_id"], row["device_id"]
        if row["nudges"] < MAX_NUDGES:
            # Only re-nudge if it's still NOTIFIED: it may have answered while we awaited another nudge.
            if db.mark_nudged(campaign_id, device_id, "NOTIFIED"):
                log(device_id, f"no callback, nudge {row['nudges'] + 1} of {MAX_NUDGES}")
                metrics.RENUDGES.inc()
                await nudge(client, device_id)
        elif move(campaign_id, device_id, "TIMED_OUT", "NOTIFIED", f"no callback after {MAX_NUDGES} nudges"):
            metrics.TIMEOUTS.labels("NOTIFIED").inc()
    # DOWNLOAD_SENT and UPGRADING: the device may be installing, so never resend. Just give up.
    for state in AWAITING_RESULT:
        for row in db.get_stale_devices(state, now - RESULT_TIMEOUT_SEC):
            if move(row["campaign_id"], row["device_id"], "TIMED_OUT", state, "no result in time"):
                metrics.TIMEOUTS.labels(state).inc()

async def nudge(client, device_id):
    """Send a connection request through the simulator. Delivery doesn't guarantee a callback."""
    try:
        response = await client.post(f"{SIMULATOR_URL}/devices/{device_id}/connection-request")
        metrics.OUTGOING.labels("/devices/{device_id}/connection-request", str(response.status_code)).inc()
        log(device_id, f"NUDGE -> {response.status_code}")
    except httpx.HTTPError as exc:
        metrics.OUTGOING.labels("/devices/{device_id}/connection-request", "error").inc()
        log(device_id, f"NUDGE failed: {exc!r}")

@app.post("/devices/{device_id}/inform")
async def inform(device_id: str, request: Request) -> dict:
    body = await request.json()
    event = body.get("event")
    version = body.get("firmwareVersion")
    tc = body.get("transferComplete")

    text = f"INFORM  event={str(event):<18} version={version}"
    if tc:
        text += f"  commandKey={tc.get('commandKey')} faultCode={tc.get('faultCode')}"
    log(device_id, text)
    metrics.INFORMS.labels(event or "unknown").inc()

    device = db.get_device(device_id)
    if device is None:                             # not part of any campaign
        return {"commands": []}

    campaign_id = device["campaign_id"]
    campaign = db.get_campaign(campaign_id)
    state = device["state"]

    if state in FINAL_STATES:
        # First result wins: later messages are logged, never applied.
        if event in ("TRANSFER_COMPLETE", "BOOT"):
            log(device_id, f"already {state}, {event} ignored")
            metrics.IGNORED.labels("already_final").inc()
    elif state in AWAITING_RESULT:
        # We're waiting for this device's result.
        if event == "TRANSFER_COMPLETE" and tc:
            if tc.get("commandKey") != device["command_key"]:
                log(device_id, "unknown commandKey, ignored")
                metrics.IGNORED.labels("unknown_command_key").inc()
            elif tc.get("faultCode") == 0:
                move(campaign_id, device_id, "SUCCEEDED", state, "TransferComplete faultCode 0")
            else:
                move(campaign_id, device_id, "FAILED", state, f"TransferComplete faultCode {tc.get('faultCode')}")
        elif event == "BOOT":
            # No TransferComplete: the reported version is the evidence.
            if version == campaign["target_version"]:
                move(campaign_id, device_id, "SUCCEEDED", state, "BOOT on target version")
            else:
                move(campaign_id, device_id, "FAILED", state, f"BOOT on old version {version}")
        elif version == campaign["target_version"]:
            # Any check-in on the new version proves the upgrade, even if BOOT and TransferComplete were lost.
            move(campaign_id, device_id, "SUCCEEDED", state, f"{event} on target version")
        elif state == "DOWNLOAD_SENT":
            # Order never acknowledged and still on the old version: it was lost. Resend the same order.
            log(device_id, f"order not acknowledged, resending command_key={device['command_key']}")
            return {"commands": [{
                "type": "DOWNLOAD",
                "commandKey": device["command_key"],
                "url": campaign["image_url"],
                "targetVersion": campaign["target_version"],
            }]}
    elif state == "NOTIFIED" and campaign["status"] == "RUNNING":
        # Waiting for its order, and the campaign is running: send it.
        if version == campaign["target_version"]:
            move(campaign_id, device_id, "SUCCEEDED", state, "already on target version")
            return {"commands": []}

        command_key = str(uuid.uuid4())
        if not db.save_order(campaign_id, device_id, command_key):
            return {"commands": []}             # the device moved on meanwhile (e.g. paused): no order
        log(device_id, f"-> DOWNLOAD command_key={command_key}")
        metrics.DOWNLOADS_SENT.inc()
        return {"commands": [{
            "type": "DOWNLOAD",
            "commandKey": command_key,
            "url": campaign["image_url"],
            "targetVersion": campaign["target_version"],
        }]}

    return {"commands": []}
    


@app.post("/devices/{device_id}/command-response")
async def command_response(device_id: str, request: Request) -> dict:
    body = await request.json()
    status = body.get("status")
    command_key = body.get("commandKey")
    log(device_id, f"RESPONSE commandKey={command_key} status={status} reason={body.get('reason')}")
    metrics.COMMAND_RESPONSES.labels(status or "unknown").inc()
    
    device = db.get_device(device_id)
    if device and device["state"] == "DOWNLOAD_SENT" and command_key == device["command_key"]:
        if status == "ACCEPTED":
            move(device["campaign_id"], device_id, "UPGRADING", "DOWNLOAD_SENT", "ACCEPTED")        
        elif status == "REJECTED":
            move(device["campaign_id"], device_id, "FAILED", "DOWNLOAD_SENT", f"REJECTED {body.get('reason')}")    
    return {}


class DeviceFilter(BaseModel):
    currentVersion: str


class CampaignRequest(BaseModel):
    """The body of POST /campaigns, as specified in the assignment."""
    targetVersion: str
    imageUrl: str
    deviceFilter: DeviceFilter
    maxConcurrent: int = Field(gt=0)    # at least 1
    failureThresholdPercent: float = Field(ge=0, le=100) # a percentage

def get_campaign_or_404(campaign_id):
    """Return the campaign's row, or stop the request with 404 if it doesn't exist."""
    campaign = db.get_campaign(campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return campaign

@app.post("/campaigns/{campaign_id}/pause")
async def pause_campaign(campaign_id: int) -> dict:
    campaign = get_campaign_or_404(campaign_id)
    if campaign["status"] == "PAUSED":
        return {"id": campaign_id, "status": "PAUSED"}          # already paused: nothing to do
    if campaign["status"] != "RUNNING":
        raise HTTPException(status_code=409,
                            detail=f"Cannot pause a campaign that is {campaign['status']}")

    db.set_campaign_status(campaign_id, "PAUSED")
    db.reset_notified(campaign_id)                              # nudged but not ordered: back to PENDING
    log("operator", f"campaign {campaign_id} PAUSED")
    return {"id": campaign_id, "status": "PAUSED"}


@app.post("/campaigns/{campaign_id}/resume")
async def resume_campaign(campaign_id: int) -> dict:
    campaign = get_campaign_or_404(campaign_id)
    if campaign["status"] == "RUNNING":
        return {"id": campaign_id, "status": "RUNNING"}         # already running: nothing to do
    if campaign["status"] != "PAUSED":
        raise HTTPException(status_code=409,
                            detail=f"Cannot resume a campaign that is {campaign['status']}")

    db.set_campaign_status(campaign_id, "RUNNING")
    log("operator", f"campaign {campaign_id} RESUMED")
    return {"id": campaign_id, "status": "RUNNING"}


@app.post("/campaigns", status_code=201)
async def start_campaign(req: CampaignRequest) -> dict:
    """Create a campaign for every device currently on deviceFilter.currentVersion."""
    
    # One campaign at a time: a device's messages are matched to its newest campaign,
    # so two active campaigns over the same devices would starve the older one.
    active = db.get_campaigns_by_status("RUNNING") + db.get_campaigns_by_status("PAUSED")
    if active:
        current = active[0]
        raise HTTPException(
            status_code=409,
            detail=f"Campaign {current['id']} is {current['status']}; finish it before starting another",
        )
    
    current_version = req.deviceFilter.currentVersion
    # Ask the simulator which devices are on the current version.
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"{SIMULATOR_URL}/devices", params={"version": current_version})
    except httpx.HTTPError as exc:
        # No response at all: simulator down, timeout, connection refused.
        metrics.OUTGOING.labels("/devices", "error").inc()
        raise HTTPException(status_code=502, detail=f"Simulator unavailable: {exc}")

    metrics.OUTGOING.labels("/devices", str(response.status_code)).inc()
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Simulator returned {response.status_code}")

    devices = response.json()          # [{"deviceId": ..., "firmwareVersion": ...}, ...]
    if not devices:
        raise HTTPException(status_code=400, detail=f"No devices on version {current_version}")

    device_ids = [d["deviceId"] for d in devices]
    campaign_id = db.create_campaign(
        req.targetVersion, req.imageUrl, current_version,
        req.maxConcurrent, req.failureThresholdPercent, device_ids,
    )
    log("operator", f"campaign {campaign_id} created with {len(device_ids)} devices")
    return {"id": campaign_id, "deviceCount": len(device_ids)}


@app.get("/campaigns/{campaign_id}")
async def campaign_status(campaign_id: int) -> dict:
    campaign = get_campaign_or_404(campaign_id)
    counts = db.count_states(campaign_id)
    finished, failure_rate = failure_stats(counts)

    return {
        "id": campaign_id,
        "status": campaign["status"],
        "targetVersion": campaign["target_version"],
        "totalDevices": sum(counts.values()),
        "counts": counts,
        "failureRatePercent": failure_rate,
        "finishedDevices": finished,
    }

@app.get("/metrics")
def metrics_endpoint():
    """Prometheus scrapes this page."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)