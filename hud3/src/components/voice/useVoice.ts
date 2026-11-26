import { useEffect, useRef, useState } from "react";
import { readVoice, saveVoiceSetting, type VoiceSetting, type VoiceSnapshot } from "./voiceApi";

const POLL_MS = 10_000;
const MIC_POLL_MS = 125;
// After a failed or timed-out read the daemon may still be working on it:
// wait before asking again, even during the mic test.
const RETRY_MS = 2_000;

export function useVoice(testing: boolean) {
  const [snapshot, setSnapshot] = useState<VoiceSnapshot>();
  const [unreachable, setUnreachable] = useState(false);
  const [refreshCount, setRefreshCount] = useState(0);
  const [saving, setSaving] = useState(false);
  const savingRef = useRef(false);
  const revision = useRef(0);
  const refresh = () => setRefreshCount((count) => count + 1);

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout>;
    let active: AbortController | undefined;

    async function poll() {
      if (document.hidden) return;
      const request = new AbortController();
      active = request;
      const started = revision.current;
      const timeout = setTimeout(() => request.abort(), 5_000);
      let failed = false;
      try {
        const result = await readVoice(request.signal);
        if (active !== request) return;
        const freshSettings = started === revision.current && !savingRef.current;
        setSnapshot((previous) => freshSettings || !previous
          ? result : { ...result, settings: previous.settings });
        setUnreachable(false);
      } catch {
        failed = true;
        if (active === request) setUnreachable(true);
      } finally {
        clearTimeout(timeout);
        if (active === request) {
          active = undefined;
          timer = setTimeout(poll, testing ? (failed ? RETRY_MS : MIC_POLL_MS) : POLL_MS);
        }
      }
    }

    function cancel() {
      clearTimeout(timer);
      const request = active;
      active = undefined;
      request?.abort();
    }

    function onVisibilityChange() {
      cancel();
      void poll();
    }

    document.addEventListener("visibilitychange", onVisibilityChange);
    void poll();
    return () => {
      document.removeEventListener("visibilitychange", onVisibilityChange);
      cancel();
    };
  }, [testing, refreshCount]);

  async function save(name: VoiceSetting, value: number) {
    if (savingRef.current || !snapshot || value === snapshot.settings[name].value) return;
    savingRef.current = true;
    setSaving(true);
    revision.current++;
    try {
      const settings = await saveVoiceSetting(name, value);
      setSnapshot((previous) => previous && { ...previous, settings });
    } catch (error) {
      refresh();
      throw error;
    } finally {
      revision.current++;
      savingRef.current = false;
      setSaving(false);
    }
  }

  return { snapshot, unreachable, refresh, saving, save };
}
