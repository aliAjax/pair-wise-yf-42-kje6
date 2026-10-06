from uuid import uuid4

from .audit import AuditTrail
from .domain import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from .rules import (
    INBREEDING_LIMIT,
    RuleEngine,
    claims_conflict,
    entity_view,
    inbreeding_coefficient,
    normalize_parent_id,
    parent_claim,
)

# 外部谱系导入时，已成功处理的行在断点重试中直接跳过；
# error/pending 行是断点，需要用修正后的载荷重新处理。
ROW_DONE_STATUSES = ("merged", "created", "conflict", "skipped")


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
        # 读取—校验—写入放在同一个 BEGIN IMMEDIATE 事务里：两个协调员同时确认
        # 同一配对时，后提交者在锁内读到的是新版本，只放行一次。
        connection = self.repository.begin()
        try:
            entity = self.repository.get_entity(entity_id, connection=connection)
            if not entity:
                raise NotFoundError("entity not found: " + entity_id)
            expected = (
                int(expected_version) if expected_version is not None
                else entity["version"]
            )
            next_status, patch = self.rules.validate_transition(
                actor, entity, action, dict(data or {}),
                lambda kind, field, value: self.repository.find_entities(
                    kind, field, value, connection=connection
                ),
            )
            merged = dict(entity["data"])
            merged.update(patch)
            try:
                updated = self.repository.update_entity(
                    entity_id, expected, next_status, merged, connection=connection
                )
            except ConflictError as exc:
                if action == "approve":
                    raise ConflictError(
                        "配对已被另一位协调员确认，只放行一次: " + entity_id
                    ) from exc
                raise
            self.repository.append_audit(
                entity_id, actor.user_id, actor.role, action,
                entity["status"], updated["status"], {"patch": patch},
                connection=connection,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
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

    def audit_log(self, entity_id=None, action_like=None):
        return self.repository.list_audit(entity_id=entity_id, action_like=action_like)

    # ---------- 外部谱系导入 ----------

    def import_pedigree(self, actor, batch_id, source, records):
        """导入合作园谱系。

        - 按动物编号归并：本园已有则逐头对账，父母说法冲突两版都留；
        - 同一个 batch_id 重复送不重复入库（已处理行直接跳过），失败可断点重试；
        - 每一行独立事务，某行失败只标记该行，批次从断点继续。
        """
        if actor.role not in self.rules.IMPORT_ROLES:
            raise PermissionDenied("role %s is not allowed to import pedigree" % actor.role)
        if not batch_id or not str(batch_id).strip():
            raise ValidationError("batch_id is required")
        if not isinstance(records, list) or not records:
            raise ValidationError("records must be a non-empty list")

        existing_batch = self.repository.get_import_batch(str(batch_id))
        if existing_batch is None:
            batch = self.repository.create_import_batch(
                str(batch_id),
                str(source or "external"),
                len(records),
                {"records": records},
                actor.user_id,
            )
            self.audit.record(
                batch["id"], actor, "pedigree_import_start", None, "in_progress",
                {"source": batch["source"], "total": batch["total"]},
            )
        else:
            batch = existing_batch
            if batch["status"] == "completed":
                # 同一批重复送：原样返回，不重复入库。
                return batch
            if len(records) != batch["total"]:
                raise ValidationError(
                    "batch %s already exists with %s records, retry must resend the same batch"
                    % (batch_id, batch["total"])
                )

        counts = {"created": 0, "merged": 0, "conflict": 0, "skipped": 0, "error": 0}
        rows = self.repository.list_import_rows(batch["id"])
        failed_line = None
        for row in rows:
            if row["status"] in ROW_DONE_STATUSES:
                # 断点重试：已成功/已记冲突的行不重复处理。
                counts[row["status"]] = counts.get(row["status"], 0) + 1
                continue
            line_no = row["line_no"]
            record = records[line_no - 1]
            try:
                outcome = self._import_one(actor, batch["id"], line_no, record)
            except Exception as exc:
                # 事务已回滚；把这行标成断点，批次可从这里重试。
                self.repository.mark_import_row(
                    batch["id"], line_no, "error",
                    animal_id=str(record.get("id") or "") or None,
                    error=str(exc),
                )
                counts["error"] += 1
                failed_line = line_no
                break
            counts[outcome] += 1
            if outcome == "error":
                failed_line = line_no
                break

        if failed_line is not None:
            summary = dict(batch["summary"])
            summary.update(counts)
            summary["failed_line"] = failed_line
            self.repository.mark_import_batch(batch["id"], "failed", summary)
            self.audit.record(
                batch["id"], actor, "pedigree_import_failed", "in_progress", "failed",
                {"failed_line": failed_line, "counts": counts},
            )
            return self.repository.get_import_batch(batch["id"])

        summary = dict(batch["summary"])
        summary.update(counts)
        summary.pop("failed_line", None)
        self.repository.mark_import_batch(batch["id"], "completed", summary)
        self.audit.record(
            batch["id"], actor, "pedigree_import_complete", "in_progress", "completed",
            {"counts": counts},
        )
        return self.repository.get_import_batch(batch["id"])

    def _import_one(self, actor, batch_id, line_no, record):
        if not isinstance(record, dict):
            self.repository.mark_import_row(
                batch_id, line_no, "error", error="每条记录必须是 JSON 对象"
            )
            return "error"
        animal_id = record.get("id")
        if animal_id is None or str(animal_id).strip() == "":
            self.repository.mark_import_row(
                batch_id, line_no, "error", error="缺少动物编号 id"
            )
            return "error"
        animal_id = str(animal_id)
        sex = record.get("sex", "unknown")
        if sex not in ("male", "female", "unknown"):
            self.repository.mark_import_row(
                batch_id, line_no, "error", error="sex must be male, female or unknown"
            )
            return "error"
        external_parents = parent_claim(record)

        connection = self.repository.begin()
        try:
            animal = self.repository.get_entity(animal_id, connection=connection)
            if animal is None:
                # 本园没有这头动物：按编号建档，直接采信外部父母。
                data = {
                    "name": record.get("name") or animal_id,
                    "sex": sex,
                    "sire_id": external_parents["sire_id"],
                    "dam_id": external_parents["dam_id"],
                    "external_claims": [
                        {"batch_id": batch_id, "line_no": line_no, **external_parents}
                    ],
                    "open_conflicts": [],
                }
                if record.get("birth_date"):
                    data["birth_date"] = record["birth_date"]
                animal = self.repository.create_entity(
                    animal_id, "animal", "active", data, actor.user_id,
                    connection=connection,
                )
                self.repository.append_audit(
                    animal_id, actor.user_id, actor.role,
                    "pedigree_import_created", None, "active",
                    {"batch_id": batch_id, "line_no": line_no,
                     "parents": external_parents},
                    connection=connection,
                )
                self.repository.mark_import_row(
                    batch_id, line_no, "created", animal_id=animal_id,
                    connection=connection,
                )
                outcome = "created"
            else:
                local_parents = parent_claim(animal["data"])
                claims = list(animal["data"].get("external_claims", []))
                already_seen = any(
                    item.get("batch_id") == batch_id and item.get("line_no") == line_no
                    for item in claims
                )
                if already_seen:
                    outcome = "skipped"
                elif not claims_conflict(local_parents, external_parents):
                    # 父母说法一致：只并入外部来源，不动血缘。
                    if not any(
                        parent_claim(item) == external_parents for item in claims
                    ):
                        claims.append(
                            {"batch_id": batch_id, "line_no": line_no,
                             **external_parents}
                        )
                    data = dict(animal["data"])
                    data["external_claims"] = claims
                    self.repository.update_entity(
                        animal_id, animal["version"], animal["status"], data,
                        connection=connection,
                    )
                    self.repository.append_audit(
                        animal_id, actor.user_id, actor.role,
                        "pedigree_import_merged", animal["status"], animal["status"],
                        {"batch_id": batch_id, "line_no": line_no,
                         "parents": external_parents},
                        connection=connection,
                    )
                    outcome = "merged"
                else:
                    # 父母说法冲突：两版都留，挂起等协调员逐条裁定。
                    duplicate = self.repository.find_open_conflict(
                        animal_id, external_parents, connection=connection
                    )
                    if duplicate is None:
                        self.repository.add_conflict(
                            animal_id, batch_id, line_no,
                            local_parents, external_parents,
                            connection=connection,
                        )
                        open_conflicts = list(
                            animal["data"].get("open_conflicts", [])
                        )
                        if not any(
                            c.get("batch_id") == batch_id and c.get("line_no") == line_no
                            for c in open_conflicts
                        ):
                            open_conflicts.append(
                                {"batch_id": batch_id, "line_no": line_no}
                            )
                        if not any(
                            parent_claim(item) == external_parents for item in claims
                        ):
                            claims.append(
                                {"batch_id": batch_id, "line_no": line_no,
                                 **external_parents}
                            )
                        data = dict(animal["data"])
                        data["external_claims"] = claims
                        data["open_conflicts"] = open_conflicts
                        self.repository.update_entity(
                            animal_id, animal["version"], animal["status"], data,
                            connection=connection,
                        )
                        self.repository.append_audit(
                            animal_id, actor.user_id, actor.role,
                            "pedigree_conflict_opened", animal["status"],
                            animal["status"],
                            {"batch_id": batch_id, "line_no": line_no,
                             "local_parents": local_parents,
                             "external_parents": external_parents},
                            connection=connection,
                        )
                    outcome = "conflict"
                self.repository.mark_import_row(
                    batch_id, line_no,
                    outcome, animal_id=animal_id, connection=connection,
                )
            connection.commit()
            return outcome
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    # ---------- 冲突裁定 ----------

    def list_conflicts(self, status=None, animal_id=None):
        return self.repository.list_conflicts(status=status, animal_id=animal_id)

    def get_import_batch(self, batch_id):
        batch = self.repository.get_import_batch(batch_id)
        if not batch:
            raise NotFoundError("import batch not found: " + str(batch_id))
        return batch

    def list_import_batches(self):
        return self.repository.list_import_batches()

    def list_import_rows(self, batch_id, status=None):
        self.get_import_batch(batch_id)
        return self.repository.list_import_rows(batch_id, status=status)

    def resolve_conflict(self, actor, conflict_id, decision, custom_parents=None,
                         expected_version=None):
        """协调员逐条裁定父母冲突。

        decision:
        - keep_local：保留本园记录的父母；
        - take_external：采用合作园谱系的父母；
        - custom：使用 custom_parents 里人工指定的 sire_id/dam_id。
        裁定后血缘改动，重算所有待批配对近交系数，超限退回待复核。
        """
        if actor.role not in self.rules.RESOLVE_ROLES:
            raise PermissionDenied(
                "role %s is not allowed to resolve conflicts" % actor.role
            )
        conflict = self.repository.get_conflict(conflict_id)
        if not conflict:
            raise NotFoundError("conflict not found: " + str(conflict_id))
        if conflict["status"] != "open":
            raise ConflictError("conflict %s already resolved" % conflict_id)
        if decision not in ("keep_local", "take_external", "custom"):
            raise ValidationError("decision must be keep_local, take_external or custom")
        if decision == "keep_local":
            chosen = conflict["local_parents"]
        elif decision == "take_external":
            chosen = conflict["external_parents"]
        else:
            custom_parents = custom_parents or {}
            chosen = parent_claim(custom_parents)

        animal_id = conflict["animal_id"]
        connection = self.repository.begin()
        try:
            animal = self.repository.get_entity(animal_id, connection=connection)
            if not animal:
                raise NotFoundError("animal not found: " + animal_id)
            if expected_version is not None and animal["version"] != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, animal["version"])
                )
            before_parents = parent_claim(animal["data"])

            self.repository.resolve_conflict(
                conflict_id, decision, chosen, actor.user_id, connection=connection
            )
            data = dict(animal["data"])
            data["sire_id"] = chosen["sire_id"]
            data["dam_id"] = chosen["dam_id"]
            # open_conflicts 以真实冲突表为准重新核对，避免引用漂移。
            remaining_refs = []
            for open_conflict in self.repository.list_conflicts(
                status="open", animal_id=animal_id, connection=connection
            ):
                remaining_refs.append(
                    {"batch_id": open_conflict["batch_id"],
                     "line_no": open_conflict["line_no"]}
                )
            data["open_conflicts"] = remaining_refs
            updated = self.repository.update_entity(
                animal_id, animal["version"], animal["status"], data,
                connection=connection,
            )
            self.repository.append_audit(
                animal_id, actor.user_id, actor.role,
                "pedigree_conflict_resolved", animal["status"], animal["status"],
                {"conflict_id": conflict_id, "decision": decision,
                 "before_parents": before_parents, "after_parents": chosen},
                connection=connection,
            )
            returned = self._recalc_pairings(actor, connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return {
            "conflict": self.repository.get_conflict(conflict_id),
            "animal": self.repository.get_entity(animal_id),
            "returned_pairings": returned,
        }

    def _recalc_pairings(self, actor, connection):
        """血缘改动后重算待批配对；超过红线的退回待复核。已批准的结论照旧保留。"""
        returned = []
        pairings = self.repository.list_entities(
            kind="pairing", status="proposed", connection=connection
        )
        for pairing in pairings:
            sire_id = normalize_parent_id(pairing["data"].get("sire_id"))
            dam_id = normalize_parent_id(pairing["data"].get("dam_id"))
            if not sire_id or not dam_id:
                continue
            sire = self.repository.get_entity(sire_id, connection=connection)
            dam = self.repository.get_entity(dam_id, connection=connection)
            if not sire or not dam:
                continue
            coefficient = inbreeding_coefficient(
                entity_view(sire), entity_view(dam)
            )
            blocked = bool(sire["data"].get("open_conflicts")) or bool(
                dam["data"].get("open_conflicts")
            )
            over_line = coefficient > INBREEDING_LIMIT
            data = dict(pairing["data"])
            if over_line or blocked:
                if over_line:
                    reason = (
                        "血缘裁定后重算近交系数 %s 超过红线 %s，退回待复核"
                        % (coefficient, INBREEDING_LIMIT)
                    )
                else:
                    reason = (
                        "相关个体的谱系冲突尚未全部裁定，退回待复核"
                    )
                history = list(data.get("return_history", []))
                history.append(
                    {"reason": reason, "coefficient": coefficient,
                     "by": actor.user_id}
                )
                data["return_reason"] = reason
                data["return_history"] = history
                data["coefficient"] = coefficient
                self.repository.update_entity(
                    pairing["id"], pairing["version"], "returned", data,
                    connection=connection,
                )
                self.repository.append_audit(
                    pairing["id"], actor.user_id, actor.role,
                    "auto_return", "proposed", "returned",
                    {"reason": reason, "coefficient": coefficient},
                    connection=connection,
                )
                returned.append({"pairing_id": pairing["id"], "reason": reason})
            elif data.get("coefficient") != coefficient:
                # 系数有变化但仍安全：只更新系数，保持待批；无变化不动版本。
                data["coefficient"] = coefficient
                self.repository.update_entity(
                    pairing["id"], pairing["version"], "proposed", data,
                    connection=connection,
                )
        return returned
