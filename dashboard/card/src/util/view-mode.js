// One switch for every SEM card: "Everyday" (false) or "All settings" (true).
// Kept per browser under the key the Configuration tab has always used.
export const VIEW_MODE_KEY = "sem_config_advanced_v1";
export const VIEW_MODE_EVENT = "sem-view-mode-changed";

export function getAllSettings() {
  try {
    return localStorage.getItem(VIEW_MODE_KEY) === "1";
  } catch (_e) {
    return false;
  }
}

export function setAllSettings(on) {
  try {
    localStorage.setItem(VIEW_MODE_KEY, on ? "1" : "0");
  } catch (_e) {
    // private window or blocked storage: the choice lasts this page only
  }
  window.dispatchEvent(new CustomEvent(VIEW_MODE_EVENT, { detail: { all: !!on } }));
}
