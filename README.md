# Herdr Agent Lifecycle

Makes [Herdr](https://github.com/herdrdev/herdr) show **Working** while a
**Claude Code** or **OpenCode V2** agent still has subagents or background
shells running, instead of **Done** the moment the prompt comes back.

```text
agent turn ends, but a subagent or `sleep 300 &` is still running
        Herdr alone:   idle -> Done      (wrong, work is still going)
        with overlay:  Working           (until that work is gone)
```

Herdr decides state by reading the screen and cannot see background work. This
project is a thin **overlay** on top of that: it reports `working` (or `blocked`
while a question or permission is pending) only while background work exists,
and otherwise hands the pane straight back so Herdr's own detection decides. It
never reports idle, Done or unknown, never sends notifications and never
reports sessions or resume data.

| | Claude Code | OpenCode V2 |
| --- | --- | --- |
| Packaging | Plugin of plain command hooks (`claude/`) | TUI plugin (`opencode/`) |
| Counts as background work | A running shell or subagent in the Stop hook's `background_tasks` | A running descendant session, or a running shell owned by the session family |
| Not counted | Named teammates, session cron jobs | The root session's own turn |
| Back to Herdr's detection | Next prompt, session boundary, or a Stop with nothing running | Work finished, route left the session, or plugin disposed |

Details and limits: [claude/README.md](claude/README.md),
[opencode/README.md](opencode/README.md).

## Install a release

Requirements: **macOS or Linux, Herdr 0.9.3+, Python 3.9+**. OpenCode also needs
**OpenCode 2.0.18** (Node 22.14+ only to run this repository's tests). Claude
needs a Claude Code whose `Stop` hook payload carries `background_tasks`
(tested on 2.1.318).

Download the [latest release](https://github.com/balakumardev/herdr-agent-lifecycle/releases/latest)
into a permanent directory; OpenCode loads the plugin from there.

```sh
VERSION=$(GH_HOST=github.com gh release view --repo balakumardev/herdr-agent-lifecycle --json tagName --jq .tagName)
GH_HOST=github.com gh release download "$VERSION" --repo balakumardev/herdr-agent-lifecycle --dir "lifecycle-$VERSION"
cd "lifecycle-$VERSION"
shasum -a 256 -c SHA256SUMS
tar -xzf "herdr-agent-lifecycle-${VERSION#v}.tar.gz"
cd "herdr-agent-lifecycle-${VERSION#v}"
```

### Claude Code

```sh
claude plugin marketplace add balakumardev/herdr-agent-lifecycle
claude plugin install task-lifecycle@herdr-agent-lifecycle --scope user
```

or, from the extracted release, `claude plugin marketplace add "$PWD"`. Start a
new Claude session to load the hooks. Do not also install Herdr's own Claude hook
(`herdr integration install claude`): after it reports a session on a pane,
state reports from other sources are ignored.

### OpenCode

Preview the registration changes, then apply them:

```sh
python3 scripts/install-opencode.py --config-dir "${XDG_CONFIG_HOME:-$HOME/.config}/opencode"
python3 scripts/install-opencode.py --config-dir "${XDG_CONFIG_HOME:-$HOME/.config}/opencode" --apply
```

The installer registers the `opencode/` directory in `cli.json`, keeps Herdr's
two own OpenCode reporters off (`-herdr.opencode.agent-state` in
`opencode.json`, `-herdr.opencode.session-selection` in `cli.json`), removes
registrations left by earlier releases, and preserves other plugins and options.
It never restarts OpenCode. Open a fresh OpenCode TUI to load the plugin.

### Upgrade and rollback

Download a new release into a new directory and rerun its OpenCode installer;
update Claude with `claude plugin update task-lifecycle@herdr-agent-lifecycle`.
The installer prints a private backup directory. Preview a restoration with
`python3 scripts/install-opencode.py --restore BACKUP` and add `--apply` to
restore. Later edits to the configuration are refused rather than overwritten.

### Upgrading from 1.0.x

1.0.x worked differently, so move off it in this order:

1. **Claude hooks.** The old Claude installer wrapped other plugins' hooks (and
   your own) so they ran through `~/.claude/hooks/task-lifecycle/hook_runner.py`.
   Undo that first, with its own backup: `python3
   ~/.claude/hooks/task-lifecycle/install.py --restore BACKUP --apply`
   (`BACKUP` is the directory it printed, under
   `~/.claude/hooks/task-lifecycle/backups`). Do not delete that directory
   before then, or the wrapped hooks stop working.
2. **Plugins.** `claude plugin uninstall task-lifecycle@herdr-agent-lifecycle`,
   then install the new one as above. Run the new OpenCode installer: it
   replaces the old `cli.json` entry and removes the old `opencode.json` one.
3. **Restart.** Start new Claude sessions, and restart the OpenCode service and
   every TUI: running processes keep executing the old code (including the old
   OpenCode notifier) until they restart. The new OpenCode plugin frees a pane
   the old one left on `unknown` as soon as it loads.

The new plugin sends no notifications. If you relied on the old ones, use your
agent's own notification hook.

## Development

Requires Node 22.14+, Python 3.9+, and macOS or Linux.

```sh
npm ci
npm ci --prefix opencode
npm test
claude plugin validate claude
```

The tests use fake Herdr sockets, fake OpenCode objects and temporary homes; run
them outside a Herdr pane. CI also builds the two release archives, installs the
OpenCode package, applies and restores the installer in a temporary home, and
runs the extracted Claude hook. To verify already prepared assets, run
`python3 scripts/verify-packages.py --dist dist --version X.Y.Z`.

## Automatic releases

```text
PR merged into main/master
          ↓
Linux + macOS tests
          ↓
Conventional Commit → next shared version
          ↓
versioned manifests + CHANGELOG + vX.Y.Z tag
          ↓
GitHub release: source archive + OpenCode package + SHA256SUMS
```

Use a Conventional Commit title when squash-merging:

| Commit | Release |
| --- | --- |
| `fix: preserve pending work` | Patch |
| `feat: observe a new work type` | Minor |
| `feat!: require a newer host API` | Major |
| `docs:`, `ci:`, `test:`, `refactor:`, `chore:`, other configured maintenance types | Patch |

The workflow uses the job-scoped `GITHUB_TOKEN`; no npm publishing credential is
needed. Only the release job has write access. Pipelines are serialized and the
release uses its tested commit. Generated release commits use `[skip ci]`.
Re-running a completed release with no new commits creates no new version.
Use the workflow's manual dispatch to retry a failed run after fixing its cause.
Before version analysis, recovery checks the latest reachable release tag. If
GitHub publication was interrupted after tagging, it rebuilds that exact tag in
a disposable clone, completes its draft assets, verifies downloaded checksums,
and publishes that same version. A completed release is verified without being
rewritten. The release job also verifies downloaded assets after publication.

If you later protect `main`/`master` against release commits, configure an
appropriately scoped GitHub App for the release job. Do not substitute a broad
personal token. Ordinary non-Conventional Commit messages do not trigger a
release; use squash-merge titles to keep the version policy predictable.

## License

Apache-2.0. See [LICENSE](LICENSE). Herdr's public protocol is documented by
[herdrdev/herdr](https://github.com/herdrdev/herdr). This project is independent
of Herdr, Anthropic, and OpenCode.
