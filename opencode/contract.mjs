// Portable JSON-schema RPC: used by the server plugin and each terminal client.
export const LifecycleRPC = {
  id: 'local.task-lifecycle',
  methods: {
    health: {
      input: { type: 'object', properties: {}, additionalProperties: false },
      output: {
        type: 'object',
        properties: { version: { type: 'string' }, revision: { type: 'integer' }, connected: { type: 'boolean' }, references: { type: 'integer' } },
        required: ['version', 'revision', 'connected', 'references'], additionalProperties: false,
      },
    },
    snapshot: {
      input: { type: 'object', properties: { sessionID: { type: 'string' } }, required: ['sessionID'], additionalProperties: false },
      output: {
        type: 'object',
        properties: {
          rootID: { type: 'string' }, state: { enum: ['working', 'blocked', 'idle'] },
          complete: { type: 'boolean' }, reason: { type: 'string' },
          activeCount: { type: 'integer' }, pendingCount: { type: 'integer' },
          questionIDs: { type: 'array', items: { type: 'string' } },
          title: { type: 'string' }, location: { type: 'object', properties: { directory: { type: 'string' } }, required: ['directory'], additionalProperties: false },
          completionKey: { type: 'string' },
        },
        required: ['rootID', 'state', 'complete', 'reason', 'activeCount', 'pendingCount', 'questionIDs', 'title', 'location'],
        additionalProperties: false,
      },
    },
  },
  events: {},
};
