export type FunctionActionHandler = (payload: unknown) => unknown | Promise<unknown>;

export interface FunctionManifest {
  id: string;
  version: string;
  capabilities: string[];
  events: string[];
  permissions: string[];
  runtime: {
    node: boolean;
    electron: boolean;
    browser: boolean;
  };
}

export interface FunctionAdapter {
  manifest: FunctionManifest;
  actions: Record<string, FunctionActionHandler>;
}

export interface FunctionAdapterContext {
  moduleRoot: string;
  emit: (event: string, payload: unknown) => void;
}

export interface MonitorDriver {
  startScreen: (rateMs?: number) => unknown;
  stopScreen: () => unknown;
  getScreenStatus: () => unknown;
  startCamera: () => unknown;
  stopCamera: () => unknown;
  getCameraStatus: () => unknown;
  showCamera: () => unknown;
  hideCamera: () => unknown;
  getStorageStats: () => unknown;
  getScreenRecords: (limit?: number) => unknown;
  getCameraRecords: (limit?: number) => unknown;
}

export interface MonitorAdapterContext extends FunctionAdapterContext {
  monitor: MonitorDriver;
}
