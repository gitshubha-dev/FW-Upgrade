import sqlite3
import time
from config import DB_PATH

# Open a connection to the file.
# isolation_level=None: save each statement to disk immediately (autocommit).
# check_same_thread=False: allow FastAPI to use this connection from any thread.

conn = sqlite3.connect(DB_PATH, isolation_level=None, check_same_thread=False)
conn.row_factory = sqlite3.Row

def init_db():
    conn.execute("""
        CREATE TABLE IF NOT EXISTS campaigns (
            id                        INTEGER PRIMARY KEY AUTOINCREMENT,   -- 1, 2, 3... assigned by SQLite
            target_version            TEXT NOT NULL,
            image_url                 TEXT NOT NULL,
            current_version           TEXT NOT NULL,     -- which devices to target, from deviceFilter
            max_concurrent            INTEGER NOT NULL,
            failure_threshold_percent REAL NOT NULL,
            status                    TEXT NOT NULL,     -- RUNNING, PAUSED, HALTED, COMPLETED
            created_at                REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS device_upgrades (
            campaign_id INTEGER NOT NULL,
            device_id   TEXT NOT NULL,
            command_key TEXT,                            -- empty (NULL) until an order is sent
            state       TEXT NOT NULL,
            nudges      INTEGER NOT NULL DEFAULT 0,
            updated_at  REAL NOT NULL,
            PRIMARY KEY (campaign_id, device_id)          -- one row per device per campaign
        )
    """)

    # device_id lookups (every inform) and stale-device scans (every tick) without full table scans.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_device ON device_upgrades (device_id, campaign_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_state_time ON device_upgrades (state, updated_at)")


def get_campaign(campaign_id):
    """Return the campaign's row, or None."""
    return conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()

def get_device(device_id):
    """Return this device's row from the most recent campaign that includes it, or None."""
    return conn.execute(
        "SELECT * FROM device_upgrades WHERE device_id = ? "
        "ORDER BY campaign_id DESC LIMIT 1",          # newest campaign first, take only one
        (device_id,),
    ).fetchone()

def save_order(campaign_id, device_id, command_key):
    """Record the order, only if the device is still NOTIFIED. Returns True if it was saved."""
    cursor = conn.execute(
        "UPDATE device_upgrades SET command_key = ?, state = 'DOWNLOAD_SENT', updated_at = ? "
        "WHERE campaign_id = ? AND device_id = ? AND state = 'NOTIFIED'",
        (command_key, time.time(), campaign_id, device_id),
    )
    return cursor.rowcount == 1

def set_state(campaign_id, device_id, state, from_state):
    """Move a device to `state`, only if it's currently in `from_state`. Returns True if it moved."""
    cursor = conn.execute(
        "UPDATE device_upgrades SET state = ?, updated_at = ? "
        "WHERE campaign_id = ? AND device_id = ? AND state = ?",
        (state, time.time(), campaign_id, device_id, from_state),
    )
    return cursor.rowcount == 1


def count_states(campaign_id):
    """Number of devices in each state for one campaign."""
    rows = conn.execute(
        "SELECT state, COUNT(*) AS n FROM device_upgrades WHERE campaign_id = ? GROUP BY state",
        (campaign_id,),
    ).fetchall()
    return {row["state"]: row["n"] for row in rows} 

def create_campaign(target_version, image_url, current_version, max_concurrent, threshold, device_ids):
    """Insert a campaign and all its devices as PENDING, all or nothing. Returns the campaign id."""
    now = time.time()
    conn.execute("BEGIN")
    try:
        cursor = conn.execute(
            "INSERT INTO campaigns (target_version, image_url, current_version, max_concurrent, "
            "failure_threshold_percent, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (target_version, image_url, current_version, max_concurrent, threshold, "RUNNING", now),
        )
        campaign_id = cursor.lastrowid
        conn.executemany(
            "INSERT INTO device_upgrades (campaign_id, device_id, state, updated_at) VALUES (?, ?, ?, ?)",
            [(campaign_id, device_id, "PENDING", now) for device_id in device_ids],
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return campaign_id

def get_campaigns_by_status(status):
    """All campaigns with this status, e.g. every RUNNING campaign."""
    return conn.execute("SELECT * FROM campaigns WHERE status = ?", (status,)).fetchall()


def set_campaign_status(campaign_id, status):
    """Change a campaign's status, e.g. to COMPLETED."""
    conn.execute("UPDATE campaigns SET status = ? WHERE id = ?", (status, campaign_id))


def get_pending_devices(campaign_id, limit):
    """Up to `limit` device ids still waiting in PENDING."""
    rows = conn.execute(
        "SELECT device_id FROM device_upgrades WHERE campaign_id = ? AND state = 'PENDING' LIMIT ?",
        (campaign_id, limit),
    ).fetchall()
    return [row["device_id"] for row in rows]

def mark_nudged(campaign_id, device_id, from_state):
    """Set NOTIFIED and count the nudge, only if still in `from_state`. Returns True if it moved."""
    cursor = conn.execute(
        "UPDATE device_upgrades SET state = 'NOTIFIED', nudges = nudges + 1, updated_at = ? "
        "WHERE campaign_id = ? AND device_id = ? AND state = ?",
        (time.time(), campaign_id, device_id, from_state),
    )
    return cursor.rowcount == 1


def get_stale_devices(state, older_than):
    """Devices (in any campaign) that have been in this state since before `older_than`."""
    return conn.execute(
        "SELECT campaign_id, device_id, nudges FROM device_upgrades WHERE state = ? AND updated_at < ?",
        (state, older_than),
    ).fetchall()

def reset_notified(campaign_id):
    """Put nudged-but-not-ordered devices back to PENDING (used when a campaign stops)."""
    conn.execute(
        "UPDATE device_upgrades SET state = 'PENDING', nudges = 0, updated_at = ? "
        "WHERE campaign_id = ? AND state = 'NOTIFIED'",
        (time.time(), campaign_id),
    )

def count_all_states():
    """Device counts per (campaign, state), across all campaigns."""
    return conn.execute(
        "SELECT campaign_id, state, COUNT(*) AS n FROM device_upgrades GROUP BY campaign_id, state"
    ).fetchall()


def list_campaigns():
    """Every campaign's id and status."""
    return conn.execute("SELECT id, status FROM campaigns").fetchall()