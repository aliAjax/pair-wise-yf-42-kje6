import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import (
    Actor,
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)
from src.repository import SQLiteRepository
from src.rules import INBREEDING_LIMIT, RuleEngine
from src.service import DomainService

REGISTRAR = Actor("reg-1", "registrar")
COORD_A = Actor("coord-a", "coordinator")
COORD_B = Actor("coord-b", "coordinator")
ADMIN = Actor("admin", "admin")


class PedigreeImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())

    def tearDown(self):
        self.tmp.cleanup()

    def _animal(self, actor=ADMIN, **data):
        payload = {"name": data.get("id", "A"), "sex": "unknown"}
        payload.update(data)
        return self.service.create(actor, "animal", payload)

    def test_import_creates_new_animals_and_merges_matching_parents(self):
        self._animal(id="A001", name="一号", sex="female",
                     sire_id="S1", dam_id="D1")
        batch = self.service.import_pedigree(
            REGISTRAR, "batch-1", "合作园甲",
            [
                {"id": "A001", "name": "一号", "sex": "female",
                 "sire_id": "S1", "dam_id": "D1"},
                {"id": "A002", "name": "二号", "sex": "male",
                 "sire_id": "S2", "dam_id": "D2"},
            ],
        )
        self.assertEqual(batch["status"], "completed")
        self.assertEqual(batch["summary"]["merged"], 1)
        self.assertEqual(batch["summary"]["created"], 1)
        # 父母一致，不产生冲突；外部说法留痕在 external_claims。
        a001 = self.service.get("A001")
        self.assertEqual(a001["data"].get("open_conflicts", []), [])
        self.assertEqual(len(a001["data"]["external_claims"]), 1)
        a002 = self.service.get("A002")
        self.assertEqual(a002["data"]["sire_id"], "S2")

    def test_conflicting_parents_kept_as_two_versions_and_block_pairing(self):
        self._animal(id="C1", sex="female", sire_id="S-local", dam_id="D-local")
        sire = self._animal(id="S", sex="male")
        batch = self.service.import_pedigree(
            REGISTRAR, "batch-2", "合作园乙",
            [{"id": "C1", "sex": "female",
              "sire_id": "S-ext", "dam_id": "D-ext"}],
        )
        self.assertEqual(batch["summary"]["conflict"], 1)
        conflicts = self.service.list_conflicts(status="open")
        self.assertEqual(len(conflicts), 1)
        conflict = conflicts[0]
        self.assertEqual(conflict["animal_id"], "C1")
        self.assertEqual(conflict["local_parents"]["sire_id"], "S-local")
        self.assertEqual(conflict["external_parents"]["sire_id"], "S-ext")
        # 两版说法都留着：本园父母未被覆盖，外部说法留在冲突里和 external_claims。
        c1 = self.service.get("C1")
        self.assertEqual(c1["data"]["sire_id"], "S-local")
        self.assertTrue(c1["data"]["open_conflicts"])

        pairing = self.service.create(
            COORD_A, "pairing",
            {"proposed_by": "coord-a", "sire_id": "S", "dam_id": "C1"},
        )
        with self.assertRaises(ValidationError):
            self.service.transition(
                COORD_A, pairing["id"], "approve",
                {"approvals": ["coord-a"]},
            )

    def test_resolve_conflict_take_external_recalculates_and_returns_over_limit(self):
        # 家系：S 是 G 的父亲；若 C 的父亲改判为 S，则 S x C 这一配对近交系数 0.25，超限。
        self._animal(id="G", sex="male", sire_id="S", dam_id="DG")
        self._animal(id="S", sex="male")
        self._animal(id="DS", sex="female")
        self._animal(id="C", sex="female", sire_id="G", dam_id="DS")
        proposed = self.service.create(
            COORD_A, "pairing",
            {"proposed_by": "coord-a", "sire_id": "S", "dam_id": "C"},
        )
        # 改判前：G x 母 unknown 结构下 S 与 C 系数 0，不超限，处于待批。
        self.service.import_pedigree(
            REGISTRAR, "batch-3", "合作园丙",
            [{"id": "C", "sex": "female", "sire_id": "S", "dam_id": "DS"}],
        )
        conflict = self.service.list_conflicts(status="open", animal_id="C")[0]

        # 已批准的配对结论照旧保留：先准备一个当时合法并批准的配对。
        approved_dam = self._animal(id="C2", sex="female", sire_id="GX", dam_id="DX")
        approved = self.service.create(
            COORD_A, "pairing",
            {"proposed_by": "coord-a", "sire_id": "S", "dam_id": "C2"},
        )
        self.service.transition(
            COORD_A, approved["id"], "approve", {"approvals": ["coord-a"]}
        )

        result = self.service.resolve_conflict(
            COORD_A, conflict["id"], "take_external"
        )
        self.assertEqual(result["animal"]["data"]["sire_id"], "S")
        self.assertEqual(result["animal"]["data"]["open_conflicts"], [])
        returned = result["returned_pairings"]
        self.assertEqual([item["pairing_id"] for item in returned], [proposed["id"]])
        self.assertIn("近交系数", returned[0]["reason"])

        refreshed = self.service.get(proposed["id"])
        self.assertEqual(refreshed["status"], "returned")
        self.assertGreater(refreshed["data"]["coefficient"], INBREEDING_LIMIT)
        self.assertTrue(refreshed["data"]["return_reason"])
        self.assertEqual(len(refreshed["data"]["return_history"]), 1)

        # 已批准的结论照旧。
        approved_after = self.service.get(approved["id"])
        self.assertEqual(approved_after["status"], "approved")

        # 冲突裁定后可以重新提交，系数回落后可重新批准。
        self.service.transition(COORD_A, proposed["id"], "resubmit", {})
        # 此时 S 与 C 是父女，仍然超限 -> 换无亲缘母本重新提交批准。
        fixed = self.service.create(
            COORD_A, "pairing",
            {"proposed_by": "coord-a", "sire_id": "S", "dam_id": "C2"},
        )
        self.service.transition(
            COORD_A, fixed["id"], "approve", {"approvals": ["coord-a"]}
        )

    def test_resolve_keep_local_clears_blocking_and_allows_approval(self):
        self._animal(id="K", sex="female", sire_id="L1", dam_id="L2")
        sire = self._animal(id="S", sex="male")
        self.service.import_pedigree(
            REGISTRAR, "batch-4", "合作园丁",
            [{"id": "K", "sex": "female", "sire_id": "E1", "dam_id": "E2"}],
        )
        conflict = self.service.list_conflicts(status="open")[0]
        result = self.service.resolve_conflict(
            COORD_B, conflict["id"], "keep_local"
        )
        self.assertEqual(result["animal"]["data"]["sire_id"], "L1")
        pairing = self.service.create(
            COORD_B, "pairing",
            {"proposed_by": "coord-b", "sire_id": "S", "dam_id": "K"},
        )
        approved = self.service.transition(
            COORD_B, pairing["id"], "approve", {"approvals": ["coord-b"]}
        )
        self.assertEqual(approved["status"], "approved")

    def test_concurrent_confirmations_only_one_approved(self):
        self._animal(id="S", sex="male")
        self._animal(id="D", sex="female")
        pairing = self.service.create(
            COORD_A, "pairing",
            {"proposed_by": "coord-a", "sire_id": "S", "dam_id": "D"},
        )
        errors = []

        def approve(actor):
            try:
                self.service.transition(
                    actor, pairing["id"], "approve",
                    {"approvals": [actor.user_id]},
                )
            except Exception as exc:  # noqa: BLE001 - 记录到列表再断言
                errors.append(exc)

        t1 = threading.Thread(target=approve, args=(COORD_A,))
        t2 = threading.Thread(target=approve, args=(COORD_B,))
        t1.start(); t2.start()
        t1.join(); t2.join()

        self.assertEqual(len(errors), 1)
        # 后到者要么撞上版本冲突，要么读到已批准状态——两者都是 409，确认只放行一次。
        self.assertIsInstance(errors[0], (ConflictError, InvalidTransition))
        self.assertEqual(self.service.get(pairing["id"])["status"], "approved")
        audit = self.repo.list_audit(entity_id=pairing["id"])
        self.assertEqual(
            sum(1 for item in audit if item["action"] == "approve"), 1
        )

    def test_failed_import_resumes_from_breakpoint(self):
        self._animal(id="R1", sex="female", sire_id="X", dam_id="Y")
        records = [
            {"id": "R1", "sex": "female", "sire_id": "X", "dam_id": "Y"},
            {"id": "", "sex": "female"},  # 缺编号的坏行
            {"id": "R3", "sex": "male"},
        ]
        batch = self.service.import_pedigree(REGISTRAR, "batch-5", "合作园戊", records)
        self.assertEqual(batch["status"], "failed")
        self.assertEqual(batch["summary"]["failed_line"], 2)
        rows = self.service.list_import_rows("batch-5")
        self.assertEqual([row["status"] for row in rows],
                         ["merged", "error", "pending"])
        # 坏行之前的数据没有因为失败回滚。
        self.assertIsNotNone(self.repo.get_entity("R1"))

        fixed = [
            records[0],
            {"id": "R2", "sex": "female"},
            records[2],
        ]
        retried = self.service.import_pedigree(
            REGISTRAR, "batch-5", "合作园戊", fixed
        )
        self.assertEqual(retried["status"], "completed")
        rows = self.service.list_import_rows("batch-5")
        self.assertEqual([row["status"] for row in rows],
                         ["merged", "created", "created"])
        # 第 1 行只入库一次。
        animal = self.service.get("R1")
        self.assertEqual(
            sum(1 for c in animal["data"]["external_claims"]
                if c["batch_id"] == "batch-5"),
            1,
        )

    def test_resend_same_batch_does_not_duplicate(self):
        records = [{"id": "N1", "sex": "male", "sire_id": "P", "dam_id": "Q"}]
        first = self.service.import_pedigree(REGISTRAR, "batch-6", "合作园己", records)
        second = self.service.import_pedigree(REGISTRAR, "batch-6", "合作园己", records)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["status"], "completed")
        animal = self.service.get("N1")
        self.assertEqual(len(animal["data"]["external_claims"]), 1)
        audit = self.repo.list_audit(action_like="pedigree_%")
        self.assertEqual(
            sum(1 for item in audit if item["action"] == "pedigree_import_complete"),
            1,
        )

    def test_three_audit_trails_are_kept(self):
        self._animal(id="T1", sex="female", sire_id="L", dam_id="M")
        self.service.import_pedigree(
            REGISTRAR, "batch-7", "合作园庚",
            [{"id": "T1", "sex": "female", "sire_id": "E", "dam_id": "M"}],
        )
        conflict = self.service.list_conflicts(status="open")[0]
        self.service.resolve_conflict(COORD_A, conflict["id"], "take_external")

        # 动物档案留痕
        animal_audit = self.service.audit_log(entity_id="T1")
        actions = {item["action"] for item in animal_audit}
        self.assertIn("pedigree_conflict_opened", actions)
        self.assertIn("pedigree_conflict_resolved", actions)
        # 导入批次留痕
        batch_audit = self.service.audit_log(entity_id="batch-7")
        self.assertTrue(any(
            item["action"] == "pedigree_import_complete" for item in batch_audit
        ))
        # 审批/自动退回留痕：制造一对超限配对
        self._animal(id="GG", sex="male", sire_id="SS", dam_id="DD")
        self._animal(id="SS", sex="male")
        self._animal(id="CC", sex="female", sire_id="GG", dam_id="DD2")
        waiting = self.service.create(
            COORD_A, "pairing",
            {"proposed_by": "coord-a", "sire_id": "SS", "dam_id": "CC"},
        )
        self.service.import_pedigree(
            REGISTRAR, "batch-8", "合作园辛",
            [{"id": "CC", "sex": "female", "sire_id": "SS", "dam_id": "DD2"}],
        )
        c2 = self.service.list_conflicts(status="open", animal_id="CC")[0]
        result = self.service.resolve_conflict(COORD_A, c2["id"], "take_external")
        pairing_id = result["returned_pairings"][0]["pairing_id"]
        self.assertEqual(pairing_id, waiting["id"])
        pairing_audit = self.service.audit_log(entity_id=pairing_id)
        self.assertTrue(any(
            item["action"] == "auto_return" for item in pairing_audit
        ))

    def test_permissions_for_import_and_resolution(self):
        with self.assertRaises(PermissionDenied):
            self.service.import_pedigree(
                Actor("v", "viewer"), "batch-x", "z", [{"id": "Z1"}]
            )
        self._animal(id="Z1", sex="male", sire_id="loc", dam_id="loc2")
        self.service.import_pedigree(
            REGISTRAR, "batch-9", "合作园壬",
            [{"id": "Z1", "sex": "male", "sire_id": "a", "dam_id": "b"}],
        )
        conflict = self.service.list_conflicts(status="open")[0]
        with self.assertRaises(PermissionDenied):
            self.service.resolve_conflict(
                Actor("r", "registrar"), conflict["id"], "keep_local"
            )


if __name__ == "__main__":
    unittest.main()
