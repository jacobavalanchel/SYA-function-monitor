import * as fs from 'fs';
import * as path from 'path';
import type {
  FunctionAdapter,
  FunctionManifest,
  MonitorAdapterContext,
} from './types';

export const manifest: FunctionManifest = {
  id: 'monitor',
  version: '0.1.0',
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
  return {
    manifest,
    actions: {
      health: () => ({ status: 'ok', functionId: manifest.id, version: manifest.version }),
      manifest: () => manifest,
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
    },
  };
}
