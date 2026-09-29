import type { Settings } from "../../lib/api";
export type UpdateFn = <K extends keyof Settings>(group: K, patch: Partial<Settings[K]>) => void;
