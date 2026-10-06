from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)
from .repository import utcnow


def _validate_animal(actor, data, lookup):
    if data.get("sex") not in ("male", "female", "unknown"):
        raise ValidationError("sex must be male, female or unknown")


def _validate_conflict_create(actor, data, lookup):
    animal_id = data.get("animal_id")
    if not animal_id:
        raise ValidationError("conflict requires animal_id")
    local = data.get("local")
    external = data.get("external")
    if not isinstance(local, dict) or not isinstance(external, dict):
        raise ValidationError("conflict requires local and external parent versions")
    return {}


def _validate_import_create(actor, data, lookup):
    batch_key = data.get("batch_key")
    records = data.get("records")
    if not batch_key:
        raise ValidationError("import requires batch_key")
    if not isinstance(records, list) or not records:
        raise ValidationError("import requires a non-empty records list")
    return {}


def _validate_conflict_adjudicate(actor, entity, data, lookup):
    choice = data.get("choice")
    if choice not in ("local", "external"):
        raise ValidationError("choice must be local or external")
    return {
        "choice": choice,
        "resolved_by": actor.user_id,
        "resolved_at": utcnow(),
    }


def inbreeding_coefficient(sire, dam):
    if not sire or not dam:
        return 1.0
    sire_id = sire.get("id")
    dam_id = dam.get("id")
    if sire_id is None or dam_id is None:
        return 0.0
    if sire_id == dam_id:
        return 0.5
    sire_data = sire.get("data", sire)
    dam_data = dam.get("data", dam)
    if sire_data.get("sire_id") == dam_id or dam_data.get("sire_id") == sire_id:
        return 0.25
    if (sire_data.get("sire_id") and sire_data.get("dam_id")
            and sire_data.get("sire_id") == dam_data.get("sire_id")
            and sire_data.get("dam_id") == dam_data.get("dam_id")):
        return 0.25
    if (sire_data.get("sire_id") and sire_data.get("sire_id") == dam_data.get("sire_id")) or \
       (sire_data.get("dam_id") and sire_data.get("dam_id") == dam_data.get("dam_id")):
        return 0.125
    sire_parents = {sire_data.get("sire_id"), sire_data.get("dam_id")} - {None}
    dam_parents = {dam_data.get("sire_id"), dam_data.get("dam_id")} - {None}
    if sire_parents & dam_parents:
        return 0.0625
    return 0.0


def _validate_pairing(actor, entity, data, lookup):
    sire = _find_one(lookup, "animal", "id", data.get("sire_id"))
    dam = _find_one(lookup, "animal", "id", data.get("dam_id"))
    if not sire or not dam:
        raise ValidationError("pairing requires two existing animals")
    if sire["status"] != "active" or dam["status"] != "active":
        raise ValidationError("pairing animals must be active")
    for animal in (sire, dam):
        conflicts = lookup("conflict", "animal_id", animal["id"]) or []
        if any(item["status"] == "pending" for item in conflicts):
            raise ValidationError(
                "animal %s has unresolved pedigree conflict" % animal["id"]
            )
    coeff = inbreeding_coefficient(sire, dam)
    if coeff > 0.125:
        raise ValidationError("pairing exceeds inbreeding threshold")
    return {"approved_by": actor.user_id, "inbreeding_coefficient": coeff}


CUSTOM_CREATE = {
    'animal': _validate_animal,
    'conflict': _validate_conflict_create,
    'pedigree_import': _validate_import_create,
}
CUSTOM_TRANSITIONS = {
    ('pairing', 'approve'): _validate_pairing,
    ('conflict', 'adjudicate'): _validate_conflict_adjudicate,
}


class RuleEngine:
    ALIASES = {'animals': 'animal', 'pairings': 'pairing', 'transfers': 'transfer', 'conflicts': 'conflict', 'pedigree-imports': 'pedigree_import'}
    INITIAL_STATUS = {'animal': 'active', 'pairing': 'proposed', 'transfer': 'planned', 'conflict': 'pending', 'pedigree_import': 'processing'}
    TRANSITIONS = {'animal': {'mark_deceased': (('active',), 'deceased'), 'quarantine_animal': (('active',), 'quarantined'), 'release_quarantine': (('quarantined',), 'active')}, 'pairing': {'approve': (('proposed',), 'approved'), 'reject': (('proposed',), 'rejected'), 'complete': (('approved',), 'completed'), 'resubmit': (('returned',), 'proposed')}, 'transfer': {'authorize': (('planned',), 'authorized'), 'ship': (('authorized',), 'in_transit'), 'arrive': (('in_transit',), 'completed')}, 'conflict': {'adjudicate': (('pending',), 'adjudicated')}}
    CREATE_REQUIRED = {'animal': ('name', 'sex'), 'pairing': ('proposed_by',), 'transfer': ('animal_id', 'from_institution', 'to_institution'), 'conflict': ('animal_id', 'local', 'external'), 'pedigree_import': ('batch_key', 'records')}
    ACTION_REQUIRED = {('animal', 'mark_deceased'): ('cause',), ('animal', 'quarantine_animal'): ('reason',), ('pairing', 'approve'): ('sire_id', 'dam_id', 'approvals'), ('pairing', 'reject'): ('reason',), ('pairing', 'complete'): ('offspring_ids',), ('transfer', 'authorize'): ('permit_id',), ('transfer', 'ship'): ('transport_id',), ('transfer', 'arrive'): ('arrival_date',), ('conflict', 'adjudicate'): ('choice',)}
    CREATE_ROLES = {'animal': ('admin', 'registrar'), 'pairing': ('admin', 'coordinator'), 'transfer': ('admin', 'registrar'), 'conflict': ('admin', 'registrar'), 'pedigree_import': ('admin', 'registrar')}
    ROLE_ACTIONS = {'mark_deceased': ('admin', 'veterinarian'), 'quarantine_animal': ('admin', 'veterinarian'), 'release_quarantine': ('admin', 'veterinarian'), 'approve': ('admin', 'coordinator'), 'reject': ('admin', 'coordinator'), 'complete': ('admin', 'coordinator'), 'authorize': ('admin', 'registrar'), 'ship': ('admin', 'registrar'), 'arrive': ('admin', 'registrar'), 'adjudicate': ('admin', 'coordinator'), 'resubmit': ('admin', 'coordinator')}

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


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
