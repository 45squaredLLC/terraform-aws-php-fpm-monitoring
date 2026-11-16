"""PHP-FPM slow requests from CloudWatch Logs to Slack.

Two entry points, one function:

* Real time. A subscription filter on the slow-log group hands batches of
  entries (one CloudWatch event per entry: the agent splits on the
  "[13-Nov-2026 01:30:12]" header). Each batch becomes at most one message:
  site, script and the frame it was stuck in, grouped and counted. A group
  already posted from this warm container in the last QUIET_MINUTES is skipped
  (best-effort; a cold start forgets).
* Pool full. A second subscription, on the PHP-FPM error log and filtered to
  "reached pm.max_children", posts once per QUIET_MINUTES per server when every
  worker is busy: the warning that comes minutes before sites time out.
* Ban context (optional). With BAN_LOG_GROUP set (the module's ship_fail2ban),
  every alert looks up that server's fail2ban bans from the last
  BAN_WINDOW_SECONDS and names them, so "pool full" reads as "bot X, banned"
  rather than a wall of stack traces.
* Digest. An EventBridge schedule invokes it with {"mode": "digest"}. It runs a
  CloudWatch Logs Insights query over the last DIGEST_HOURS and posts the
  slowest sites, or nothing if there were none.

Delivery (either or both):
* ALERT_TOPIC_ARN + ALERT_FORMAT: an SNS topic. "amazon_q" publishes Amazon Q
  Developer's custom-notification JSON (it drops plain text); "text" publishes
  subject + body for email or anything else.
* SLACK_WEBHOOK_PARAM: name of an SSM SecureString holding a Slack incoming
  webhook URL, read once per container.
"""

from __future__ import annotations

import base64
import calendar
import gzip
import json
import os
import re
import time
import urllib.parse
import urllib.request

import boto3

ALERT_TOPIC_ARN = os.environ.get("ALERT_TOPIC_ARN", "")
ALERT_FORMAT = os.environ.get("ALERT_FORMAT", "amazon_q")
SLACK_WEBHOOK_PARAM = os.environ.get("SLACK_WEBHOOK_PARAM", "")
QUIET_MINUTES = int(os.environ.get("QUIET_MINUTES", "30"))
# Entries older than this are history (the agent reads a file from the start the first
# time it sees it), not news.
MAX_AGE_MINUTES = int(os.environ.get("MAX_AGE_MINUTES", "15"))
SLOW_SECONDS = os.environ.get("SLOW_SECONDS", "10")
SLOW_LOG_GROUP = os.environ.get("SLOW_LOG_GROUP", "")
FPM_LOG_GROUP = os.environ.get("FPM_LOG_GROUP", "")
BAN_LOG_GROUP = os.environ.get("BAN_LOG_GROUP", "")
BAN_WINDOW_SECONDS = int(os.environ.get("BAN_WINDOW_SECONDS", "300"))
DIGEST_HOURS = int(os.environ.get("DIGEST_HOURS", "24"))
SERVER_NAMES: dict[str, str] = json.loads(os.environ.get("SERVER_NAMES", "{}"))
REGION = os.environ.get("AWS_REGION", "us-east-1")
MAX_FRAMES = 6

_clients: dict[str, object] = {}
_webhook: list[str] = []
_recent: dict[tuple[str, str, str], float] = {}

_HEADER = re.compile(r"^\[(?P<ts>[^\]]+)\]\s+\[pool (?P<pool>[^\]]+)\]\s+pid (?P<pid>\d+)")
_SCRIPT = re.compile(r"^script_filename\s*=\s*(?P<path>\S+)")
_FRAME = re.compile(r"^\[0x[0-9a-f]+\]\s+(?P<call>\S+)\s+(?P<file>\S+?)(?::(?P<line>\d+))?$")
_FULL = re.compile(
    r"^\[(?P<ts>[^\]]+)\]\s+WARNING:\s+\[pool (?P<pool>[^\]]+)\] "
    r"server reached pm\.max_children setting \((?P<n>\d+)\)"
)
_BAN = re.compile(r"\[(?P<jail>[^\]]+)\]\s+(?:Restore )?Ban (?P<ip>[0-9A-Fa-f:.]+)")
_SITE = re.compile(r"^/var/www/(?:html/)?(?P<site>[^/]+)/")

DIGEST_QUERY = r"""fields @message, @logStream
| parse @message /script_filename = \/var\/www\/(?:html\/)?(?<site>[^\/\s]+)\//
| parse @message /wp-content\/(?<component>(?:plugins|themes|mu-plugins)\/[^\/\s]+)/
| stats count(*) as slow by @logStream, site, component
| sort slow desc
| limit 25"""


def _client(name: str):
    if name not in _clients:
        region = ALERT_TOPIC_ARN.split(":")[3] if name == "sns" and ALERT_TOPIC_ARN else REGION
        _clients[name] = boto3.client(name, region_name=region)
    return _clients[name]


# --- parsing ---------------------------------------------------------------


def entry_epoch(ts: str) -> float | None:
    """'13-Nov-2026 01:30:12' (PHP-FPM writes the server's local time; UTC on most
    servers) -> epoch seconds, or None if it doesn't parse."""
    try:
        return float(calendar.timegm(time.strptime(ts.strip(), "%d-%b-%Y %H:%M:%S")))
    except ValueError:
        return None


def is_recent(ts: str, now: float) -> bool:
    t = entry_epoch(ts)
    return t is None or now - t <= MAX_AGE_MINUTES * 60


def parse_entry(text: str) -> dict | None:
    """One slow-log entry -> {ts, pid, script, site, frames:[(call, where)]}."""
    lines = [ln.rstrip() for ln in text.strip().splitlines() if ln.strip()]
    if not lines:
        return None
    head = _HEADER.match(lines[0])
    if not head:
        return None
    entry = {"ts": head["ts"], "pid": head["pid"], "script": "", "site": "unknown", "frames": []}
    for ln in lines[1:]:
        m = _SCRIPT.match(ln)
        if m:
            entry["script"] = m["path"]
            s = _SITE.match(m["path"])
            if s:
                entry["site"] = s["site"]
            continue
        f = _FRAME.match(ln)
        if f:
            where = short_path(f["file"]) + (f":{f['line']}" if f["line"] else "")
            entry["frames"].append((f["call"], where))
    return entry


def short_path(path: str) -> str:
    """/var/www/site/wp-content/plugins/jetpack/x.php -> plugins/jetpack/x.php"""
    if "/wp-content/" in path:
        return path.split("/wp-content/", 1)[1]
    for marker in ("/wp-includes/", "/wp-admin/"):
        if marker in path:
            return marker.strip("/") + "/" + path.split(marker, 1)[1]
    m = _SITE.match(path)
    return path[m.end() :] if m else path


def culprit(entry: dict) -> str:
    """The most useful frame: the first inside a plugin or theme, else the top."""
    for call, where in entry["frames"]:
        if where.startswith(("plugins/", "themes/", "mu-plugins/")):
            return f"{call} in {where}"
    if entry["frames"]:
        call, where = entry["frames"][0]
        return f"{call} in {where}"
    return "no stack captured"


def group(entries: list[dict]) -> list[dict]:
    groups: dict[tuple[str, str], dict] = {}
    for e in entries:
        key = (e["site"], culprit(e))
        g = groups.setdefault(
            key,
            {
                "site": e["site"],
                "culprit": key[1],
                "count": 0,
                "sample": e,
                "first": e["ts"],
                "last": e["ts"],
            },
        )
        g["count"] += 1
        g["last"] = e["ts"]
    return sorted(groups.values(), key=lambda g: -g["count"])


def fresh(server: str, groups: list[dict], now: float) -> list[dict]:
    out = []
    for g in groups:
        key = (server, g["site"], g["culprit"])
        if now - _recent.get(key, 0) >= QUIET_MINUTES * 60:
            _recent[key] = now
            out.append(g)
    return out


# --- rendering -------------------------------------------------------------


def logs_link(log_group: str, stream: str, needle: str) -> str:
    """Console link to the stream's events, filtered to `needle`."""

    def enc(s: str) -> str:
        return urllib.parse.quote(urllib.parse.quote(s, safe=""), safe="").replace("%", "$")

    frag = (
        f"logsV2:log-groups/log-group/{enc(log_group)}/log-events/{enc(stream)}"
        f"$3FfilterPattern$3D{enc(chr(34) + needle + chr(34))}"
    )
    return f"https://{REGION}.console.aws.amazon.com/cloudwatch/home?region={REGION}#{frag}"


def recent_bans(stream: str, now: float) -> list[tuple[str, str]] | None:
    """(ip, jails) fail2ban banned on this server in the last BAN_WINDOW_SECONDS.

    None when ban context is off or the lookup failed (the alert still goes out)."""
    if not BAN_LOG_GROUP:
        return None
    try:
        resp = _client("logs").filter_log_events(
            logGroupName=BAN_LOG_GROUP,
            logStreamNames=[stream],
            startTime=int((now - BAN_WINDOW_SECONDS) * 1000),
            endTime=int((now + 60) * 1000),
            filterPattern='"Ban"',
            limit=200,
        )
    except Exception:  # noqa: BLE001 - context is a nice-to-have, never block the alert
        return None
    jails: dict[str, list[str]] = {}
    for ev in resp.get("events", []):
        m = _BAN.search(ev.get("message", ""))
        if m and m["jail"] not in jails.setdefault(m["ip"], []):
            jails[m["ip"]].append(m["jail"])
    return [(ip, ", ".join(js)) for ip, js in jails.items()]


def ban_line(bans: list[tuple[str, str]] | None) -> str:
    if bans is None:
        return ""
    mins = max(1, BAN_WINDOW_SECONDS // 60)
    if not bans:
        return f"No IP was auto-banned on this server in the last {mins} min."
    shown = ", ".join(f"`{ip}` ({jail})" for ip, jail in bans[:8])
    more = f" and {len(bans) - 8} more" if len(bans) > 8 else ""
    return (
        f"Auto-banned in the last {mins} min: {shown}{more}. "
        "Requests from these IPs are now dropped."
    )


def render(server: str, stream: str, groups: list[dict]) -> tuple[str, str]:
    total = sum(g["count"] for g in groups)
    sites = sorted({g["site"] for g in groups})
    plural = "s" if total != 1 else ""
    title = (
        f"Slow PHP on {server}: {total} request{plural} over {SLOW_SECONDS} s "
        f"({', '.join(sites)[:120]})"
    )
    parts = []
    for g in groups[:5]:
        e = g["sample"]
        frames = "\n".join(f"  {c}  {w}" for c, w in e["frames"][:MAX_FRAMES]) or "  (no frames)"
        when = g["first"] if g["first"] == g["last"] else f"{g['first']} to {g['last']}"
        link = logs_link(SLOW_LOG_GROUP, stream, g["site"]) if SLOW_LOG_GROUP else ""
        parts.append(
            f"*{g['site']}* x{g['count']} ({when})\n"
            f"Stuck in: `{g['culprit']}`\n"
            f"Script: `{short_path(e['script']) or e['script']}`\n```\n{frames}\n```"
            + (f"\n<{link}|Open in CloudWatch>" if link else "")
        )
    if len(groups) > 5:
        parts.append(f"...and {len(groups) - 5} more site/frame combinations.")
    parts.append(
        f"Each request held a PHP worker for {SLOW_SECONDS} s or more. "
        "When every worker is held, every site on the server times out."
    )
    bans = ban_line(recent_bans(stream, time.time()))
    if bans:
        parts.append(bans)
    return title, "\n\n".join(parts)


def render_digest(rows: list[dict], hours: int) -> tuple[str, str] | None:
    if not rows:
        return None
    total = sum(int(r.get("slow", 0) or 0) for r in rows)
    title = f"PHP slow requests, last {hours} h: {total}"
    lines = []
    for r in rows:
        server = SERVER_NAMES.get(r.get("@logStream", ""), r.get("@logStream", "?"))
        comp = r.get("component") or "WordPress core / theme code"
        lines.append(f"• *{r.get('site') or 'unknown'}* on {server}: {r.get('slow')} ({comp})")
    return title, "\n".join(lines)


# --- delivery --------------------------------------------------------------


def _amazon_q(title: str, body: str) -> str:
    return json.dumps(
        {
            "version": "1.0",
            "source": "custom",
            "content": {
                "textType": "client-markdown",
                "title": title[:250],
                "description": body[:8000],
            },
        }
    )


def deliver(title: str, body: str) -> int:
    sent = 0
    if ALERT_TOPIC_ARN:
        if ALERT_FORMAT == "amazon_q":
            _client("sns").publish(TopicArn=ALERT_TOPIC_ARN, Message=_amazon_q(title, body))
        else:
            _client("sns").publish(TopicArn=ALERT_TOPIC_ARN, Subject=title[:99], Message=body)
        sent += 1
    if SLACK_WEBHOOK_PARAM:
        if not _webhook:
            p = _client("ssm").get_parameter(Name=SLACK_WEBHOOK_PARAM, WithDecryption=True)
            _webhook.append(p["Parameter"]["Value"])
        payload = {"text": f"*{title}*\n{body}"[:39000]}
        req = urllib.request.Request(
            _webhook[0],
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10).read()
        sent += 1
    return sent


# --- handlers --------------------------------------------------------------


def decode(event: dict) -> dict:
    return json.loads(gzip.decompress(base64.b64decode(event["awslogs"]["data"])))


def handle_pool_full(server: str, stream: str, messages: list[str]) -> dict:
    now = time.time()
    hits = [m for m in (_FULL.match(x.strip()) for x in messages) if m and is_recent(m["ts"], now)]
    if not hits:
        return {"posted": 0}
    if time.time() - _recent.get((server, "pool-full", ""), 0) < QUIET_MINUTES * 60:
        return {"posted": 0, "hits": len(hits)}
    _recent[(server, "pool-full", "")] = time.time()
    first, last = hits[0]["ts"], hits[-1]["ts"]
    when = first if first == last else f"{first} to {last}"
    title = f"PHP pool full on {server} (pool {hits[0]['pool']}, {hits[0]['n']} workers)"
    bans = recent_bans(stream, now)
    if bans:
        title += f": {len(bans)} IP{'s' if len(bans) != 1 else ''} auto-banned"
    plural = "s" if len(hits) != 1 else ""
    body = (
        f"Every PHP worker was busy {len(hits)} time{plural} ({when}). New requests queue "
        "behind them; if it lasts, every site on the server times out and the uptime checks "
        "fail. The slow-request messages show what is holding the workers."
    )
    line = ban_line(bans)
    if line:
        body += f"\n{line}"
    if FPM_LOG_GROUP:
        body += f"\n<{logs_link(FPM_LOG_GROUP, stream, 'max_children')}|Open in CloudWatch>"
    return {"posted": deliver(title, body), "hits": len(hits)}


def handle_logs(event: dict) -> dict:
    data = decode(event)
    if data.get("messageType") != "DATA_MESSAGE":
        return {"posted": 0}
    stream = data.get("logStream", "server")
    server = SERVER_NAMES.get(stream, stream)
    if FPM_LOG_GROUP and data.get("logGroup") == FPM_LOG_GROUP:
        return handle_pool_full(server, stream, [x.get("message", "") for x in data["logEvents"]])
    now = time.time()
    messages = [x.get("message", "") for x in data.get("logEvents", [])]
    entries = [e for e in map(parse_entry, messages) if e and is_recent(e["ts"], now)]
    groups = fresh(server, group(entries), now)
    if not groups:
        return {"posted": 0, "entries": len(entries)}
    title, body = render(server, stream, groups)
    return {"posted": deliver(title, body), "entries": len(entries), "groups": len(groups)}


def run_query(hours: int) -> list[dict]:
    logs = _client("logs")
    end = int(time.time())
    qid = logs.start_query(
        logGroupName=SLOW_LOG_GROUP,
        startTime=end - hours * 3600,
        endTime=end,
        queryString=DIGEST_QUERY,
    )["queryId"]
    for _ in range(60):
        res = logs.get_query_results(queryId=qid)
        if res["status"] in ("Complete", "Failed", "Cancelled", "Timeout"):
            break
        time.sleep(1)
    if res["status"] != "Complete":
        raise RuntimeError(f"Logs Insights query {qid} ended {res['status']}")
    return [{f["field"]: f["value"] for f in row} for row in res["results"]]


def handle_digest(hours: int = DIGEST_HOURS) -> dict:
    out = render_digest(run_query(hours), hours)
    if not out:
        return {"posted": 0}
    return {"posted": deliver(*out)}


def lambda_handler(event, context):
    if isinstance(event, dict) and "awslogs" in event:
        return handle_logs(event)
    if isinstance(event, dict) and event.get("mode") == "digest":
        return handle_digest(int(event.get("hours", DIGEST_HOURS)))
    return {"posted": 0, "ignored": True}
