from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'lib'))
from desktop.main import Bridge
from desktop.server import ReaderServer
from desktop.digest_job import DigestJob, run_process, parse_result, child_environment, resolve_cli
from meeting_library import Library
from meeting_digests import DigestError, record
from tests.test_assemblyai_adapter import write_published_run


def finished(job, identifier):
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        status = job.status(identifier)
        if status['state'] != 'running':
            return status
        time.sleep(.02)
    raise AssertionError('job did not finish')


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bundle = self.root / '20261101-100000-synthetic'
        self.t1 = write_published_run(self.bundle / 'transcripts' / 'T1', deletion={'confirmed': True})
        self.t2 = write_published_run(self.bundle / 'transcripts' / 'T2', deletion={'confirmed': True})
        value = json.loads(self.t2.read_text())
        value['text'] = 'Selected T2 evidence only.'
        value['segments'][0]['text'] = value['text']
        self.t2.write_text(json.dumps(value))
        self.library = Library(self.root)
        self.row = self.library.scan()['meetings'][0]
        self.runs = {run['label']: run['id'] for run in self.row['runs']}
        self.source = self.library.digest_source(self.row['id'], self.runs['T2'])
        self.skill = (ROOT / 'skills/meeting/SKILL.md').read_bytes()

    def fixture_cli(self, body=None, provider='claude'):
        executable = self.root / provider
        executable.write_text('#!' + sys.executable + '\n' + '''
import sys,json,os
args=sys.argv[1:]
if args==['--version']: print('CLI synthetic');sys.exit()
if args==['login','status']: print('Logged in using ChatGPT');sys.exit()
if args==['exec','--help']: print('--ignore-user-config --ignore-rules --ephemeral --json');sys.exit()
if args==['auth','status']: print(json.dumps({'loggedIn':True,'authMethod':'claude.ai','apiProvider':'firstParty','subscriptionType':'max'}));sys.exit()
if args==['--help']: print('--safe-mode --restricted --tools --strict-mcp-config --no-session-persistence --permission-prompts');sys.exit()
text=sys.stdin.read()
''' + (body or '''
assert 'Selected T2 evidence only.' in text
assert not any(k in os.environ for k in ['OPENAI_API_KEY','CODEX_API_KEY','ASSEMBLYAI_API_KEY','CODEX_APP_TOOLS_PIPE_PATH'])
assert '--safe-mode' in args and '--restricted' in args
assert args[args.index('--tools')+1]=='' and args[args.index('--mcp-config')+1]=='{"mcpServers":{}}'
print(json.dumps({'type':'system','subtype':'init','tools':[]}))
print(json.dumps({'type':'result','subtype':'success','result':'# Summary\\nSelected T2 evidence.'}))
'''))
        executable.chmod(0o700)
        return executable

    @unittest.skipIf(os.name == 'nt', 'POSIX executable fixture; Windows native CLI proof is separate')
    def test_real_bridge_generation_pinned_to_t2_and_history_survives_failure(self):
        self.fixture_cli()
        server = ReaderServer(ROOT / 'desktop/assets')
        self.addCleanup(server.close)
        server.start()
        bridge = Bridge(server, lambda: None)
        self.addCleanup(bridge._close)
        bridge._library = self.library
        hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.bundle.rglob('*') if p.is_file()}
        with patch.dict(os.environ, {'PATH':str(self.root)+os.pathsep+os.environ.get('PATH',''),
                                     'OPENAI_API_KEY':'synthetic-sentinel','CODEX_APP_TOOLS_PIPE_PATH':'synthetic-sentinel'}):
            started = bridge.start_summary(self.row['id'], self.runs['T2'], 'claude')
            self.assertEqual(bridge.load_run(self.row['id'], self.runs['T1'])['canonical']['text'], 'First.')
            # Even replacing the active Library cannot retarget this job.
            bridge._library = Library()
            result = finished(bridge._digests, started['id'])
        self.assertEqual(result['state'], 'succeeded', result)
        self.assertEqual(self.library.summary_history(self.row['id'], self.runs['T1'])['summaries'], [])
        history = self.library.summary_history(self.row['id'], self.runs['T2'])
        self.assertEqual(history['summary']['markdown'], '# Summary\nSelected T2 evidence.')
        self.assertFalse(history['summary']['earlier_revision'])
        self.assertFalse(history['summary']['provider_warning'])
        for path, digest in hashes.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
        saved = next(self.bundle.glob('summaries/*/summary.json'))
        if os.name != 'nt':
            self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
            self.assertEqual(saved.parent.stat().st_mode & 0o777, 0o700)
        prior = saved.read_bytes()
        self.fixture_cli("print('not a CLI envelope')\n")
        bridge._library = self.library
        with patch.dict(os.environ, {'PATH':str(self.root)+os.pathsep+os.environ.get('PATH','')}):
            started = bridge.start_summary(self.row['id'], self.runs['T2'], 'claude')
            self.assertEqual(finished(bridge._digests, started['id'])['state'], 'failed')
        self.assertEqual(saved.read_bytes(), prior)
        self.assertEqual(len(list(self.bundle.glob('summaries/*/summary.json'))), 1)

    @unittest.skipIf(os.name == 'nt', 'POSIX executable fixture; Windows native proof is separate')
    def test_codex_cannot_submit_to_unverified_mcp_configuration(self):
        # A synthetic remaining configuration layer, independent of user config.
        # If exec is reached, the fixture starts that server and records only
        # receipt, never transcript content. No model or external service runs.
        server_marker = self.root / 'server-started'
        input_marker = self.root / 'input-received'
        exec_marker = self.root / 'exec-started'
        server = self.root / 'remaining-server.py'
        server.write_text('from pathlib import Path\nPath('+repr(str(server_marker))+').touch()\n')
        config = self.root / 'remaining-defaults.json'
        config.write_text(json.dumps({'mcp_servers':{'synthetic':{'command':sys.executable,'args':[str(server)]}}}))
        self.fixture_cli('''
import subprocess
from pathlib import Path
Path('''+repr(str(exec_marker))+''').touch()
servers=json.loads(Path('''+repr(str(config))+''').read_text())['mcp_servers']
for server in servers.values(): subprocess.run([server['command'],*server['args']],check=True)
if text: Path('''+repr(str(input_marker))+''').touch()
''', provider='codex')
        previous = record(self.source,self.skill,'codex','synthetic','Keep prior Codex success',None)
        self.library.publish_digest(self.source,previous)
        saved = next(self.bundle.glob('summaries/*/summary.json'))
        prior = saved.read_bytes()
        job = DigestJob(ROOT/'skills/meeting/SKILL.md')
        self.addCleanup(job.close)
        with patch.dict(os.environ,{'PATH':str(self.root)+os.pathsep+os.environ.get('PATH','')}):
            started = job.start(self.source,self.library.publish_digest,'codex',self.row['id'],self.runs['T2'])
            result = finished(job,started['id'])
        self.assertEqual(result['state'],'failed',result)
        self.assertIn('MCP',result['error'])
        self.assertFalse(exec_marker.exists())
        self.assertFalse(server_marker.exists())
        self.assertFalse(input_marker.exists())
        self.assertEqual(saved.read_bytes(),prior)
        self.assertEqual(len(list(self.bundle.glob('summaries/*/summary.json'))),1)

    def test_reload_copy_version_hash_and_malformed_history(self):
        first = record(self.source, self.skill, 'codex', 'synthetic', 'Earlier success', None, provider_warning=True)
        second = record(self.source, self.skill, 'claude', 'synthetic', 'Newest success', 'reported')
        self.library.publish_digest(self.source, first)
        self.library.publish_digest(self.source, second)
        malformed = self.bundle / 'summaries' / ('20261101T111111000000Z-'+'a'*32)
        malformed.mkdir(mode=0o700)
        (malformed/'summary.json').write_text('{}')
        self.t2.write_text(self.t2.read_text()+'\n')
        result = self.library.summary_history(self.row['id'], self.runs['T2'])
        self.assertEqual([r['provider'] for r in result['summaries']], ['claude','codex'])
        self.assertTrue(result['summary']['earlier_revision'])
        self.assertTrue(result['summary_warning'])
        self.assertEqual(self.library.summary_history(self.row['id'],self.runs['T2'],first['id'])['summary']['markdown'],'Earlier success')
        self.assertTrue(self.library.summary_history(self.row['id'],self.runs['T2'],first['id'])['summary']['provider_warning'])
        import shutil
        copied = self.root / 'copy'
        shutil.copytree(self.bundle, copied / self.bundle.name)
        library = Library(copied)
        row = library.scan()['meetings'][0]
        self.assertEqual(library.summary_history(row['id'],row['default_run_id'])['summary']['id'],second['id'])

    @unittest.skipIf(os.name == 'nt', 'POSIX link/permissions proof; Windows runtime is separately deferred')
    def test_linked_or_replaced_target_never_receives_summary(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (self.bundle/'summaries').symlink_to(outside, target_is_directory=True)
        value = record(self.source,self.skill,'codex','synthetic','Summary',None)
        with self.assertRaises(ValueError):
            self.library.publish_digest(self.source,value)
        self.assertEqual(list(outside.iterdir()),[])
        (self.bundle/'summaries').unlink()
        self.source.run_path.rename(self.source.run_path.with_name('moved'))
        self.source.run_path.mkdir()
        with self.assertRaises(ValueError):
            self.library.publish_digest(self.source,value)
        self.assertFalse((self.bundle/'summaries').exists())

    def test_action_error_and_truncated_cli_results_are_rejected(self):
        claude_success = b'\n{"type":"result","subtype":"success","result":"# Summary"}'
        claude_init = b'{"type":"system","subtype":"init","tools":[]}\n'
        cases = [b'{"type":"system","subtype":"init","tools":["Bash"]}'+claude_success,
                 claude_init+b'{"type":"assistant","message":{"content":[{"type":"tool_use"}]}}'+claude_success,
                 claude_init+b'{"type":"result","subtype":"error","result":"wrong"}',
                 claude_success,
                 b'{"type":"system","subtype":"init"}'+claude_success,
                 b'{"type":"system","subtype":"init","tools":""}'+claude_success]
        for data in cases:
            with self.subTest(data=data), self.assertRaises(DigestError):
                parse_result(data)
        text,model,admission = parse_result(b'{"type":"system","subtype":"init","tools":[],"model":"reported"}\n{"type":"result","subtype":"success","result":"# Summary"}')
        self.assertEqual((text,model,admission['init_tools']),('# Summary','reported',[]))

    @unittest.skipIf(os.name == 'nt', 'Mac per-user installation and executable fixture')
    def test_native_cli_fallback_is_available_without_shell_path_configuration(self):
        executable = self.fixture_cli()
        fallback = self.root / '.local/bin/claude'
        fallback.parent.mkdir(parents=True)
        executable.rename(fallback)
        resolved = resolve_cli('claude', {'PATH':'/usr/bin:/bin', 'HOME':str(self.root)})
        result = subprocess.run([resolved,'--version'],capture_output=True,text=True,check=True)
        self.assertEqual(result.stdout.strip(),'CLI synthetic')
        self.assertEqual(Path(resolved),fallback.resolve())

    @unittest.skipIf(os.name == 'nt', 'POSIX operation; actual Windows Job Object proof remains deferred')
    def test_real_process_timeout_cancel_and_root_exit_kill_descendants(self):
        for mode in ('timeout','cancel','root-exit'):
            with self.subTest(mode=mode):
                marker = self.root / (mode+'.pid')
                child = 'import os,time;from pathlib import Path;Path('+repr(str(marker))+').write_text(str(os.getpid()));time.sleep(60)'
                script = 'import subprocess,sys,time;subprocess.Popen([sys.executable,"-c",'+repr(child)+']);'
                script += 'time.sleep(.3)' if mode=='root-exit' else 'time.sleep(60)'
                cancel = threading.Event()
                if mode=='cancel':
                    timer = threading.Timer(.5,cancel.set)
                    timer.start()
                    self.addCleanup(timer.cancel)
                try:
                    run_process([sys.executable,'-c',script],b'',self.root,child_environment(),cancel,timeout=.6)
                    self.assertEqual(mode,'root-exit')
                except DigestError:
                    self.assertNotEqual(mode,'root-exit')
                self.assertTrue(marker.exists())
                pid = int(marker.read_text())
                result = subprocess.run(['ps','-o','stat=','-p',str(pid)],capture_output=True,text=True)
                self.assertTrue(not result.stdout.strip() or result.stdout.strip().startswith('Z'),result.stdout)

    @unittest.skipIf(os.name == 'nt', 'POSIX executable fixture; Windows process proof is separate')
    def test_overflow_is_bounded_and_cancelled_job_keeps_previous_success(self):
        with self.assertRaises(DigestError):
            run_process([sys.executable,'-c','import sys;sys.stderr.write("x"*1000000)'],b'',self.root,child_environment(),threading.Event(),timeout=5)
        self.fixture_cli('import time\ntime.sleep(60)\n')
        previous = record(self.source,self.skill,'codex','synthetic','Keep this success',None)
        self.library.publish_digest(self.source,previous)
        job = DigestJob(ROOT/'skills/meeting/SKILL.md')
        self.addCleanup(job.close)
        with patch.dict(os.environ,{'PATH':str(self.root)+os.pathsep+os.environ.get('PATH','')}):
            status = job.start(self.source,self.library.publish_digest,'claude',self.row['id'],self.runs['T2'])
            with self.assertRaises(DigestError):
                job.start(self.source,self.library.publish_digest,'codex',self.row['id'],self.runs['T2'])
            job.cancel(status['id'])
            self.assertEqual(finished(job,status['id'])['state'],'cancelled')
        self.assertEqual(self.library.summary_history(self.row['id'],self.runs['T2'])['summary']['id'],previous['id'])


if __name__ == '__main__':
    unittest.main()
