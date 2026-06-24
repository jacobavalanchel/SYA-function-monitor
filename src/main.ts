import * as fs from 'fs';
import * as path from 'path';
import type {
  FunctionAdapter,
  FunctionActionHandler,
  FunctionManifest,
  MonitorAdapterContext,
} from './types';

export const manifest: FunctionManifest = {
  id: 'monitor',
  version: '0.1.3',
  capabilities: [
    'screen-monitoring',
    'camera-monitoring',
    'record-storage',
    'attention-config',
  ],
  events: ['screen.capture', 'camera.frame', 'screen.status', 'camera.status'],
  permissions: ['screen', 'camera', 'storage'],
  runtime: {
    node: true,
    electron: true,
    browser: false,
  },
};

interface RatePayload {
  rateMs?: number;
}

interface LimitPayload {
  limit?: number;
}

interface InvokePayload {
  action: string;
  payload?: unknown;
}

const ACTION_DEFINITIONS = [
  { action: 'health', method: 'GET', path: '/health', description: 'Check monitor runtime health.' },
  { action: 'manifest', method: 'GET', path: '/manifest', description: 'Read monitor runtime manifest.' },
  { action: 'actions.list', method: 'GET', path: '/api/monitor/actions', description: 'List callable monitor actions.' },
  { action: 'capabilities.list', method: 'GET', path: '/api/monitor/capabilities', description: 'List monitor capabilities, events, permissions, and actions.' },
  { action: 'invoke', method: 'POST', path: '/api/monitor/invoke', description: 'Invoke a monitor action by action name.' },
  { action: 'screen.status', method: 'GET', path: '/api/monitor/screen/status', description: 'Read screen monitor status.' },
  { action: 'screen.start', method: 'POST', path: '/api/monitor/screen/start', description: 'Start screen monitoring.' },
  { action: 'screen.stop', method: 'POST', path: '/api/monitor/screen/stop', description: 'Stop screen monitoring.' },
  { action: 'camera.status', method: 'GET', path: '/api/monitor/camera/status', description: 'Read camera monitor status.' },
  { action: 'camera.start', method: 'POST', path: '/api/monitor/camera/start', description: 'Start camera monitoring.' },
  { action: 'camera.stop', method: 'POST', path: '/api/monitor/camera/stop', description: 'Stop camera monitoring.' },
  { action: 'camera.show', method: 'POST', path: '/api/monitor/camera/show', description: 'Show the camera monitor window.' },
  { action: 'camera.hide', method: 'POST', path: '/api/monitor/camera/hide', description: 'Hide the camera monitor window.' },
  { action: 'storage.stats', method: 'GET', path: '/api/monitor/storage/stats', description: 'Read monitor storage statistics.' },
  { action: 'storage.screenRecords', method: 'GET', path: '/api/monitor/storage/screen-records', description: 'List stored screen records.' },
  { action: 'storage.cameraRecords', method: 'GET', path: '/api/monitor/storage/camera-records', description: 'List stored camera records.' },
  { action: 'attention.config.status', method: 'GET', path: '/api/monitor/attention/config/status', description: 'Read attention LLM config status.' },
];

function readObjectPayload(payload: unknown): Record<string, unknown> {
  if (payload === undefined || payload === null) return {};
  if (typeof payload !== 'object' || Array.isArray(payload)) {
    throw new Error('Payload must be an object.');
  }
  return payload as Record<string, unknown>;
}

function readRatePayload(payload: unknown): RatePayload {
  const data = readObjectPayload(payload);
  if (data.rateMs === undefined) return {};
  if (typeof data.rateMs !== 'number' || !Number.isFinite(data.rateMs) || data.rateMs <= 0) {
    throw new Error('Payload rateMs must be a positive number.');
  }
  return { rateMs: data.rateMs };
}

function readLimitPayload(payload: unknown): LimitPayload {
  const data = readObjectPayload(payload);
  if (data.limit === undefined) return {};
  if (typeof data.limit !== 'number' || !Number.isInteger(data.limit) || data.limit <= 0) {
    throw new Error('Payload limit must be a positive integer.');
  }
  return { limit: data.limit };
}

function readInvokePayload(payload: unknown): InvokePayload {
  const data = readObjectPayload(payload);
  if (typeof data.action !== 'string' || !data.action) {
    throw new Error('Payload action must be a non-empty string.');
  }
  return {
    action: data.action,
    payload: data.payload,
  };
}

function getAttentionConfigStatus(moduleRoot: string) {
  const candidates = [
    process.env.MONITOR_CONFIG_PATH,
    path.join(moduleRoot, 'config.local.json'),
    path.join(moduleRoot, 'config.json'),
  ].filter(Boolean) as string[];

  const configPath = candidates.find((candidate) => fs.existsSync(candidate));
  if (!configPath) {
    return {
      configured: false,
      configFile: '',
      apiPath: '',
      model: 'gpt-4-turbo',
    };
  }

  try {
    const raw = JSON.parse(fs.readFileSync(configPath, 'utf-8'));
    const api = raw.api ?? raw;
    return {
      configured: Boolean(api.path && api.key),
      configFile: path.basename(configPath),
      apiPath: api.path || '',
      model: api.model || 'gpt-4-turbo',
    };
  } catch (error) {
    return {
      configured: false,
      configFile: path.basename(configPath),
      apiPath: '',
      model: 'gpt-4-turbo',
      error: String(error),
    };
  }
}

export function createAdapter(context: MonitorAdapterContext): FunctionAdapter {
  const actions: Record<string, FunctionActionHandler> = {
      health: () => ({ status: 'ok', functionId: manifest.id, version: manifest.version }),
      manifest: () => manifest,
      'actions.list': () => ACTION_DEFINITIONS,
      'capabilities.list': () => ({
        id: manifest.id,
        version: manifest.version,
        capabilities: manifest.capabilities,
        events: manifest.events,
        permissions: manifest.permissions,
        actions: ACTION_DEFINITIONS,
      }),
      'screen.start': (payload) => {
        const { rateMs } = readRatePayload(payload);
        const status = context.monitor.startScreen(rateMs);
        context.emit('screen.status', status);
        return status;
      },
      'screen.stop': () => {
        const status = context.monitor.stopScreen();
        context.emit('screen.status', status);
        return status;
      },
      'screen.status': () => context.monitor.getScreenStatus(),
      'camera.start': () => {
        const status = context.monitor.startCamera();
        context.emit('camera.status', status);
        return status;
      },
      'camera.stop': () => {
        const status = context.monitor.stopCamera();
        context.emit('camera.status', status);
        return status;
      },
      'camera.status': () => context.monitor.getCameraStatus(),
      'camera.show': () => context.monitor.showCamera(),
      'camera.hide': () => context.monitor.hideCamera(),
      'storage.stats': () => context.monitor.getStorageStats(),
      'storage.screenRecords': (payload) => {
        const { limit } = readLimitPayload(payload);
        return context.monitor.getScreenRecords(limit);
      },
      'storage.cameraRecords': (payload) => {
        const { limit } = readLimitPayload(payload);
        return context.monitor.getCameraRecords(limit);
      },
      'attention.config.status': () => getAttentionConfigStatus(context.moduleRoot),
  };

  actions.invoke = (payload: unknown) => {
    const next = readInvokePayload(payload);
    if (next.action === 'invoke') {
      throw new Error('Nested invoke is not supported.');
    }
    const handler = actions[next.action];
    if (!handler) {
      throw new Error(`Unknown monitor action: ${next.action}`);
    }
    return handler(next.payload);
  };

  return {
    manifest,
    actions,
  };
}
