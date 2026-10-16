import unittest

from scripts.screenshots.selectors import grant_option_matches


class GrantOptionTests(unittest.TestCase):
    def test_exact_government_ready_grant_wins_in_a_mixed_cloud_group(self) -> None:
        options = [
            "Customer Insights · CI-204 · Azure Commercial · Production · GPT-4o mini"
            " · Granted directly to you · Applied to APIM",
            "Customer Insights · CI-204 · Azure Government · Production · GPT-4o mini"
            " · Granted directly to you · Ready",
        ]
        self.assertEqual(
            [
                text
                for text in options
                if grant_option_matches(text, "Customer Insights", "Azure Government")
            ],
            [options[1]],
        )
        self.assertTrue(grant_option_matches(options[0], "Customer Insights", None))

    def test_both_ready_labels_are_accepted(self) -> None:
        for status in ("Ready", "Applied to APIM"):
            with self.subTest(status=status):
                self.assertTrue(
                    grant_option_matches(
                        "Customer Insights · CI-204 · Azure Government · Production"
                        " · GPT-4o mini · 2024-07-18"
                        f" · Granted directly to you · {status}",
                        "Customer Insights",
                        "Azure Government",
                    )
                )

    def test_cloud_and_cost_center_are_exact_label_segments(self) -> None:
        option = (
            "Customer Insights · CI-204 · Azure Government · Production · GPT-4o mini"
            " · Granted directly to you · Ready"
        )
        for cloud in ("AzGov", "Government", "Azure", "Azure Commercial"):
            with self.subTest(cloud=cloud):
                self.assertFalse(grant_option_matches(option, "Customer Insights", cloud))
        self.assertFalse(grant_option_matches(option, "Customer", "Azure Government"))
        self.assertFalse(
            grant_option_matches(
                option.replace("Customer Insights ·", "Customer Insights Extended ·"),
                "Customer Insights",
                "Azure Government",
            )
        )

    def test_nonready_indirect_and_other_cost_center_options_are_rejected(self) -> None:
        prefix = "Customer Insights · CI-204 · Azure Government · Production · GPT-4o mini"
        for attribution, status in [
            ("Granted directly to you", "Preparing key"),
            ("Granted directly to you", "Key setup failed"),
            ("Granted directly to you", "Key deleted"),
            ("Granted through a group", "Ready"),
        ]:
            with self.subTest(attribution=attribution, status=status):
                self.assertFalse(
                    grant_option_matches(
                        f"{prefix} · {attribution} · {status}",
                        "Customer Insights",
                        "Azure Government",
                    )
                )
        self.assertFalse(
            grant_option_matches(
                f"{prefix} · Granted directly to you · Ready", "General", "Azure Government"
            )
        )
