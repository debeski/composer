import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from composer import ops, executor_ops, executor_protocol as protocol
from composer.launcher import DockerComposeLauncher


def _reader(frame):
    position={'at':0}
    def recv_exactly(n):
        chunk=frame[position['at']:position['at']+n]
        position['at']+=n
        return chunk
    return recv_exactly


class DeploymentCheckTests(unittest.TestCase):
    def request(self, payload):
        return protocol.validate_executor_request({'protocol_version':1, 'operation_id':str(uuid.uuid4()),
                                                  'op':'deployment_check', 'payload':payload})

    def test_read_only_request_rejects_commands_paths_and_non_boolean_flags(self):
        self.assertEqual(self.request({'dev':True})['payload'], {'dev':True, 'file':''})
        for payload in ({'dev':'true'}, {'command':'sh'}, {'file':'../compose.yml'}, {'file':'/tmp/compose.yml'},
                        {'file':'https://example.invalid/a.yml'}, {'force':True}):
            with self.subTest(payload=payload), self.assertRaises(protocol.ProtocolError):
                self.request(payload)

    def test_agent_returns_executor_diagnosis_without_reading_local_secrets(self):
        report={'exit_code':1,'findings':[{'level':'fail','name':'secrets','message':'executor cannot read'}]}
        runtime=SimpleNamespace(args=SimpleNamespace(dev=True,file=None))
        with patch('composer.executor_client.executor_configured',return_value=True), \
             patch('composer.executor_client.send_request',return_value={'state':'succeeded','report':report}) as send, \
             patch('composer.ops._run_check_local',side_effect=AssertionError('agent must not inspect secrets')):
            self.assertEqual(ops._run_check(runtime),report)
        send.assert_called_once_with('deployment_check',{'dev':True,'file':''})

    def test_version_skew_cannot_turn_into_a_clean_result(self):
        with patch('composer.executor_client.executor_configured',return_value=True), \
             patch('composer.executor_client.send_request',return_value={'state':'rejected','detail':'Unsupported op'}):
            with self.assertRaisesRegex(ValueError,'Update both'):
                ops._run_check(SimpleNamespace(args=SimpleNamespace(dev=False)))

    def test_executor_returns_failed_findings_as_a_successfully_completed_read_only_check(self):
        report={'exit_code':1,'findings':[{'level':'fail','name':'secrets'}]}
        with patch('composer.ops._run_check_local',return_value=report) as check:
            result=executor_ops.default_operation_handler(self.request({'dev':False}))
        self.assertEqual(result['state'],'succeeded')
        self.assertEqual(result['report'],report)
        self.assertFalse(check.call_args.args[0].args.dev)
        self.assertLess(len(protocol.encode_frame(result)),protocol.MAX_EXECUTOR_MESSAGE_BYTES)

    def test_a_report_too_big_for_one_frame_has_its_longest_diffs_shortened_to_fit(self):
        """Each repair diff may be 20,000 characters: a stack needing several repairs overflows."""
        def diff(tag, lines):
            return ''.join(f'+  {tag}: "line {n}"\n' for n in range(lines))
        report={'exit_code':1,'findings':[{'level':'warn','name':'topology','message':'repairs pending'}],
                'repairs':[{'name':'agent-enable','files':['compose.yml'],'diff':diff('a',ops.MAX_DIFF_CHARS//20),'note':''},
                           {'name':'executor-enable','files':['compose.yml'],'diff':diff('b',ops.MAX_DIFF_CHARS//20),'note':'kept a custom block'},
                           {'name':'dlux-runtime','files':['compose.yml'],'diff':diff('c',ops.MAX_DIFF_CHARS//20),'note':''},
                           {'name':'maintenance-page','files':['Caddyfile'],'diff':diff('d',ops.MAX_DIFF_CHARS//20),'note':''},
                           {'name':'restart-labels','files':['compose.yml'],'diff':'+  org.dlux.restart: "safe"\n','note':''}],
                'compose_digest':'f'*64}
        before={r['name']:r['diff'] for r in report['repairs']}
        self.assertGreater(protocol.encoded_size({'report':report}),protocol.MAX_EXECUTOR_MESSAGE_BYTES)

        with patch('composer.ops._run_check_local',return_value=report):
            result=executor_ops.default_operation_handler(self.request({'dev':False}))

        self.assertEqual(result['state'],'succeeded')
        self.assertLessEqual(protocol.encoded_size(result),protocol.MAX_EXECUTOR_MESSAGE_BYTES)
        sent=protocol.read_frame(_reader(protocol.encode_result_frame(result)))
        repairs={r['name']:r for r in sent['report']['repairs']}
        shortened=[name for name,r in repairs.items() if r['diff']!=before[name]]
        self.assertTrue(shortened)
        for name,repair in repairs.items():
            with self.subTest(repair=name):
                self.assertTrue(before[name].startswith(repair['diff']))
                self.assertTrue(repair['diff']=='' or repair['diff'].endswith('\n'))
                self.assertEqual(executor_ops.SHORTENED_DIFF_NOTE in repair['note'],name in shortened)
        self.assertTrue(repairs['executor-enable']['note'].startswith('kept a custom block'))
        self.assertEqual(repairs['restart-labels']['diff'],before['restart-labels'])
        self.assertEqual(sent['report']['compose_digest'],'f'*64)
        self.assertEqual(sent['report']['findings'],report['findings'])

    def test_a_failed_executor_check_is_reported_without_update_advice(self):
        detail='Executor result is 70000 bytes, over the 64 KiB limit, so it was not sent.'
        with patch('composer.executor_client.executor_configured',return_value=True), \
             patch('composer.executor_client.send_request',return_value={'state':'failed','detail':detail}):
            with self.assertRaises(ValueError) as raised:
                ops._run_check(SimpleNamespace(args=SimpleNamespace(dev=False)))
        self.assertIn(detail,str(raised.exception))
        self.assertNotIn('Update both',str(raised.exception))

    def test_inaccessible_secret_parent_is_not_reported_as_a_missing_file(self):
        def stat(path,*args,**kwargs):
            if str(path)=='.secrets/.env':raise PermissionError('parent not searchable')
            raise FileNotFoundError()
        with patch.object(Path,'stat',stat):
            self.assertEqual(DockerComposeLauncher().plaintext_env_candidates(),[Path('.secrets/.env')])
