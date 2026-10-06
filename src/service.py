from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, ValidationError
from .rules import RuleEngine, inbreeding_coefficient


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
        if action == "approve" and entity["kind"] == "pairing" and entity["status"] == "approved":
            return entity
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
        if entity["kind"] == "conflict" and action == "adjudicate":
            self._apply_conflict_resolution(actor, entity, updated)
        return updated

    def _apply_conflict_resolution(self, actor, conflict, updated_conflict):
        animal_id = conflict["data"].get("animal_id")
        choice = updated_conflict["data"].get("choice")
        if not animal_id or choice not in ("local", "external"):
            return
        resolution = conflict["data"].get(choice) or {}
        animals = self.repository.find_entities("animal", "id", animal_id)
        if animals:
            animal = animals[0]
            animal_data = dict(animal["data"])
            animal_data["sire_id"] = resolution.get("sire_id")
            animal_data["dam_id"] = resolution.get("dam_id")
            self.repository.update_entity(
                animal["id"], animal["version"], animal["status"], animal_data
            )
            self.audit.record(
                animal["id"],
                actor,
                "pedigree_update",
                animal["status"],
                animal["status"],
                {"conflict_id": conflict["id"], "choice": choice, "parents": resolution},
            )
        self.recalculate_pairings(actor, animal_id)

    def recalculate_pairings(self, actor, animal_id):
        pairings = self.repository.list_entities(kind="pairing", status="proposed")
        returned = []
        for pairing in pairings:
            data = pairing["data"]
            if data.get("sire_id") != animal_id and data.get("dam_id") != animal_id:
                continue
            sire = self.repository.find_entities("animal", "id", data.get("sire_id"))
            dam = self.repository.find_entities("animal", "id", data.get("dam_id"))
            if not sire or not dam:
                continue
            coeff = inbreeding_coefficient(sire[0], dam[0])
            if coeff > 0.125:
                reason = {
                    "reason": "inbreeding_coefficient_exceeded",
                    "coefficient": coeff,
                    "threshold": 0.125,
                    "trigger_animal_id": animal_id,
                }
                returned.append(self._return_pairing_for_review(actor, pairing, reason))
        return returned

    def _return_pairing_for_review(self, actor, pairing, reason):
        try:
            updated = self.repository.update_entity(
                pairing["id"],
                pairing["version"],
                "returned",
                {**pairing["data"], "return_reason": reason},
            )
        except ConflictError:
            fresh = self.repository.get_entity(pairing["id"])
            if not fresh or fresh["status"] != "proposed":
                return fresh
            updated = self.repository.update_entity(
                fresh["id"],
                fresh["version"],
                "returned",
                {**fresh["data"], "return_reason": reason},
            )
        self.audit.record(
            updated["id"], actor, "return_for_review", "proposed", "returned", reason
        )
        return updated

    def import_pedigree(self, actor, batch_key, records):
        existing = self.repository.find_entities("pedigree_import", "batch_key", batch_key)
        if existing:
            imp = existing[0]
            if imp["status"] == "completed":
                return imp
        else:
            imp = self.create(
                actor,
                "pedigree_import",
                {"batch_key": batch_key, "records": list(records)},
            )
        return self._process_import(actor, imp)

    def _process_import(self, actor, imp):
        data = imp["data"]
        records = data.get("records", [])
        processed = int(data.get("processed", 0))
        try:
            for index in range(processed, len(records)):
                record = records[index]
                result = self._process_record(actor, imp, record)
                current = imp["data"]
                results = list(current.get("results", []))
                results.append(result)
                new_data = dict(current)
                new_data["processed"] = index + 1
                new_data["results"] = results
                imp = self.repository.update_entity(
                    imp["id"], imp["version"], "processing", new_data
                )
            final = dict(imp["data"])
            final["processed"] = len(records)
            imp = self.repository.update_entity(
                imp["id"], imp["version"], "completed", final
            )
            self.audit.record(
                imp["id"],
                actor,
                "import_complete",
                "processing",
                "completed",
                {"processed": len(records)},
            )
            return imp
        except Exception as exc:
            current = imp["data"]
            failed_data = dict(current)
            failed_data["error"] = str(exc)
            self.repository.update_entity(imp["id"], imp["version"], "failed", failed_data)
            self.audit.record(
                imp["id"],
                actor,
                "import_failed",
                "processing",
                "failed",
                {"error": str(exc), "processed": failed_data.get("processed", 0)},
            )
            raise

    def _process_record(self, actor, imp, record):
        animal_id = record.get("animal_id")
        if not animal_id:
            raise ValidationError("record missing animal_id")
        external_sire = record.get("sire_id")
        external_dam = record.get("dam_id")
        locals_ = self.repository.find_entities("animal", "id", animal_id)
        if not locals_:
            animal = self.create(
                actor,
                "animal",
                {
                    "id": animal_id,
                    "name": record.get("name") or animal_id,
                    "sex": "unknown",
                    "sire_id": external_sire,
                    "dam_id": external_dam,
                    "pedigree_source": "external",
                },
            )
            return {"animal_id": animal_id, "outcome": "created", "entity_id": animal["id"]}
        local = locals_[0]
        local_sire = local["data"].get("sire_id")
        local_dam = local["data"].get("dam_id")
        if local_sire == external_sire and local_dam == external_dam:
            return {"animal_id": animal_id, "outcome": "matched"}
        conflict = self.create(
            actor,
            "conflict",
            {
                "animal_id": animal_id,
                "local": {"sire_id": local_sire, "dam_id": local_dam},
                "external": {"sire_id": external_sire, "dam_id": external_dam},
                "batch_key": imp["data"].get("batch_key"),
            },
        )
        return {"animal_id": animal_id, "outcome": "conflict", "conflict_id": conflict["id"]}

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
