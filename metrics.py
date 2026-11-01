"""Prometheus metrics for the controller, served at GET /metrics.

Counters are updated by the controller as things happen. Device and campaign
counts are read from the database each time /metrics is fetched, so they are
always correct, including right after a restart.
"""

from prometheus_client import REGISTRY, Counter, Histogram
from prometheus_client.core import GaugeMetricFamily

import db

# Incoming HTTP requests, recorded once per request by the middleware in controller.py.
# endpoint is the route template (e.g. /devices/{device_id}/inform), not the real URL,
# so there is one series per endpoint instead of one per device.
REQUESTS = Counter(
    "controller_requests_total",
    "HTTP requests received by the controller",
    ["endpoint", "method", "status"],
)
REQUEST_SECONDS = Histogram(
    "controller_request_duration_seconds",
    "Time taken to answer an HTTP request",
    ["endpoint"],
)

# Requests the controller sends to the simulator.
# status is the HTTP status code, or "error" if no response came back.
OUTGOING = Counter(
    "controller_outgoing_requests_total",
    "HTTP requests sent by the controller to devices/simulator",
    ["endpoint", "status"],
)

# What devices tell us.
INFORMS = Counter("controller_informs_total", "Device informs received, by event", ["event"])
COMMAND_RESPONSES = Counter(
    "controller_command_responses_total", "Device answers to commands", ["status"]
)
IGNORED = Counter(
    "controller_ignored_messages_total",
    "Device messages logged but not applied (duplicates, late or unknown)",
    ["reason"],
)

# What the controller decides.
DOWNLOADS_SENT = Counter("controller_downloads_sent_total", "DOWNLOAD commands issued")
RENUDGES = Counter("controller_renudges_total", "Nudges repeated after no callback")
TIMEOUTS = Counter(
    "controller_timeouts_total", "Devices moved to TIMED_OUT, by the state they timed out in",
    ["from_state"],
)


# Every device state, so a state with no devices is reported as 0 instead of disappearing.
DEVICE_STATES = ("PENDING", "NOTIFIED", "DOWNLOAD_SENT", "UPGRADING", "SUCCEEDED", "FAILED", "TIMED_OUT")


class CampaignCollector:
    """Reads device and campaign counts from the database on every scrape."""

    @staticmethod
    def _families():
        devices = GaugeMetricFamily(
            "controller_campaign_devices",
            "Devices currently in each state, per campaign",
            labels=["campaign", "state"],
        )
        status = GaugeMetricFamily(
            "controller_campaign_status",
            "1 for the campaign's current status",
            labels=["campaign", "status"],
        )
        return devices, status

    def describe(self):
        # Lets the registry learn the metric names without querying the database.
        return list(self._families())

    def collect(self):
        devices, status = self._families()
        counts = {(row["campaign_id"], row["state"]): row["n"] for row in db.count_all_states()}
        for row in db.list_campaigns():
            campaign_id = row["id"]
            status.add_metric([str(campaign_id), row["status"]], 1)
            for state in DEVICE_STATES:
                devices.add_metric([str(campaign_id), state], counts.get((campaign_id, state), 0))
        return [devices, status]


REGISTRY.register(CampaignCollector())
