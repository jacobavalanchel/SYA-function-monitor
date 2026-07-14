import * as fs from 'fs';
import * as os from 'os';
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
    'realsense-rgbd-source',
    'training-data-capture',
  ],
  events: [
    'screen.capture',
    'camera.frame',
    'screen.status',
    'camera.status',
    'realsense.frame',
    'realsense.training.sample',
  ],
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
  { action: 'camera.sources', method: 'GET', path: '/api/monitor/camera/sources', description: 'List available camera data sources.' },
  { action: 'realsense.status', method: 'GET', path: '/api/monitor/realsense/status', description: 'Read Intel RealSense adapter status.' },
  { action: 'realsense.devices', method: 'GET', path: '/api/monitor/realsense/devices', description: 'List connected Intel RealSense devices.' },
  { action: 'realsense.start', method: 'POST', path: '/api/monitor/realsense/start', description: 'Start Intel RealSense streaming.' },
  { action: 'realsense.stop', method: 'POST', path: '/api/monitor/realsense/stop', description: 'Stop Intel RealSense streaming.' },
  { action: 'realsense.capture', method: 'POST', path: '/api/monitor/realsense/capture', description: 'Capture a RealSense RGB/depth sample for inference or training.' },
  { action: 'realsense.training.start', method: 'POST', path: '/api/monitor/realsense/training/start', description: 'Start a RealSense training data capture session.' },
  { action: 'realsense.training.stop', method: 'POST', path: '/api/monitor/realsense/training/stop', description: 'Stop the active RealSense training data capture session.' },
  { action: 'storage.stats', method: 'GET', path: '/api/monitor/storage/stats', description: 'Read monitor storage statistics.' },
  { action: 'storage.screenRecords', method: 'GET', path: '/api/monitor/storage/screen-records', description: 'List stored screen records.' },
  { action: 'storage.cameraRecords', method: 'GET', path: '/api/monitor/storage/camera-records', description: 'List stored camera records.' },
  { action: 'attention.config.status', method: 'GET', path: '/api/monitor/attention/config/status', description: 'Read attention LLM config status.' },
  { action: 'settings.status', method: 'GET', path: '/api/monitor/settings/status', description: 'Read persisted monitor settings.' },
  { action: 'settings.save', method: 'POST', path: '/api/monitor/settings/save', description: 'Persist monitor settings for camera, RealSense, storage, and recording.' },
];

const USER_CONFIG_PATH = path.join(os.homedir() || process.cwd(), '.sya', 'monitor', 'config.local.json');

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

function isObjectRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function configPathCandidates(moduleRoot: string): string[] {
  return [
    process.env.MONITOR_CONFIG_PATH,
    USER_CONFIG_PATH,
    path.join(moduleRoot, 'config.local.json'),
    path.join(moduleRoot, 'config.json'),
  ].filter((candidate): candidate is string => Boolean(candidate));
}

function writableConfigPath(): string {
  return process.env.MONITOR_CONFIG_PATH || USER_CONFIG_PATH;
}

function loadMonitorConfig(moduleRoot: string): Record<string, unknown> {
  for (const configPath of configPathCandidates(moduleRoot)) {
    if (!fs.existsSync(configPath)) continue;
    const raw = JSON.parse(fs.readFileSync(configPath, 'utf-8'));
    return isObjectRecord(raw) ? raw : {};
  }
  return {};
}

function deepMerge(base: unknown, update: unknown): Record<string, unknown> {
  const merged: Record<string, unknown> = isObjectRecord(base) ? { ...base } : {};
  if (!isObjectRecord(update)) return merged;

  for (const [key, value] of Object.entries(update)) {
    const existing = merged[key];
    merged[key] = isObjectRecord(value) && isObjectRecord(existing) ? deepMerge(existing, value) : value;
  }

  return merged;
}

function maskSensitiveConfig(config: unknown): Record<string, unknown> {
  if (!isObjectRecord(config)) return {};
  const safeConfig = JSON.parse(JSON.stringify(config)) as Record<string, unknown>;
  if (isObjectRecord(safeConfig.api) && safeConfig.api.key) {
    safeConfig.api.key = '********';
  }
  return safeConfig;
}

function loadRealSenseConfig(moduleRoot: string): Record<string, unknown> {
  const config = loadMonitorConfig(moduleRoot);
  if (!isObjectRecord(config.camera)) return {};
  return isObjectRecord(config.camera.realsense) ? config.camera.realsense : {};
}

function getSettingsStatus(moduleRoot: string) {
  const config = loadMonitorConfig(moduleRoot);
  const readablePath = configPathCandidates(moduleRoot).find((candidate) => fs.existsSync(candidate)) || '';
  const writablePath = writableConfigPath();
  return {
    configured: Object.keys(config).length > 0,
    configPath: readablePath || writablePath,
    writablePath,
    config: maskSensitiveConfig(config),
    realsense: loadRealSenseConfig(moduleRoot),
  };
}

function saveSettings(moduleRoot: string, payload: unknown) {
  const data = readObjectPayload(payload);
  const configPath = writableConfigPath();
  fs.mkdirSync(path.dirname(configPath), { recursive: true });
  const nextConfig = deepMerge(loadMonitorConfig(moduleRoot), data);
  const tempPath = `${configPath}.tmp`;
  fs.writeFileSync(tempPath, `${JSON.stringify(nextConfig, null, 2)}\n`, 'utf-8');
  fs.renameSync(tempPath, configPath);
  return getSettingsStatus(moduleRoot);
}

function getAttentionConfigStatus(moduleRoot: string) {
  const configPath = configPathCandidates(moduleRoot).find((candidate) => fs.existsSync(candidate));
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

function realsenseUnavailableStatus() {
  return {
    source: 'realsense',
    available: false,
    sdkLoaded: false,
    active: false,
    startedAt: null,
    totalFrames: 0,
    lastFrameTime: null,
    lastSamplePath: '',
    recordsDir: process.env.MONITOR_RECORDS_DIR || '',
    stream: {
      width: 640,
      height: 480,
      fps: 30,
      colorWidth: 640,
      colorHeight: 480,
      colorFps: 30,
      depthWidth: 640,
      depthHeight: 480,
      depthFps: 30,
      enableColor: true,
      enableDepth: true,
      serial: '',
    },
    devices: [],
    trainingSession: null,
    error:
      'RealSense capture is available through the local HTTP artifact runtime with Intel RealSense SDK and pyrealsense2 installed.',
  };
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
      'camera.sources': () => [
        {
          id: 'builtin',
          label: 'Built-in camera',
          available: true,
          active: false,
          modes: ['inference'],
        },
        {
          id: 'realsense',
          label: 'Intel RealSense RGB-D camera',
          available: false,
          active: false,
          modes: ['inference', 'training'],
          devices: [],
          error: realsenseUnavailableStatus().error,
        },
      ],
      'realsense.status': realsenseUnavailableStatus,
      'realsense.devices': () => [],
      'realsense.start': realsenseUnavailableStatus,
      'realsense.stop': realsenseUnavailableStatus,
      'realsense.capture': realsenseUnavailableStatus,
      'realsense.training.start': realsenseUnavailableStatus,
      'realsense.training.stop': () => ({
        stoppedAt: new Date().toISOString(),
        session: null,
        status: realsenseUnavailableStatus(),
      }),
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
      'settings.status': () => getSettingsStatus(context.moduleRoot),
      'settings.save': (payload) => saveSettings(context.moduleRoot, payload),
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
