"""Checks parts of tests/e2e/kind.sh without a cluster: python3 tests/test_e2e_harness.py"""
from pathlib import Path
import subprocess
import unittest

KIND = Path(__file__).resolve().parent / 'e2e' / 'kind.sh'


class HarnessChecks(unittest.TestCase):
    def run_shell(self, body):
        script = 'set -euo pipefail\nfailures=0\nok() { echo "ok   $*"; }\nfail() { echo "FAIL $*"; failures=$((failures + 1)); }\n' + body
        return subprocess.run(['bash', '-c', script], capture_output=True, text=True)

    def test_helm_test_reports_and_logs_failures(self):
        text = KIND.read_text()
        snippet = 'if helm test' + text.split('if helm test', 1)[1].split('\nfi', 1)[0] + '\nfi\n'
        for status in (0, 7):
            with self.subTest(status=status):
                mocks = f'helm() {{ echo "helm $*" >&2; return {status}; }}\nkubectl() {{ echo "test pod log: $*" >&2; return 19; }}\n'
                result = self.run_shell(mocks + snippet + 'echo "failures=$failures"\n')
                # A failed test neither aborts the suite (set -e) nor hides the pod's log.
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('helm test stalwart --namespace mail', result.stderr)
                if status == 0:
                    self.assertIn('ok   helm test', result.stdout)
                    self.assertIn('failures=0', result.stdout)
                    self.assertNotIn('test pod log', result.stderr)
                else:
                    self.assertIn('FAIL helm test', result.stdout)
                    self.assertIn('failures=1', result.stdout)
                    self.assertIn('test pod log: --namespace mail logs stalwart-test', result.stderr)


if __name__ == '__main__':
    unittest.main()
