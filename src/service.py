from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import RuleEngine


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

        updates = []
        creates = []
        generated_audits = []
        kind = self.rules.normalize_kind(entity["kind"])
        merged = dict(entity["data"])
        merged.update(patch)

        if kind == "change" and action == "commission":
            now = self.rules.clock()
            for days in self.rules.review_schedule(merged.get("risk_level", "low")):
                review_id = str(uuid4())
                due_date = (now + self._days(days)).date().isoformat()
                review_data = {
                    "change_id": entity_id,
                    "stage_days": days,
                    "stage_label": "%d天效果复核" % days,
                    "commission_date": now.date().isoformat(),
                    "due_date": due_date,
                    "risk_level": merged.get("risk_level", "low"),
                    "generated_by_action": "commission",
                }
                creates.append(
                    {
                        "id": review_id,
                        "kind": "effect_review",
                        "status": "pending",
                        "data": review_data,
                        "actor_id": actor.user_id,
                    }
                )
                generated_audits.append(
                    {
                        "entity_id": review_id,
                        "actor": actor,
                        "action": "create",
                        "from_status": None,
                        "to_status": "pending",
                        "detail": {
                            "kind": "effect_review",
                            "trigger": "commission",
                            "change_id": entity_id,
                            "due_date": due_date,
                            "stage_days": days,
                        },
                    }
                )

        if kind == "effect_review" and action == "submit":
            if merged.get("deviation_result") == "exceeded":
                action_item_id = str(uuid4())
                stage_days = entity["data"].get("stage_days")
                merged["action_item_id"] = action_item_id
                control_data = {
                    "change_id": entity["data"]["change_id"],
                    "source_review_id": entity_id,
                    "source": "effect_review",
                    "description": "%s天效果复核运行偏差超标，需采取纠正措施" % stage_days,
                    "owner": "safety-team",
                    "deviation_result": "exceeded",
                    "stage_days": stage_days,
                }
                creates.append(
                    {
                        "id": action_item_id,
                        "kind": "action_item",
                        "status": "open",
                        "data": control_data,
                        "actor_id": actor.user_id,
                    }
                )
                generated_audits.append(
                    {
                        "entity_id": action_item_id,
                        "actor": actor,
                        "action": "auto_create",
                        "from_status": None,
                        "to_status": "open",
                        "detail": {
                            "kind": "action_item",
                            "trigger": "effect_review.submit",
                            "effect_review_id": entity_id,
                            "change_id": entity["data"]["change_id"],
                        },
                    }
                )

        updates.append(
            {
                "entity_id": entity_id,
                "status": next_status,
                "data": merged,
                "expected_version": expected,
            }
        )

        if creates:
            result = self.repository.update_and_create(updates, creates)
            updated = result["updated"][0]
        else:
            updated = self.repository.update_entity(
                entity_id, expected, next_status, merged
            )

        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        for item in generated_audits:
            self.audit.record(
                item["entity_id"],
                item["actor"],
                item["action"],
                item["from_status"],
                item["to_status"],
                item["detail"],
            )
        return updated

    @staticmethod
    def _days(value):
        from datetime import timedelta

        return timedelta(days=int(value))

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
