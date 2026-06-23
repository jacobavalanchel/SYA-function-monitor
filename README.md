# Monitor Function

Private Git submodule for SYA local focus monitoring.

## Scope

- Screen monitoring.
- Camera monitoring.
- Local monitor record storage.
- Monitor test and permission validation hooks.

## Current source anchors

- `app/electron/monitor/screen.ts`
- `app/electron/monitor/camera.ts`
- `app/electron/monitor/storage.ts`
- `app/src/pages/MonitorTestPage.tsx`
- `app/src/types/electron.d.ts`

Runtime code is still wired through the main app. Move implementation here once
the private submodule repository is created and published.
