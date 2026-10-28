# Herdr Agent Lifecycle: public release design

## Intent and approval

Publish the existing Claude Code and OpenCode V2 completion integrations as
`balakumardev/herdr-agent-lifecycle`, automate versioned GitHub releases on
successful `main`/`master` updates, and install the released integration on the
author's Mac. The user approved Apache-2.0, both adapters, Linux/macOS CI,
strict teammate completion by default, optional best-effort behavior, and
autonomous execution including documentation without further design/plan gates.

## Architecture

Keep the two existing implementations in `claude/` and `opencode/`. They observe
their host's lifecycle and report through Herdr's public `pane.report_agent`
socket API. Preserve the existing RPC IDs and metadata-only state/receipts.
The OpenCode server owns task-tree observation; its TUI owns pane attribution.
The Claude plugin uses native mods plus command hooks and transparent review
wrappers. A public marketplace at `.claude-plugin/marketplace.json` installs the
Claude adapter. The OpenCode adapter is an independently installable npm-format
package distributed as a GitHub release asset.

## Configuration

Both adapters read `$XDG_CONFIG_HOME/herdr-agent-lifecycle/config.json`, falling
back to `~/.config/herdr-agent-lifecycle/config.json`; `HERDR_LIFECYCLE_CONFIG`
overrides that file. The schema is:

```json
{
  "notifications": { "enabled": false, "endpoint": "https://ntfy.sh", "topic": "" },
  "opencode": { "command": "opencode" },
  "claude": { "allowIdleTeammates": false }
}
```

Notifications require explicit enablement and a valid topic. Configuration
errors suppress notification/completion heuristics instead of exposing secrets
or accepting a truthy string as consent. Direct transport constructors retain
their explicit legacy topic-file inputs for existing tests and migration.
No settings, credentials, topics, runtime state, or private hook payloads ship.

## Completion contract

Keep recursive descendant, inbox, shell, question, permission, successful-root
reply, review, and continuation checks. Ordinary completion remains unchanged.
Claude's public idle roster cannot prove an empty teammate inbox. Therefore
`allowIdleTeammates` defaults to false: a nonempty Stop snapshot remains pending.
Only explicit opt-in enables the existing best-effort fallback. Status and docs
must explain this policy and the possible pending state; never advertise perfect
team-wide completion. Existing best-effort tests remain, explicitly opted in,
alongside strict-default and invalid-configuration regression coverage.

## Installation

Use the existing conservative Claude installer, publishing its local marketplace
under the new repository identity and preserving unrelated hooks and settings.
Keep async review behavior, conditions, output, and exit status intact.
The OpenCode installer registers an already prepared, dependency-complete plugin
directory, disables both competing Herdr reporter IDs, and preserves other
plugin options. Known legacy notification bridge migration remains supported;
unfamiliar layouts fail before mutation. Back up every changed configuration.
Provide guarded restoration that refuses to overwrite subsequent user edits.
Neither installer starts or restarts an agent service.

For this Mac, prepare the exact release in an isolated dependency directory,
preserve the existing notification topic locally when present, register both
adapters, and verify with fresh sessions/fixtures. Use the proven watched-plugin
reload path for OpenCode only after checking active-session preservation. Existing
Claude sessions may continue with their loaded configuration; verify activation
with a fresh process. Record backups and service/session readback outside Git.

## Releases

Use one synchronized version for both adapters and the repository. GitHub Actions
runs Node and Python regression tests on Linux and macOS for PRs and pushes.
After both platforms pass on `main` or `master`, semantic-release analyzes
Conventional Commits, prepares synchronized manifests and lockfiles, creates a
`vX.Y.Z` tag, and publishes a GitHub release with source bundle, OpenCode package,
and SHA-256 checksums. `feat` means minor, `fix` means patch, and breaking changes
mean major; other conventional maintenance changes get a patch release. Release
commits use `[skip ci]`, and serialized releases prevent duplicate versions.
Use GitHub's job token with repository-content permissions; no npm token is
required. Document the first release and how contributors trigger later ones.

## Verification and compatibility

Runtime targets: OpenCode 2.0.18, Claude Code native mods 2.1.292+, Herdr 0.9.3+,
Node 22.14+, Python 3.9+, macOS and Linux. Test the actual current Mac versions
and report them. CI release tooling uses Node 24. Tests must create their own
temporary directories, run from arbitrary checkouts, and carry their own bridge
fixture. Keep all inherited cases and assertions; source portability changes
must not lower their functional bar.

Success requires a public repo, a verified CI-generated release/tag, a working
second release triggered by a real conventional change, released package tests,
native Claude validation/tests, installed OpenCode health and registered TUI,
fresh local lifecycle fixtures, and preserved running-service identity.
