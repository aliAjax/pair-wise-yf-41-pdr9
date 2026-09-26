from datetime import datetime, timedelta

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
    eligible = _eligible_reports(reports, lookup)
    if len(eligible) < 2:
        raise ValidationError("two reports from non-offline stations are required for association")
    stations = _station_codes(eligible)
    return {
        "associated_count": len(eligible),
        "participating_stations": stations,
        "participating_count": len(stations),
    }


def _validate_publish(actor, entity, data, lookup):
    patch = {"revision_no": 1}
    if "participating_count" not in entity["data"]:
        stations = _station_codes(_eligible_reports(entity["data"].get("reports") or [], lookup))
        patch["participating_stations"] = stations
        patch["participating_count"] = len(stations)
    return patch


def _validate_revise(actor, entity, data, lookup):
    return {"revision_no": _next_revision_no(entity)}


def _validate_backfill(actor, entity, data, lookup):
    if not entity["data"].get("magnitude"):
        raise InvalidTransition("backfill requires a reviewed magnitude")
    if entity["data"].get("pending_revision"):
        raise InvalidTransition("a revision is already pending review")
    station_code = data.get("station")
    station = _find_one(lookup, "station", "code", station_code)
    if not station:
        raise ValidationError("unknown station: %s" % station_code)
    # 台站恢复上线后才允许补录检修期间的缺报
    if station["status"] != "online":
        raise InvalidTransition("station %s must be back online before backfill" % station_code)
    new_report = {
        "station": station_code,
        "time_offset": _as_number(data.get("time_offset"), "time_offset"),
        "distance_km": _as_number(data.get("distance_km"), "distance_km"),
    }
    if not _report_in_window(new_report):
        raise ValidationError("backfilled report is outside the association window")
    amplitude = data.get("amplitude")
    if amplitude not in (None, ""):
        new_report["amplitude"] = _as_number(amplitude, "amplitude")
    revised_magnitude = _as_number(data.get("magnitude"), "magnitude")

    current_stations = entity["data"].get("participating_stations")
    if current_stations is None:
        current_stations = _station_codes(
            _eligible_reports(entity["data"].get("reports") or [], lookup)
        )
    revised_stations = sorted(set(current_stations) | {station_code})

    pending = {
        "reason": data.get("reason"),
        "magnitude": revised_magnitude,
        "participating_count": len(revised_stations),
        "participating_stations": revised_stations,
        "added_report": new_report,
        "base_status": entity["status"],
        "base_revision_no": int(entity["data"].get("revision_no") or 1),
    }
    amplitudes = [
        report["amplitude"]
        for report in entity["data"].get("reports") or []
        if isinstance(report, dict)
        and report.get("station") in current_stations
        and report.get("amplitude") is not None
    ]
    if "amplitude" in new_report:
        amplitudes.append(new_report["amplitude"])
    if amplitudes:
        pending["median_amplitude"] = magnitude_median(amplitudes)
    # 建议震级、补录报告等只进入待审修订，不覆盖已发布数据
    return {
        "pending_revision": pending,
        "__ignore_input__": (
            "station", "time_offset", "distance_km", "amplitude",
            "magnitude", "reason",
        ),
    }


def _validate_approve_revision(actor, entity, data, lookup):
    pending = entity["data"].get("pending_revision")
    if not pending:
        raise InvalidTransition("no pending revision to approve")
    # 复核员看过新旧震级和参评台站数后必须显式确认，修订才对外生效
    if data.get("confirmed") is not True:
        raise ValidationError("reviewer must explicitly confirm the revised magnitude and stations")
    return {
        "magnitude": pending["magnitude"],
        "participating_stations": pending["participating_stations"],
        "participating_count": pending["participating_count"],
        "revision_no": _next_revision_no(entity),
        "pending_revision": None,
        "__ignore_input__": ("confirmed",),
    }


def _validate_reject_revision(actor, entity, data, lookup):
    pending = entity["data"].get("pending_revision")
    if not pending:
        raise InvalidTransition("no pending revision to reject")
    return {
        "pending_revision": None,
        "__next_status__": pending.get("base_status") or "published",
        "__ignore_input__": ("reason",),
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


def _as_number(value, field):
    if value in (None, ""):
        raise ValidationError("missing required field: " + field)
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValidationError("%s must be a number" % field)


def _station_codes(reports):
    codes = {report.get("station") for report in reports if report.get("station")}
    return sorted(codes)


def _eligible_reports(reports, lookup):
    """台站下线后其观测不参与新事件关联，关联时剔除下线台站报告。"""
    offline = set()
    if lookup is not None:
        offline = {
            station["data"]["code"]
            for station in lookup("station", "status", "offline")
            if station["data"].get("code")
        }
    return [
        report
        for report in reports
        if isinstance(report, dict) and report.get("station") not in offline
    ]


def _report_in_window(report, max_delta=120, max_distance=3.0):
    return (
        abs(float(report.get("time_offset", 0))) <= max_delta
        and float(report.get("distance_km", 0)) <= max_distance
    )


def _next_revision_no(entity):
    return int(entity["data"].get("revision_no") or 1) + 1


CUSTOM_CREATE = {'station': _validate_station, 'event': _validate_event}
CUSTOM_TRANSITIONS = {
    ('event', 'associate'): _validate_associate,
    ('event', 'publish'): _validate_publish,
    ('event', 'revise'): _validate_revise,
    ('event', 'backfill'): _validate_backfill,
    ('event', 'approve_revision'): _validate_approve_revision,
    ('event', 'reject_revision'): _validate_reject_revision,
}


class RuleEngine:
    ALIASES = {'stations': 'station', 'events': 'event'}
    INITIAL_STATUS = {'station': 'online', 'event': 'candidate'}
    TRANSITIONS = {'station': {'offline': (('online',), 'offline'), 'online': (('offline',), 'online')}, 'event': {'associate': (('candidate',), 'associated'), 'review': (('associated',), 'reviewed'), 'publish': (('reviewed',), 'published'), 'revise': (('published', 'revised'), 'revised'), 'backfill': (('published', 'revised'), 'revision_pending'), 'approve_revision': (('revision_pending',), 'revised'), 'reject_revision': (('revision_pending',), ('published', 'revised')), 'withdraw': (('published', 'revised'), 'withdrawn')}}
    CREATE_REQUIRED = {'station': ('code', 'lat', 'lon'), 'event': ('title', 'origin_time', 'location', 'reports')}
    ACTION_REQUIRED = {('station', 'offline'): ('reason',), ('event', 'review'): ('reviewer', 'magnitude'), ('event', 'publish'): ('communication_id',), ('event', 'revise'): ('reason', 'magnitude'), ('event', 'backfill'): ('station', 'magnitude', 'reason'), ('event', 'approve_revision'): ('confirmed',), ('event', 'reject_revision'): ('reason',), ('event', 'withdraw'): ('reason',)}
    CREATE_ROLES = {'station': ('admin', 'station'), 'event': ('admin', 'analyst')}
    ROLE_ACTIONS = {'offline': ('admin', 'station'), 'online': ('admin', 'station'), 'associate': ('admin', 'analyst'), 'review': ('admin', 'reviewer'), 'publish': ('admin', 'reviewer'), 'revise': ('admin', 'reviewer'), 'backfill': ('admin', 'analyst'), 'approve_revision': ('admin', 'reviewer'), 'reject_revision': ('admin', 'reviewer'), 'withdraw': ('admin', 'reviewer')}

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
        allowed_statuses, next_targets = transition
        if isinstance(next_targets, str):
            allowed_next = (next_targets,)
        else:
            allowed_next = tuple(next_targets)
        next_status = allowed_next[0]
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
        next_status = extra.pop("__next_status__", next_status)
        if next_status not in allowed_next:
            raise InvalidTransition(
                "cannot %s to status %s" % (action, next_status)
            )
        ignore_input = extra.pop("__ignore_input__", ())
        patch = {key: value for key, value in data.items() if key not in ignore_input}
        if extra:
            patch.update(extra)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
