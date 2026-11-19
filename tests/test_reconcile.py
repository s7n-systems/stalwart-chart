"""Reconciliation state-machine checks without a cluster or a Stalwart server."""
import base64
import copy
import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('reconcile', Path(__file__).resolve().parents[1] / 'files/reconcile.py')
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


class ReconcileChecks(unittest.TestCase):
    def setUp(self):
        self.config = {'release': 'mail', 'lockTimeoutSeconds': 300,
                       'restart': {'namespace': 'mail', 'statefulset': 'mail', 'settingsHash': 'hash', 'timeoutSeconds': 30}}
        self.account = {'id': 'mailer', 'name': 'mailer', 'address': 'mailer@example.org',
                        'secret': {'namespace': 'mail', 'name': 'smtp', 'keys': ['password', 'smtpPassword']},
                        'appPassword': {'allowedIps': ['10.0.0.0/8'], 'permissions': ['authenticate', 'emailSend']}}
        self.secret = {'metadata': {'namespace': 'mail', 'name': 'smtp', 'resourceVersion': '1',
                                    'annotations': {r.CREDENTIAL: 'ref'}},
                       'data': {'password': base64.b64encode(b'old').decode(), 'unrelated': 'a2VlcA=='}}
        self.kube = Mock()
        self.kube.get_secret.side_effect = lambda *a: copy.deepcopy(self.secret)
        def replace(secret):
            self.secret = copy.deepcopy(secret)
            return copy.deepcopy(secret)
        self.kube.replace_secret.side_effect = replace
        self.stalwart = Mock()
        self.stalwart.account_id.return_value = 'account'
        self.password = {'id': 'old-id', 'description': 'managed:mail/mailer #ref',
                         'allowedIps': {'10.0.0.0/8': True}, 'permissions': ['authenticate', 'emailSend']}
        self.stalwart.app_passwords.return_value = [self.password]
        self.stalwart.create_app_password.return_value = ('new-id', 'new-password')
        self.stalwart.set_settings_marker.return_value = 'role'
        self.ss = {'metadata': {'generation': 2}, 'spec': {'replicas': 1, 'template': {'metadata': {'annotations': {r.SETTINGS: 'hash@1'}}}},
                   'status': {'observedGeneration': 2, 'updatedReplicas': 1, 'readyReplicas': 1, 'currentRevision': 'v2', 'updateRevision': 'v2'}}
        self.kube.get_statefulset.return_value = self.ss

    def reconcile(self):
        r.reconcile_account(self.config, self.account, self.kube, self.stalwart)

    def test_existing_password_syncs_keys_without_rotation(self):
        self.reconcile()
        self.assertEqual(self.secret['data']['smtpPassword'], self.secret['data']['password'])
        self.assertEqual(self.secret['data']['unrelated'], 'a2VlcA==')
        self.assertNotIn(r.LOCK, self.secret['metadata']['annotations'])
        self.stalwart.create_app_password.assert_not_called()
        self.stalwart.update_app_password.assert_not_called()
        self.stalwart.destroy_app_passwords.assert_not_called()

    def test_restrictions_change_without_rotation(self):
        self.account['appPassword']['allowedIps'] = ['fd00::/8']
        self.reconcile()
        self.stalwart.update_app_password.assert_called_once_with('account', 'old-id', {'allowedIps': {'fd00::/8': True}, 'permissions': ['authenticate', 'emailSend']})
        self.stalwart.create_app_password.assert_not_called()

    def test_database_reset_creates_and_publishes_password(self):
        self.stalwart.app_passwords.return_value = []
        self.reconcile()
        self.stalwart.create_app_password.assert_called_once()
        self.assertEqual(base64.b64decode(self.secret['data']['password']), b'new-password')
        self.assertNotEqual(self.secret['metadata']['annotations'][r.CREDENTIAL], 'ref')

    def test_lost_secret_data_revokes_unrecoverable_password(self):
        self.secret['data'] = {}
        self.reconcile()
        self.stalwart.destroy_app_passwords.assert_called_once_with('account', ['old-id'])
        self.stalwart.create_app_password.assert_called_once()

    def test_orphans_are_revoked_but_foreign_credentials_are_preserved(self):
        self.stalwart.app_passwords.return_value += [
            {'id': 'orphan', 'description': 'managed:mail/mailer #lost'},
            {'id': 'legacy', 'description': 'managed:mail/mailer'},
            {'id': 'foreign', 'description': 'managed:other/mailer #foreign'}]
        self.reconcile()
        self.stalwart.destroy_app_passwords.assert_called_once_with('account', ['legacy', 'orphan'])
        self.stalwart.create_app_password.assert_not_called()

    def test_admin_api_failure_does_not_rotate(self):
        self.stalwart.app_passwords.side_effect = r.Failure('unreachable')
        with self.assertRaises(r.Failure):
            self.reconcile()
        self.stalwart.create_app_password.assert_not_called()
        self.stalwart.destroy_app_passwords.assert_not_called()
        self.assertNotIn(r.LOCK, self.secret['metadata']['annotations'])

    def test_failed_smtp_check_does_not_replace_existing_password(self):
        self.config['smtpCheck'] = {'host': 'smtp', 'port': 2525}
        with patch.object(r, 'smtp_check', side_effect=r.Failure('login failed')), self.assertRaises(r.Failure):
            self.reconcile()
        self.stalwart.create_app_password.assert_not_called()
        self.stalwart.destroy_app_passwords.assert_not_called()
        self.assertEqual(self.secret['metadata']['annotations'][r.CREDENTIAL], 'ref')

    def test_failed_new_password_check_revokes_without_publishing(self):
        self.stalwart.app_passwords.return_value = []
        self.config['smtpCheck'] = {'host': 'smtp', 'port': 2525}
        with patch.object(r, 'smtp_check', side_effect=r.Failure('login failed')), self.assertRaises(r.Failure):
            self.reconcile()
        self.stalwart.destroy_app_passwords.assert_called_once_with('account', ['new-id'])
        self.assertEqual(base64.b64decode(self.secret['data']['password']), b'old')

    def test_failed_publication_is_recovered_as_an_orphan(self):
        self.stalwart.app_passwords.return_value = []
        replace = self.kube.replace_secret.side_effect
        writes = 0
        def fail_publication(secret):
            nonlocal writes
            writes += 1
            if writes == 2:
                raise r.Failure('conflict during publication')
            return replace(secret)
        self.kube.replace_secret.side_effect = fail_publication
        with self.assertRaises(r.Failure):
            self.reconcile()
        created = self.stalwart.create_app_password.call_args.args[1]
        self.assertEqual(self.secret['metadata']['annotations'][r.CREDENTIAL], 'ref')
        self.stalwart.app_passwords.return_value = [{'id': 'new-id', **created}]
        self.stalwart.create_app_password.return_value = ('replacement-id', 'replacement-password')
        self.reconcile()
        self.stalwart.destroy_app_passwords.assert_called_once_with('account', ['new-id'])
        self.assertEqual(base64.b64decode(self.secret['data']['password']), b'replacement-password')

    def test_active_lock_blocks_another_job(self):
        self.secret['metadata']['annotations'][r.LOCK] = 'other@100'
        with patch.dict(os.environ, {'JOB_UID': 'this'}), patch.object(r.time, 'time', return_value=101), self.assertRaises(r.Failure):
            self.reconcile()
        self.kube.replace_secret.assert_not_called()
        self.stalwart.account_id.assert_not_called()

    def test_conflict_on_lock_does_not_touch_stalwart(self):
        self.kube.replace_secret.side_effect = r.Failure('conflict')
        with self.assertRaises(r.Failure):
            self.reconcile()
        self.stalwart.account_id.assert_not_called()

    def test_kubernetes_conflict_is_reported(self):
        kube = r.Kubernetes.__new__(r.Kubernetes)
        kube.request = Mock(return_value=(409, {}))
        with self.assertRaisesRegex(r.Failure, 'changed concurrently'):
            kube.replace_secret(self.secret)

    def test_unchanged_settings_skip_restart(self):
        self.stalwart.settings_marker.return_value = ('role', 'done', 'hash@1')
        r.restart(self.config, self.kube, self.stalwart)
        self.kube.patch_statefulset.assert_not_called()
        self.stalwart.set_settings_marker.assert_not_called()

    def test_pending_restart_resumes_same_stamp(self):
        for applied in ('hash@1', 'old@0'):
            with self.subTest(applied=applied):
                self.kube.reset_mock()
                self.ss['spec']['template']['metadata']['annotations'][r.SETTINGS] = applied
                self.stalwart.settings_marker.return_value = ('role', 'pending', 'hash@1')
                r.restart(self.config, self.kube, self.stalwart)
                self.assertEqual(self.kube.patch_statefulset.call_count, int(applied != 'hash@1'))
                self.stalwart.set_settings_marker.assert_called_with('mail', 'role', 'done', 'hash@1')

    def test_database_reset_restarts_unchanged_settings(self):
        self.stalwart.settings_marker.return_value = (None, None, None)
        with patch.object(r.time, 'time', return_value=100):
            r.restart(self.config, self.kube, self.stalwart)
        self.kube.patch_statefulset.assert_called_once()
        self.assertEqual([c.args[2:] for c in self.stalwart.set_settings_marker.call_args_list], [('pending', 'hash@100'), ('done', 'hash@100')])

    def test_rollout_timeout_does_not_mark_done(self):
        self.ss['status']['readyReplicas'] = 0
        self.stalwart.settings_marker.return_value = ('role', 'pending', 'hash@1')
        with patch.object(r.time, 'time', side_effect=[0, 31]), self.assertRaises(r.Failure):
            r.restart(self.config, self.kube, self.stalwart)
        self.stalwart.set_settings_marker.assert_not_called()

    def test_rollout_requires_observed_generation_and_matching_revisions(self):
        self.assertTrue(r.rolled_out(self.ss))
        for key, value in [('observedGeneration', 1), ('updatedReplicas', 0), ('readyReplicas', 0), ('currentRevision', 'old')]:
            ss = copy.deepcopy(self.ss)
            ss['status'][key] = value
            self.assertFalse(r.rolled_out(ss), key)

    def test_forced_restart_rolls_unchanged_settings(self):
        self.stalwart.settings_marker.return_value = ('role', 'done', 'hash@1')
        with patch.object(r.time, 'time', return_value=100):
            r.restart(self.config, self.kube, self.stalwart, force=True)
        self.kube.patch_statefulset.assert_called_once()
        self.assertEqual([c.args[2:] for c in self.stalwart.set_settings_marker.call_args_list], [('pending', 'hash@100'), ('done', 'hash@100')])

    def blob_stalwart(self, email, files, keys=None):
        settings = {'Email': email, 'FileStorage': files}
        calls = []
        def call(method, args):
            calls.append((method, args))
            obj, verb = method[2:].split('/')
            if obj == 'SymmetricKey':
                return {'list': [{'id': 'x', 'description': 'other'}, {'id': 'k', 'description': 'Helm chart blob key'}] if keys is None else keys}
            if verb == 'get':
                return {'list': [{'id': 'singleton', 'blobEncryptionKey': settings[obj]}]}
            settings[obj] = args['update']['singleton']['blobEncryptionKey']
            return {'updated': {'singleton': None}}
        stalwart = Mock()
        stalwart.call.side_effect = call
        return stalwart, settings, calls

    def test_blob_key_points_settings_at_the_chart_key(self):
        self.config['blobKey'] = {'description': 'Helm chart blob key', 'email': True, 'fileStorage': False}
        stalwart, settings, calls = self.blob_stalwart(None, None)
        self.assertTrue(r.blob_key(self.config, stalwart))
        self.assertEqual(settings, {'Email': 'k', 'FileStorage': None})
        # Unchanged on the next run: no update, no forced restart.
        self.assertFalse(r.blob_key(self.config, stalwart))
        self.assertEqual(sum(1 for m, _ in calls if m.endswith('/set')), 1)

    def test_blob_key_disabled_clears_only_the_chart_key(self):
        self.config['blobKey'] = {'description': 'Helm chart blob key', 'email': False, 'fileStorage': False}
        stalwart, settings, _ = self.blob_stalwart('k', 'x')
        self.assertTrue(r.blob_key(self.config, stalwart))
        self.assertEqual(settings, {'Email': None, 'FileStorage': 'x'})

    def test_blob_key_missing_or_unconfigured(self):
        stalwart, _, calls = self.blob_stalwart(None, None, keys=[])
        self.assertFalse(r.blob_key(self.config, stalwart))
        self.assertEqual(calls, [])
        self.config['blobKey'] = {'description': 'Helm chart blob key', 'email': True, 'fileStorage': True}
        with self.assertRaises(r.Failure):
            r.blob_key(self.config, stalwart)

    def test_ipv6_api_url(self):
        with patch.dict(os.environ, {'KUBERNETES_SERVICE_HOST': 'fd00::1', 'KUBERNETES_SERVICE_PORT': '443', 'KUBERNETES_API': ''}), patch.object(r.ssl, 'create_default_context'):
            self.assertEqual(r.Kubernetes().base, 'https://[fd00::1]:443')


def b64(value):
    return base64.b64encode(value.encode()).decode()


class AdminBootstrapChecks(unittest.TestCase):
    """The administrator account's bootstrap: a temporary recovery administrator only while the
    recorded app password does not work."""

    def setUp(self):
        self.config = {'release': 'mail', 'lockTimeoutSeconds': 300, 'stalwartUrl': 'http://mail-admin:8081', 'smtpCheck': {'host': 'x'},
                       'restart': {'namespace': 'mail', 'statefulset': 'mail', 'settingsHash': 'hash', 'timeoutSeconds': 30},
                       'adminAccount': {'name': 'chart-admin', 'domain': 'stalwart.internal', 'address': 'chart-admin@stalwart.internal',
                                        'allowedIps': ['127.0.0.1', '::1'], 'secret': {'namespace': 'mail', 'name': 'mail-admin-account'},
                                        'clients': []}}
        self.secret = {'metadata': {'namespace': 'mail', 'name': 'mail-admin-account', 'annotations': {}}, 'data': {}}
        self.kube = Mock()
        self.kube.get_secret.side_effect = lambda *a: copy.deepcopy(self.secret)
        def replace(secret):
            self.secret = copy.deepcopy(secret)
            return copy.deepcopy(secret)
        self.kube.replace_secret.side_effect = replace
        self.rolls = []
        # The StatefulSet's bootstrap stamp and whether its rollout finished.
        self.stamp, self.rolled = '', True
        def statefulset(*_):
            return {'metadata': {'generation': 1}, 'spec': {'replicas': 1, 'template': {'metadata': {'annotations': {r.BOOTSTRAP: self.stamp}}}},
                    'status': ({'observedGeneration': 1, 'updatedReplicas': 1, 'readyReplicas': 1, 'currentRevision': 'a', 'updateRevision': 'a'}
                               if self.rolled else {'observedGeneration': 1, 'updatedReplicas': 0, 'readyReplicas': 1, 'currentRevision': 'a', 'updateRevision': 'b'})}
        self.kube.get_statefulset.side_effect = statefulset
        # Stalwart accepts the recovery administrator only while the pods run with it, and the
        # account's app password once the Secret records the created one.
        self.running_recovery = None
        self.created = None
        def roll(config, kube, stamp):
            self.stamp = stamp
            self.rolls.append(stamp.split('@')[0])
            data = self.secret.get('data') or {}
            self.running_recovery = base64.b64decode(data[r.RECOVERY_KEY]).decode() if data.get(r.RECOVERY_KEY) else None
        def accepted(url, user, password, **_):
            if user == 'chart-admin@stalwart.internal':
                return password == self.created
            return self.running_recovery == f'{user}:{password}'
        def reconcile_account(config, account, kube, stalwart):
            self.reconciled = (config, account)
            self.created = 'app-password'
            secret = kube.get_secret('mail', 'mail-admin-account')
            secret['data'] = {**secret['data'], 'password': b64('app-password')}
            kube.replace_secret(secret)
        self.waited = []
        self.patches = [patch.object(r, 'roll', side_effect=roll), patch.object(r, 'login_accepted', side_effect=accepted),
                        patch.object(r, 'wait_rollout', side_effect=lambda *a: self.waited.append(True)),
                        patch.object(r, 'reconcile_account', side_effect=reconcile_account), patch.object(r, 'Stalwart')]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def test_working_password_needs_nothing(self):
        self.secret['data'] = {'password': b64('app-password')}
        self.created = 'app-password'
        r.bootstrap(self.config, self.kube)
        self.assertEqual(self.rolls, [])
        r.Stalwart.assert_not_called()
        self.kube.replace_secret.assert_not_called()

    def test_first_run_creates_account_and_removes_recovery_admin(self):
        r.bootstrap(self.config, self.kube)
        self.assertEqual(self.rolls, ['recovery', 'done'])
        self.assertEqual(self.secret['data'], {'password': b64('app-password')})
        user, password = r.Stalwart.call_args.args[1:]
        self.assertTrue(user.startswith('bootstrap-'))
        self.assertGreaterEqual(len(password), 32)
        r.Stalwart.return_value.ensure_admin_account.assert_called_once_with('mail', 'chart-admin', 'stalwart.internal')
        config, account = self.reconciled
        self.assertIsNone(config['smtpCheck'])
        self.assertEqual(account['appPassword'], {'allowedIps': ['127.0.0.1', '::1'], 'permissions': {'@type': 'Inherit'}})
        self.assertEqual(account['secret'], {'namespace': 'mail', 'name': 'mail-admin-account', 'keys': ['password']})
        self.assertIsNone(self.running_recovery)

    def test_retry_reuses_the_running_recovery_admin(self):
        self.secret['data'] = {r.RECOVERY_KEY: b64('bootstrap-1:secret')}
        self.running_recovery = 'bootstrap-1:secret'
        r.bootstrap(self.config, self.kube)
        self.assertEqual(self.rolls, ['done'])
        self.assertEqual(r.Stalwart.call_args.args[1:], ('bootstrap-1', 'secret'))

    def test_leftover_recovery_admin_is_removed(self):
        self.secret['data'] = {'password': b64('app-password'), r.RECOVERY_KEY: b64('bootstrap-1:secret')}
        self.created = 'app-password'
        self.running_recovery = 'bootstrap-1:secret'
        r.bootstrap(self.config, self.kube)
        self.assertEqual(self.rolls, ['done'])
        self.assertNotIn(r.RECOVERY_KEY, self.secret['data'])
        r.Stalwart.assert_not_called()

    def test_interrupted_cleanup_restarts_without_recovery_admin(self):
        # The key is gone from the Secret, but the run died before the restart: the pods still run
        # with the recovery administrator (stamp "recovery@...").
        self.secret['data'] = {'password': b64('app-password')}
        self.created = 'app-password'
        self.stamp, self.running_recovery = 'recovery@1', 'bootstrap-1:secret'
        r.bootstrap(self.config, self.kube)
        self.assertEqual(self.rolls, ['done'])
        self.assertIsNone(self.running_recovery)
        r.Stalwart.assert_not_called()

    def test_unfinished_rollout_without_recovery_admin_is_awaited(self):
        self.secret['data'] = {'password': b64('app-password')}
        self.created = 'app-password'
        self.stamp, self.rolled = 'done@2', False
        r.bootstrap(self.config, self.kube)
        self.assertEqual((self.rolls, self.waited), ([], [True]))

    def test_finished_bootstrap_needs_nothing(self):
        self.secret['data'] = {'password': b64('app-password')}
        self.created = 'app-password'
        self.stamp = 'done@2'
        r.bootstrap(self.config, self.kube)
        self.assertEqual((self.rolls, self.waited), ([], []))

    def test_refused_password_is_replaced(self):
        self.secret['data'] = {'password': b64('revoked')}
        seen = []
        original = r.reconcile_account.side_effect
        def reconcile_account(config, account, kube, stalwart):
            seen.append(dict(kube.get_secret('mail', 'mail-admin-account')['data']))
            original(config, account, kube, stalwart)
        r.reconcile_account.side_effect = reconcile_account
        r.bootstrap(self.config, self.kube)
        # reconcile_account sees no password, so it revokes the recorded credential and creates one.
        self.assertNotIn('password', seen[0])
        self.assertEqual(self.secret['data'], {'password': b64('app-password')})

    def test_refused_new_password_keeps_recovery_admin_for_the_retry(self):
        r.login_accepted.side_effect = lambda url, user, password, **_: user != 'chart-admin@stalwart.internal' and self.running_recovery == f'{user}:{password}'
        with self.assertRaises(r.Failure):
            r.bootstrap(self.config, self.kube)
        self.assertIn(r.RECOVERY_KEY, self.secret['data'])
        self.assertEqual(self.rolls, ['recovery'])

    def test_recovery_admin_refused_after_restart_fails(self):
        r.roll.side_effect = lambda config, kube, stamp: self.rolls.append(stamp)
        with self.assertRaises(r.Failure):
            r.bootstrap(self.config, self.kube)
        r.Stalwart.assert_not_called()

    def test_recovery_admin_still_accepted_fails(self):
        r.roll.side_effect = lambda config, kube, stamp: (self.rolls.append(stamp), setattr(self, 'running_recovery', self.running_recovery or base64.b64decode(self.secret['data'][r.RECOVERY_KEY]).decode()))
        with self.assertRaises(r.Failure):
            r.bootstrap(self.config, self.kube)

    def test_regular_run_publishes_client_passwords_without_smtp_check(self):
        client = {'id': 'admin-connector', 'name': 'chart-admin', 'address': 'chart-admin@stalwart.internal',
                  'appPassword': {'allowedIps': ['127.0.0.1', '::1'], 'permissions': {'@type': 'Inherit'}},
                  'secret': {'create': True, 'name': 'connector', 'namespace': 'mail', 'keys': ['password']}}
        self.config['adminAccount']['clients'] = [client]
        self.config['accounts'] = []
        calls = []
        r.reconcile_account.side_effect = lambda config, account, kube, stalwart: calls.append((config['smtpCheck'], account['id']))
        with patch.object(r, 'Kubernetes', return_value=self.kube), patch.object(r, 'blob_key', return_value=False), \
                patch.object(r, 'restart'), patch.object(r.sys, 'argv', ['reconcile.py']), \
                patch.dict(os.environ, {'STALWART_USER': 'chart-admin@stalwart.internal', 'STALWART_PASSWORD': 'x', 'CONFIG_FILE': '/dev/null'}), \
                patch.object(r.json, 'load', return_value=self.config), patch('builtins.open'):
            with self.assertRaises(SystemExit) as exit:
                r.main()
        self.assertEqual(exit.exception.code, 0)
        self.assertEqual(calls, [(None, 'admin-connector')])


if __name__ == '__main__':
    unittest.main()
