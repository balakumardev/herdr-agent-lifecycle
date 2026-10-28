"""Real CLI engine callbacks + loopback delivery; no model requests or external ntfy."""
import argparse
import http.server
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading


ROOT = Path(__file__).resolve().parents[1]
CASES = ('final', 'background-shell', 'background-agent', 'stop-veto', 'named-teammate')


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


def run_cli(command, env, directory):
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
            topic = root / 'topic.sh'
            topic.write_text('TOPIC="local-test-only"\n')
            fixture = root / 'fixture'
            for relative in ('.claude-plugin/plugin.json', 'hooks/hooks.json', 'hooks/register.js', 'stop-control.py'):
                target = fixture / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT / 'tests/live-fixture' / relative).read_bytes())
            with (fixture / 'stop-control.py').open('a') as control:
                control.write('\nwith open(os.environ["LIFECYCLE_PROBE_STOPS"], "a") as output:\n'
                              '    output.write(json.dumps({key: event.get(key) for key in '
                              '("background_tasks", "session_crons", "last_assistant_message")}) + "\\n")\n')
            for mode in args.case or CASES:
                before = len(server.received)
                server.delivered.clear()
                env = dict(os.environ)
                for key in ('HERDR_ENV', 'HERDR_PANE_ID', 'HERDR_SOCKET_PATH'):
                    env.pop(key, None)
                env.update(CLAUDE_TASK_LIFECYCLE_STATE_DIR=str(root / mode),
                           CLAUDE_TASK_LIFECYCLE_ENDPOINT='http://127.0.0.1:' + str(server.server_port),
                           CLAUDE_TASK_LIFECYCLE_TOPIC_FILE=str(topic), LIFECYCLE_PROBE_MODE=mode,
                           LIFECYCLE_PROBE_TRACE=str(root / (mode + '.json')),
                           LIFECYCLE_PROBE_STOPS=str(root / (mode + '-stops.jsonl')),
                           PYTHONDONTWRITEBYTECODE='1')
                result = run_cli([
                    'claude', '-p', 'Run the local lifecycle fixture.', '--plugin-dir', str(ROOT),
                    '--plugin-dir', str(fixture), '--strict-mcp-config',
                    '--mcp-config', '{"mcpServers":{}}', '--setting-sources', '',
                    '--settings', '{"teammateMode":"in-process"}',
                    '--allowedTools', 'Bash', 'Agent', '--max-turns', '5',
                ], env, directory)
                server.delivered.wait(3)
                trace_path = root / (mode + '.json')
                trace = json.loads(trace_path.read_text()) if trace_path.exists() else []
                states = [item['result']['state'] for item in trace if item.get('action') == 'complete']
                stops_path = root / (mode + '-stops.jsonl')
                stops = [json.loads(line) for line in stops_path.read_text().splitlines()] if stops_path.exists() else []
                teammates = [task for stop in stops if stop.get('last_assistant_message') == 'Lifecycle fixture complete.'
                             for task in stop.get('background_tasks', []) if task.get('type') == 'teammate']
                summary = {'case': mode, 'exitCode': result.returncode, 'parentFinalStates': states,
                           'notifications': len(server.received) - before,
                           'finalReplyBodyMatches': server.bodies[before:] == ['Lifecycle fixture complete.'],
                           'childCompletions': sum(item.get('action') == 'child_complete' for item in trace),
                           'finalStopTeammates': [{key: task.get(key) for key in ('id', 'type', 'status')} for task in teammates]}
                print(json.dumps(summary), flush=True)
                if mode == 'named-teammate' and not summary['childCompletions']:
                    print(json.dumps({'fixtureAgentResults': [item['result'] for item in trace
                                                              if item.get('action') == 'fixture_agent_result']}), flush=True)
                assert result.returncode == 0, mode + ': CLI failed: ' + (result.stderr or result.stdout).strip()[-2000:]
                if mode == 'named-teammate':
                    assert summary['childCompletions'] > 0, 'named teammate did not finish its actual work'
                    assert any(task.get('status') == 'running' for task in teammates), 'native Stop did not retain a running teammate'
                assert server.received[before:] == ['Claude Code - Task Complete'], mode + ': expected exactly one completion notification'
                assert server.bodies[before:] == ['Lifecycle fixture complete.'], mode
                assert states[-1] == 'idle', mode
                if mode.startswith('background-'):
                    assert states[0] == 'working', mode
                results.append(summary)
    finally:
        server.shutdown()
        server.server_close()
    print('PASS: %d real CLI lifecycle cases, one correctly timed completion each' % len(results))


if __name__ == '__main__':
    main()
