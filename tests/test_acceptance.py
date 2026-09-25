import unittest

from skills_workspace.acceptance import run


class AcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertFalse(result["first_replayed"])
        self.assertTrue(result["second_replayed"])
        self.assertEqual(1, result["records"])
        self.assertTrue(result["product_traced"])
        self.assertTrue(result["material_conserved"])
        self.assertEqual(0, result["open_discrepancies"])
        self.assertEqual("reevaluated", result["rework_route_status"])
        self.assertEqual(3, result["trace_input_versions"])


if __name__ == "__main__":
    unittest.main()
