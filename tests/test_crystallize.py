import unittest

from memcore.crystallize import SkillProof, crystallize_skill


class TestCrystallizeSkill(unittest.TestCase):
    def test_crystallize_valid_skill(self):
        steps = [
            "Run platform tools probe to verify ADB connection.",
            "Apply safe power tuning settings via shell command.",
            "Verify device responds with battery status OK.",
        ]
        skill_md, proof = crystallize_skill(
            name="android-battery-tune",
            description="Tune device power settings for battery longevity.",
            steps=steps,
            verification_cmd="adb shell dumpsys battery",
            exit_code=0,
            category="devops",
        )

        self.assertTrue(skill_md.startswith("---\n"))
        self.assertIn("name: android-battery-tune", skill_md)
        self.assertIn('description: "Tune device power settings for battery longevity."', skill_md)
        self.assertIn("platforms: [linux, macos, windows]", skill_md)
        self.assertIn("## Procedure", skill_md)
        self.assertIn("## Verification", skill_md)
        self.assertIn("adb shell dumpsys battery", skill_md)

        # Check Skill Proof
        self.assertEqual(proof.status, "verified")
        self.assertEqual(proof.exit_code, 0)
        self.assertTrue(len(proof.proof_hash) == 64)
        self.assertIn(f"proof_hash: {proof.proof_hash}", skill_md)

    def test_description_length_enforced_under_60_chars(self):
        long_desc = "This is an unnecessarily long description that exceeds sixty characters easily."
        skill_md, _ = crystallize_skill(
            name="short-skill",
            description=long_desc,
            steps=["Step 1"],
            verification_cmd="echo ok",
            exit_code=0,
        )
        for line in skill_md.splitlines():
            if line.startswith("description:"):
                desc_val = line.split(":", 1)[1].strip().strip('"\'')
                self.assertLessEqual(len(desc_val), 60)
                self.assertTrue(desc_val.endswith("."))

    def test_failed_verification_marks_unverified(self):
        _, proof = crystallize_skill(
            name="failing-skill",
            description="Failing skill workflow.",
            steps=["Step 1"],
            verification_cmd="pytest tests/fail.py",
            exit_code=1,
        )
        self.assertEqual(proof.status, "unverified")


if __name__ == "__main__":
    unittest.main()
