/**
 * Shared save path for the Receptionist and Voice views (they edit the same server-side config but are
 * separate screens). The PUT body is built from the FRESHEST saved config plus only the fields the calling
 * view edits, so neither view can overwrite the other's fields with stale values.
 */
import { api, type AgentConfig, type AgentConfigUpdate, type AgentOut } from "./api";
import { classifySaveError, reconcileReceptionistSave, SAVED, type SaveOutcome } from "./settingsLogic";

export interface AgentSaveResult {
  outcome: SaveOutcome;
  /** The config re-read after a persisted save (null on a failed save or a failed re-read). */
  fresh: AgentConfig | null;
}

export async function saveAgentSettings(
  orgId: string,
  businessId: string,
  patch: Partial<AgentConfigUpdate>,
): Promise<AgentSaveResult> {
  let base: AgentConfig;
  try {
    base = await api.getAgentConfig(orgId, businessId);
  } catch {
    return {
      outcome: { state: "error", message: "Not saved. We couldn't load your current settings first. Please try again." },
      fresh: null,
    };
  }

  let result: SaveOutcome = SAVED;
  let put: AgentOut | null = null;
  try {
    put = await api.saveAgentConfig(orgId, businessId, {
      name: base.name,
      greeting: base.greeting,
      personality: base.personality,
      language: base.language,
      voice_id: base.voice_id ?? "",
      tasks: base.tasks,
      transfer_number: base.transfer_number,
      ...patch,
    });
  } catch (err) {
    result = classifySaveError(err);
  }

  let synced: boolean | null = null;
  let fresh: AgentConfig | null = null;
  try {
    fresh = await api.getAgentConfig(orgId, businessId);
    synced = fresh.synced;
  } catch {
    /* follow-up read failed: reconcile from the PUT response below */
  }
  if (result.state === "error") return { outcome: result, fresh: null };
  return { outcome: reconcileReceptionistSave(result, put, synced), fresh };
}
