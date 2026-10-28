"""Real CLI engine callbacks + loopback delivery; no model requests or external ntfy."""
import argparse
import fcntl
import http.server
import json
import os
import pty
import re
from pathlib import Path
import signal
import shutil
import socketserver
import select
import struct
import subprocess
import tempfile
import threading
import termios
import time
import uuid


ROOT = Path(__file__).resolve().parents[1]
CASES = ('final', 'background-shell', 'background-agent', 'stop-veto', 'named-teammate', 'named-teammate-strict')


class Receiver(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0))).decode('utf-8')
        self.server.received.append(self.headers.get('Title'))
        self.server.bodies.append(body)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{}')
        self.server.delivered.set()

    def log_message(self, *args):
        pass


class HerdrReceiver(socketserver.StreamRequestHandler):
    def handle(self):
        request = json.loads(self.rfile.readline(262144))
        self.server.reports.append({**request['params'], 'observedAt': int(time.time() * 1000)})
        self.wfile.write((json.dumps({'id': request['id'], 'result': {'type': 'ok'}}) + '\n').encode())


def run_interactive(command, env, directory):
    """Named teammates require an actual terminal, not Claude's -p mode."""
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 140, 0, 0))
    process = subprocess.Popen(command, env={**env, 'TERM': 'xterm-256color'}, cwd=directory,
                               stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
    os.close(slave)
    output = bytearray()
    trusted = False
    finished = False
    deadline = time.monotonic() + 60
    try:
        while process.poll() is None and time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                output.extend(chunk)
                text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', ' ', output.decode(errors='replace'))
                if re.search(r'Managed\s+settings\s+require\s+approval', text):
                    raise RuntimeError('Claude requires human approval of organization-managed settings; '
                                       'approve them in an attended Claude session before this interactive fixture.')
                # Accept only the generated fixture directory's normal trust
                # prompt. Tool permission or managed-policy prompts are not bypassed.
                if not trusted and re.search(r'Yes,\s+I\s+trust\s+this\s+folder', text):
                    os.write(master, b'\r')
                    trusted = True
            try:
                trace = json.loads(Path(env['LIFECYCLE_PROBE_TRACE']).read_text())
                stops = [json.loads(line) for line in Path(env['LIFECYCLE_PROBE_STOPS']).read_text().splitlines()]
            except (OSError, ValueError):
                continue
            complete = [item['result'] for item in trace if item.get('action') == 'complete']
            if (not finished and complete and 'state' in complete[-1]
                    and any(item.get('action') == 'child_complete' for item in trace)
                    and any(stop.get('last_assistant_message') == 'Lifecycle fixture complete.' for stop in stops)):
                os.write(master, b'\x04')
                finished = True
        if process.poll() is None:
            raise TimeoutError('interactive fixture did not finish: ' + output.decode(errors='replace')[-2000:])
        return subprocess.CompletedProcess(command, process.returncode, output.decode(errors='replace'), '')
    finally:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        os.close(master)


def run_cli(command, env, directory):
    if env.get('LIFECYCLE_PROBE_MODE') == 'named-teammate':
        return run_interactive(command, env, directory)
    process = subprocess.Popen(command, env=env, cwd=directory, text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=45)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    finally:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', choices=CASES, action='append')
    args = parser.parse_args()
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Receiver)
    server.received = []
    server.bodies = []
    server.delivered = threading.Event()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    results = []
    try:
        with tempfile.TemporaryDirectory(prefix='live-claude-') as directory:
            root = Path(directory)
            herdr = socketserver.ThreadingUnixStreamServer(str(root / 'h.sock'), HerdrReceiver)
            herdr.daemon_threads = True
            herdr.reports = []
            threading.Thread(target=herdr.serve_forever, daemon=True).start()
            fixture = root / 'fixture'
            for relative in ('.claude-plugin/plugin.json', 'hooks/hooks.json', 'hooks/register.js', 'stop-control.py'):
                target = fixture / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT / 'tests/live-fixture' / relative).read_bytes())
            with (fixture / 'stop-control.py').open('a') as control:
                control.write('\nimport time\nwith open(os.environ["LIFECYCLE_PROBE_STOPS"], "a") as output:\n'
                               '    output.write(json.dumps({**{key: event.get(key) for key in '
                               '("background_tasks", "session_crons", "last_assistant_message")}, '
                               '"observedAt": int(time.time() * 1000)}) + "\\n")\n')
            for mode in args.case or CASES:
                before = len(server.received)
                server.delivered.clear()
                env = dict(os.environ)
                for key in ('HERDR_ENV', 'HERDR_PANE_ID', 'HERDR_SOCKET_PATH',
                            'CLAUDE_TASK_LIFECYCLE_TOPIC_FILE', 'CLAUDE_TASK_LIFECYCLE_ENDPOINT'):
                    env.pop(key, None)
                named = mode.startswith('named-teammate')
                strict = mode == 'named-teammate-strict'
                configuration = root / (mode + '-config.json')
                configuration.write_text(json.dumps({
                    'notifications': {'enabled': True, 'topic': 'local-test-only',
                                      'endpoint': 'http://127.0.0.1:' + str(server.server_port)},
                    'claude': {'allowIdleTeammates': mode == 'named-teammate'},
                }))
                session_id = str(uuid.uuid4())
                team = 'session-' + session_id[:8]
                claude_home = Path(env.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude')
                owned_paths = [claude_home / kind / team for kind in ('teams', 'tasks')] if named else []
                assert not any(path.exists() for path in owned_paths), 'fixture team already exists'
                env.update(CLAUDE_TASK_LIFECYCLE_STATE_DIR=str(root / mode),
                           HERDR_LIFECYCLE_CONFIG=str(configuration), LIFECYCLE_PROBE_MODE='named-teammate' if named else mode,
                           HERDR_ENV='1', HERDR_SOCKET_PATH=str(root / 'h.sock'), HERDR_PANE_ID='fixture-' + mode,
                           CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS='1' if named else '0',
                           LIFECYCLE_PROBE_TRACE=str(root / (mode + '.json')),
                           LIFECYCLE_PROBE_STOPS=str(root / (mode + '-stops.jsonl')),
                           PYTHONDONTWRITEBYTECODE='1')
                try:
                    result = run_cli([
                        'claude', *([] if named else ['-p']), 'Run the local lifecycle fixture.', '--plugin-dir', str(ROOT),
                        '--plugin-dir', str(fixture), '--strict-mcp-config',
                        '--mcp-config', '{"mcpServers":{}}', '--setting-sources', '',
                        '--settings', '{"teammateMode":"in-process"}',
                        '--allowedTools', 'Bash', 'Agent', '--session-id', session_id,
                        *([] if named else ['--max-turns', '5']),
                    ], env, directory)
                finally:
                    # These unique fixture paths did not exist before this run;
                    # all in-process teammates are stopped by run_cli first.
                    for path in owned_paths:
                        if path.is_dir():
                            shutil.rmtree(path)
                server.delivered.wait(3)
                trace_path = root / (mode + '.json')
                trace = json.loads(trace_path.read_text()) if trace_path.exists() else []
                completions = [item['result'] for item in trace if item.get('action') == 'complete']
                assert all('state' in value or value == {'ignored': True, 'reason': 'old_turn'}
                           for value in completions), 'unexpected runtime completion response: ' + repr(completions)
                states = [value['state'] for value in completions if 'state' in value]
                assert states, 'no accepted parent completion was observed'
                stops_path = root / (mode + '-stops.jsonl')
                stops = [json.loads(line) for line in stops_path.read_text().splitlines()] if stops_path.exists() else []
                teammates = [task for stop in stops if stop.get('last_assistant_message') == 'Lifecycle fixture complete.'
                              for task in stop.get('background_tasks', []) if task.get('type') == 'teammate']
                reports = [report for report in herdr.reports if report['pane_id'] == 'fixture-' + mode]
                reported_states = [report['state'] for index, report in enumerate(reports)
                                   if index == 0 or report['state'] != reports[index - 1]['state']]
                summary = {'case': mode, 'exitCode': result.returncode, 'parentFinalStates': states,
                           'herdrStates': reported_states,
                           'notifications': len(server.received) - before,
                           'finalReplyBodyMatches': server.bodies[before:] == ['Lifecycle fixture complete.'],
                           'childCompletions': sum(item.get('action') == 'child_complete' for item in trace),
                           'finalStopTeammates': [{key: task.get(key) for key in ('id', 'type', 'status')} for task in teammates]}
                print(json.dumps(summary), flush=True)
                if named and not summary['childCompletions']:
                    print(json.dumps({'fixtureAgentResults': [item['result'] for item in trace
                                                              if item.get('action') == 'fixture_agent_result']}), flush=True)
                assert result.returncode == 0, mode + ': CLI failed: ' + (result.stderr or result.stdout).strip()[-2000:]
                if named:
                    assert summary['childCompletions'] > 0, 'named teammate did not finish its actual work'
                    assert any(task.get('status') == 'running' for task in teammates), 'native Stop did not retain a running teammate'
                if strict:
                    assert server.received[before:] == [], 'strict mode must not infer completion from idle teammates'
                    assert completions[-1].get('reason') == 'teammate_completion_unproven', mode
                    assert states[-1] == 'working', mode
                    assert all(report['state'] != 'idle' for report in reports), mode + ': premature Herdr Idle'
                else:
                    assert server.received[before:] == ['Claude Code - Task Complete'], mode + ': expected exactly one completion notification'
                    assert server.bodies[before:] == ['Lifecycle fixture complete.'], mode
                    assert states[-1] == 'idle', mode
                    final_stops = [stop for stop in stops if stop.get('last_assistant_message') == 'Lifecycle fixture complete.']
                    idle_reports = [report for report in reports if report['state'] == 'idle']
                    assert final_stops and idle_reports, mode + ': missing final Stop or Herdr completion'
                    assert min(report['observedAt'] for report in idle_reports) >= max(stop['observedAt'] for stop in final_stops), \
                        mode + ': Herdr reported Idle before the final reconciled Stop'
                if mode.startswith('background-'):
                    # A superseded native turn.complete can correctly return
                    # old_turn. Check the external Working → Idle contract and
                    # final-Stop timing instead of assuming callback ordering.
                    assert reported_states[0] == 'working', mode
                results.append(summary)
    finally:
        server.shutdown()
        server.server_close()
        if 'herdr' in locals():
            herdr.shutdown()
            herdr.server_close()
    print('PASS: %d real CLI lifecycle cases with the expected completion policy' % len(results))


if __name__ == '__main__':
    main()
