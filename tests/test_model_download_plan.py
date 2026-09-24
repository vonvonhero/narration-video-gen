"""CPU-only tests: acquisition check mode never downloads weights."""
import json
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DownloadPlanTests(unittest.TestCase):
    def run_script(self, *args):
        return subprocess.run(['bash', str(ROOT / 'scripts/download-models.sh'),
                               *args], text=True, capture_output=True)

    def test_q4_plan(self):
        result = self.run_script('--model', 'wan21-i2v-14b-480p-q4ks',
                                 '--model', 'wan22-s2v-14b-q4ks', '--check')
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertEqual(plan['operation'], 'model-acquisition-only')
        self.assertEqual(plan['downloads']['total_bytes'], 23395726304)
        self.assertEqual(len(plan['downloads']['models']), 2)
        self.assertTrue(all(m['license'] == 'Apache-2.0'
                            for m in plan['downloads']['models']))

    def test_duplicate_ids_are_deduplicated(self):
        result = self.run_script('--model', 'wan21-i2v-14b-480p-q4ks',
                                 '--model', 'wan21-i2v-14b-480p-q4ks', '--check')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(json.loads(result.stdout)['downloads']['models']), 1)

    def test_unknown_id(self):
        result = self.run_script('--model', 'not-a-model', '--check')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unknown locked model id', result.stderr)

    def test_profile_cannot_be_mixed(self):
        result = self.run_script('--model', 'wan21-i2v-14b-480p-q4ks',
                                 '--profile', 'unused', '--check')
        self.assertEqual(result.returncode, 2)
        self.assertIn('cannot be combined', result.stderr)

    def test_model_requires_value(self):
        result = self.run_script('--model', '--check')
        self.assertEqual(result.returncode, 2)
        self.assertIn('requires a value', result.stderr)


if __name__ == '__main__':
    unittest.main()
