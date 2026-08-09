"""Jinja filters for the perfmon_grafana alerting templates."""

import hashlib


def darling_alert_uid(prefix, hostname):
    """Deterministic alert rule UID: prefix + a 12-hex-char hash of the hostname.

    Grafana rejects rule UIDs over 40 characters. Hashing the hostname keeps every UID
    short and stable regardless of instance name length, instead of just concatenating
    the raw hostname onto the prefix.
    """
    digest = hashlib.sha256(str(hostname).encode()).hexdigest()[:12]
    return f"{prefix}{digest}"


class FilterModule:  # pylint: disable=too-few-public-methods
    """Expose the alerting UID filter to Jinja."""

    def filters(self):
        """Return the filter map."""
        return {"darling_alert_uid": darling_alert_uid}
