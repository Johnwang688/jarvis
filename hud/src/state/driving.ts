import { useSyncExternalStore } from "react";
import { DriveRegistry } from "../lib/drivestate";

/** One memory-only owner window; App retains exactly its Remote tab handles. */
export const driving = new DriveRegistry();
export function useDriving(tabId: string) {
  useSyncExternalStore(driving.subscribe, driving.revision, driving.revision);
  return driving.get(tabId);
}
