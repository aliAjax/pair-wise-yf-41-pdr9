from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import RuleEngine

# 这些动作一旦完成即对外生效，需要留存不可变的发布版本快照
SNAPSHOT_ACTIONS = ("publish", "revise", "approve_revision")


def data_diff(before, after):
    """计算顶层字段变化，供审计记录“变化内容”。"""
    changes = {}
    for key in sorted(set(before) | set(after)):
        old = before.get(key)
        new = after.get(key)
        if old == new:
            continue
        if key not in before:
            changes[key] = {"op": "added", "to": new}
        elif key not in after:
            changes[key] = {"op": "removed", "from": old}
        else:
            changes[key] = {"op": "changed", "from": old, "to": new}
    return changes


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    @staticmethod
    def _apply_patch(before, patch):
        merged = dict(before)
        for key, value in patch.items():
            if value is None:
                merged.pop(key, None)
            else:
                merged[key] = value
        return merged

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        before_data = dict(entity["data"])
        merged = self._apply_patch(before_data, patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        detail = {"patch": patch, "changes": data_diff(before_data, merged)}
        if action in SNAPSHOT_ACTIONS:
            communication_id = merged.get("communication_id")
            version = self.repository.save_version(updated, actor.user_id, communication_id)
            detail["published_version_no"] = version["version_no"]
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            detail,
        )
        return updated

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def versions(self, entity_id):
        if not self.repository.get_entity(entity_id):
            raise NotFoundError("entity not found: " + entity_id)
        return self.repository.list_versions(entity_id)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
