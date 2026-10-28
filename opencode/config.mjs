import { readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';

const object = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);

/** Shared on-disk schema; errors never include configuration values. */
export function loadConfig({ env = process.env, home = homedir() } = {}) {
  const path = env.HERDR_LIFECYCLE_CONFIG || join(env.XDG_CONFIG_HOME || join(home, '.config'), 'herdr-agent-lifecycle', 'config.json');
  let value = {};
  try {
    try { value = JSON.parse(readFileSync(path, 'utf8')); }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
    if (!object(value)) throw new Error();
    const notifications = value.notifications === undefined ? {} : value.notifications;
    const opencode = value.opencode === undefined ? {} : value.opencode;
    const claude = value.claude === undefined ? {} : value.claude;
    if (![notifications, opencode, claude].every(object)) throw new Error();
    const enabled = notifications.enabled === undefined ? false : notifications.enabled;
    const topic = notifications.topic === undefined ? '' : notifications.topic;
    const endpoint = notifications.endpoint === undefined ? 'https://ntfy.sh' : notifications.endpoint;
    const command = opencode.command === undefined ? 'opencode' : opencode.command;
    const allowIdleTeammates = claude.allowIdleTeammates === undefined ? false : claude.allowIdleTeammates;
    if (typeof enabled !== 'boolean' || typeof allowIdleTeammates !== 'boolean' ||
        typeof topic !== 'string' || (topic && !/^[A-Za-z0-9_-]{1,256}$/.test(topic)) || (enabled && !topic) ||
        typeof endpoint !== 'string' || typeof command !== 'string' || !command.trim() ||
        command.length > 4096 || /[\r\n\0]/.test(command)) throw new Error();
    const url = new URL(endpoint);
    if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) throw new Error();
    return { notifications: { enabled, topic, endpoint: endpoint.replace(/\/+$/, '') },
      opencode: { command }, claude: { allowIdleTeammates } };
  } catch {
    throw new Error('Invalid lifecycle configuration');
  }
}
