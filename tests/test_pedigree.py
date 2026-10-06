import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine, inbreeding_coefficient
from src.service import DomainService


class PedigreeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.coordinator = Actor("coord", "coordinator")
        self.registrar = Actor("reg", "registrar")

    def tearDown(self):
        self.tmp.cleanup()

    def _animal(self, animal_id, sex, sire_id=None, dam_id=None):
        data = {"id": animal_id, "name": animal_id, "sex": sex}
        if sire_id is not None:
            data["sire_id"] = sire_id
        if dam_id is not None:
            data["dam_id"] = dam_id
        return self.service.create(self.admin, "animal", data)

    def test_inbreeding_coefficient_enhanced(self):
        self.assertEqual(inbreeding_coefficient({"id": "a"}, {"id": "a"}), 0.5)
        self.assertEqual(
            inbreeding_coefficient({"id": "a", "sire_id": "b"}, {"id": "b"}), 0.25
        )
        self.assertEqual(
            inbreeding_coefficient(
                {"id": "a", "sire_id": "s", "dam_id": "d"},
                {"id": "b", "sire_id": "s", "dam_id": "d"},
            ),
            0.25,
        )
        self.assertEqual(
            inbreeding_coefficient(
                {"id": "a", "sire_id": "s"}, {"id": "b", "sire_id": "s"}
            ),
            0.125,
        )
        self.assertEqual(
            inbreeding_coefficient(
                {"id": "a", "sire_id": "x"}, {"id": "b", "sire_id": "y"}
            ),
            0.0,
        )

    def test_import_creates_new_animals_and_matches_existing(self):
        self._animal("A", "male")
        result = self.service.import_pedigree(
            self.registrar,
            "B1",
            [
                {"animal_id": "A", "sire_id": "S", "dam_id": "D"},
                {"animal_id": "B", "sire_id": "S", "dam_id": "D"},
            ],
        )
        self.assertEqual(result["status"], "completed")
        outcomes = {r["animal_id"]: r["outcome"] for r in result["data"]["results"]}
        self.assertEqual(outcomes["A"], "conflict")
        self.assertEqual(outcomes["B"], "created")
        b = self.service.get("B")
        self.assertEqual(b["data"]["sire_id"], "S")
        self.assertEqual(b["data"]["dam_id"], "D")

    def test_import_creates_conflict_for_mismatched_parents(self):
        self._animal("A", "male", sire_id="OLD-S", dam_id="OLD-D")
        result = self.service.import_pedigree(
            self.registrar,
            "B2",
            [{"animal_id": "A", "sire_id": "NEW-S", "dam_id": "NEW-D"}],
        )
        self.assertEqual(result["status"], "completed")
        conflict_id = result["data"]["results"][0]["conflict_id"]
        conflict = self.service.get(conflict_id)
        self.assertEqual(conflict["status"], "pending")
        self.assertEqual(conflict["data"]["local"], {"sire_id": "OLD-S", "dam_id": "OLD-D"})
        self.assertEqual(conflict["data"]["external"], {"sire_id": "NEW-S", "dam_id": "NEW-D"})
        animal = self.service.get("A")
        self.assertEqual(animal["data"]["sire_id"], "OLD-S")

    def test_readjudicate_conflict_updates_animal_and_recalculates(self):
        self._animal("A", "male")
        self._animal("B", "female")
        pairing = self.service.create(
            self.admin, "pairing", {"proposed_by": "coord", "sire_id": "A", "dam_id": "B"}
        )
        self.assertEqual(pairing["status"], "proposed")
        imp = self.service.import_pedigree(
            self.registrar,
            "B3",
            [
                {"animal_id": "A", "sire_id": "S", "dam_id": "D"},
                {"animal_id": "B", "sire_id": "S", "dam_id": "D"},
            ],
        )
        conflict_ids = {
            r["animal_id"]: r["conflict_id"]
            for r in imp["data"]["results"]
            if r["outcome"] == "conflict"
        }
        self.service.transition(
            self.coordinator, conflict_ids["A"], "adjudicate", {"choice": "external"}
        )
        self.service.transition(
            self.coordinator, conflict_ids["B"], "adjudicate", {"choice": "external"}
        )
        updated = self.service.get(pairing["id"])
        self.assertEqual(updated["status"], "returned")
        self.assertAlmostEqual(updated["data"]["return_reason"]["coefficient"], 0.25)

    def test_unadjudicated_animal_blocks_pairing_approval(self):
        self._animal("A", "male")
        self._animal("B", "female")
        self.service.import_pedigree(
            self.registrar,
            "B4",
            [{"animal_id": "A", "sire_id": "S", "dam_id": "D"}],
        )
        pairing = self.service.create(
            self.admin, "pairing", {"proposed_by": "coord", "sire_id": "A", "dam_id": "B"}
        )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.coordinator,
                pairing["id"],
                "approve",
                {"sire_id": "A", "dam_id": "B", "approvals": ["vet-1"]},
            )

    def test_approve_is_idempotent(self):
        self._animal("A", "male")
        self._animal("B", "female")
        pairing = self.service.create(
            self.admin, "pairing", {"proposed_by": "coord", "sire_id": "A", "dam_id": "B"}
        )
        first = self.service.transition(
            self.coordinator,
            pairing["id"],
            "approve",
            {"sire_id": "A", "dam_id": "B", "approvals": ["vet-1"]},
        )
        self.assertEqual(first["status"], "approved")
        second = self.service.transition(
            self.coordinator,
            pairing["id"],
            "approve",
            {"sire_id": "A", "dam_id": "B", "approvals": ["vet-1"]},
        )
        self.assertEqual(second["status"], "approved")
        self.assertEqual(first["id"], second["id"])
        approve_audits = [
            a for a in self.service.audit_log(pairing["id"]) if a["action"] == "approve"
        ]
        self.assertEqual(len(approve_audits), 1)

    def test_already_approved_pairing_not_recalculated(self):
        self._animal("A", "male")
        self._animal("B", "female")
        pairing = self.service.create(
            self.admin, "pairing", {"proposed_by": "coord", "sire_id": "A", "dam_id": "B"}
        )
        self.service.transition(
            self.coordinator,
            pairing["id"],
            "approve",
            {"sire_id": "A", "dam_id": "B", "approvals": ["vet-1"]},
        )
        self.service.import_pedigree(
            self.registrar,
            "B5",
            [
                {"animal_id": "A", "sire_id": "S", "dam_id": "D"},
                {"animal_id": "B", "sire_id": "S", "dam_id": "D"},
            ],
        )
        conflicts = self.service.list("conflicts", status="pending")
        for c in conflicts:
            self.service.transition(
                self.coordinator, c["id"], "adjudicate", {"choice": "external"}
            )
        updated = self.service.get(pairing["id"])
        self.assertEqual(updated["status"], "approved")

    def test_same_batch_resend_does_not_duplicate(self):
        self._animal("A", "male")
        records = [{"animal_id": "A", "sire_id": "S", "dam_id": "D"}]
        first = self.service.import_pedigree(self.registrar, "B6", records)
        second = self.service.import_pedigree(self.registrar, "B6", records)
        self.assertEqual(first["id"], second["id"])
        conflicts = self.service.list("conflicts")
        self.assertEqual(len(conflicts), 1)

    def test_import_resumes_from_breakpoint(self):
        self._animal("A", "male")
        self._animal("B", "female")
        records = [
            {"animal_id": "A", "sire_id": "S1", "dam_id": "D1"},
            {"animal_id": "B", "sire_id": "S2", "dam_id": "D2"},
        ]
        result = self.service.import_pedigree(self.registrar, "B7", records)
        self.assertEqual(result["status"], "completed")
        imp = self.repo.find_entities("pedigree_import", "batch_key", "B7")[0]
        failed_data = dict(imp["data"])
        failed_data["processed"] = 1
        failed_data["results"] = imp["data"]["results"][:1]
        self.repo.update_entity(imp["id"], imp["version"], "failed", failed_data)
        resumed = self.service.import_pedigree(self.registrar, "B7", records)
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(resumed["data"]["processed"], 2)
        self.assertEqual(len(resumed["data"]["results"]), 2)


if __name__ == "__main__":
    unittest.main()
