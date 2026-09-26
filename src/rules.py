from datetime import datetime, timedelta, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _validate_station(actor, data, lookup):
    if not data.get("code"):
        raise ValidationError("station code is required")


def _validate_event(actor, data, lookup):
    reports = data.get("reports") or []
    if len(reports) < 2:
        raise ValidationError("event requires at least two station reports")
    if not data.get("title"):
        raise ValidationError("event title is required")


def _validate_associate(actor, entity, data, lookup):
    reports = entity["data"].get("reports") or []
    online, excluded = _partition_by_station_status(reports, lookup)
    if len(online) < 2:
        raise ValidationError("association requires at least two reports from online stations")
    return {
        "associated_count": len(online),
        "station_count": _station_count(online),
        "excluded_offline": excluded,
    }


def _validate_backfill(actor, entity, data, lookup):
    reason = data.pop("reason")
    magnitude = data.pop("magnitude")
    backfill = data.pop("backfill_reports")
    current = entity["data"].get("reports") or []
    known = {report.get("station") for report in current}
    for report in backfill:
        code = report.get("station")
        if not code:
            raise ValidationError("backfill report requires a station code")
        if code in known:
            raise ValidationError("station %s already reported for this event" % code)
        station = _find_station(lookup, code)
        if station is not None and station.get("status") == "offline":
            raise ValidationError("station %s has not recovered yet" % code)
        known.add(code)
    merged = list(current) + list(backfill)
    return {
        "pending_revision": {
            "reason": reason,
            "magnitude": magnitude,
            "backfill_reports": backfill,
            "reports": merged,
            "station_count": _station_count(merged),
            "base_status": entity["status"],
            "base_magnitude": entity["data"].get("magnitude"),
            "base_station_count": entity["data"].get("station_count") or _station_count(current),
            "submitted_by": actor.user_id,
            "submitted_at": _now(),
        }
    }


def _validate_confirm_revision(actor, entity, data, lookup):
    pending = entity["data"].get("pending_revision")
    if not pending:
        raise ValidationError("no pending revision to confirm")
    reviewer = data.pop("reviewer")
    revision = (entity["data"].get("effective_revision") or 1) + 1
    return {
        "reports": list(pending.get("reports") or []),
        "magnitude": pending.get("magnitude"),
        "station_count": pending.get("station_count"),
        "effective_revision": revision,
        "pending_revision": None,
        "last_revision": {
            "revision": revision,
            "reason": pending.get("reason"),
            "previous_magnitude": pending.get("base_magnitude"),
            "magnitude": pending.get("magnitude"),
            "previous_station_count": pending.get("base_station_count"),
            "station_count": pending.get("station_count"),
            "backfilled_stations": [
                report.get("station") for report in pending.get("backfill_reports") or []
            ],
            "submitted_by": pending.get("submitted_by"),
            "submitted_at": pending.get("submitted_at"),
            "reviewer": reviewer,
        },
    }


def _validate_reject_revision(actor, entity, data, lookup):
    pending = entity["data"].get("pending_revision")
    if not pending:
        raise ValidationError("no pending revision to reject")
    return {
        "__status__": pending.get("base_status") or "published",
        "pending_revision": None,
        "last_rejection": {
            "reason": data.pop("reason"),
            "rejected_magnitude": pending.get("magnitude"),
            "rejected_station_count": pending.get("station_count"),
            "submitted_by": pending.get("submitted_by"),
            "rejected_by": actor.user_id,
        },
    }


def associate_reports(reports, max_delta=120, max_distance=3.0):
    if not reports:
        return []
    anchor = reports[0]
    result = [anchor]
    for report in reports[1:]:
        if abs(float(report.get("time_offset", 0))) <= max_delta and float(report.get("distance_km", 0)) <= max_distance:
            result.append(report)
    return result


def magnitude_median(amplitudes):
    values = sorted(float(value) for value in amplitudes)
    if not values:
        raise ValidationError("amplitudes are required")
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    return (values[middle - 1] + values[middle]) / 2.0


CUSTOM_CREATE = {'station': _validate_station, 'event': _validate_event}
CUSTOM_TRANSITIONS = {('event', 'associate'): _validate_associate, ('event', 'backfill'): _validate_backfill, ('event', 'confirm_revision'): _validate_confirm_revision, ('event', 'reject_revision'): _validate_reject_revision}


class RuleEngine:
    ALIASES = {'stations': 'station', 'events': 'event'}
    INITIAL_STATUS = {'station': 'online', 'event': 'candidate'}
    TRANSITIONS = {'station': {'offline': (('online',), 'offline'), 'online': (('offline',), 'online')}, 'event': {'associate': (('candidate',), 'associated'), 'review': (('associated',), 'reviewed'), 'publish': (('reviewed',), 'published'), 'revise': (('published', 'revised'), 'revised'), 'backfill': (('published', 'revised'), 'revision_pending'), 'confirm_revision': (('revision_pending',), 'revised'), 'reject_revision': (('revision_pending',), 'published'), 'withdraw': (('published', 'revised'), 'withdrawn')}}
    CREATE_REQUIRED = {'station': ('code', 'lat', 'lon'), 'event': ('title', 'origin_time', 'location', 'reports')}
    ACTION_REQUIRED = {('station', 'offline'): ('reason',), ('event', 'review'): ('reviewer', 'magnitude'), ('event', 'publish'): ('communication_id',), ('event', 'revise'): ('reason', 'magnitude'), ('event', 'backfill'): ('reason', 'magnitude', 'backfill_reports'), ('event', 'confirm_revision'): ('reviewer',), ('event', 'reject_revision'): ('reason',), ('event', 'withdraw'): ('reason',)}
    CREATE_ROLES = {'station': ('admin', 'station'), 'event': ('admin', 'analyst')}
    ROLE_ACTIONS = {'offline': ('admin', 'station'), 'online': ('admin', 'station'), 'associate': ('admin', 'analyst'), 'review': ('admin', 'reviewer'), 'publish': ('admin', 'reviewer'), 'revise': ('admin', 'reviewer'), 'backfill': ('admin', 'analyst'), 'confirm_revision': ('admin', 'reviewer'), 'reject_revision': ('admin', 'reviewer'), 'withdraw': ('admin', 'reviewer')}

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        if extra:
            next_status = extra.pop("__status__", next_status)
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _find_station(lookup, code):
    if lookup is None or not code:
        return None
    return _find_one(lookup, "station", "code", code)


def _partition_by_station_status(reports, lookup):
    online, excluded = [], []
    for report in reports:
        station = _find_station(lookup, report.get("station"))
        if station is not None and station.get("status") == "offline":
            excluded.append(report.get("station"))
        else:
            online.append(report)
    return online, excluded


def _station_count(reports):
    return len({report.get("station") for report in reports})


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
