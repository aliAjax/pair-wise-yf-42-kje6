from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


# 近交系数红线：超过该值的配对不予批准 / 退回待复核。
INBREEDING_LIMIT = 0.125


def _validate_animal(actor, data, lookup):
    if data.get("sex") not in ("male", "female", "unknown"):
        raise ValidationError("sex must be male, female or unknown")


def inbreeding_coefficient(sire, dam):
    if not sire or not dam:
        return 1.0
    # 调用方可能传入完整实体（id 在键上），也可能只传 data 字典。
    sire_id = sire.get("id", sire.get("_id"))
    dam_id = dam.get("id", dam.get("_id"))
    if sire_id is None or dam_id is None:
        return 0.0
    if sire_id == dam_id:
        return 0.5
    # 任一方是另一方的亲本（父女、母子等直系亲子配对），系数 0.25。
    if sire_id == dam.get("sire_id") or sire_id == dam.get("dam_id"):
        return 0.25
    if dam_id == sire.get("sire_id") or dam_id == sire.get("dam_id"):
        return 0.25
    return 0.0


def entity_view(entity):
    """把实体主键 id 并入 data 视图，供近交系数计算使用。"""
    view = dict(entity["data"])
    view["id"] = entity["id"]
    return view


def normalize_parent_id(value):
    """外部数据里空串、None 都按“未知父母”处理。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def parent_claim(record):
    """归一化一头动物的父母说法，便于两版逐条比对。"""
    record = record or {}
    return {
        "sire_id": normalize_parent_id(record.get("sire_id")),
        "dam_id": normalize_parent_id(record.get("dam_id")),
    }


def claims_conflict(local_parents, external_parents):
    return parent_claim(local_parents) != parent_claim(external_parents)


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _validate_pairing(actor, entity, data, lookup):
    # 配对在“提议”阶段就可以带上雌雄编号；裁定时也允许现场补录。
    current = entity["data"]
    sire_id = normalize_parent_id(data.get("sire_id")) or normalize_parent_id(
        current.get("sire_id")
    )
    dam_id = normalize_parent_id(data.get("dam_id")) or normalize_parent_id(
        current.get("dam_id")
    )
    if not sire_id or not dam_id:
        raise ValidationError("pairing requires sire_id and dam_id")
    sire = _find_one(lookup, "animal", "id", sire_id)
    dam = _find_one(lookup, "animal", "id", dam_id)
    if not sire or not dam:
        raise ValidationError("pairing requires two existing animals")
    if sire["status"] != "active" or dam["status"] != "active":
        raise ValidationError("pairing animals must be active")
    # 父母说法还没裁定的个体，先不参与配对。
    for animal in (sire, dam):
        if animal["data"].get("open_conflicts"):
            raise ValidationError(
                "animal %s has unresolved pedigree conflicts" % animal["id"]
            )
    coefficient = inbreeding_coefficient(entity_view(sire), entity_view(dam))
    if coefficient > INBREEDING_LIMIT:
        raise ValidationError(
            "pairing exceeds inbreeding threshold: %s > %s"
            % (coefficient, INBREEDING_LIMIT)
        )
    return {
        "sire_id": sire_id,
        "dam_id": dam_id,
        "approved_by": actor.user_id,
        "coefficient": coefficient,
    }


def _resubmit_pairing(actor, entity, data, lookup):
    # 退回后重新提交：清掉当前退回原因（历史保留在 return_history 里）。
    return {"return_reason": None, "resubmitted_by": actor.user_id}


CUSTOM_CREATE = {'animal': _validate_animal}
CUSTOM_TRANSITIONS = {
    ('pairing', 'approve'): _validate_pairing,
    ('pairing', 'resubmit'): _resubmit_pairing,
}


class RuleEngine:
    ALIASES = {'animals': 'animal', 'pairings': 'pairing', 'transfers': 'transfer'}
    INITIAL_STATUS = {'animal': 'active', 'pairing': 'proposed', 'transfer': 'planned'}
    TRANSITIONS = {'animal': {'mark_deceased': (('active',), 'deceased'), 'quarantine_animal': (('active',), 'quarantined'), 'release_quarantine': (('quarantined',), 'active')}, 'pairing': {'approve': (('proposed',), 'approved'), 'reject': (('proposed',), 'rejected'), 'resubmit': (('returned',), 'proposed'), 'complete': (('approved',), 'completed')}, 'transfer': {'authorize': (('planned',), 'authorized'), 'ship': (('authorized',), 'in_transit'), 'arrive': (('in_transit',), 'completed')}}
    CREATE_REQUIRED = {'animal': ('name', 'sex'), 'pairing': ('proposed_by',), 'transfer': ('animal_id', 'from_institution', 'to_institution')}
    ACTION_REQUIRED = {('animal', 'mark_deceased'): ('cause',), ('animal', 'quarantine_animal'): ('reason',), ('pairing', 'approve'): ('approvals',), ('pairing', 'reject'): ('reason',), ('pairing', 'complete'): ('offspring_ids',), ('transfer', 'authorize'): ('permit_id',), ('transfer', 'ship'): ('transport_id',), ('transfer', 'arrive'): ('arrival_date',)}
    CREATE_ROLES = {'animal': ('admin', 'registrar'), 'pairing': ('admin', 'coordinator'), 'transfer': ('admin', 'registrar')}
    ROLE_ACTIONS = {'mark_deceased': ('admin', 'veterinarian'), 'quarantine_animal': ('admin', 'veterinarian'), 'release_quarantine': ('admin', 'veterinarian'), 'approve': ('admin', 'coordinator'), 'reject': ('admin', 'coordinator'), 'resubmit': ('admin', 'coordinator'), 'complete': ('admin', 'coordinator'), 'authorize': ('admin', 'registrar'), 'ship': ('admin', 'registrar'), 'arrive': ('admin', 'registrar')}
    IMPORT_ROLES = ('admin', 'registrar')
    RESOLVE_ROLES = ('admin', 'coordinator')

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
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
