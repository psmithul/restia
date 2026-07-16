/** Normalize the Notes API response without trusting one historical shape. */
export function notesFromPayload(payload) {
  if (Array.isArray(payload)) return payload;
  if (payload && Array.isArray(payload.notes)) return payload.notes;
  return [];
}

export default notesFromPayload;
