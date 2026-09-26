from datetime import datetime, timedelta, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _default_clock():
    return datetime.now(timezone.utc)


def _validate_change(actor, data, lookup):
    unit = _find_one(lookup, "unit", "id", data.get("unit_id"))
    if not unit:
        raise ValidationError("unit does not exist")
    if not data.get("description", "").strip():
        raise ValidationError("change description is required")


def required_approval_level(risk_level):
    levels = {"low": 1, "medium": 2, "high": 3, "critical": 4}
    return levels.get(str(risk_level).lower(), 4)


def review_schedule_for_risk(risk_level):
    """Return post-commission review offsets in days."""
    if str(risk_level).lower() in ("high", "critical"):
        return [30, 90]
    return [90]


def _validate_assess(actor, entity, data, lookup, clock):
    return {"required_approvals": required_approval_level(data.get("risk_level"))}


def _validate_approve(actor, entity, data, lookup, clock):
    required = int(entity["data"].get("required_approvals", 1))
    approvals = data.get("approvals") or []
    if len(set(approvals)) < required:
        raise ValidationError("not enough distinct approvals")
    return {"approved_by": actor.user_id}


def _validate_commission(actor, entity, data, lookup, clock):
    items = lookup("action_item", "change_id", entity["id"]) or [] if lookup else []
    unresolved = [item["id"] for item in items if item["status"] != "verified"]
    if unresolved:
        raise ValidationError("unresolved action items: " + ", ".join(unresolved))
    return {"commissioned_by": actor.user_id}


def _reject_generated_review(actor, data, lookup):
    raise ValidationError(
        "effect_review records are generated automatically when a change is commissioned"
    )


def _validate_effect_review_submit(actor, entity, data, lookup, clock):
    condition = data.get("operating_conditions")
    if not isinstance(condition, str) or not condition.strip():
        raise ValidationError("operating_conditions must be a non-empty string")

    indicators = data.get("indicators")
    if not isinstance(indicators, list) or not indicators:
        raise ValidationError("indicators must be a non-empty list")
    for indicator in indicators:
        if not isinstance(indicator, dict):
            raise ValidationError("each indicator must be an object")
        if not str(indicator.get("name", "")).strip():
            raise ValidationError("each indicator requires a name")
        if indicator.get("value") in (None, ""):
            raise ValidationError("each indicator requires a value")

    conclusion = data.get("deviation_conclusion")
    if conclusion not in ("within_limit", "exceeded"):
        raise ValidationError(
            "deviation_conclusion must be 'within_limit' or 'exceeded'"
        )

    today = clock().date()
    due_date = datetime.fromisoformat(entity["data"]["due_date"]).date()
    if today < due_date:
        raise ValidationError("effect review is not due until " + due_date.isoformat())

    return {
        "deviation_result": conclusion,
        "submitted_by": actor.user_id,
        "submitted_date": today.isoformat(),
        "submitted_at": clock().isoformat(timespec="seconds"),
    }


def _validate_close(actor, entity, data, lookup, clock):
    if entity["status"] != "commissioned" or not lookup:
        return {}

    change_id = entity["id"]
    reviews = lookup("effect_review", "change_id", change_id) or []
    schedule = review_schedule_for_risk(entity["data"].get("risk_level", "low"))

    pending = []
    for days in schedule:
        completed = next(
            (
                review
                for review in reviews
                if review["data"].get("stage_days") == days
                and review["status"] == "completed"
            ),
            None,
        )
        if not completed:
            pending.append("%sd" % days)
    if pending:
        raise ValidationError("pending effect reviews: " + ", ".join(pending))

    for review in reviews:
        if review["status"] != "completed":
            continue
        if review["data"].get("deviation_result") != "exceeded":
            continue
        controls = lookup("action_item", "source_review_id", review["id"]) or []
        unresolved = [
            item["id"] + ":" + item["status"]
            for item in controls
            if item["status"] != "verified"
        ]
        if not controls or unresolved:
            detail = ", ".join(unresolved) if unresolved else "missing control item"
            raise ValidationError(
                "unresolved control item for exceeded effect review %s: %s"
                % (review["id"], detail)
            )

    return {}


CUSTOM_CREATE = {
    "change": _validate_change,
    "effect_review": _reject_generated_review,
}
CUSTOM_TRANSITIONS = {
    ("change", "assess"): _validate_assess,
    ("change", "approve"): _validate_approve,
    ("change", "commission"): _validate_commission,
    ("change", "close"): _validate_close,
    ("effect_review", "submit"): _validate_effect_review_submit,
}


class RuleEngine:
    ALIASES = {
        "units": "unit",
        "changes": "change",
        "action_items": "action_item",
        "effect_reviews": "effect_review",
    }
    INITIAL_STATUS = {
        "unit": "operating",
        "change": "draft",
        "action_item": "open",
        "effect_review": "pending",
    }
    TRANSITIONS = {
        "unit": {
            "shutdown": (("operating",), "shutdown"),
            "startup": (("shutdown",), "operating"),
            "freeze": (("operating",), "frozen"),
            "unfreeze": (("frozen",), "operating"),
        },
        "change": {
            "assess": (("draft",), "assessed"),
            "approve": (("assessed",), "approved"),
            "implement": (("approved",), "implemented"),
            "commission": (("implemented",), "commissioned"),
            "rollback": (("implemented", "commissioned"), "rolled_back"),
            "close": (("rolled_back", "commissioned"), "closed"),
        },
        "action_item": {
            "complete": (("open",), "completed"),
            "verify": (("completed",), "verified"),
            "reopen": (("verified",), "open"),
        },
        "effect_review": {
            "submit": (("pending",), "completed"),
        },
    }
    CREATE_REQUIRED = {
        "unit": ("name", "location"),
        "change": ("unit_id", "description"),
        "action_item": ("change_id", "description", "owner"),
        "effect_review": (),
    }
    ACTION_REQUIRED = {
        ("unit", "shutdown"): ("reason",),
        ("unit", "freeze"): ("reason",),
        ("change", "assess"): ("risk_level", "analyst"),
        ("change", "approve"): ("approvals", "permit_id"),
        ("change", "implement"): ("procedure_version",),
        ("change", "commission"): ("tests_passed",),
        ("change", "rollback"): ("reason",),
        ("change", "close"): ("outcome",),
        ("action_item", "complete"): ("completed_by", "evidence"),
        ("action_item", "verify"): ("verifier",),
        ("action_item", "reopen"): ("reason",),
        ("effect_review", "submit"): (
            "operating_conditions",
            "indicators",
            "deviation_conclusion",
        ),
    }
    CREATE_ROLES = {
        "unit": ("admin", "engineer"),
        "change": ("admin", "engineer"),
        "action_item": ("admin", "safety"),
    }
    ROLE_ACTIONS = {
        "shutdown": ("admin", "operator"),
        "startup": ("admin", "operator"),
        "freeze": ("admin", "operator"),
        "unfreeze": ("admin", "operator"),
        "assess": ("admin", "engineer"),
        "approve": ("admin", "safety"),
        "implement": ("admin", "engineer"),
        "commission": ("admin", "engineer"),
        "rollback": ("admin", "engineer"),
        "close": ("admin", "safety"),
        "complete": ("admin", "engineer"),
        "verify": ("admin", "verifier"),
        "reopen": ("admin", "verifier"),
        "submit": ("admin", "operator", "engineer", "safety"),
    }

    def __init__(self, clock=None):
        self.clock = clock or _default_clock

    @staticmethod
    def review_schedule(risk_level):
        return review_schedule_for_risk(risk_level)

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
        extra = custom(actor, entity, data, lookup, self.clock) if custom else {}
        patch = dict(data)
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
