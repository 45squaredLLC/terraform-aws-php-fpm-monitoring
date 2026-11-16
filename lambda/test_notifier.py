"""Run: python3 -m pytest modules/php-fpm-monitoring/lambda (no AWS calls; clients are faked)."""

import base64
import gzip
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
os.environ["ALERT_TOPIC_ARN"] = "arn:aws:sns:us-west-2:111111111111:alerts"
os.environ["SLOW_LOG_GROUP"] = "/php-fpm/slow"
os.environ["FPM_LOG_GROUP"] = "/php-fpm/error"
os.environ["SERVER_NAMES"] = json.dumps({"i-0abc": "ClientServer"})
import notifier as n  # noqa: E402

# Fixtures are stamped 13-Nov-2026; pin "now" just after them.
NOW = n.entry_epoch("13-Nov-2026 01:31:00")

ENTRY = """[13-Nov-2026 01:30:12]  [pool www] pid 2877196
script_filename = /var/www/sentencesermons.com/wp-cron.php
[0x00007f3a1c215e40] curl_exec() /var/www/sentencesermons.com/wp-includes/Requests/src/Transport/Curl.php:205
[0x00007f3a1c215d10] request() /var/www/sentencesermons.com/wp-includes/class-wp-http.php:394
[0x00007f3a1c215b80] post() /var/www/sentencesermons.com/wp-content/plugins/jetpack/jetpack_vendor/automattic/jetpack-sync/src/class-sender.php:512
[0x00007f3a1c2159f0] do_sync() /var/www/sentencesermons.com/wp-content/plugins/jetpack/jetpack_vendor/automattic/jetpack-sync/src/class-sender.php:300
"""


class FakeSNS:
    def __init__(self):
        self.sent = []

    def publish(self, **kw):
        self.sent.append(kw)


class FakeLogs:
    def __init__(self, rows):
        self.rows = rows
        self.query = None

    def start_query(self, **kw):
        self.query = kw
        return {"queryId": "q1"}

    def get_query_results(self, queryId):
        return {
            "status": "Complete",
            "results": [[{"field": k, "value": v} for k, v in r.items()] for r in self.rows],
        }


def _event(messages, stream="i-0abc", kind="DATA_MESSAGE", group="/php-fpm/slow"):
    payload = {
        "messageType": kind,
        "logStream": stream,
        "logGroup": group,
        "logEvents": [{"id": str(i), "timestamp": 0, "message": m} for i, m in enumerate(messages)],
    }
    return {
        "awslogs": {"data": base64.b64encode(gzip.compress(json.dumps(payload).encode())).decode()}
    }


def setup_function(_):
    n._recent.clear()
    n._clients.clear()
    n.time.time = lambda: NOW


def test_parse_entry():
    e = n.parse_entry(ENTRY)
    assert e["site"] == "sentencesermons.com"
    assert e["pid"] == "2877196"
    assert e["frames"][0] == ("curl_exec()", "wp-includes/Requests/src/Transport/Curl.php:205")
    assert len(e["frames"]) == 4


def test_culprit_prefers_plugin_frame():
    assert (
        n.culprit(n.parse_entry(ENTRY))
        == "post() in plugins/jetpack/jetpack_vendor/automattic/jetpack-sync/src/class-sender.php:512"
    )


def test_parse_rejects_garbage():
    assert n.parse_entry("") is None
    assert n.parse_entry("random line") is None


def test_group_counts_identical_entries():
    e = n.parse_entry(ENTRY)
    other = n.parse_entry(ENTRY.replace("sentencesermons.com", "mrbenny.co"))
    assert [(g["site"], g["count"]) for g in n.group([e, e, other])] == [
        ("sentencesermons.com", 2),
        ("mrbenny.co", 1),
    ]


def test_render_and_link():
    title, body = n.render("ClientServer", "i-0abc", n.group([n.parse_entry(ENTRY)] * 2))
    assert title.startswith("Slow PHP on ClientServer: 2 requests over 10 s")
    assert "x2" in body and "class-sender.php:512" in body
    assert "console.aws.amazon.com/cloudwatch" in body and "$252Fphp-fpm$252Fslow" in body


def test_handler_posts_once_then_stays_quiet():
    fake = FakeSNS()
    n._clients["sns"] = fake
    assert n.lambda_handler(_event([ENTRY, ENTRY]), None)["posted"] == 1
    msg = json.loads(fake.sent[0]["Message"])
    assert msg["source"] == "custom" and "ClientServer" in msg["content"]["title"]
    assert n.lambda_handler(_event([ENTRY]), None)["posted"] == 0
    assert len(fake.sent) == 1


def test_handler_ignores_control_messages_and_unknown_events():
    assert n.lambda_handler(_event([], kind="CONTROL_MESSAGE"), None) == {"posted": 0}
    assert n.lambda_handler({"hello": 1}, None)["ignored"] is True


def test_digest_posts_top_sites():
    fake, logs = (
        FakeSNS(),
        FakeLogs(
            [
                {
                    "@logStream": "i-0abc",
                    "site": "sentencesermons.com",
                    "component": "plugins/jetpack",
                    "slow": "12",
                },
                {"@logStream": "i-0abc", "site": "mrbenny.co", "slow": "3"},
            ]
        ),
    )
    n._clients.update(sns=fake, logs=logs)
    assert n.lambda_handler({"mode": "digest"}, None) == {"posted": 1}
    assert logs.query["logGroupName"] == "/php-fpm/slow"
    desc = json.loads(fake.sent[0]["Message"])["content"]
    assert desc["title"] == "PHP slow requests, last 24 h: 15"
    assert "*sentencesermons.com* on ClientServer: 12 (plugins/jetpack)" in desc["description"]


def test_digest_quiet_when_nothing_slow():
    fake = FakeSNS()
    n._clients.update(sns=fake, logs=FakeLogs([]))
    assert n.lambda_handler({"mode": "digest"}, None) == {"posted": 0}
    assert fake.sent == []


def test_text_format():
    fake = FakeSNS()
    n._clients["sns"] = fake
    n.ALERT_FORMAT = "text"
    try:
        n.deliver("Title", "Body")
    finally:
        n.ALERT_FORMAT = "amazon_q"
    assert fake.sent[0] == {"TopicArn": n.ALERT_TOPIC_ARN, "Subject": "Title", "Message": "Body"}


FULL = (
    "[13-Nov-2026 01:26:27] WARNING: [pool www] server reached pm.max_children setting (15), "
    "consider raising it"
)


def test_pool_full_posts_once_per_window():
    fake = FakeSNS()
    n._clients["sns"] = fake
    r = n.lambda_handler(
        _event([FULL, FULL.replace("01:26:27", "01:27:01")], group="/php-fpm/error"), None
    )
    assert r == {"posted": 1, "hits": 2}
    c = json.loads(fake.sent[0]["Message"])["content"]
    assert c["title"] == "PHP pool full on ClientServer (pool www, 15 workers)"
    assert "2 times (13-Nov-2026 01:26:27 to 13-Nov-2026 01:27:01)" in c["description"]
    assert n.lambda_handler(_event([FULL], group="/php-fpm/error"), None)["posted"] == 0


def test_pool_full_ignores_other_lines():
    n._clients["sns"] = FakeSNS()
    assert n.lambda_handler(
        _event(["[13-Nov-2026] NOTICE: ready"], group="/php-fpm/error"), None
    ) == {"posted": 0}


def test_old_entries_are_history_not_news():
    fake = FakeSNS()
    n._clients["sns"] = fake
    old_full = FULL.replace("01:26:27", "00:36:27")
    assert n.lambda_handler(_event([old_full], group="/php-fpm/error"), None) == {"posted": 0}
    old_slow = ENTRY.replace("01:30:12", "00:47:40")
    assert n.lambda_handler(_event([old_slow]), None)["posted"] == 0
    assert fake.sent == []


class FakeBanLogs:
    def __init__(self, messages):
        self.messages = messages
        self.kw = None

    def filter_log_events(self, **kw):
        self.kw = kw
        return {"events": [{"message": m} for m in self.messages]}


FULL_BORCHERS = "[13-Nov-2026 01:30:40] WARNING: [pool borchers] server reached pm.max_children setting (6), consider raising it"


def test_pool_full_names_banned_ips():
    fake, logs = FakeSNS(), FakeBanLogs([
        "2026-11-13 01:30:30,100 fail2ban.actions [8]: NOTICE  [45sq-php-burst] Ban 34.95.143.122",
        "2026-11-13 01:30:31,100 fail2ban.actions [8]: NOTICE  [recidive] Ban 34.95.143.122",
        "2026-11-13 01:30:32,100 fail2ban.actions [8]: NOTICE  [45sq-php-slow] Ban 2001:db8::7",
        "2026-11-13 01:30:33,100 fail2ban.actions [8]: NOTICE  [45sq-php-burst] Unban 9.9.9.9",
    ])
    n._clients["sns"], n._clients["logs"] = fake, logs
    n.BAN_LOG_GROUP = "/php-fpm/fail2ban"
    try:
        out = n.lambda_handler(_event([FULL_BORCHERS], group="/php-fpm/error"), None)
    finally:
        n.BAN_LOG_GROUP = ""
    assert out["posted"] == 1
    msg = json.loads(fake.sent[0]["Message"])["content"]
    assert msg["title"].endswith("(pool borchers, 6 workers): 2 IPs auto-banned")
    assert "`34.95.143.122` (45sq-php-burst, recidive)" in msg["description"]
    assert "`2001:db8::7` (45sq-php-slow)" in msg["description"] and "9.9.9.9" not in msg["description"]
    assert logs.kw["logStreamNames"] == ["i-0abc"] and logs.kw["logGroupName"] == "/php-fpm/fail2ban"


def test_ban_context_off_or_failing_never_blocks_alert():
    assert n.recent_bans("i-0abc", NOW) is None and n.ban_line(None) == ""

    class Boom:
        def filter_log_events(self, **kw):
            raise RuntimeError("throttled")

    n._clients["logs"] = Boom()
    n.BAN_LOG_GROUP = "/php-fpm/fail2ban"
    try:
        assert n.recent_bans("i-0abc", NOW) is None
        assert "No IP was auto-banned" in n.ban_line([])
    finally:
        n.BAN_LOG_GROUP = ""
