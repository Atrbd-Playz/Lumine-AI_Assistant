import { invoke } from "@tauri-apps/api/core";

/** Open or focus the persistent browser profile owned by Lumine. */
export async function openLumineBrowser(): Promise<void> {
  await invoke("open_browser");
}
