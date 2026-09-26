from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import RuleEngine, review_windows


def _today():
    return datetime.now(timezone.utc).date()


def _parse_date(value):
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except (TypeError, ValueError):
        return None


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

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        self._apply_side_effects(actor, entity, action, updated, patch)
        return updated

    def _apply_side_effects(self, actor, entity, action, updated, patch):
        if entity["kind"] == "change" and action == "commission":
            base = _parse_date(patch.get("commissioned_at")) or _today()
            risk_level = updated["data"].get("risk_level")
            for days in review_windows(risk_level):
                self._system_create(actor, "review_task", {
                    "change_id": updated["id"],
                    "window": "%dd" % days,
                    "due_date": (base + timedelta(days=days)).isoformat(),
                    "risk_level": risk_level,
                })
        if entity["kind"] == "review_task" and action == "submit":
            if patch.get("within_limits") is False:
                self._system_create(actor, "action_item", {
                    "change_id": updated["data"].get("change_id"),
                    "description": "复核超标控制项(%s): %s"
                    % (updated["data"].get("window"), patch.get("conclusion")),
                    "owner": patch.get("control_owner") or actor.user_id,
                    "source_review_id": updated["id"],
                })

    def _system_create(self, actor, kind, payload):
        entity = self.repository.create_entity(
            str(uuid4()), kind, self.rules.initial_status(kind), payload, actor.user_id
        )
        self.audit.record(entity["id"], actor, "auto_create", None, entity["status"], {"kind": kind})
        return entity

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
